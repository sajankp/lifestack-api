FROM python:3.13-slim

WORKDIR /app

# --no-install-recommends: ffmpeg's Recommends pull ~560 MB of desktop/audio/video libraries the
# API never uses (verified: WebM/Opus, Ogg/Opus and M4A/AAC decode byte-identical either way).
# ffmpeg decodes the voice-capture stream (app/capture/audio.py); curl is for the HEALTHCHECK.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install uv for deterministic dependency resolution from uv.lock (pinned: ":latest" made the
# build depend on whatever uv shipped that day).
COPY --from=ghcr.io/astral-sh/uv:0.13.10 /uv /uvx /bin/

# Create the runtime user before copying the code, so COPY --chown sets ownership directly
# instead of a recursive chown that duplicated the whole /app tree in a ~35 MB extra layer.
RUN useradd -m -u 1001 appuser

COPY pyproject.toml uv.lock ./
# --compile-bytecode: site-packages is root-owned, so appuser cannot write .pyc at runtime and
# every container start would otherwise recompile ~2,500 modules from source (slow cold start).
RUN --mount=type=cache,target=/root/.cache/uv \
    uv export --frozen --no-dev -o requirements.txt && \
    uv pip install --system --no-cache --compile-bytecode -r requirements.txt

COPY --chown=appuser:appuser . .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --system --no-cache --compile-bytecode --no-deps .

COPY --chown=appuser:appuser docker-entrypoint.sh .
RUN chmod +x docker-entrypoint.sh \
    && mkdir -p /app/imports_temp \
    && chown appuser:appuser /app /app/imports_temp

# Run as non-root user for reduced blast radius
USER appuser

HEALTHCHECK --interval=30s --timeout=10s --start-period=120s --retries=3 \
  CMD curl -f http://localhost:8000/health || exit 1

ENTRYPOINT ["./docker-entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
