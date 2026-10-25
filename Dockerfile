# Build stage: uv installs the locked dependencies, then the project itself as a real (non-editable) package.
FROM python:3.13-slim AS build
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=0
WORKDIR /app

# Dependencies first: this layer is cached until pyproject.toml or uv.lock change.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Then the project. Hatch packs everything under src/patient360 (the prompt .md and .json files too).
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

# Run stage: only the virtual environment and the SQL migrations, as a non-root user.
FROM python:3.13-slim
RUN useradd --system --create-home --uid 10001 app
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY migrations ./migrations
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 MIGRATIONS_DIR=/app/migrations
USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request as u; u.urlopen('http://127.0.0.1:8000/health', timeout=4)"]
CMD ["uvicorn", "--factory", "patient360.main:create_app_from_env", "--host", "0.0.0.0", "--port", "8000"]
