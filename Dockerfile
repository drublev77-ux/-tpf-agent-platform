FROM python:3.11-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends git docker.io \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .

ENV WORKSPACE_ROOT=/workspaces
CMD ["tpf-api"]
