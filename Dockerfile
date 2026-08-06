# Use lightweight official Python image
FROM python:3.11-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Build dependencies only exist in this stage — never shipped in the runtime image.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt /app/backend/
RUN pip install --no-cache-dir --prefix=/install -r /app/backend/requirements.txt


FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PORT=8000
ENV PYTHONPATH=/app
# S25: SQLite lived inside the source tree, forcing the whole /app/backend
# directory to stay writable. Point it at a dedicated, owned data directory.
ENV SQLITE_DATA_DIR=/data

WORKDIR /app

COPY --from=builder /install /usr/local

# Copy backend source code
COPY backend/ /app/backend/

# S25: no USER directive previously — the process ran as root by default.
RUN groupadd --system app && useradd --system --gid app --home /app app \
    && mkdir -p /data \
    && chown -R app:app /app /data
USER app

VOLUME ["/data"]

# Expose service port
EXPOSE 8000

# Start Uvicorn pointing to backend.main:app
CMD uvicorn backend.main:app --host 0.0.0.0 --port $PORT
