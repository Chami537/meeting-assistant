"""Fallback MVP demo without WeChat (design doc §11 G3): a local recording -> minutes, end to end against the
real COS / ASR / LLM configured in .env.

    python -m app.tools.demo meeting.m4a            (from backend/; the runner does the processing)
    python -m app.tools.demo meeting.m4a --run      (process here; no runner needed)

Consent is recorded on the operator's word (channel api): only use recordings you may process. When the
minutes are ready a one-time result link is printed (PUBLIC_BASE_URL, or the local API on 127.0.0.1:8000).
"""

import argparse
import asyncio
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select

from ..config import get_settings
from ..db import get_engine
from ..deps import keyring, object_storage, postprocessing
from ..intake import messages as msgs
from ..models.tables import meeting_tasks
from ..runner import run_jobs_pass
from ..services.dispatch import scheduler_tick
from ..services.results import mint_link
from ..services.user_uploads import UploadError, stage_local_file

POLL_S = 5


async def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.tools.demo")
    parser.add_argument("recording", type=Path)
    parser.add_argument("--user", default="demo-user", help="label of the demo user the task belongs to")
    parser.add_argument("--run", action="store_true", help="process the jobs in this process")
    parser.add_argument("--timeout", type=int, default=3600, help="seconds to wait for the minutes")
    args = parser.parse_args(argv)
    if not args.recording.is_file():
        print(f"找不到文件 {args.recording}", file=sys.stderr)
        return 2

    pipeline = None
    if args.run:  # check the whole configuration before anything is uploaded
        try:
            pipeline = postprocessing()
        except RuntimeError as exc:
            print(f"无法开始：{exc}", file=sys.stderr)
            return 1
        if pipeline.media is None:
            print("无法开始：没有安装 ffmpeg / ffprobe（sudo apt install ffmpeg）", file=sys.stderr)
            return 1
    engine = get_engine()
    try:
        task_id = await stage_local_file(
            engine,
            object_storage(),
            keyring(),
            args.recording,
            user_label=args.user,
            now=datetime.now(UTC),
        )
    except (UploadError, RuntimeError) as exc:
        print(f"无法开始：{exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # storage or database unreachable: a message, not a traceback
        print(
            f"无法开始：{type(exc).__name__}（请先运行 python -m app.tools.smoke 检查配置）", file=sys.stderr
        )
        return 1
    print(f"任务 {task_id} 已创建，录音已上传，开始处理…")

    t = meeting_tasks.c
    last = None
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        if pipeline is not None:
            await scheduler_tick(engine, now=datetime.now(UTC))
            await run_jobs_pass(pipeline)
        async with engine.connect() as conn:
            row = (
                await conn.execute(select(t.status, t.failure_code, t.user_id).where(t.id == task_id))
            ).one()
        if row.status != last:
            print(f"  状态：{row.status}")
            last = row.status
        if row.status == "COMPLETED":
            base = get_settings().public_base_url or "http://127.0.0.1:8000"
            async with engine.begin() as conn:
                link = await mint_link(
                    conn, task_id=task_id, user_id=row.user_id, now=datetime.now(UTC), base_url=base
                )
            print(f"纪要已生成：{link}\n（链接 24 小时内有效、只能打开一次；本地调试请用 localhost 打开）")
            return 0
        if row.status in ("FAILED", "CANCELED"):
            print(msgs.failure(row.failure_code) if row.failure_code else "任务已取消", file=sys.stderr)
            return 1
        await asyncio.sleep(POLL_S)
    print("等待超时；任务仍在处理，稍后可在结果页查看。", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
