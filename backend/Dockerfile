# Build context is the repo root (see docker-compose.yml) so this stage can reach
# frontend/ — that's what lets `docker compose up --build` work from a fresh clone
# with no local npm run build first.
FROM node:20-slim AS frontend-build

WORKDIR /frontend

COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim

# Install curl for the docker-compose healthcheck
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install uv
RUN pip install --no-cache-dir uv

# Install Python dependencies (cached layer)
COPY backend/pyproject.toml backend/uv.lock* ./
RUN uv sync --no-dev

# Copy application code, in the one direction the imports run: api -> app -> superforecaster
COPY backend/superforecaster/ ./superforecaster/
COPY backend/app/ ./app/
COPY backend/api/ ./api/

# The built frontend — see the frontend-build stage above. Deployment shape is still
# ADR 47's "one process serving everything"; only the build step moved into the image.
COPY --from=frontend-build /frontend/dist ./frontend

# Persistent data goes here (mounted as a volume in docker-compose)
RUN mkdir -p /app/data

EXPOSE 8000

# --workers 1 is load-bearing, not a default. The one-step-in-flight lock
# (machine._slot) is per-process, so a second worker could run two agent steps at
# once; SQLite writes are also single-writer. Scaling out starts with Postgres.
CMD ["uv", "run", "uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
