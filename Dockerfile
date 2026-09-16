# syntax=docker/dockerfile:1
# uv para instalar. CPython 3.12 (no PyPy ni Cython del API): lxml, cryptography y
# argon2 no tienen rueda fiable en PyPy; el cuello es I/O + JSON, no el intérprete.
# orjson (C sobre CPython) serializa el cache; el gzip de /api/listings se cocina una vez.
FROM python:3.12-slim-bookworm AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /build

COPY requirements.txt pyproject.toml ./
RUN python -m venv /opt/venv
ENV VIRTUAL_ENV=/opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --python /opt/venv/bin/python -r requirements.txt

FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    HOST=0.0.0.0 \
    PORT=8000 \
    DATA_DIR=/app/data

WORKDIR /app

RUN groupadd --gid 1000 app \
    && useradd --uid 1000 --gid app --create-home --home-dir /home/app app \
    && mkdir -p /app/data \
    && chown -R app:app /app

COPY --from=builder /opt/venv /opt/venv
COPY --chown=app:app app ./app
COPY --chown=app:app static ./static
COPY --chown=app:app run.py ./

USER app

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=3s --start-period=35s --retries=3 \
    CMD python -c "import sys,time; from pathlib import Path; p=Path('/app/data/heartbeat'); sys.exit(0 if p.is_file() and time.time()-p.stat().st_mtime<12 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
