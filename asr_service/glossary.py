"""The glossary's short list for the recognizer: the terms marked 识别上下文=是 among the reviewed rows
(状态 通过), in the way they are said (识别写法: IR for 投资者关系). The service adds them to every job's
context after the title, so every caller gets them: the backend and RAGFlow's SYSTRAN provider only send the
title.

There is one glossary, backend/app/postprocess/glossary.csv; the image gets a copy (ASR_GLOSSARY).
"""

import csv
from pathlib import Path

DEFAULT = Path(__file__).resolve().parents[1] / "backend" / "app" / "postprocess" / "glossary.csv"


def recognizer_terms(path: Path) -> list[str]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        return [
            r["识别写法"].strip() or r["术语"].strip()
            for r in csv.DictReader(f)
            if r["状态"].strip() == "通过" and r["识别上下文"].strip() == "是" and r["术语"].strip()
        ]
