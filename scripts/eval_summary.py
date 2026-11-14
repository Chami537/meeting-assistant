"""Summaries of exported articles, for a person to grade before the summary API goes live.

Input: JSON lines of {id, kb, name, tokens, chunks: [{text, page}]}, the chunks in page order as RAGFlow's
chunk_list returns them (exported read-only from the RAGFlow container). Every article is summarized with the
production prompt and the configured LLM (deps.llm_client). With --force-grouped, the N longest are also
summarized the long-article way (points per group, then the summary) with the thresholds lowered, so the two
can be compared on the same article.

Writes results.json and review.md (one row per summary, with blank columns for the three grades: figures and
names right, core conclusion kept, length fine) to --out. This sends the articles to the configured LLM.

    python scripts/eval_summary.py CHUNKS.jsonl --out ./summary-eval-out --force-grouped 3
"""

import argparse
import asyncio
import json
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.deps import llm_client  # noqa: E402
from app.postprocess import article_summary as asum  # noqa: E402
from app.postprocess import minutes as mm  # noqa: E402
from app.postprocess.summary import describe  # noqa: E402

FORCED_WHOLE_CHARS = 20_000
FORCED_GROUP_CHARS = 15_000


async def one(llm, article, label):
    chunks = [c["text"] for c in article["chunks"]]
    started = time.monotonic()
    deadline = asyncio.get_running_loop().time() + 1200
    try:
        written = await asum.write_summary(
            llm, chunks, title=mm.meeting_title(article["name"]), deadline=deadline, label=label
        )
    except Exception as exc:
        return {"error": describe(exc) if not isinstance(exc, asum.SummaryRejected) else str(exc)}
    return {
        "summary": written.summary,
        "chars": len(written.summary),
        "mode": written.mode,
        "groups": written.groups,
        "truncated": written.truncated,
        "used_chars": written.used_chars,
        "seconds": round(time.monotonic() - started, 1),
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("articles", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--force-grouped", type=int, default=0, metavar="N")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")  # the asking-again and dropping lines
    articles = [json.loads(line) for line in args.articles.read_text(encoding="utf-8").splitlines() if line]
    llm = llm_client()
    results = []
    for a in articles:
        results.append(
            {"id": a["id"], "kb": a["kb"], "name": a["name"], "run": "as-is", **await one(llm, a, a["id"])}
        )
        print(a["name"][:40], results[-1].get("mode") or results[-1].get("error"), flush=True)
    longest = sorted(articles, key=lambda a: -sum(len(c["text"]) for c in a["chunks"]))[: args.force_grouped]
    if longest:
        asum.MAX_WHOLE_CHARS, asum.GROUP_CHARS = FORCED_WHOLE_CHARS, FORCED_GROUP_CHARS
        for a in longest:
            results.append(
                {
                    "id": a["id"],
                    "kb": a["kb"],
                    "name": a["name"],
                    "run": "forced-grouped",
                    **await one(llm, a, a["id"]),
                }
            )
            print(a["name"][:40], "forced", results[-1].get("groups") or results[-1].get("error"), flush=True)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "results.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    rows = [
        "| 文档 | 方式 | 字数 | 摘要 | 数字名称对 | 核心结论在 | 长度合适 |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in sorted(results, key=lambda r: (r["name"], r["run"])):
        text = r.get("summary") or f"**失败**：{r.get('error')}"
        how = (
            f"{r['run']} {r.get('mode', '')}{'·' + str(r['groups']) + '组' if r.get('groups', 1) > 1 else ''}"
        )
        rows.append(f"| {r['name'][:30]} | {how} | {r.get('chars', '')} | {text} |  |  |  |")
    (args.out / "review.md").write_text("\n".join(rows) + "\n", encoding="utf-8")
    print("wrote", args.out / "review.md")


if __name__ == "__main__":
    asyncio.run(main())
