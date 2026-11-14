"""Joining a meeting's audio segments into the single file that ASR receives (design doc §7.5).

One recognition job over the whole meeting keeps speaker labels consistent, which per-segment jobs would not.
FFmpeg must be installed on the server that runs the runner (`apt install ffmpeg`); tests use a fake merger.
"""

import asyncio
import tempfile
from pathlib import Path
from typing import Protocol, runtime_checkable

MERGE_TIMEOUT_S = 15 * 60


class AudioMerger(Protocol):
    async def concat(self, sources: list[str], target: str) -> None:
        """Write the stored objects `sources`, in order, as one audio object at `target`.
        Overwrites `target`, so a retry after a crash is harmless."""
        ...


@runtime_checkable
class BlobStore(Protocol):
    async def download_file(self, object_key: str, path: Path) -> None: ...

    async def upload_file(self, path: Path, object_key: str, *, content_type: str) -> None: ...

    async def delete(self, object_key: str) -> None: ...


class MergeFailed(Exception):
    pass


class FfmpegMerger:
    """Downloads the segments to a scratch directory, concatenates them with the concat demuxer and
    re-encodes to 16 kHz mono AAC (what the recognition engine takes), then uploads the result."""

    def __init__(self, store: BlobStore, *, ffmpeg: str = "ffmpeg"):
        self._store = store
        self._ffmpeg = ffmpeg

    async def concat(self, sources: list[str], target: str) -> None:
        with tempfile.TemporaryDirectory(prefix="merge-") as tmp:
            work = Path(tmp)
            parts = []
            for i, key in enumerate(sources):
                part = work / f"part-{i:05d}{Path(key).suffix}"
                await self._store.download_file(key, part)
                parts.append(part)
            listing = work / "parts.txt"
            # The names are ours (part-00000.m4a), so no quoting issues in the concat list.
            listing.write_text("".join(f"file '{p.name}'\n" for p in parts))
            out = work / "merged.m4a"
            proc = await asyncio.create_subprocess_exec(
                self._ffmpeg,
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(listing),
                "-vn",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-c:a",
                "aac",
                "-b:a",
                "48k",
                str(out),
                cwd=work,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                _, stderr = await asyncio.wait_for(proc.communicate(), timeout=MERGE_TIMEOUT_S)
            except TimeoutError:
                proc.kill()
                await proc.wait()
                raise MergeFailed("ffmpeg timed out") from None
            if proc.returncode != 0:
                tail = stderr.decode(errors="replace")[-300:]
                raise MergeFailed(f"ffmpeg exited {proc.returncode}: {tail}")
            await self._store.upload_file(out, target, content_type="audio/mp4")
