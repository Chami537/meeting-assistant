"""Compare minutes models on the transcripts already in the local database (no audio, no COS).

    cd backend && ../.venv/bin/python ../scripts/eval_minutes.py [--runs 1] [--out ./eval-out] [--gold ...]
        [--transcript NAME=PATH ...]

For every stored transcript (one per ASR provider: the newest; or the given JSON files) and every candidate
model, the minutes are generated with the production system prompt and schema, then checked by the production
validator (citations exist, no invented links or phone numbers). Writes results.json and a side-by-side
report.md.

Scoring: a gold file (JSON, kept by whoever grades the meeting's content) applies to transcripts whose upload
file name contains its "match": every "must" check needs each of its groups (any alternative) somewhere in the
minutes (in one sentence or item when "same_fragment"), a "must_not" check fails on any of its words, a
"fragments" check fails when one sentence or item holds all its groups, any of its "with" words (when given)
and none of its "unless" words ("clause": judge each clause between commas rather than each sentence).
Whitespace is ignored; misheard words listed in name_fixes do not count.

Candidates: the configured production LLM (deps.llm_client, whatever LLM_API selects) and claude-haiku-5-5
through the official Anthropic SDK with structured outputs at effort low and medium. Claude credentials:
EVAL_ANTHROPIC_API_KEY (direct to api.anthropic.com) if set, otherwise the ANTHROPIC_* environment.
Only the "configured" rows are production-equivalent: the claude-haiku-5-5 rows use messages.parse, whose own
pydantic check runs before the production validator (and its citation trimming).
"""

import argparse
import asyncio
import json
import os
import re
import sys
import time
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import anthropic
from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.config import get_settings  # noqa: E402
from app.db import get_engine  # noqa: E402
from app.deps import llm_client  # noqa: E402
from app.postprocess import minutes as mm  # noqa: E402

HAIKU = "claude-haiku-5-5"
HAIKU_USD_PER_MTOK = (0.10, 0.50)  # input, output (prompts up to 100K tokens)
DEEPSEEK_USD_PER_MTOK = (0.30, 1.20)  # deepseek-flash, peak hours (the higher rate)


@dataclass
class Run:
    transcript: str
    model: str
    ok: bool
    seconds: float
    input_tokens: int | None = None
    output_tokens: int | None = None
    usd: float | None = None
    error: str | None = None
    stop_reason: str | None = None
    minutes: dict[str, Any] | None = None
    counts: dict[str, int] = field(default_factory=dict)
    score: dict[str, Any] | None = None
    raw_items: int | None = None  # items in the reply before validation (dropped = raw_items - kept)


@dataclass
class Transcript:
    segments: list[dict[str, Any]]
    title: str | None  # file name of the upload, the reference spelling the minutes prompt gets


async def load_transcripts(tasks: list[str]) -> dict[str, Transcript]:
    """The given tasks' transcripts (newest version), or else the newest transcript per ASR provider."""
    columns = (
        "SELECT DISTINCT ON ({key}) j.provider, tr.task_id, tr.body, ti.file_name FROM transcripts tr "
        "JOIN processing_jobs j ON j.id = tr.processing_job_id "
        "LEFT JOIN task_inputs ti ON ti.task_id = tr.task_id "
    )
    if tasks:
        sql = columns.format(key="tr.task_id") + (
            "WHERE tr.task_id::text = ANY(:tasks) ORDER BY tr.task_id, tr.version DESC"
        )
    else:
        sql = columns.format(key="j.provider") + "ORDER BY j.provider, tr.created_at DESC"
    async with get_engine().connect() as conn:
        rows = (await conn.execute(text(sql), {"tasks": tasks})).all()

    def name(r) -> str:
        return f"{r.provider}:{str(r.task_id)[:8]}" if tasks else r.provider

    return {name(r): Transcript(r.body["segments"], r.file_name) for r in rows}


def load_files(specs: list[str]) -> dict[str, Transcript]:
    """NAME=PATH pairs: transcripts prepared outside the database (e.g. from another ASR service)."""
    out = {}
    for spec in specs:
        name, _, path = spec.partition("=")
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        out[name] = Transcript(data["segments"], data.get("title"))
    return out


def user_prompt(t: Transcript) -> str:
    return mm.build_user_prompt(t.segments, title=t.title)


