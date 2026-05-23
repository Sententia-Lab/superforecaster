FROM python:3.12-slim

# Install curl for the docker-compose healthcheck
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install uv
RUN pip install --no-cache-dir uv

# Install Python dependencies (cached layer)
COPY pyproject.toml uv.lock* ./
RUN uv sync --no-dev

# Copy application code
COPY config.py ./
COPY superforecaster/ ./superforecaster/
COPY api/ ./api/

# Persistent data goes here (mounted as a volume in docker-compose)
RUN mkdir -p /app/data

EXPOSE 8000

CMD ["uv", "run", "uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
