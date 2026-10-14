# Voice agent server: FastAPI + local ONNX models (~1 GB, baked in at build time).
# Builds on linux/amd64 and linux/arm64; every pinned wheel exists for both.
# Secrets are never baked in: pass GROQ_API_KEY etc. at run time (see docs/deploy.md).
ARG PYTHON_IMAGE=python:3.14-slim-trixie

# Models: same script as local development, so versions cannot drift.
FROM ${PYTHON_IMAGE} AS models
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl bzip2 \
 && rm -rf /var/lib/apt/lists/*
COPY scripts/download-models.sh /tmp/download-models.sh
RUN MODELS_DIR=/models sh /tmp/download-models.sh \
 && test -s /models/sherpa-onnx-nemotron-speech-streaming-en-0.6b-160ms-int8-2026-04-25/encoder.int8.onnx

# Python dependencies, binary wheels only (no compilers in the image).
FROM ${PYTHON_IMAGE} AS deps
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
COPY requirements.lock /tmp/requirements.lock
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --only-binary=:all: -r /tmp/requirements.lock

FROM ${PYTHON_IMAGE}
# UID 1000 also matches what container hosts such as Hugging Face Spaces expect.
RUN useradd --create-home --uid 1000 app
COPY --from=deps /opt/venv /opt/venv
COPY --from=models /models /models
WORKDIR /app
# The app runs from source: app/main.py serves ../static relative to itself.
COPY app ./app
COPY static ./static
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    MODELS_DIR=/models \
    HOST=0.0.0.0 \
    PORT=7860 \
    STT_THREADS=1 \
    TTS_THREADS=1
USER app
EXPOSE 7860
# Model loading and warm-up take a while on 2 vCPU before /healthz answers.
HEALTHCHECK --interval=30s --timeout=5s --start-period=180s --retries=3 \
  CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/healthz', timeout=4)"
CMD ["sh", "-c", "exec uvicorn app.main:app --host \"$HOST\" --port \"$PORT\" --proxy-headers --forwarded-allow-ips '*'"]
