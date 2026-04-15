# VN Edge production Docker image
FROM python:3.13-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# System deps for asyncpg + bcrypt
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libpq-dev curl && rm -rf /var/lib/apt/lists/*

# Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code
COPY . .

# Run as non-root
RUN useradd -m -u 1000 vnedge && chown -R vnedge:vnedge /app
USER vnedge

EXPOSE 8080

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -fs http://localhost:8080/health || exit 1

CMD ["python3", "main.py"]
