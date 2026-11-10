# The hosted endpoint's image (deploy/cloudflare). Also works for self-hosting:
#   docker build -t portugal-mcp . && docker run -p 8000:8000 -e FOGOS_API_KEY=... portugal-mcp
# then point your MCP client at http://localhost:8000/mcp
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORTUGAL_MCP_CACHE_DIR=/cache

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY portugal_mcp ./portugal_mcp
RUN pip install . \
    && useradd --system --uid 10001 --no-create-home app \
    && mkdir /cache && chown app /cache

USER app
EXPOSE 8000
CMD ["portugal-mcp", "--hosted"]
