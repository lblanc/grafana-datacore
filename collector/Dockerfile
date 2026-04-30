# syntax=docker/dockerfile:1.7

FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install dependencies first to leverage build cache
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY collector.py ./
COPY collector.ini ./
COPY lib ./lib

# Run as a non-root user
RUN useradd --no-create-home --shell /sbin/nologin --uid 10001 collector \
    && chown -R collector:collector /app \
    && mkdir -p /status /app/dumps \
    && chown collector:collector /status /app/dumps
USER collector

ENV DATACORE_CONFIG=/app/collector.ini

ENTRYPOINT ["python", "-u", "collector.py"]