FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

COPY src/ ./src/
COPY app/ ./app/

RUN uv sync --frozen --no-dev --no-editable \
    && useradd --create-home --uid 10001 --user-group app \
    && chown -R app:app /app

ENV PATH="/app/.venv/bin:${PATH}"

USER app

EXPOSE 8501

CMD ["uv", "run", "--no-sync", "streamlit", "run", "app/ui/streamlit_app.py", "--server.address=0.0.0.0", "--server.port=8501"]
