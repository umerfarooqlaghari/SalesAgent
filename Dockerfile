# Use lightweight official Python image
FROM python:3.11-slim

# Prevent writing bytecode and buffer outputs
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PORT=8000

# Set Python Path so the backend package can be imported
ENV PYTHONPATH=/app

WORKDIR /app

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Copy backend requirements and install dependencies
COPY backend/requirements.txt /app/backend/
RUN pip install --no-cache-dir -r /app/backend/requirements.txt

# Copy backend source code
COPY backend/ /app/backend/

# Expose service port
EXPOSE 8000

# Start Uvicorn pointing to backend.main:app
CMD uvicorn backend.main:app --host 0.0.0.0 --port $PORT
