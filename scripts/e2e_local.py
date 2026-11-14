"""End-to-end run of the upload path with object storage replaced by a local directory.

    cd backend && ../.venv/bin/python ../scripts/e2e_local.py RECORDING [--clip SECONDS] [--out ./e2e-out]

The same loop as `python -m app.tools.demo RECORDING --run`, for when no object storage is available. The only
stand-in is storage: objects are files in a temporary directory and presigned GETs are served by a local HTTP
server on localhost (the ASR client downloads the converted audio from that URL as it would from a cloud
bucket).
Everything else is production code and configuration (.env): stage_local_file, the scheduler, run_jobs_pass
(ingest: ffprobe / ffmpeg to 16 kHz mono AAC; ASR: ASR_PROVIDER; minutes: LLM_API / LLM_MODEL), then the
result page is opened through the FastAPI app (landing, token redeem, cookie, minutes page, a second
redeem refused).

This is also the offline-mode driver: with LLM_API=fake and ASR_PROVIDER=qwen pointed at a local service
running ASR_ENGINE=fake, the whole loop runs with no model account, no GPU and no network. The scheduler
and the jobs pass serve the whole database, so the run refuses to start while any other task is in progress
(it would be processed against the local stand-in too). --clip cuts the first SECONDS of the recording
first (a quick, cheap run). Writes report.md, checks.json,
transcript.txt, minutes.json and result.html to --out. The task stays in the local database like a demo task.
"""

import argparse
import asyncio
import hashlib
import html
import json
import logging
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit
from uuid import UUID

import httpx
from sqlalchemy import select, text

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.config import get_settings  # noqa: E402
from app.db import get_engine  # noqa: E402
from app.deps import _asr, _media_tool, keyring, llm_client  # noqa: E402
from app.intake import messages as msgs  # noqa: E402
from app.main import app  # noqa: E402
from app.models.tables import meeting_tasks  # noqa: E402
from app.postprocess import minutes as mm  # noqa: E402
from app.postprocess.audio import FfmpegMerger  # noqa: E402
from app.postprocess.jobs import PostProcessing  # noqa: E402
from app.postprocess.providers import AsrState  # noqa: E402
from app.runner import run_jobs_pass  # noqa: E402
from app.services.dispatch import scheduler_tick  # noqa: E402
from app.services.results import mint_link  # noqa: E402
from app.services.uploads import ObjectStat  # noqa: E402
from app.services.user_uploads import stage_local_file  # noqa: E402

POLL_S = 5
SITE = "https://testserver"  # the result cookie is Secure: the in-process client must use https


class SubmitLog:
    """The configured ASR provider, keeping the ids it handed out (the job row forgets them on discard)."""

    def __init__(self, inner):
        self._inner = inner
        self.submitted: list[str] = []

    def __getattr__(self, name):
        return getattr(self._inner, name)

    async def submit(self, audio_url: str, title: str | None = None) -> str:
        job_id = await self._inner.submit(audio_url, title)
        self.submitted.append(job_id)
        return job_id


