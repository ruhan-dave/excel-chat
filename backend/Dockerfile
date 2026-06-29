# Stage 1: Build the React frontend
FROM node:22-bookworm-slim AS frontend-build

WORKDIR /frontend

RUN apt-get update && \
    apt-get dist-upgrade -y && \
    rm -rf /var/lib/apt/lists/*

COPY frontend/ragsheets/package.json frontend/ragsheets/package-lock.json ./
RUN npm install

COPY frontend/ragsheets/ ./
RUN npm run build

# Stage 2: Python backend with frontend static files
FROM python:3.12.12-slim-bookworm

WORKDIR /app

RUN apt-get update && \
    apt-get dist-upgrade -y && \
    apt-get install -y --no-install-recommends \
    build-essential \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt .

RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Pre-download the sentence-transformers embedding model so the first query
# doesn't pay the cold-load cost (Optimization 4: ~80MB MiniLM model).
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')"

RUN mkdir -p /app/data

COPY backend/ .

# Copy built frontend into the container
COPY --from=frontend-build /frontend/dist /app/static

WORKDIR /app/src

ENV OPENROUTER_BASE_URL=https://openrouter.ai/api/v1

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]
