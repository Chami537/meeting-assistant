"""The pieces of audio the Qwen engine recognizes: pause-cut segments, long ones cut again where it is
quietest, then consecutive segments of one speaker merged up to a length. Free of torch, funasr and numpy,
so it is tested locally."""

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class Chunk:
    start_ms: int
    end_ms: int
    speaker: str | None


def merge_turns(spans: list[tuple[int, int]], speakers: list[str | None], max_ms: int) -> list[Chunk]:
    """A new chunk at a change of speaker, or when the next segment would make it longer than `max_ms`; a
    segment longer than that on its own is a chunk by itself. So every cut is at a pause: Qwen's own service
    cuts long audio every 30 s wherever it is, and split 3529万 into "三千五百二。十九万"."""
    chunks: list[Chunk] = []
    for (start, end), speaker in zip(spans, speakers, strict=True):
        last = chunks[-1] if chunks else None
        if last is not None and last.speaker == speaker and end - last.start_ms <= max_ms:
            chunks[-1] = Chunk(last.start_ms, end, speaker)
        else:
            chunks.append(Chunk(start, end, speaker))
    return chunks


def split_long(
    spans: list[tuple[int, int]], energy: Sequence[float], *, frame_ms: int, max_ms: int, search_ms: int
) -> list[tuple[int, int]]:
    """Cut every span longer than `max_ms` where the audio is quietest in the last `search_ms` before the
    limit, and again until every piece fits (Qwen's own split_audio_into_chunks does the same). `energy` is
    the loudness of each `frame_ms` frame of the recording. The VAD alone cuts a long stretch of speech at a
    fixed length, wherever that falls: 11 of its 28 s cuts on one recorded call were inside speech."""
    out: list[tuple[int, int]] = []
    for start, end in spans:
        while end - start > max_ms:
            limit = start + max_ms
            frames = range((limit - search_ms) // frame_ms, limit // frame_ms)
            quietest = min(frames, key=lambda f: energy[f] if f < len(energy) else float("inf"))
            cut = min(max(quietest * frame_ms + frame_ms // 2, start + 1), limit)  # the middle of that frame
            out.append((start, cut))
            start = cut
        out.append((start, end))
    return out