class LocalStorage:
    """COS stand-in: ObjectStorage + BlobStore over a directory; presigned GETs are one-key bearer URLs."""

    def __init__(self, root: Path):
        self.root = root
        self.sha256: dict[str, str] = {}
        self.grants: dict[str, tuple[str, datetime]] = {}  # token -> (object key, expires_at)
        self.downloads: list[str] = []  # object keys fetched through a presigned URL
        self.base_url = ""

    def _file(self, object_key: str) -> Path:
        path = (self.root / object_key).resolve()
        if not path.is_relative_to(self.root.resolve()):
            raise ValueError(f"object key escapes the store: {object_key}")
        return path

    def exists(self, object_key: str) -> bool:
        return self._file(object_key).is_file()

    async def upload_file(self, path: Path, object_key: str, *, content_type: str) -> None:
        dest = self._file(object_key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, dest)
        self.sha256[object_key] = _sha256(dest)

    async def download_file(self, object_key: str, path: Path) -> None:
        shutil.copyfile(self._file(object_key), path)  # FileNotFoundError, as COS answers 404

    async def stat(self, object_key: str) -> ObjectStat | None:
        f = self._file(object_key)
        return ObjectStat(f.stat().st_size, self.sha256.get(object_key)) if f.is_file() else None

    async def delete(self, object_key: str) -> None:
        self._file(object_key).unlink(missing_ok=True)

    async def delete_prefix(self, prefix: str) -> None:
        for f in list(self.root.rglob("*")):
            if f.is_file() and f.relative_to(self.root).as_posix().startswith(prefix):
                f.unlink()

    async def presign_get(self, object_key: str, *, expires_at: datetime) -> str:
        token = secrets.token_urlsafe(16)
        self.grants[token] = (object_key, expires_at)
        return f"{self.base_url}/{quote(object_key)}?t={token}"

    async def presign_put(self, object_key, *, size, sha256, expires_at):
        raise NotImplementedError("the upload path stages files directly")

    def serve(self) -> ThreadingHTTPServer:
        store = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                url = urlsplit(self.path)
                key = unquote(url.path.lstrip("/"))
                grant = store.grants.get((parse_qs(url.query).get("t") or [""])[0])
                if grant is None or grant[0] != key or grant[1] <= datetime.now(UTC):
                    self.send_error(403)
                    return
                if not store.exists(key):
                    self.send_error(404)
                    return
                body = store._file(key).read_bytes()
                store.downloads.append(key)
                self.send_response(200)
                self.send_header("Content-Type", "audio/mp4")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        # "localhost": the environment's NO_PROXY lists it, while httpx ignores the 127.* pattern there.
        self.base_url = f"http://localhost:{server.server_address[1]}"
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server


@dataclass
class Check:
    name: str
    ok: bool
    detail: str


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _audio_format(path: Path) -> dict:
    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=codec_name,sample_rate,channels",
            "-of",
            "json",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    streams = json.loads(probe.stdout).get("streams") or [{}]
    return streams[0]


async def others_in_progress(engine) -> list[str]:
    """Tasks the scheduler or the jobs pass would also pick up (waiting for user input does not count)."""
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT id::text || ' ' || status FROM meeting_tasks WHERE status NOT IN "
                    "('COMPLETED', 'FAILED', 'CANCELED', 'AWAITING_DETAILS', 'AWAITING_CONSENT') "
                    "UNION ALL SELECT task_id::text || ' job ' || kind FROM processing_jobs "
                    "WHERE status IN ('PENDING', 'SUBMITTED')"
                )
            )
        ).scalars()
        return list(rows)


def _clip(source: Path, seconds: int, workdir: Path) -> Path:
    clipped = workdir / f"clip{seconds}s-{source.stem}.m4a"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-i",
            str(source),
            "-t",
            str(seconds),
            "-vn",
            "-c:a",
            "aac",
            str(clipped),
        ],
        check=True,
    )
    return clipped


async def drive(engine, pipeline: PostProcessing, task_id: UUID, timeout: int) -> tuple[str, list]:
    """demo --run's loop: scheduler tick, one jobs pass, then look at the task. Returns (status, timeline)."""
    t = meeting_tasks.c
    start = time.monotonic()
    timeline: list[tuple[float, str]] = []
    last = None
    while time.monotonic() - start < timeout:
        await scheduler_tick(engine, now=datetime.now(UTC))
        await run_jobs_pass(pipeline)
        async with engine.connect() as conn:
            status = await conn.scalar(select(t.status).where(t.id == task_id))
        if status != last:
            elapsed = round(time.monotonic() - start, 1)
            timeline.append((elapsed, status))
            print(f"  {elapsed:7.1f}s  状态：{status}", flush=True)
            last = status
        if status in ("COMPLETED", "FAILED", "CANCELED"):
            return status, timeline
        await asyncio.sleep(POLL_S)
    return "TIMEOUT", timeline


