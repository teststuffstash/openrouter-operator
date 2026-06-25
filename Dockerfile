# Operator image — pure Python (kopf). Built amd64 on the homelab Proxmox VM runner (Docker),
# pushed to ghcr. Installs runtime deps + the `sdk` extra (the official `openrouter` SDK) from the
# lockfile, runs kopf against all namespaces with a liveness endpoint.
FROM python:3.11-slim

RUN pip install --no-cache-dir uv
WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev --extra sdk

ENV PATH="/app/.venv/bin:$PATH"
USER 65532:65532
EXPOSE 8080
ENTRYPOINT ["kopf", "run", "-m", "openrouter_operator.operator", \
            "--all-namespaces", "--liveness=http://0.0.0.0:8080/healthz"]
