import logging
import re
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .api import (
    files,
    health,
    meeting_parse_api,
    minutes_api,
    results,
    summary_api,
    upload_page,
    wecom,
    workers,
)
from .config import get_settings

# Under uvicorn the root logger has no handler, so the app's INFO lines (the minutes API's request ids, ...)
# were dropped; they carry ids and counts, never a transcript. uvicorn's own loggers do not propagate here.
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
# httpx logs every request URL at INFO; WeCom URLs carry corpsecret / access_token (design doc §10).
for _name in ("httpx", "httpcore"):
    logging.getLogger(_name).setLevel(logging.WARNING)

_TOKEN_PATH = re.compile(r"/(r|u)/[A-Za-z0-9_-]{43}")


class RedactTokens(logging.Filter):
    """One-time result and upload links carry their token in the path; access logs must not keep it."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(
                _TOKEN_PATH.sub(r"/\1/<token>", a) if isinstance(a, str) else a for a in record.args
            )
        return True


logging.getLogger("uvicorn.access").addFilter(RedactTokens())


@asynccontextmanager
async def lifespan(app: FastAPI):
    """The AI meeting page's job runner, when MEETING_PARSE_DB is set (the minutes API's deployment)."""
    app.state.meeting_parser = meeting_parse_api.start(get_settings())
    try:
        yield
    finally:
        if app.state.meeting_parser is not None:
            await app.state.meeting_parser.close()


app = FastAPI(title="AI Meeting Assistant", lifespan=lifespan)
app.include_router(health.router)
app.include_router(workers.router)
app.include_router(wecom.router)
app.include_router(results.router)
app.include_router(upload_page.router)
app.include_router(minutes_api.router)
app.include_router(summary_api.router)
app.include_router(meeting_parse_api.router)
app.include_router(files.router)  # only answers with STORAGE_BACKEND=local