async def load_rows(engine, task_id: UUID) -> dict:
    q = {
        "task": "SELECT status, failure_code, failure_reason, consent_version, retention_until "
        "FROM meeting_tasks WHERE id = :id",
        "inputs": "SELECT kind, source_msg_type, file_name, consumed_at FROM task_inputs WHERE task_id = :id",
        "assets": "SELECT object_key, format, duration_ms, bytes, file_sha256, rms_dbfs, upload_status "
        "FROM audio_assets WHERE task_id = :id",
        "jobs": "SELECT kind, provider, status, attempts, external_job_id, last_error, input_tokens, "
        "output_tokens, created_at, updated_at FROM processing_jobs WHERE task_id = :id ORDER BY created_at",
        "events": "SELECT event_seq, event_type, actor, data, created_at FROM task_events "
        "WHERE task_id = :id ORDER BY created_at, id",
        "transcripts": "SELECT id, source, language, version, body FROM transcripts WHERE task_id = :id",
        "minutes": "SELECT transcript_id, version, is_current, summary, key_points, key_facts, outlook, "
        "decisions, action_items, risks, name_fixes, model_provider, model_name, model_region, "
        "prompt_version, meeting_type, meeting_date, meeting_date_source "
        "FROM meeting_minutes WHERE task_id = :id",
    }
    out = {}
    async with engine.connect() as conn:
        for name, sql in q.items():
            out[name] = [dict(r) for r in (await conn.execute(text(sql), {"id": task_id})).mappings().all()]
    return out


async def open_result_page(engine, task_id: UUID) -> tuple[list[Check], str]:
    """A one-time link, opened like a phone would (landing, POST redeem, cookie), then redeemed again."""
    async with engine.connect() as conn:
        user_id = await conn.scalar(select(meeting_tasks.c.user_id).where(meeting_tasks.c.id == task_id))
    async with engine.begin() as conn:
        link = await mint_link(conn, task_id=task_id, user_id=user_id, now=datetime.now(UTC), base_url=SITE)
    path = urlsplit(link).path
    checks = []
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=SITE) as phone:
        landing = await phone.get(path)
        checks.append(
            Check("结果页：落地页不消耗链接", landing.status_code == 200, f"HTTP {landing.status_code}")
        )
        redeem = await phone.post(path)
        cookie_set = any(k.startswith("ma_s_") for k in phone.cookies.keys())
        checks.append(
            Check(
                "结果页：兑换链接后跳转并下发会话 cookie",
                redeem.status_code == 303 and cookie_set,
                f"HTTP {redeem.status_code}，cookie {'有' if cookie_set else '无'}",
            )
        )
        view = await phone.get(f"/r/t/{task_id}")
        page = view.text
        checks.append(
            Check(
                "结果页：纪要页面可打开",
                view.status_code == 200,
                f"HTTP {view.status_code}，{len(page)} 字符",
            )
        )
    async with httpx.AsyncClient(transport=transport, base_url=SITE) as stranger:
        again = await stranger.post(path)
        checks.append(
            Check("结果页：同一链接换设备再兑换被拒", again.status_code == 410, f"HTTP {again.status_code}")
        )
    return checks, page


def transcript_checks(segments: list[dict], duration_ms: int | None) -> list[Check]:
    ids_ok = [s["i"] for s in segments] == list(range(len(segments)))
    ordered = all(a["start_ms"] <= b["start_ms"] for a, b in zip(segments, segments[1:], strict=False))
    sane = all(0 <= s["start_ms"] <= s["end_ms"] for s in segments)
    last_end = max((s["end_ms"] for s in segments), default=0)
    within = duration_ms is None or last_end <= duration_ms + 2000
    chars = sum(len(s["text"]) for s in segments)
    return [
        Check("逐字稿：有内容", bool(segments) and chars > 0, f"{len(segments)} 段，{chars} 字"),
        Check(
            "逐字稿：段号连续、时间有序", ids_ok and ordered and sane, f"段号连续 {ids_ok}，有序 {ordered}"
        ),
        Check(
            "逐字稿：时间不超出音频长度",
            within,
            f"最后一段结束 {last_end / 1000:.1f}s，音频 {(duration_ms or 0) / 1000:.1f}s",
        ),
    ]


