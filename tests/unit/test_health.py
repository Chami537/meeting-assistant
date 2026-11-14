import httpx
from app.main import app


async def test_healthz():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_access_log_hides_one_time_tokens():
    import logging

    from app.main import RedactTokens

    token = "A" * 43
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        "",
        0,
        '%s - "%s %s HTTP/%s" %d',
        ("1.2.3.4", "GET", f"/u/{token}", "1.1", 200),
        None,
    )
    RedactTokens().filter(record)
    assert token not in record.getMessage() and "/u/<token>" in record.getMessage()
