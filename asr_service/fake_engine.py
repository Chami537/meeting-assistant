"""Offline stand-in for the recognition engine (ASR_ENGINE=fake): no GPU, no model, no network.

The job API in front of it is the real one (api.py, jobs.py): the bounded queue, 429 with Retry-After when
it is full, priority jobs first, retries, the 24-hour keep. Only the transcription is made up: the audio's
length is read (WAV header, else ffprobe when installed, else one minute) and filled with a fixed synthetic
script, one segment every SEGMENT_SECONDS, two speakers taking turns, every segment with timed sentences
when the job asks for them. Numerals go through the real number conversion (itn.py) as recognized text does.
Every text starts from SCRIPT below, so nothing it returns comes from the audio.

ASR_FAKE_DELAY (seconds, default 0) holds every job that long, for watching the queue fill up by hand.
"""

import json
import shutil
import subprocess
import time
import wave
from pathlib import Path
from typing import Any

from .itn import itn
from .jobs import AudioError

SEGMENT_SECONDS = 6.0
DEFAULT_SECONDS = 60.0
# A made-up quarterly call of a made-up company: numbers, a plan, a risk, so the minutes have every section.
SCRIPT = (
    ("欢迎参加示例科技二〇二六年三季度业绩交流会。", "我先介绍一下本季度的经营情况。"),
    ("三季度营业收入一点二亿元，同比增长百分之十五。", "毛利率是百分之四十二，比上季度提高两个百分点。"),
    ("新产品线已经完成小批量试产。", "我们计划在明年一季度正式量产。"),
    ("海外订单占比提高到百分之三十。", "主要来自东南亚的两个新客户。"),
    ("请问原材料价格上涨对毛利有什么影响？", "原材料成本压力还在，这是主要的风险。"),
    ("公司预计全年收入增长百分之十二左右。", "目标是年底前把交付周期缩短到四周。"),
    ("研发投入占收入的百分之九。", "研发团队今年新增了二十人。"),
    ("谢谢大家的参与。", "后续有问题可以联系投资者关系部门。"),
)


class FakeEngine:
    sentences = True  # the health report says sentence times are available, as with the aligner loaded

    def __init__(self, *, delay: float = 0.0):
        self.delay = delay

    def healthy(self) -> bool:
        return True

    def transcribe(self, audio: Path, options: dict[str, Any]) -> list[dict[str, Any]]:
        if self.delay:
            time.sleep(self.delay)
        seconds = _duration(audio)
        segments, start, k = [], 0.0, 0
        while start < seconds:
            end = min(seconds, start + SEGMENT_SECONDS)
            first, second = (itn(t) for t in SCRIPT[k % len(SCRIPT)])
            segment: dict[str, Any] = {
                "start": round(start, 2),
                "end": round(end, 2),
                "text": first + second,
                "speaker": f"发言人{k % 2 + 1}",
                "compression_ratio": 1.0,
            }
            if options.get("sentences"):
                middle = round(start + (end - start) / 2, 2)
                segment["sentences"] = [
                    {"start": round(start, 2), "end": middle, "text": first},
                    {"start": middle, "end": round(end, 2), "text": second},
                ]
            segments.append(segment)
            start, k = end, k + 1
        return segments


def _duration(audio: Path) -> float:
    try:
        with wave.open(str(audio), "rb") as w:
            return w.getnframes() / float(w.getframerate())
    except (wave.Error, EOFError):
        pass
    if shutil.which("ffprobe"):
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", str(audio)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if probe.returncode != 0:
            raise AudioError("the file is not audio that can be decoded")
        try:
            return float(json.loads(probe.stdout)["format"]["duration"])
        except (KeyError, ValueError):
            raise AudioError("the audio has no duration") from None
    return DEFAULT_SECONDS
