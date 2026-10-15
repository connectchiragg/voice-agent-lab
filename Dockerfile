# Voice agent server: FastAPI; speech runs on Deepgram, the LLM on Groq. No model weights.
# Builds on linux/amd64 and linux/arm64; every pinned wheel exists for both.
# Secrets are never baked in: pass DEEPGRAM_API_KEY, GROQ_API_KEY etc. at run time
# (see docs/deploy.md).
ARG PYTHON_IMAGE=python:3.14-slim-trixie

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
WORKDIR /app
# The app runs from source: app/main.py serves ../static relative to itself.
COPY app ./app
COPY static ./static
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=7860
USER app
EXPOSE 7860
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/healthz', timeout=4)"
CMD ["sh", "-c", "exec uvicorn app.main:app --host \"$HOST\" --port \"$PORT\" --proxy-headers --forwarded-allow-ips '*'"]
