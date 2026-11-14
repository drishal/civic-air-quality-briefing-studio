# Cloud Run friendly image for the Civic Air Quality Briefing Studio.
# Build:  docker build -t air-briefing .
# Run:    docker run -p 8080:8080 -e GEMINI_API_KEY=... air-briefing
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    STREAMLIT_SERVER_HEADLESS=true \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false \
    PATH="/app/.venv/bin:$PATH"

# Install uv from the official distroless image (no pip, no build tooling in the final layer).
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

COPY . .

# Reproducible install: frozen when a lockfile is committed, plain sync otherwise.
RUN if [ -f uv.lock ]; then uv sync --frozen --no-dev; else uv sync --no-dev; fi

# Cloud Run injects PORT (defaults to 8080); the app must bind 0.0.0.0.
ENV PORT=8080
EXPOSE 8080

CMD exec streamlit run app.py --server.port=${PORT:-8080} --server.address=0.0.0.0 --server.headless=true