def minutes_checks(minutes: dict, segments: list[dict], page: str, title: str | None) -> list[Check]:
    s = get_settings()
    known = {seg["i"] for seg in segments}
    cited = [i for key in mm.CITED_SECTIONS for item in minutes[key] for i in item["evidence_segments"]]
    revalidated = True
    try:  # the stored minutes still pass the production validator unchanged
        # meeting_date is stored as resolved (file name, upload...), not as the model said it: not compared
        fields = {k: minutes[k] for k in mm.Minutes.model_fields if k != "meeting_date"}
        again = mm.as_columns(mm.validate(fields, segments, title=title))
        revalidated = again == {k: minutes[k] for k in again}
    except Exception:
        revalidated = False
    summary_head = minutes["summary"][:20]
    day, source, kind = minutes["meeting_date"], minutes["meeting_date_source"], minutes["meeting_type"]
    meta = f"会议时间：{day:%Y-%m-%d}" if day else ""
    return [
        Check(
            "纪要：由配置的模型生成",
            minutes["model_name"] == s.llm_model and minutes["model_provider"] == s.llm_provider,
            f"{minutes['model_provider']} / {minutes['model_name']} / {minutes['prompt_version']}",
        ),
        Check("纪要：摘要非空", bool(minutes["summary"].strip()), f"{len(minutes['summary'])} 字"),
        Check(
            "纪要：引用的段号都存在",
            all(i in known for i in cited),
            f"引用 {len(cited)} 处；"
            + "，".join(f"{SECTIONS[k]} {len(minutes[k])}" for k in mm.CITED_SECTIONS),
        ),
        Check(
            "纪要：存库内容可再次通过生产校验",
            revalidated,
            "mm.validate 结果与存库一致" if revalidated else "不一致",
        ),
        Check(
            "结果页：显示的是这份纪要",
            bool(summary_head) and html.escape(summary_head) in page,
            f"摘要开头「{summary_head}」",
        ),
        Check(
            "结果页：标题后有会议时间和类型",
            bool(day and kind) and meta in page and f"会议类型：{kind}" in page,
            f"{day} （{source}） · {kind}",
        ),
    ]


async def provider_forgot(asr: SubmitLog, job: dict) -> Check:
    name = "ASR：识别服务上的逐字稿副本已删除"
    if getattr(asr, "discard", None) is None:
        return Check(name, True, "不适用（提供方不保留结果）")
    if not asr.submitted:
        return Check(name, False, "没有记录到提交")
    states = [(await asr.poll(i)).state for i in asr.submitted]
    ok = all(state == AsrState.LOST for state in states) and job["external_job_id"] is None
    return Check(name, ok, f"提交 {len(asr.submitted)} 次，再查询返回 {', '.join(map(str, states))}")


SECTIONS = {
    "key_points": "主要观点",
    "key_facts": "重要数据与事实",
    "outlook": "计划与预期",
    "decisions": "决定",
    "action_items": "待办",
    "risks": "风险",
    "name_fixes": "名称更正",
}


def _item_line(key: str, item: dict) -> str:
    if key == "name_fixes":
        return f"{item['heard']} → {item['written']}"
    text_ = item.get("text") or item.get("task")
    if key == "action_items":
        text_ += f"（{item.get('owner')}，{item.get('due') or '无期限'}）"
    if key == "outlook":
        text_ = f"[{item['kind']}] {text_}（{item.get('timeframe') or '无时间'}）"
    text_ += "".join(f"\n    - {d}" for d in item.get("details", []))
    return f"{text_} 〔引用 {item['evidence_segments']}〕"


