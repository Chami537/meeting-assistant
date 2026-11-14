"""Looking at a file a user sent through WeChat KF before deciding what to do with it.

sync_msg gives only a media_id (no name, size or type; official doc 94670), so the first bytes are fetched
(one Range request) to tell a recording from an exported transcript. Documents are small (KF caps files at
20 MB) and are read in full so their text can be confirmed with the user; recordings are only downloaded
after consent, by the AUDIO_NORMALIZE job. Runs outside any database transaction.
"""

import asyncio
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..intake import material
from ..integrations.wecom.client import MEDIA_EXPIRED, MediaFile, MediaTooLarge, WeComApiError

EXTRACT_TIMEOUT_S = 10


class MediaPeek(Protocol):
    async def media_head(self, media_id: str, size: int) -> tuple[bytes, MediaFile]: ...

    async def download_media(self, media_id: str, dest: Path, *, max_bytes: int) -> MediaFile: ...


@dataclass(frozen=True)
class Prefetched:
    kind: material.Kind | None  # None: the media_id has expired
    text: str | None = None  # documents only
    file_name: str | None = None


async def prefetch(peek: MediaPeek, media_id: str) -> Prefetched:
    try:
        head, info = await peek.media_head(media_id, material.HEAD_BYTES)
    except WeComApiError as exc:
        if exc.errcode in MEDIA_EXPIRED:
            return Prefetched(None)
        raise
    kind = material.classify(head)
    if kind != material.Kind.DOCUMENT:
        return Prefetched(kind, file_name=info.file_name)
    if info.size > material.MAX_DOCUMENT_BYTES:
        return Prefetched(material.Kind.UNKNOWN, file_name=info.file_name)
    with tempfile.TemporaryDirectory(prefix="kfdoc-") as tmp:
        path = Path(tmp) / "doc"
        try:
            await peek.download_media(media_id, path, max_bytes=material.MAX_DOCUMENT_BYTES)
        except MediaTooLarge:
            return Prefetched(material.Kind.UNKNOWN, file_name=info.file_name)
        data = path.read_bytes()
    try:  # CPU-bound and fed untrusted input: off the event loop, with a deadline
        text = await asyncio.wait_for(
            asyncio.to_thread(material.extract_text, data), timeout=EXTRACT_TIMEOUT_S
        )
    except TimeoutError:
        text = None
    if text is None:
        return Prefetched(material.Kind.UNKNOWN, file_name=info.file_name)
    return Prefetched(material.Kind.DOCUMENT, text=text, file_name=info.file_name)
