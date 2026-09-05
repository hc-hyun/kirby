FROM python:3.12-slim-bookworm@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254 AS build
COPY --from=ghcr.io/astral-sh/uv:0.11.14@sha256:1025398289b62de8269e70c45b91ffa37c373f38118d7da036fb8bb8efc85d97 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY scripts/install_goose.py /tmp/install_goose.py
RUN python /tmp/install_goose.py
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.12-slim-bookworm@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254
RUN apt-get update && apt-get install -y --no-install-recommends libstdc++6 ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home kirby \
    && mkdir -p /data/sessions && chown -R kirby:kirby /data
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY --from=build /app/.tools/goose /usr/local/bin/goose
COPY examples /app/examples
ENV PATH="/app/.venv/bin:$PATH" \
    KIRBY_HOST=0.0.0.0 \
    KIRBY_GOOSE_BINARY=/usr/local/bin/goose \
    KIRBY_ROLES_DIR=/app/examples \
    KIRBY_SESSIONS_DIR=/data/sessions \
    PYTHONUNBUFFERED=1
USER kirby
EXPOSE 8000
CMD ["python", "-m", "kirby.api"]
