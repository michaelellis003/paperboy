FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

ENV PATH="/app/.venv/bin:$PATH"
# Defense-in-depth: the server needs no root privileges at runtime.
RUN useradd --create-home --uid 1000 paperboy
USER paperboy
# No baked-in PORT: Cloud Run injects it at runtime (switching the
# server to Streamable HTTP), while a bare `docker run` speaks stdio —
# which is what registry inspectors like Glama's expect.
CMD ["paperboy"]
