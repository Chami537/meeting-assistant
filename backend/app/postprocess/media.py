"""Probing and normalizing a recording the user uploaded (design doc §7.5, 保底 MVP).

The file is untrusted input. ffprobe identifies the container first and only ordinary audio/video containers
are accepted: playlist and concat formats (hls, concat, ...) could make ffmpeg read other local files or
URLs. ffmpeg is then pinned to the detected demuxer, limited to the file protocol and run with a timeout.
The output is 16 kHz mono AAC in m4a: small, and what the recognition engine expects.
"""

import asyncio
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

PROBE_TIMEOUT_S = 60
CONVERT_TIMEOUT_S = 12 * 60  # inside the ingest job's STEP_TIMEOUT, with room for download and upload
SILENCE_FLOOR_DBFS = -999.9  # same floor as the Worker protocol (NUMERIC(5,1))

# ffprobe format_name -> accepted. The first name of the list is the demuxer ffmpeg is pinned to.
ACCEPTED_DEMUXERS = {
    "mov",  # mp4, m4a, 3gp, mov (format_name "mov,mp4,m4a,3gp,3g2,mj2")
    "mp3",
    "wav",
    "aac",
    "amr",
    "ogg",
    "flac",
    "matroska",  # mkv, webm
    "asf",  # wma, wmv
    "flv",
}


@dataclass(frozen=True)
class MediaInfo:
    demuxer: str
    duration_ms: int
    has_audio: bool


@dataclass(frozen=True)
class Converted:
    duration_ms: int
    mean_volume_dbfs: float | None


class MediaTool(Protocol):
    async def probe(self, path: Path) -> MediaInfo | None:
        """None: not a media file we accept."""
        ...

    async def to_speech_audio(self, src: Path, dst: Path, *, demuxer: str) -> Converted: ...


class MediaRejected(Exception):
    """The file is not usable as a meeting recording; the message is shown to nobody but the logs."""


class Ffmpeg:
    def __init__(self, *, ffmpeg: str = "ffmpeg", ffprobe: str = "ffprobe"):
        self._ffmpeg = ffmpeg
        self._ffprobe = ffprobe

    async def probe(self, path: Path) -> MediaInfo | None:
        code, out, _ = await _run(
            [
                self._ffprobe,
                "-v",
                "error",
                "-protocol_whitelist",
                "file",
                "-print_format",
                "json",
                "-show_format",
                "-show_streams",
                str(path),
            ],
            timeout=PROBE_TIMEOUT_S,
        )
        if code != 0:
            return None
        return parse_probe(out)

    async def to_speech_audio(self, src: Path, dst: Path, *, demuxer: str) -> Converted:
        if demuxer not in ACCEPTED_DEMUXERS:
            raise MediaRejected(f"demuxer {demuxer} not accepted")
        code, _, err = await _run(
            [
                self._ffmpeg,
                "-nostdin",
                "-hide_banner",
                "-nostats",
                "-protocol_whitelist",
                "file",
                "-f",
                demuxer,
                "-i",
                str(src),
                "-vn",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-af",
                "volumedetect",
                "-c:a",
                "aac",
                "-b:a",
                "48k",
                "-y",
                str(dst),
            ],
            timeout=CONVERT_TIMEOUT_S,
        )
        if code != 0:
            raise MediaRejected(f"ffmpeg exited {code}")
        info = await self.probe(dst)
        if info is None or not info.has_audio or info.duration_ms <= 0:
            raise MediaRejected("converted file has no audio")
        return Converted(info.duration_ms, parse_mean_volume(err))


def parse_probe(output: bytes) -> MediaInfo | None:
    try:
        data = json.loads(output)
        fmt = data["format"]
        demuxer = fmt["format_name"].split(",")[0]
        duration = float(fmt.get("duration") or 0)
    except (ValueError, KeyError, TypeError, AttributeError):
        return None
    if demuxer not in ACCEPTED_DEMUXERS:
        return None
    has_audio = any(s.get("codec_type") == "audio" for s in data.get("streams", []))
    return MediaInfo(demuxer=demuxer, duration_ms=int(duration * 1000), has_audio=has_audio)


_MEAN = re.compile(rb"mean_volume:\s*(-?[\d.]+|-inf)\s*dB")


def parse_mean_volume(stderr: bytes) -> float | None:
    m = _MEAN.search(stderr)
    if m is None:
        return None
    value = m.group(1)
    return SILENCE_FLOOR_DBFS if value == b"-inf" else max(float(value), SILENCE_FLOOR_DBFS)


async def _run(args: list[str], *, timeout: float) -> tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        await _reap(proc)
        raise MediaRejected("media tool timed out") from None
    except BaseException:  # the job's own deadline cancelled us: the child must not keep running
        await _reap(proc)
        raise
    return proc.returncode or 0, out, err


async def _reap(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await asyncio.shield(proc.wait())
