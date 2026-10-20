# Multi-stage Dockerfile for Harness² & MCP Server
FROM python:3.12-slim-bookworm AS base

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    ca-certificates \
    sqlite3 \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install uv for rapid Python packaging
COPY --from=ghcr.io/astral-sh/uv:0.7.13 /uv /uvx /bin/

WORKDIR /app

# Copy dependency manifests and install dependencies
COPY pyproject.toml README.md ./
COPY harness2 ./harness2

# Install harness2 and MCP dependencies
RUN uv pip install --system -e . "mcp>=1.0.0"

# Default environment variables
ENV PYTHONUNBUFFERED=1
ENV HARNESS2_RUNS_DIR=/app/runs

# Volumes for persistent memory and workspace mounting
VOLUME ["/app/runs", "/workspace"]

# Default command runs MCP server over stdio
ENTRYPOINT ["harness2-mcp"]