def _summarize(run: Run, minutes: mm.Minutes) -> None:
    run.minutes = minutes.model_dump()
    run.counts = {k: len(v) for k, v in run.minutes.items() if isinstance(v, list)}
    run.counts["summary_chars"] = len(minutes.summary)
    run.counts["owners_known"] = sum(a.owner != "未知" for a in minutes.action_items)
    # plans and expectations the summary mentions, by their time frame (the skill asks for one or two)
    run.counts["plans_in_summary"] = sum(
        bool(o.timeframe) and o.timeframe in minutes.summary for o in minutes.outlook
    )


def _strings(node) -> Iterator[str]:
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for key, value in node.items():
            if key != "evidence_segments":
                yield from _strings(value)
    elif isinstance(node, list):
        for value in node:
            yield from _strings(value)


def _fragments(minutes: dict[str, Any]) -> list[str]:
    """What a reader sees, one piece per item and per sentence of the summary."""
    out = [s for s in re.split(r"[。；;\n]", minutes.get("summary", "")) if s.strip()]
    for key, items in minutes.items():
        if key == "summary" or not isinstance(items, list):
            continue
        for item in items:
            if key == "name_fixes":
                out.append(item.get("written", ""))  # the misheard spelling is quoted on purpose
            else:
                out.append(" ".join(_strings(item)))
    return out


_ANNOTATION = re.compile(r"[（(][^（）()]*(?:逐字稿|写法)[^（）()]*[）)]")


def score(minutes: dict[str, Any], gold: dict[str, Any]) -> dict[str, Any]:
    fragments = [re.sub(r"\s+", "", f) for f in _fragments(minutes)]  # "7 月底" is "7月底"
    whole = "\n".join(fragments)

    def has(text_: str, words: list[str]) -> bool:
        return any(w in text_ for w in words)

    def found(c: dict[str, Any]) -> bool:  # "same_fragment": every group within one sentence or item
        pieces = fragments if c.get("same_fragment") else [whole]
        return any(all(has(f, alts) for alts in c["all"]) for f in pieces)

    failed = [c["id"] for c in gold["must"] if not found(c)]
    # "晨川科技（逐字稿中写作陈川科技）" quotes the misheard name on purpose: not the minutes' own word
    plain = _ANNOTATION.sub("", whole)
    failed += [c["id"] for c in gold.get("must_not", []) if has(plain, c["any"])]
    clauses = [c for f in fragments for c in re.split(r"[，,（）()]", f) if c.strip()]
    for c in gold.get("fragments", []):
        pieces = clauses if c.get("clause") else fragments
        if any(
            all(has(f, alts) for alts in c["all"])
            and ("with" not in c or has(f, c["with"]))
            and not has(f, c.get("unless", []))
            for f in pieces
        ):
            failed.append(c["id"])
    total = len(gold["must"]) + len(gold.get("must_not", [])) + len(gold.get("fragments", []))
    return {"passed": total - len(failed), "total": total, "failed": failed}


def load_gold(paths: list[Path]) -> list[dict[str, Any]]:
    return [json.loads(p.read_text(encoding="utf-8")) for p in paths]


def gold_for(t: Transcript, golds: list[dict[str, Any]]) -> dict[str, Any] | None:
    return next((g for g in golds if t.title and g["match"] in t.title), None)


async def run_configured(name: str, t: Transcript, prompt: str) -> Run:
    """Exactly the production client (deps.llm_client: LLM_API, model, effort, credentials from .env)."""
    s = get_settings()
    llm = llm_client()
    start = time.monotonic()
    run = Run(name, f"{s.llm_model} (configured)", ok=False, seconds=0)
    try:
        reply = await llm.complete_json(system=mm.SYSTEM_PROMPT, user=prompt, schema=mm.MINUTES_JSON_SCHEMA)
        run.input_tokens, run.output_tokens = reply.input_tokens, reply.output_tokens
        run.raw_items = sum(
            len(v) for k, v in reply.content.items() if k in mm.CITED_SECTIONS and isinstance(v, list)
        )
        minutes = mm.validate(reply.content, t.segments, title=t.title)
        _summarize(run, minutes)
        run.ok = True
        if thin := mm.too_thin(minutes, t.segments):  # production asks once more, keeps the fuller
            run.error = f"过薄，生产会重试：{thin}"
    except Exception as exc:  # recorded, the comparison goes on
        run.error = f"{type(exc).__name__}: {str(exc)[:200]}"
        if exc.__cause__ is not None:  # "schema: 1 errors" names no field: keep the validation detail
            run.error += f" <- {' '.join(str(exc.__cause__).split())[:300]}"
    finally:
        run.seconds = round(time.monotonic() - start, 1)
        await llm.aclose()
    price = HAIKU_USD_PER_MTOK if s.llm_api == "anthropic" else DEEPSEEK_USD_PER_MTOK
    run.usd = _usd(run, price)
    return run


