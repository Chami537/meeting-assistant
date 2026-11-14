"""Server-rendered pages for WeChat's in-app browser (result page, upload page): one stylesheet, strict
security headers. Pages have no scripts unless they pass one, which is then allowed by its hash only."""

import base64
import hashlib
import html
from urllib.parse import urlsplit

from fastapi import Request
from fastapi.responses import HTMLResponse

from ..config import get_settings

_BASE_CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
    "frame-ancestors 'none'; base-uri 'none'"
)
SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "X-Robots-Tag": "noindex, nofollow",
    "Content-Security-Policy": _BASE_CSP,
}
_STYLE = """
body{font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;max-width:720px;margin:0 auto;
padding:16px;line-height:1.6;color:#222}
h1{font-size:20px} h2{font-size:17px;margin-top:24px} li{margin:4px 0}
.muted{color:#888;font-size:13px} .note{background:#fff7e6;padding:8px 12px;border-radius:6px}
button{font-size:16px;padding:10px 20px;border:0;border-radius:6px;background:#07c160;color:#fff}
button.danger{background:#e64340} button:disabled{background:#aaa} details{margin-top:24px}
.seg{margin:6px 0;white-space:pre-wrap} li ul{margin:2px 0;padding-left:20px}
.tag{display:inline-block;font-size:12px;line-height:18px;padding:0 6px;margin-right:6px;border-radius:4px;
background:#e8f3ff;color:#1a5fb4}
.wm{position:fixed;inset:0;pointer-events:none;opacity:.07;overflow:hidden;transform:rotate(-20deg);
font-size:14px;line-height:48px;word-spacing:40px} .wm span{margin-right:60px}
"""


def script_hash(script: str) -> str:
    return "sha256-" + base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()


def page(
    title: str,
    body: str,
    *,
    status: int = 200,
    watermark: str | None = None,
    script: str | None = None,
) -> HTMLResponse:
    mark = ""
    if watermark:
        tile = f"<span>{html.escape(watermark)}</span>" * 60
        mark = f'<div class="wm" aria-hidden="true">{tile}</div>'
    headers = dict(SECURITY_HEADERS)
    tail = ""
    if script is not None:
        headers["Content-Security-Policy"] = (
            f"{_BASE_CSP}; script-src '{script_hash(script)}'; connect-src 'self'"
        )
        tail = f"<script>{script}</script>"
    doc = (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{html.escape(title)}</title><style>{_STYLE}</style></head>"
        f"<body>{mark}{body}{tail}</body></html>"
    )
    return HTMLResponse(doc, status_code=status, headers=headers)


def same_origin(request: Request) -> bool:
    """SameSite cookies / one-time tokens are the main guard; Origin is checked as well when sent."""
    origin = request.headers.get("origin")
    if origin is None:
        return True
    base = get_settings().public_base_url
    expected = urlsplit(base).netloc if base else request.headers.get("host")
    return urlsplit(origin).netloc == expected


def client_info(request: Request) -> tuple[str | None, str | None]:
    return (request.client.host if request.client else None, request.headers.get("user-agent"))
