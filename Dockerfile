# syntax=docker/dockerfile:1
# ambientq: on-prem ambient quality agent. Small, non-root, no build tools at runtime.
FROM python:3.12-slim AS build
WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip wheel --no-cache-dir --wheel-dir /wheels ".[postgres,oidc]"

FROM python:3.12-slim
ARG VERSION=dev
ARG REVISION=unknown
LABEL org.opencontainers.image.title="ambientq" \
      org.opencontainers.image.description="On-prem ambient quality agent for LLM agents (Nutanix Enterprise AI / OpenAI-compatible)" \
      org.opencontainers.image.source="https://github.com/script-repo/nai-recon" \
      org.opencontainers.image.url="https://github.com/script-repo/nai-recon/tree/ambientq" \
      org.opencontainers.image.documentation="https://github.com/script-repo/nai-recon/blob/ambientq/README.md" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}" \
      org.opencontainers.image.licenses="Apache-2.0"
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --user-group --create-home --shell /usr/sbin/nologin ambientq
COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir /wheels/* && rm -rf /wheels
USER 10001:10001
WORKDIR /home/ambientq
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 AMBIENTQ_SERVER_HOST=0.0.0.0 AMBIENTQ_SERVER_PORT=8080
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2)" || exit 1
ENTRYPOINT ["ambientq"]
CMD ["serve"]
