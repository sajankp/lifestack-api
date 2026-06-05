FROM python:3.13-slim

WORKDIR /app

RUN apt-get update && apt-get install -y ffmpeg curl && rm -rf /var/lib/apt/lists/*

# Install uv for deterministic dependency resolution from uv.lock
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

COPY . .
RUN uv export --frozen --no-dev -o requirements.txt && \
    uv pip install --system --no-cache -r requirements.txt && \
    uv pip install --system --no-cache --no-deps .

COPY docker-entrypoint.sh .
RUN chmod +x docker-entrypoint.sh

# Run as non-root user for reduced blast radius
RUN useradd -m -u 1001 appuser && chown -R appuser:appuser /app
USER appuser

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
  CMD curl -f http://localhost:8000/health || exit 1

ENTRYPOINT ["./docker-entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
