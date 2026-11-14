"""The recognition engines behind the job API. Both share decoding, pause cutting and speaker labels:

    ffmpeg -> 16 kHz mono -> fsmn-vad cuts at pauses -> cam++ speaker embeddings for all segments in large
    batches, clustered -> recognition -> numerals to digits -> segments {start, end, text, speaker,
    compression_ratio}

QwenEngine (the default): Qwen3-ASR-0.6B on its own server (vLLM, another container). Consecutive segments of
one speaker are merged into chunks of up to 25 s, each sent as one request, several at once. On one 43-minute
earnings call the server alone takes 46 s with 6.2 GB of GPU memory; its own 30 s cuts split numbers, the
chunks here are cut at pauses.
A job that asks for sentences (`options["sentences"]`) also has every chunk's raw text aligned to its audio by
Qwen3-ForcedAligner-0.6B (from ASR_ALIGNER_DIR): each segment then carries its sentences {start, end, text}
with times of their own (sentences.py). On one 43-minute call: 44 s more for 188 chunks one at a time, 2.5 GB
of GPU memory at most. The card is shared and was 94% full on one busy day: the aligner is
loaded for such a job only, when 3 GB are free, and let go after it (about 5 s to load).

FunAsrEngine: Fun-ASR-Nano-2512 in PyTorch, every segment on its own (funasr 1.4.16's batched decoding and its
vLLM pipeline garble the text), on `workers` copies in worker processes (threads barely help: the decoding
loop holds the GIL). On 10 minutes of the call: 1 copy 74 s, 2 processes 41 s, 3 processes 25 s; each copy
takes about 4.1 GB.

    python -m asr_service.engine audio.flac out.json     # one file (ASR_* variables), with each step's timing
"""

import gc
import io
import json
import logging
import multiprocessing as mp
import subprocess
import sys
import time
import wave
import zlib
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path
from typing import Any

import numpy as np
import torch
from funasr import AutoModel
from funasr.models.campplus.cluster_backend import ClusterBackend
from funasr.models.campplus.utils import distribute_spk, postprocess, sv_chunk

from .chunks import Chunk, merge_turns, split_long
from .itn import itn
from .jobs import AudioError
from .parallel import decode_all
from .qwen import QwenClient, QwenUnavailable
from .sentences import Sentence, plan
from .sentences import sentences as sentence_times

log = logging.getLogger(__name__)

RATE = 16000
MAX_SEGMENT_MS = 30000  # Fun-ASR
# The VAD's own cut is at a fixed length (60 s here, rarely reached); spans over 25 s are cut again at the
# quietest point of their last 5 s, below the Qwen server's own fixed 30 s cut.
QWEN_SEGMENT_MS, QWEN_CHUNK_MS, QUIET_SEARCH_MS, FRAME_MS = 60000, 25000, 5000, 100
SPK_BATCH = 256  # 1.5 s chunks per cam++ forward pass
_QUIET = {"disable_update": True, "disable_pbar": True, "disable_log": True}


class _Pipeline:
    """What both engines share: decoding, pause cutting, speaker labels and the output segments."""

    def __init__(self, *, vad_dir: str, spk_dir: str | None, device: str, max_segment_ms: int):
        self.device = device
        common = {"device": device, **_QUIET}
        self.vad = AutoModel(model=vad_dir, max_single_segment_time=max_segment_ms, **common)
        self.spk = AutoModel(model=spk_dir, **common) if spk_dir else None
        self.timings: dict[str, float] = {}

    def _step(self, name: str, fn, *args):
        started = time.monotonic()
        try:
            return fn(*args)
        finally:
            self.timings[name] = round(time.monotonic() - started, 1)

    def _vad(self, samples: np.ndarray) -> list[tuple[int, int]]:
        if len(samples) == 0:
            return []
        [result] = self.vad.generate(input=samples, cache={}, is_final=True)
        limit = len(samples) * 1000 // RATE
        return [(int(s), min(int(e), limit)) for s, e in result["value"] if min(int(e), limit) > int(s)]

    def _speakers(self, spans: list[tuple[int, int]], pieces: list[np.ndarray]) -> list[str | None]:
        """One embedding per 1.5 s chunk of every segment, computed in large batches, then clustered."""
        if self.spk is None:
            return [None] * len(spans)
        chunks = sv_chunk([[s / 1000, e / 1000, piece] for (s, e), piece in zip(spans, pieces, strict=True)])
        if len(chunks) < 2:
            return [_speaker(1) if spans else None] * len(spans)
        results = self.spk.generate(input=[c[2] for c in chunks], batch_size=SPK_BATCH, cache={})
        embeddings = torch.cat([r["spk_embedding"] for r in results], dim=0).cpu()
        labels = ClusterBackend().to(self.device)(embeddings, oracle_num=None)
        turns = postprocess(chunks, None, labels, embeddings)
        sentences = [{"start": s, "end": e} for s, e in spans]
        distribute_spk(sentences, turns)
        # Numbered in order of appearance (发言人1 speaks first), not by the clustering's arbitrary label
        names: dict[int, str] = {}
        spk = [s.get("spk") for s in sentences]
        return [None if k is None else names.setdefault(int(k), _speaker(len(names) + 1)) for k in spk]

    def _segments(self, spans, texts, speakers, timed=None) -> list[dict[str, Any]]:
        """`timed`: each span's sentences, times from the span's start (the Qwen engine, when asked)."""
        out = []
        for k, ((start, end), text, speaker) in enumerate(zip(spans, texts, speakers, strict=True)):
            text = itn(text.strip())
            if text:
                segment = {
                    "start": start / 1000,
                    "end": end / 1000,
                    "text": text,
                    "speaker": speaker,
                    "compression_ratio": _compression_ratio(text),
                }
                if timed is not None:
                    segment["sentences"] = _timed(timed[k], start, end)
                out.append(segment)
        return out


