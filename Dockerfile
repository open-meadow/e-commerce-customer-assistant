FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /bin/

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:${PATH}" \
    APP_DB_PATH=/app/data/app_d2.db \
    VECTOR_STORE_DIR=/app/data/vector_store

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

COPY app/ ./app/
COPY .streamlit/ ./.streamlit/
COPY docker-entrypoint.sh ./docker-entrypoint.sh

RUN useradd --create-home --uid 10001 --user-group app \
    && mkdir -p /app/data \
    && chown -R app:app /app

USER app

EXPOSE 8501

ENTRYPOINT ["sh", "/app/docker-entrypoint.sh"]
CMD ["streamlit", "run", "app/ui/streamlit_app.py", "--server.address=0.0.0.0", "--server.port=8501"]
