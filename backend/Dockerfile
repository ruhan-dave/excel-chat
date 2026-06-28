FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && \
    apt-get upgrade -y && \
    apt-get install -y --no-install-recommends \
    build-essential \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

RUN groupadd appgroup && useradd -G appgroup -m -s /bin/bash appuser
RUN mkdir -p /app/data && chown appuser:appgroup /app/data
USER appuser

COPY --chown=appuser:appuser . .

WORKDIR /app/src

ENV OPENROUTER_BASE_URL=https://openrouter.ai/api/v1

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]
