# Voice agent server: FastAPI; speech runs on Deepgram, the LLM on Groq. No model weights.
# Builds on linux/amd64 and linux/arm64; every pinned wheel exists for both.
# Secrets are never baked in: pass DEEPGRAM_API_KEY, GROQ_API_KEY etc. at run time
# (see docs/deploy.md).
ARG PYTHON_IMAGE=python:3.14-slim-trixie
ARG WORKER_PYTHON_IMAGE=python:3.13-slim-trixie

# Python dependencies, binary wheels only (no compilers in the image). One exception: http-ece
# (pywebpush's encryption, phone push) is published only as a pure-Python sdist; pip builds it
# with setuptools, no compiler.
# BuildKit only (docker buildx; deploy/vm-buildx.sh on the VM): pip's download cache lives in
# a cache mount, outside the image, so a lock change re-downloads only the changed wheels.
FROM ${PYTHON_IMAGE} AS deps
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
COPY requirements.lock /tmp/requirements.lock
RUN --mount=type=cache,target=/root/.cache/pip,id=pip-app \
    python -m venv /opt/venv \
 && /opt/venv/bin/pip install --only-binary=:all: --no-binary=http-ece -r /tmp/requirements.lock

# The VALR research worker (`--target worker`): Python 3.13, because crewai requires
# <3.14, with its own lock (crewai, anthropic, temporalio, psycopg). Only this image has
# an LLM SDK for research; the app image below is unchanged by it.
FROM ${WORKER_PYTHON_IMAGE} AS worker-deps
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
COPY requirements-worker.lock /tmp/requirements-worker.lock
RUN --mount=type=cache,target=/root/.cache/pip,id=pip-worker \
    python -m venv /opt/venv \
 && /opt/venv/bin/pip install --only-binary=:all: --no-binary=http-ece -r /tmp/requirements-worker.lock

FROM ${WORKER_PYTHON_IMAGE} AS worker
RUN useradd --create-home --uid 1000 app
COPY --from=worker-deps /opt/venv /opt/venv
WORKDIR /app
COPY app ./app
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    CREWAI_DISABLE_TELEMETRY=true \
    CREWAI_DISABLE_TRACKING=true \
    CREWAI_TRACING_ENABLED=false \
    OTEL_SDK_DISABLED=true \
    CREWAI_STORAGE_DIR=/tmp/crewai
USER app
CMD ["python", "-m", "app.research_worker"]

# The app (the default target: keep it last).
FROM ${PYTHON_IMAGE}
# UID 1000 also matches what container hosts such as Hugging Face Spaces expect.
RUN useradd --create-home --uid 1000 app
COPY --from=deps /opt/venv /opt/venv
WORKDIR /app
# The app runs from source: app/main.py serves ../static relative to itself.
COPY app ./app
COPY static ./static
# Settings › Phone call recordings › "iPhone shortcut steps" renders this doc (app/phone_help.py).
COPY docs/phone-recordings.md ./docs/phone-recordings.md
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=7860
USER app
EXPOSE 7860
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/healthz', timeout=4)"
CMD ["sh", "-c", "exec uvicorn app.main:app --host \"$HOST\" --port \"$PORT\" --proxy-headers --forwarded-allow-ips '*'"]
