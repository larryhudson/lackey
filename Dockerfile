# minion-base — Base image for lackey minion runs (DESIGN.md §8.1)
#
# Contains: minion infrastructure, agent framework, common utilities.
# Project-specific images extend this with their own tooling and deps.

FROM python:3.12-slim

# System dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        git \
        curl \
        jq \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user (uid 1000)
RUN groupadd -g 1000 lackey && \
    useradd -u 1000 -g 1000 -m -s /bin/sh lackey

# Install lackey package
COPY pyproject.toml /app/pyproject.toml
COPY src/ /app/src/
RUN pip install --no-cache-dir /app

# Copy entrypoint
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# Create working and output directories writable by lackey user
RUN mkdir -p /work /output && chown lackey:lackey /work /output

# Run as non-root
USER lackey

ENTRYPOINT ["entrypoint.sh"]
