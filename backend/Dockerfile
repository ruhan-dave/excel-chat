FROM python:3.12-alpine

WORKDIR /app

RUN apk update && \
    apk upgrade --no-cache && \
    rm -rf /var/cache/apk/*

COPY requirements.txt .

RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

RUN addgroup -S appgroup && adduser -S -G appgroup -h /home/appuser -s /bin/sh appuser
USER appuser

COPY --chown=appuser:appuser . .

WORKDIR /app/src

ENV OPENROUTER_BASE_URL=https://openrouter.ai/api/v1

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]
