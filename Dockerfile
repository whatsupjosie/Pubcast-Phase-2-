FROM python:3.12-slim

LABEL maintainer="Rear View Foresight LLC"
LABEL description="PubCast AI v5.0 — Collaborative AI-Infused Virtual Production"

WORKDIR /app

# System dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application
COPY . .

# Create data directories
RUN mkdir -p data/bots data/global data/logs data/users data/jeremy \
    data/ethereal data/vault data/governance data/recordings \
    data/exports data/imports assets

EXPOSE 8000

ENV PUBCAST_HOST=0.0.0.0
ENV PUBCAST_PORT=8000
ENV PUBCAST_DATA_DIR=data
ENV PUBCAST_STATIC_DIR=static
ENV PUBCAST_ASSETS_DIR=assets

CMD ["python", "main.py"]
