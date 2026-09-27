# Multi-stage build: dependencies are resolved with uv in a builder stage; the runtime
# stage only adds the system libraries WeasyPrint needs (Pango/HarfBuzz/fontconfig).

FROM python:3.12-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
# Dependencies first so code changes don't invalidate this layer.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --extra pdf --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --extra pdf


FROM python:3.12-slim AS runtime
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz0b libharfbuzz-subset0 libfontconfig1 \
        fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*
RUN useradd --create-home --uid 1000 mre
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY src ./src
COPY sql ./sql
COPY templates ./templates
COPY prompts ./prompts
COPY sample_data ./sample_data
COPY config.yaml ./
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    MPLCONFIGDIR=/tmp/matplotlib
# data/ (Parquet cache, LLM cache) and out/ (reports) are mounted as volumes.
# Secrets come from --env-file at run time; nothing secret is baked into the image.
RUN mkdir -p data out && chown -R mre:mre /app
USER mre
ENTRYPOINT ["mre"]
CMD ["--help"]


# Test image: runtime + dev dependencies + tests, so the PDF smoke test runs with Pango.
# docker build --target test -t mre:test . && docker run --rm mre:test
FROM runtime AS test
USER root
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /bin/uv
ENV UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --extra pdf
COPY tests ./tests
ENTRYPOINT ["pytest"]
CMD ["-q"]


# Default target (last stage): the application image.
FROM runtime AS app
