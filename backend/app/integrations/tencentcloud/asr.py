"""Tencent Cloud 录音文件识别 (official doc 1093/37823 CreateRecTask, 1093/37822 DescribeTaskStatus).

The audio is passed as a URL the service downloads (a presigned COS GET); at most 5 hours and 1 GB per file.
ResTextFormat 2 returns ResultDetail sentences with millisecond timestamps and punctuation; with speaker
diarization on, each sentence carries a SpeakerId.
"""

import math
import re

from ...postprocess.providers import AsrResult, AsrSegment, AsrState, ProviderError
from .api import TencentCloudApi, TencentCloudError

SERVICE, VERSION = "asr", "2019-06-14"
DEFAULT_ENGINE = "16k_zh_en_meeting"  # large model for multi-speaker meetings; supports diarization
_WAITING, _DOING, _SUCCESS, _FAILED = 0, 1, 2, 3
_LINE = re.compile(r"^\[([\d:.]+),([\d:.]+)(?:,(\d+))?\]\s*(.*)$")


class TencentAsr:
    name = "tencent_asr"

    def __init__(self, api: TencentCloudApi, *, engine: str = DEFAULT_ENGINE):
        self._api = api
        self._engine = engine

    async def submit(self, audio_url: str, title: str | None = None) -> str:  # no context: title unused
        data = await self._call(
            "CreateRecTask",
            {
                "EngineModelType": self._engine,
                "ChannelNum": 1,
                "ResTextFormat": 2,
                "SourceType": 0,
                "Url": audio_url,
                "SpeakerDiarization": 1,
                "SpeakerNumber": 0,  # automatic
            },
        )
        return str(data["Data"]["TaskId"])

    async def poll(self, task_id: str) -> AsrResult:
        try:
            data = await self._call("DescribeTaskStatus", {"TaskId": int(task_id)})
        except ProviderError as exc:
            if str(exc) == "FailedOperation.NoSuchTask":
                return AsrResult(AsrState.FAILED, error=str(exc))
            raise
        task = data["Data"]
        status = task["Status"]
        if status in (_WAITING, _DOING):
            return AsrResult(AsrState.RUNNING)
        if status != _SUCCESS:
            error = task.get("ErrorMsg") or task.get("StatusStr") or ""
            return AsrResult(AsrState.FAILED, error=error[:200])
        duration = task.get("AudioDuration")
        return AsrResult(
            AsrState.DONE,
            segments=_segments(task),
            billed_seconds=math.ceil(duration) if duration else None,
        )

    async def _call(self, action: str, params: dict) -> dict:
        """Errors become ProviderError(code): the documented code is worth storing, the message may quote the
        request (the presigned audio URL)."""
        try:
            return await self._api.call(action, params)
        except TencentCloudError as exc:
            raise ProviderError(exc.code) from None


def _segments(task: dict) -> list[AsrSegment]:
    detail = task.get("ResultDetail")
    if detail:
        return [
            AsrSegment(
                start_ms=int(s["StartMs"]),
                end_ms=int(s["EndMs"]),
                text=s.get("FinalSentence") or "",
                speaker=None if s.get("SpeakerId") is None else str(s["SpeakerId"]),
            )
            for s in detail
        ]
    out = []  # ResultDetail missing: fall back to the "[0:0.020,0:2.380,0]  text" lines of Result
    for line in (task.get("Result") or "").splitlines():
        m = _LINE.match(line.strip())
        if m:
            out.append(AsrSegment(_ms(m[1]), _ms(m[2]), m[4], m[3]))
    return out


def _ms(clock: str) -> int:
    seconds = 0.0
    for part in clock.split(":"):
        seconds = seconds * 60 + float(part)
    return round(seconds * 1000)