def _usd(run: Run, price: tuple[float, float]) -> float | None:
    if run.input_tokens is None or run.output_tokens is None:
        return None
    return (run.input_tokens * price[0] + run.output_tokens * price[1]) / 1e6


def _claude_client() -> anthropic.AsyncAnthropic:
    key = os.environ.get("EVAL_ANTHROPIC_API_KEY")
    if key:  # direct to Anthropic, bypassing any ANTHROPIC_BASE_URL / ANTHROPIC_AUTH_TOKEN in the environment
        return anthropic.AsyncAnthropic(api_key=key, auth_token=None, base_url="https://api.anthropic.com")
    return anthropic.AsyncAnthropic()


async def run_haiku(
    client: anthropic.AsyncAnthropic, name: str, t: Transcript, prompt: str, effort: str
) -> Run:
    start = time.monotonic()
    run = Run(name, f"{HAIKU} (effort {effort})", ok=False, seconds=0)
    try:
        response = await client.messages.parse(
            model=HAIKU,
            max_tokens=16000,
            system=mm.SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
            output_format=mm.Minutes,
            output_config={"effort": effort},
        )
        run.stop_reason = response.stop_reason
        usage = response.usage  # a caching gateway moves most of the prompt into the cache counters
        run.input_tokens = (
            usage.input_tokens
            + (usage.cache_creation_input_tokens or 0)
            + (usage.cache_read_input_tokens or 0)
        )
        run.output_tokens = usage.output_tokens
        if response.stop_reason == "refusal":
            category = response.stop_details.category if response.stop_details else None
            raise RuntimeError(f"refusal ({category})")
        if response.stop_reason == "max_tokens":
            raise RuntimeError("truncated at max_tokens")
        parsed = response.parsed_output
        # The same post-checks as production: citations must exist, invented links/phones are removed.
        _summarize(run, mm.validate(parsed.model_dump(), t.segments, title=t.title))
        run.ok = True
    except anthropic.APIStatusError as exc:
        run.error = f"HTTP {exc.status_code} {type(exc).__name__}"
    except anthropic.APIConnectionError as exc:
        run.error = f"connection: {type(exc).__name__}"
    except Exception as exc:
        run.error = f"{type(exc).__name__}: {str(exc)[:200]}"
    finally:
        run.seconds = round(time.monotonic() - start, 1)
    run.usd = _usd(run, HAIKU_USD_PER_MTOK)
    return run


def _cite(seg_ids: list[int], segments) -> str:
    by_i = {s["i"]: s for s in segments}
    parts = []
    for i in seg_ids[:3]:
        seg = by_i.get(i)
        if seg:
            parts.append(f"[{i}] {mm.segment_label(seg)}{seg['text'][:60]}")
    return "<br>".join(parts)


_HEADER = (
    "逐字稿",
    "模型",
    "成功",
    "耗时 s",
    "输入 tok",
    "输出 tok",
    "约 $",
    "标准答案",
    "各栏条数",
    "摘要字数",
    "错误",
)
_LABELS = {
    "key_points": "主要观点",
    "key_facts": "重要数据与事实",
    "outlook": "计划与预期",
    "decisions": "决定",
    "action_items": "待办",
    "risks": "风险",
    "name_fixes": "名称更正",
}


def _row(cells) -> str:
    return "| " + " | ".join(str(c) for c in cells) + " |"


def _summary_row(r: Run) -> str:
    c = r.counts
    sections = " ".join(f"{label}{c[k]}" for k, label in _LABELS.items() if k in c)
    return _row(
        (
            r.transcript,
            r.model,
            "✅" if r.ok else "❌",
            r.seconds,
            r.input_tokens or "-",
            r.output_tokens or "-",
            f"{r.usd:.4f}" if r.usd is not None else "-",
            f"{r.score['passed']}/{r.score['total']}" if r.score else "-",
            sections or "-",
            c.get("summary_chars", "-"),
            r.error or "",
        )
    )


def _describe(key: str, item: dict[str, Any]) -> tuple[str, str]:
    """(content, extra column) of one item, whatever section it is in."""
    if key == "name_fixes":
        return f"{item['heard']} → {item['written']}", ""
    content = item.get("text") or item.get("task") or ""
    if item.get("details"):
        content += "".join(f"<br>· {d}" for d in item["details"])
    if key == "action_items":
        return content, f"{item.get('owner', '')} / {item.get('due') or '-'}"
    if key == "outlook":
        return content, f"{item.get('kind', '')} / {item.get('timeframe') or '-'}"
    return content, ""


