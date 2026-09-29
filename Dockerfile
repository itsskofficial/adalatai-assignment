# syntax=docker/dockerfile:1.7
#
# One image for the app (the dashboard, which serves the built front end) and the runner
# (which collects). compose.yaml starts each with its own command.
#
# Layers are ordered so a change to the code rebuilds only the last few: the dependencies
# come from the lock file alone, and the browser from the dependencies alone.

ARG PYTHON_IMAGE=python:3.12-slim-bookworm
ARG NODE_IMAGE=node:22-bookworm-slim
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.11.7

FROM ${UV_IMAGE} AS uv

# --- The front end: the files npm run build writes -------------------------------------
FROM ${NODE_IMAGE} AS frontend
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# --- The app and the runner ------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PATH=/app/.venv/bin:$PATH

WORKDIR /app/backend

# The dependencies, from the lock file alone. uv is mounted for the build and not kept.
COPY backend/pyproject.toml backend/uv.lock ./
RUN --mount=from=uv,source=/uv,target=/usr/local/bin/uv \
    --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# Chromium for Playwright, with the system packages it needs, from the dependencies alone.
# The headless shell is all the renderer launches.
RUN playwright install --with-deps --only-shell chromium \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/*

# The code, installed in place so it finds the sample mail beside it.
COPY backend/src ./src
RUN --mount=from=uv,source=/uv,target=/usr/local/bin/uv \
    --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev
COPY backend/samples ./samples
COPY --from=frontend /app/frontend/dist /app/frontend/dist

# A user that is not root, owning the one folder the tool writes: the ledger, the archive,
# the summaries and the stored sign-ins, on the data volume.
RUN groupadd --gid 10001 collector \
    && useradd --uid 10001 --gid collector --create-home --shell /usr/sbin/nologin collector \
    && mkdir -p /data/tokens \
    && chown -R collector:collector /data \
    && chmod 0700 /data/tokens
USER collector:collector
VOLUME ["/data"]

# The settings a container always has. Everything else comes from the environment file.
ENV INVOICE_COLLECTOR_SKIP_DOTENV=1 \
    INVOICE_COLLECTOR_TOKEN_DIR=/data/tokens \
    INVOICE_COLLECTOR_FRONTEND_DIR=/app/frontend/dist

EXPOSE 8000 8001
CMD ["invoice-collector-dashboard", "--ledger", "/data/ledger.sqlite"]
