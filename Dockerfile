# exchange-mcp: one image, one listener. Key material is mounted as files, never passed in the environment.
FROM python:3.12-slim-bookworm@sha256:6a0e06f5ba0b373d051aabd3fffb9b873f27f9a2a3a14bcbe37dee6f0e3bbc70 AS build
WORKDIR /src
COPY requirements.lock ./
RUN pip install --no-cache-dir --require-hashes --target /deps -r requirements.lock
# The package is pure Python: copied as source, so no unlocked build backend runs.
COPY src/exchange_mcp /app/exchange_mcp

FROM python:3.12-slim-bookworm@sha256:6a0e06f5ba0b373d051aabd3fffb9b873f27f9a2a3a14bcbe37dee6f0e3bbc70
RUN apt-get update && apt-get upgrade -y --no-install-recommends && rm -rf /var/lib/apt/lists/* \
 && useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app
COPY --from=build /deps /opt/deps
COPY --from=build /app /opt/app
ENV PYTHONPATH=/opt/app:/opt/deps PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8000
USER 10001
EXPOSE 8000
ENTRYPOINT ["python", "-m", "exchange_mcp.app"]
