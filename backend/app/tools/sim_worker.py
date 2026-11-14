"""Simulated Worker for end-to-end runs without Windows or a real meeting (design doc §11 G3 demo).

It follows the Worker protocol of §5.2: lease, join events, recording approval, then for each segment
seal -> upload-url -> PUT -> complete, and finally MEETING_ENDED and LEFT. The audio is a local file you
supply (any format ASR accepts); every segment uploads the same file.

    python -m app.tools.sim_worker --api http://127.0.0.1:8000 --token <worker token> --audio sample.m4a
"""

import argparse
import asyncio
import hashlib
import logging
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

log = logging.getLogger("sim_worker")


class Stopped(Exception):
    """The backend told us to stop (409 CANCELED / NOT_OWNER, or STOP_AND_UPLOAD)."""


@dataclass
class SimWorker:
    api: httpx.AsyncClient
    storage: httpx.AsyncClient  # PUTs go to the presigned URL, not to the API
    token: str
    audio: bytes
    audio_format: str = "m4a"
    segment_ms: int = 60_000
    rms_dbfs: float = -20.0
    seq: int = 0
    attempt_id: str | None = None
    clock_offset: timedelta = timedelta(0)
    log: list[str] = field(default_factory=list)

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    def now(self) -> datetime:
        return datetime.now(UTC) + self.clock_offset

    async def lease(self) -> dict | None:
        r = await self.api.post("/v1/workers/lease", headers=self.headers)
        if r.status_code == 204:
            return None
        r.raise_for_status()
        body = r.json()
        self.attempt_id = body["attempt_id"]
        self.seq = body["last_event_seq"]
        self.clock_offset = datetime.fromisoformat(body["server_time"]) - datetime.now(UTC)
        return body

    async def event(self, type_: str, **data) -> None:
        self.seq += 1
        body = {"seq": self.seq, "type": type_, "occurred_at": self.now().isoformat(), "data": data}
        url = f"/v1/workers/attempts/{self.attempt_id}/events"
        r = await self.api.post(url, headers=self.headers, json=body)
        if r.status_code == 409:
            conflict = r.json()
            if conflict["reason"] == "OUT_OF_ORDER" and conflict.get("expected_seq"):
                self.seq = conflict["expected_seq"] - 1
                return await self.event(type_, **data)
            raise Stopped(conflict["reason"])
        r.raise_for_status()
        self.log.append(type_)

    async def heartbeat(self) -> str:
        r = await self.api.post(f"/v1/workers/attempts/{self.attempt_id}/heartbeat", headers=self.headers)
        if r.status_code == 409:
            raise Stopped(r.json()["reason"])
        r.raise_for_status()
        return r.json()["command"]

    async def upload(self, segment_no: int, started: datetime) -> None:
        sha = hashlib.sha256(self.audio).hexdigest()
        base = f"/v1/workers/attempts/{self.attempt_id}/segments/{segment_no}"
        r = await self.api.post(
            f"{base}/upload-url",
            headers=self.headers,
            json={
                "file_sha256": sha,
                "bytes": len(self.audio),
                "format": self.audio_format,
                "duration_ms": self.segment_ms,
                "recorded_started_at": started.isoformat(),
                "recorded_ended_at": (started + timedelta(milliseconds=self.segment_ms)).isoformat(),
            },
        )
        if r.status_code == 409:
            raise Stopped(r.json()["reason"])
        r.raise_for_status()
        target = r.json()
        put = await self.storage.put(
            target["upload_url"], content=self.audio, headers=target["upload_headers"]
        )
        put.raise_for_status()
        r = await self.api.post(
            f"{base}/complete",
            headers=self.headers,
            json={"file_sha256": sha, "bytes": len(self.audio), "rms_dbfs": self.rms_dbfs},
        )
        r.raise_for_status()
        self.log.append(f"UPLOADED {segment_no}")

    async def attend(self, segments: int) -> None:
        """One meeting: join, get approval, record `segments` segments, leave."""
        await self.event("JOIN_REQUESTED")
        await self.event("JOINED")
        await self.event("RECORDING_NOTICE_SENT")
        await self.event("RECORDING_APPROVED")
        await self.event("RECORDING_STARTED")
        started = self.now()
        for n in range(segments):
            await self.event("SEGMENT_SEALED", segment_no=n)
            await self.upload(n, started + timedelta(milliseconds=n * self.segment_ms))
            if await self.heartbeat() == "STOP_AND_UPLOAD":
                break
        await self.event("RECORDING_STOPPED")
        await self.event("MEETING_ENDED")
        await self.event("LEFT")


async def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.tools.sim_worker")
    parser.add_argument("--api", required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--audio", required=True, type=Path)
    parser.add_argument("--segments", type=int, default=1)
    parser.add_argument("--segment-ms", type=int, default=60_000)
    parser.add_argument("--poll", type=float, default=5.0, help="seconds between lease attempts")
    parser.add_argument("--once", action="store_true", help="exit after one meeting")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    audio = args.audio.read_bytes()
    fmt = args.audio.suffix.lstrip(".").lower() or "m4a"
    api = httpx.AsyncClient(base_url=args.api, timeout=30)
    storage = httpx.AsyncClient(timeout=300)
    async with api, storage:
        worker = SimWorker(api, storage, args.token, audio, audio_format=fmt, segment_ms=args.segment_ms)
        while True:
            lease = await worker.lease()
            if lease is None:
                await asyncio.sleep(args.poll)
                continue
            log.info("leased attempt %s for task %s", lease["attempt_id"], lease["task"]["task_id"])
            try:
                await worker.attend(args.segments)
            except Stopped as exc:
                log.info("stopped by the backend: %s", exc)
            log.info("events: %s", ", ".join(worker.log))
            worker.log.clear()
            if args.once:
                return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
