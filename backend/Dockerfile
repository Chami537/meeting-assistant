# The minutes/summary API (app/api/minutes_api.py) as a standalone image, meant to sit on a private network
# next to a transcription service. Build from the repository root:
#     docker build -f backend/Dockerfile -t minutes-api:1 .
# Only the API process runs: no database, no runner (the WeChat and upload path needs object storage and a
# public address). LLM settings come from the environment or minutes.env (see backend/deploy/docker-compose.yml).
FROM python:3.12-slim
WORKDIR /srv
COPY pyproject.toml ./
COPY backend backend
COPY shared shared
RUN pip install --no-cache-dir .
WORKDIR /srv/backend
ENV PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
