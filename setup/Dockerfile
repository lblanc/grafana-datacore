# syntax=docker/dockerfile:1.7
# Build context for this image is the repo root (see docker-compose.yml),
# so the collector library can be reused for the connectivity tests.

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv

COPY setup/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY setup/app ./app
# Reuse the real DataCore client for the "Test connection" button so the
# test path is identical to what the collector does in production.
COPY collector/lib/datacore_client.py ./app/datacore_client.py

EXPOSE 8080

# uvicorn binds to all interfaces inside the container; expose only what
# you need on the host side via docker-compose.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers"]