class QwenEngine(_Pipeline):
    name = "qwen3-asr-0.6b"

    def __init__(
        self,
        *,
        qwen_url: str,
        vad_dir: str,
        spk_dir: str | None,
        device: str,
        concurrency: int = 8,
        aligner_dir: str | None = None,
    ):
        super().__init__(vad_dir=vad_dir, spk_dir=spk_dir, device=device, max_segment_ms=QWEN_SEGMENT_MS)
        self.qwen = QwenClient(qwen_url, concurrency=concurrency)
        self.aligner_dir = aligner_dir
        if not self.qwen.healthy():  # the API retries the load until the Qwen server is up
            raise RuntimeError("the Qwen server is not ready")

    @property
    def sentences(self) -> bool:
        """Whether a job can have sentence times (if the card has room for the aligner then)."""
        return self.aligner_dir is not None

    def healthy(self) -> bool:
        return self.qwen.healthy()

    def transcribe(self, audio: Path, options: dict[str, Any]) -> list[dict[str, Any]]:
        """Speakers first, so that every chunk is one speaker's turn, cut at pauses. The job's `context`
        (the meeting title and a term list) goes with every chunk; hotwords are not used. With
        `options["sentences"]` and an aligner, every segment also has its sentences."""
        self.timings = {}
        if not self.qwen.healthy():  # before a minute of decoding, cutting and speakers
            raise QwenUnavailable("Qwen server: not answering")
        samples = self._step("decode", _decode, audio)
        spans = self._step("vad", self._vad, samples)
        spans = split_long(
            spans, _loudness(samples), frame_ms=FRAME_MS, max_ms=QWEN_CHUNK_MS, search_ms=QUIET_SEARCH_MS
        )
        pieces = [samples[s * RATE // 1000 : e * RATE // 1000] for s, e in spans]
        speakers = self._step("speakers", self._speakers, spans, pieces)
        chunks = merge_turns(spans, speakers, QWEN_CHUNK_MS)
        wavs = [_wav(samples[c.start_ms * RATE // 1000 : c.end_ms * RATE // 1000]) for c in chunks]
        texts = self._step("asr", self.qwen.transcribe_many, wavs, options.get("context"))
        timed = None
        if options.get("sentences") and self.aligner_dir is not None:
            timed = self._step("align", self._align, samples, chunks, texts)
        spans = [(c.start_ms, c.end_ms) for c in chunks]
        out = self._segments(spans, texts, [c.speaker for c in chunks], timed)
        log.info("%.0f s of audio, %d chunks: %s", len(samples) / RATE, len(chunks), self.timings)
        return out

    def _align(self, samples: np.ndarray, chunks: list[Chunk], texts: list[str]) -> list[list[Sentence]]:
        """One chunk at a time: batches of 8 were faster (26 s instead of 44 s on one recorded call) but moved
        some characters by up to 0.48 s against the same chunks aligned one by one. No aligner (no room on the
        card): no sentences, every segment keeps its own times."""
        aligner = _load_aligner(self.aligner_dir, self.device)
        if aligner is None:
            return [[] for _ in chunks]
        out = []
        try:
            for chunk, raw in zip(chunks, texts, strict=True):
                piece = samples[chunk.start_ms * RATE // 1000 : chunk.end_ms * RATE // 1000]
                try:
                    out.append(aligner.sentences(piece, raw) if raw.strip() else [])
                except Exception as exc:  # this chunk keeps the times of its segment only
                    log.warning("aligning a chunk failed: %s", type(exc).__name__)
                    out.append([])
        finally:
            aligner.close()
        return out


ALIGNER_FREE_BYTES = 3 << 30  # loaded only with this much free on the card: 1.7 GB of weights, 2.5 GB at most


def _load_aligner(model_dir: str, device: str) -> "_Aligner | None":
    """The aligner for one job, or None (that job gets no sentence times) when the shared card has not got the
    room, or loading fails (a broken download, a library mismatch): never the job's failure."""
    try:
        if device.startswith("cuda"):
            free, _ = torch.cuda.mem_get_info(torch.device(device))
            if free < ALIGNER_FREE_BYTES:
                log.warning("aligner not loaded: %.1f GB free on the card", free / 2**30)
                return None
        return _Aligner(model_dir, device)
    except Exception as exc:
        log.error("loading the aligner failed (%s): no sentence times", type(exc).__name__)
        _free_cuda(device)
        return None


class _Aligner:
    """Qwen3-ForcedAligner-0.6B, the companion of Qwen3-ASR: given audio and the text said in it, a time for
    every unit of the text, in one forward pass."""

    def __init__(self, model_dir: str, device: str):
        from qwen_asr.inference.qwen3_forced_aligner import Qwen3ForcedAligner  # only in the service image

        self._model = Qwen3ForcedAligner.from_pretrained(model_dir, dtype=torch.bfloat16, device_map=device)
        self._tokenize = self._model.aligner_processor.tokenize_space_lang
        self._device = device

    def close(self) -> None:
        """Give the card its memory back."""
        self._model = None
        _free_cuda(self._device)

    def sentences(self, piece: np.ndarray, raw: str) -> list[Sentence]:
        planned = plan(raw, self._tokenize)
        if not planned.units:
            return []
        [result] = self._model.align(audio=[(piece, RATE)], text=[planned.text], language="Chinese")
        return sentence_times(planned, [(unit.start_time, unit.end_time) for unit in result])


def _free_cuda(device: str) -> None:
    gc.collect()  # a model's modules refer to each other: dropping the last reference is not enough
    if device.startswith("cuda"):
        torch.cuda.empty_cache()


_worker_model = None  # the Fun-ASR model of one worker process


def _load_worker(model_dir: str, device: str) -> None:
    global _worker_model
    _worker_model = AutoModel(model=model_dir, device=device, **_QUIET)


def _decode_one(piece: np.ndarray, kwargs: dict[str, Any]) -> str:
    # A tensor: the model wraps a numpy array as None (audio must be a path or a torch.Tensor)
    result = _worker_model.generate(input=[torch.from_numpy(piece)], batch_size=1, **kwargs)
    return result[0].get("text") or ""


class FunAsrEngine(_Pipeline):
    name = "fun-asr-nano-2512"

    def __init__(self, *, model_dir: str, vad_dir: str, spk_dir: str | None, device: str, workers: int = 1):
        super().__init__(vad_dir=vad_dir, spk_dir=spk_dir, device=device, max_segment_ms=MAX_SEGMENT_MS)
        self.model_dir, self.workers = model_dir, workers
        self._pool: ProcessPoolExecutor | None = self._start_workers()

    def _start_workers(self) -> ProcessPoolExecutor:
        """`workers` processes, each with its own copy of the model, loaded now, not on the first job."""
        pool = ProcessPoolExecutor(
            self.workers,
            mp_context=mp.get_context("spawn"),  # CUDA is already in use here: no fork
            initializer=_load_worker,
            initargs=(self.model_dir, self.device),
        )
        silence, kwargs = np.zeros(RATE, dtype=np.float32), {"cache": {}, "language": "中文"}
        for future in [pool.submit(_decode_one, silence, kwargs) for _ in range(self.workers)]:
            future.result()
        return pool

    def transcribe(self, audio: Path, options: dict[str, Any]) -> list[dict[str, Any]]:
        self.timings = {}
        samples = self._step("decode", _decode, audio)
        spans = self._step("vad", self._vad, samples)
        pieces = [samples[s * RATE // 1000 : e * RATE // 1000] for s, e in spans]
        texts = self._step("asr", self._recognize, pieces, options)
        speakers = self._step("speakers", self._speakers, spans, pieces)
        out = self._segments(spans, texts, speakers)
        log.info("%.0f s of audio, %d segments: %s", len(samples) / RATE, len(out), self.timings)
        return out

    def _recognize(self, pieces: list[np.ndarray], options: dict[str, Any]) -> list[str]:
        """One segment per model call, as funasr's own VAD pipeline does. Its batched decoding (funasr 1.4.16)
        garbles the text: on one recorded call 230 of 756 segments looped ("这个这个这个…"), and decoding took
        longer, as a batch runs until its longest output stops."""
        kwargs = {"cache": {}, "language": options.get("language", "中文"), "itn": True}
        if options.get("hotwords"):
            kwargs["hotwords"] = options["hotwords"]
        if self._pool is None:
            self._pool = self._start_workers()
        try:
            return decode_all(self._pool, _decode_one, pieces, kwargs)
        except BrokenProcessPool:  # a worker died (out of memory, killed): fresh ones for the job's retry
            self._pool.shutdown(wait=False, cancel_futures=True)
            self._pool = None  # started again then, when the card may have room
            raise


def _timed(found: list[Sentence], start_ms: int, end_ms: int) -> list[dict[str, Any]]:
    """A span's sentences in seconds of the recording, inside the span, numerals converted like its text."""
    lo, hi = start_ms / 1000, end_ms / 1000
    out = []
    for sentence in found:
        if text := itn(sentence.text.strip()):
            start = min(max(lo + sentence.start, lo), hi)
            end = min(max(lo + sentence.end, start), hi)
            out.append({"start": round(start, 3), "end": round(end, 3), "text": text})
    return out


def _speaker(number: int) -> str:
    """发言人3, not "3": the minutes prompt shows "00:30:13 3: 进口占65%以上", and on a call full of figures
    bare numbers cost the minutes about 5 of 41 key facts in an A/B with only the labels changed."""
    return f"发言人{number}"


def _decode(audio: Path) -> np.ndarray:
    """Any format ffmpeg reads (the backend uploads 16 kHz mono AAC) to 16 kHz mono float32."""
    command = ["ffmpeg", "-nostdin", "-v", "error", "-i", str(audio)]
    command += ["-ac", "1", "-ar", str(RATE), "-f", "f32le", "-"]
    run = subprocess.run(command, capture_output=True, check=False)
    if run.returncode != 0:
        raise AudioError("audio could not be decoded")
    return np.frombuffer(run.stdout, dtype=np.float32).copy()  # writable: torch refuses read-only memory


def _loudness(samples: np.ndarray) -> list[float]:
    """RMS of every FRAME_MS frame."""
    size = RATE * FRAME_MS // 1000
    frames = samples[: len(samples) // size * size].reshape(-1, size)
    return np.sqrt(np.mean(frames.astype(np.float64) ** 2, axis=1)).tolist()


def _wav(samples: np.ndarray) -> bytes:
    """16 kHz mono 16-bit WAV, what the Qwen server reads."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(RATE)
        out.writeframes((np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes())
    return buffer.getvalue()


def _compression_ratio(text: str) -> float:
    """As Whisper reports it: repetition compresses well. The backend drops segments above 2.4."""
    raw = text.encode()
    return round(len(raw) / len(zlib.compress(raw)), 2)


def from_env(env=None) -> _Pipeline:
    """The engine the ASR_* variables describe (see README.md)."""
    import os

    env = env or os.environ
    models = Path(env.get("ASR_MODELS", "/models/modelscope/models"))
    vad = models / "iic--speech_fsmn_vad_zh-cn-16k-common-pytorch/snapshots/master"
    spk = models / "iic--speech_campplus_sv_zh-cn_16k-common/snapshots/master"
    vad_dir, spk_dir = env.get("ASR_VAD_DIR", str(vad)), env.get("ASR_SPK_DIR", str(spk))
    common = {"vad_dir": vad_dir, "spk_dir": spk_dir or None, "device": env.get("ASR_DEVICE", "cuda:0")}
    if env.get("ASR_ENGINE", "qwen") == "funasr":
        model_dir = env.get("ASR_MODEL_DIR", str(models / "FunAudioLLM--Fun-ASR-Nano-2512/snapshots/master"))
        return FunAsrEngine(model_dir=model_dir, workers=int(env.get("ASR_WORKERS", "1")), **common)
    qwen_url = env.get("ASR_QWEN_URL", "http://qwen3-asr:80")
    aligner = env.get("ASR_ALIGNER_DIR", "/models/aligner/Qwen3-ForcedAligner-0.6B")
    if aligner and not Path(aligner).is_dir():
        log.info("no aligner at %s: jobs get no sentence times", aligner)
        aligner = None
    concurrency = int(env.get("ASR_QWEN_CONCURRENCY", "8"))
    return QwenEngine(qwen_url=qwen_url, concurrency=concurrency, aligner_dir=aligner or None, **common)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    started = time.monotonic()
    engine = from_env()
    loaded = time.monotonic() - started
    options = {"language": "中文", "hotwords": sys.argv[3:], "sentences": True}
    segments = engine.transcribe(Path(sys.argv[1]), options)
    report = {"engine": engine.name, "load": round(loaded), "timings": engine.timings, "segments": segments}
    Path(sys.argv[2]).write_text(json.dumps(report, ensure_ascii=False))  # stdout: funasr prints there too
    print(f"{engine.name} loaded in {loaded:.0f}s, {engine.timings}", file=sys.stderr)