def _minutes_section(r: Run, segments) -> list[str]:
    m = r.minutes
    lines = ["", f"### {r.model}", ""]
    if r.score and r.score["failed"]:
        lines += [f"**未通过**：{'、'.join(r.score['failed'])}", ""]
    said = m.get("meeting_date") or {}
    lines += [f"**会议类型**：{m.get('meeting_type', '-')}　**会上说的日期**：{said.get('date', '-')}", ""]
    lines += [f"**摘要**：{m['summary']}", ""]
    for key, title in _LABELS.items():
        if key not in m:
            continue
        lines += [f"**{title}**", "", _row(("内容", "补充", "引用的原话")), "|---|---|---|"]
        for item in m[key]:
            content, extra = _describe(key, item)
            lines.append(_row((content, extra, _cite(item.get("evidence_segments", []), segments))))
        lines.append("")
    return lines


def report(runs: list[Run], transcripts: dict[str, Transcript]) -> str:
    lines = ["# 纪要模型对比", "", "## 总表", "", _row(_HEADER), "|---" * len(_HEADER) + "|"]
    lines += [_summary_row(r) for r in runs]
    for name, t in transcripts.items():
        lines += ["", f"## 逐字稿：{name}（{len(t.segments)} 段，标题 {t.title or '-'}）"]
        for r in runs:
            if r.transcript == name and r.ok:
                lines += _minutes_section(r, t.segments)
    return "\n".join(lines) + "\n"


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=1, help="repetitions per model and transcript")
    parser.add_argument("--out", type=Path, default=Path(__file__).resolve().parents[1] / "study" / "eval")
    parser.add_argument("--only", choices=("configured", "haiku"), help="run one family only")
    parser.add_argument(
        "--task", action="append", default=[], help="task id (repeatable) instead of the newest"
    )
    parser.add_argument(
        "--transcript",
        action="append",
        default=[],
        metavar="NAME=PATH",
        help='a JSON file {"title", "segments"} instead of the database (repeatable)',
    )
    gold_dir = Path(__file__).resolve().parents[1] / "study" / "eval-gold"
    parser.add_argument("--gold", type=Path, nargs="*", default=sorted(gold_dir.glob("*.json")))
    parser.add_argument("--skill", type=Path, help="another SKILL.md in place of the production one (A/B)")
    args = parser.parse_args()
    golds = load_gold(args.gold)
    if args.skill:  # same fixed rules, schema and model: only the writing guide differs
        current = mm._skill_body(mm.SKILL_PATH)
        assert current in mm.SYSTEM_PROMPT
        mm.SYSTEM_PROMPT = mm.SYSTEM_PROMPT.replace(current, mm._skill_body(args.skill))
    print(f"写作规范 {args.skill or mm.SKILL_PATH}")

    transcripts = load_files(args.transcript) if args.transcript else await load_transcripts(args.task)
    if not transcripts:
        print("数据库里没有逐字稿", file=sys.stderr)
        return 1
    client = _claude_client() if args.only != "configured" else None
    runs: list[Run] = []
    print(f"提示词版本 {mm.PROMPT_VERSION}")
    for name, t in transcripts.items():
        prompt, gold = user_prompt(t), gold_for(t, golds)
        if not t.title:
            print(f"{name}: 没有文件名（任务结束后已清理），提示词里没有标题，也不打分", file=sys.stderr)
        for _ in range(args.runs):
            jobs = []
            if args.only != "haiku":
                jobs.append(run_configured(name, t, prompt))
            if client is not None:
                jobs += [run_haiku(client, name, t, prompt, e) for e in ("low", "medium")]
            for run in await asyncio.gather(*jobs):
                if run.ok and gold is not None:
                    run.score = score(run.minutes, gold)
                runs.append(run)
                status = "ok " if run.ok else "ERR"
                points = f"{run.score['passed']}/{run.score['total']}" if run.score else ""
                missed = (
                    " 未通过：" + "、".join(run.score["failed"]) if run.score and run.score["failed"] else ""
                )
                kind = (run.minutes or {}).get("meeting_type", "")
                shape = f" 观点{run.counts.get('key_points', '-')} {kind}" if run.ok else ""
                head = f"{name:12} {run.model:34} {status} {run.seconds:6}s"
                print(f"{head} {points}{shape}{missed} {run.error or ''}")
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "results.json").write_text(
        json.dumps([asdict(r) for r in runs], ensure_ascii=False, indent=1), encoding="utf-8"
    )
    (args.out / "report.md").write_text(report(runs, transcripts), encoding="utf-8")
    print(f"报告：{args.out / 'report.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
