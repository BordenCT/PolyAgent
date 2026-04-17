FROM docker.io/library/python:3.14-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHON_GIL=0

WORKDIR /app

# Install Rust toolchain for polymarket-cli
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl build-essential pkg-config libssl-dev && \
    curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y && \
    rm -rf /var/lib/apt/lists/*

ENV PATH="/root/.cargo/bin:${PATH}"

# Install polymarket-cli from source
RUN cargo install --git https://github.com/Polymarket/polymarket-cli --locked

# Install Python dependencies
COPY pyproject.toml .
RUN pip install --no-cache-dir -e ".[dev]"

COPY . .
RUN pip install --no-cache-dir -e .

ENTRYPOINT ["polyagent-bot"]