def write_outputs(out: Path, *, rows: dict, checks: list[Check], timeline, page: str, meta: dict) -> None:
    out.mkdir(parents=True, exist_ok=True)
    segments = rows["transcripts"][0]["body"]["segments"] if rows["transcripts"] else []
    (out / "transcript.txt").write_text(
        "\n".join(f"[{s['i']}] {mm.segment_label(s)}{s['text']}" for s in segments) + "\n", encoding="utf-8"
    )
    if rows["minutes"]:
        (out / "minutes.json").write_text(
            json.dumps(rows["minutes"][0], ensure_ascii=False, indent=1, default=str), encoding="utf-8"
        )
    if page:
        (out / "result.html").write_text(page, encoding="utf-8")
    (out / "checks.json").write_text(
        json.dumps(
            {
                "meta": meta,
                "timeline": timeline,
                "checks": [asdict(c) for c in checks],
                "jobs": rows["jobs"],
                "assets": rows["assets"],
                "events": rows["events"],
                "task": rows["task"],
            },
            ensure_ascii=False,
            indent=1,
            default=str,
        ),
        encoding="utf-8",
    )
    lines = ["# 端到端测试：录音 → 转码 → 识别 → 纪要 → 结果页", ""]
    lines += [f"- {k}：{v}" for k, v in meta.items()]
    lines += ["", "## 状态时间线", "", "| 用时 s | 状态 |", "|---|---|"]
    lines += [f"| {e} | {s} |" for e, s in timeline]
    lines += [
        "",
        "## 处理作业",
        "",
        "| 作业 | 提供方 | 结果 | 尝试 | 输入 tok | 输出 tok | 耗时 s | 错误 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for j in rows["jobs"]:
        took = (j["updated_at"] - j["created_at"]).total_seconds()
        lines.append(
            f"| {j['kind']} | {j['provider']} | {j['status']} | {j['attempts']} | "
            f"{j['input_tokens'] or '-'} | {j['output_tokens'] or '-'} | {took:.1f} | "
            f"{j['last_error'] or ''} |"
        )
    lines += ["", "## 检查项", "", "| 结果 | 检查 | 说明 |", "|---|---|---|"]
    lines += [f"| {'✅' if c.ok else '❌'} | {c.name} | {c.detail} |" for c in checks]
    if rows["minutes"]:
        m = rows["minutes"][0]
        lines += ["", "## 纪要", "", f"**摘要**：{m['summary']}", ""]
        for key, title in SECTIONS.items():
            lines += [f"**{title}**", ""]
            for item in m[key]:
                lines.append(f"- {_item_line(key, item)}")
            lines.append("")
    (out / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("recording", type=Path)
    parser.add_argument("--clip", type=int, help="only the first SECONDS of the recording")
    parser.add_argument("--out", type=Path, default=Path(__file__).resolve().parents[1] / "study" / "e2e")
    parser.add_argument("--timeout", type=int, default=3600)
    args = parser.parse_args()
    if not args.recording.is_file():
        print(f"找不到文件 {args.recording}", file=sys.stderr)
        return 2

    s = get_settings()
    media = _media_tool()
    if media is None:
        print("没有安装 ffmpeg / ffprobe", file=sys.stderr)
        return 1
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    engine = get_engine()
    busy = await others_in_progress(engine)
    if busy:
        print("数据库里还有处理中的任务，先等它们结束：\n  " + "\n  ".join(busy), file=sys.stderr)
        return 1
    with tempfile.TemporaryDirectory(prefix="e2e-") as tmp:
        work = Path(tmp)
        recording = _clip(args.recording, args.clip, work) if args.clip else args.recording
        source_info = await media.probe(recording)
        store = LocalStorage(work / "bucket")
        store.root.mkdir()
        server = store.serve()
        pipeline = PostProcessing(
            asr=SubmitLog(_asr()),
            llm=llm_client(),
            storage=store,
            merger=FfmpegMerger(store),
            asr_daily_audio_seconds=int(s.asr_daily_audio_hours * 3600),
            media=media,
            keyring=keyring(),
        )
        asr_model = {
            "whisper": s.asr_whisper_model,
            "qwen": "qwen3-asr-0.6b",
            "funasr": "fun-asr-nano-2512",
        }.get(s.asr_provider, s.asr_engine_model)
        meta = {
            "录音": args.recording.name + (f"（前 {args.clip} 秒）" if args.clip else ""),
            "录音时长 s": round(source_info.duration_ms / 1000, 1) if source_info else "?",
            "识别": f"{s.asr_provider}（{asr_model}）",
            "纪要模型": f"{s.llm_api} / {s.llm_model} / effort {s.llm_effort}",
            "对象存储": "本地目录替身（COS 不可用）",
        }
        try:
            print(f"录音 {meta['录音']}，{meta['录音时长 s']} 秒；{meta['识别']}；{meta['纪要模型']}")
            started = time.monotonic()
            task_id = await stage_local_file(
                engine, store, keyring(), recording, user_label="e2e-local", now=datetime.now(UTC)
            )
            staged = list(store.sha256)  # only the original so far
            print(f"任务 {task_id} 已创建，开始处理…")
            status, timeline = await drive(engine, pipeline, task_id, args.timeout)
            meta["任务"] = str(task_id)
            meta["总用时 s"] = round(time.monotonic() - started, 1)
            rows = await load_rows(engine, task_id)
            checks = [Check("任务完成", status == "COMPLETED", status)]
            if status != "COMPLETED":
                task = rows["task"][0] if rows["task"] else {}
                if task.get("failure_code"):
                    print(msgs.failure(task["failure_code"]), file=sys.stderr)
                for j in rows["jobs"]:
                    print(
                        f"  {j['kind']} {j['status']} attempts={j['attempts']} {j['last_error'] or ''}",
                        file=sys.stderr,
                    )
                write_outputs(args.out, rows=rows, checks=checks, timeline=timeline, page="", meta=meta)
                return 1

            asset = rows["assets"][0]
            converted = store._file(asset["object_key"])
            info = await media.probe(converted)
            fmt = _audio_format(converted)
            checks += [
                Check(
                    "上传：暂存的原始文件已删除",
                    all(not store.exists(k) for k in staged),
                    f"{len(staged)} 个暂存对象",
                ),
                Check(
                    "转码：时长与原录音一致",
                    source_info is not None and abs(asset["duration_ms"] - source_info.duration_ms) <= 1500,
                    f"原 {source_info and source_info.duration_ms} ms，转码后 {asset['duration_ms']} ms",
                ),
                Check(
                    "转码：存储的音频与记录的 sha256 / 大小一致",
                    _sha256(converted) == asset["file_sha256"] and converted.stat().st_size == asset["bytes"],
                    f"{asset['bytes']} 字节，{asset['format']}，音量 {asset['rms_dbfs']} dBFS",
                ),
                Check(
                    "转码：输出是 16 kHz 单声道 AAC",
                    bool(info and info.has_audio)
                    and fmt.get("codec_name") == "aac"
                    and fmt.get("sample_rate") == "16000"
                    and fmt.get("channels") == 1,
                    f"{fmt.get('codec_name')}，{fmt.get('sample_rate')} Hz，{fmt.get('channels')} 声道",
                ),
                Check(
                    "ASR：识别服务通过预签名链接取到了转码后的音频",
                    asset["object_key"] in store.downloads,
                    f"下载 {len(store.downloads)} 次",
                ),
            ]
            kinds = {j["kind"]: j for j in rows["jobs"]}
            checks.append(
                Check(
                    "作业：转码 / 识别 / 纪要各一个且都成功",
                    set(kinds) == {"AUDIO_NORMALIZE", "ASR", "SUMMARY"}
                    and len(rows["jobs"]) == 3
                    and all(j["status"] == "SUCCEEDED" for j in rows["jobs"]),
                    "，".join(f"{j['kind']}={j['status']}×{j['attempts']}" for j in rows["jobs"]),
                )
            )
            segments = rows["transcripts"][0]["body"]["segments"]
            checks += transcript_checks(segments, asset["duration_ms"])
            checks.append(await provider_forgot(pipeline.asr, kinds["ASR"]))
            page_checks, page = await open_result_page(engine, task_id)
            minutes = rows["minutes"][0]
            title = rows["inputs"][0]["file_name"] if rows["inputs"] else None
            checks += minutes_checks(minutes, segments, page, title) + page_checks
            write_outputs(args.out, rows=rows, checks=checks, timeline=timeline, page=page, meta=meta)
        finally:
            server.shutdown()
            await pipeline.llm.aclose()
            if hasattr(pipeline.asr, "aclose"):
                await pipeline.asr.aclose()

    failed = [c for c in checks if not c.ok]
    for c in checks:
        print(f"{'✅' if c.ok else '❌'} {c.name}：{c.detail}")
    print(f"报告：{args.out / 'report.md'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
