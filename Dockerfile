FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 VALTAKIRJA_DATA_DIR=/data
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY valtakirja ./valtakirja
COPY scripts ./scripts
COPY web ./web
COPY buyer ./buyer
RUN uv sync --frozen --no-dev
EXPOSE 8000
# First start downloads Valio Aimo's data with the API key from the environment,
# builds the search index and issues the demo mandates; later starts reuse /data.
CMD ["sh", "-c", "uv run --no-sync python -m scripts.fetch_data && uv run --no-sync python -m scripts.seed && exec uv run --no-sync uvicorn valtakirja.api:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips='*'"]
