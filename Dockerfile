# ---- build stage: resolve the locked environment with uv -------------------
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder
WORKDIR /app
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1

# Dependency layer first so code edits don't re-resolve packages.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    uv sync --frozen --no-install-project --no-dev --extra redis

COPY pyproject.toml uv.lock README.md ./
COPY src/ src/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --extra redis

# ---- runtime stage ----------------------------------------------------------
FROM python:3.12-slim-bookworm

# uv must be present at runtime: `marimo run --sandbox` shells out to it to
# build each sandboxed notebook's isolated environment.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

RUN useradd --create-home app
WORKDIR /app
COPY --from=builder /app/.venv .venv
COPY src/ src/
COPY notebooks/ notebooks/
RUN mkdir /data && chown app:app /data

ENV PATH="/app/.venv/bin:$PATH" \
    GALLERY_STORAGE_ROOT=/data \
    GALLERY_NOTEBOOKS_DIR=/app/notebooks \
    # Sandbox envs are built into the data volume so cold starts survive restarts.
    UV_CACHE_DIR=/data/uv-cache \
    UV_PYTHON_INSTALL_DIR=/data/uv-python

USER app
EXPOSE 8000

CMD ["uvicorn", "gallery.main:app", "--host", "0.0.0.0", "--port", "8000"]
