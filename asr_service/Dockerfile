# Our transcription service's job API (asr_service/). Built on the Qwen3-ASR image, which already has CUDA,
# torch, FastAPI, uvicorn, httpx and ffmpeg. funasr brings the pause cutting and speaker models (and Fun-ASR,
# the other engine). Built by docker-compose.yml; by hand, from the repository root:
#     docker build -f asr_service/Dockerfile -t asr-service:1 .
FROM qwenllm/qwen3-asr:latest
RUN pip3 install --no-cache-dir "funasr==1.4.16" "modelscope==1.40.2" "python-multipart>=0.0.9"
WORKDIR /srv
COPY asr_service /srv/asr_service
# The glossary's short list goes into every job's context (asr_service/glossary.py); one copy, the backend's
COPY backend/app/postprocess/glossary.csv /srv/glossary.csv
ENV ASR_MODELS=/models/modelscope/models ASR_GLOSSARY=/srv/glossary.csv PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["uvicorn", "asr_service.api:app", "--host", "0.0.0.0", "--port", "8000"]
