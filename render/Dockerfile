# syntax=docker/dockerfile:1.7
#
# SupoClip all-in-one image for a SINGLE Render web service.
#
# Runs the whole self-hosted stack inside one container:
#   PostgreSQL 15 + Redis 7 + FastAPI backend + video worker (with embedded
#   cron worker) + Next.js frontend + nginx front proxy.
#
# The one public port serves:
#   /            -> Next.js frontend (pages + its /api/* routes)
#   /b/          -> FastAPI backend, prefix stripped (public REST API for
#                    agents:  https://<app>.onrender.com/b/tasks/ etc.)
#
# Build context: repository root. Build args are intentionally unused for
# URLs: every public-URL setting is applied at RUNTIME from
# RENDER_EXTERNAL_URL by render/entrypoint.sh, so the image is
# environment-agnostic.

ARG NODE_IMAGE=node:22.23.4-bookworm-slim
ARG PY_IMAGE=python:3.11-slim-bookworm

# ---------------------------------------------------------------------------
# Stage 1: build the Next.js frontend (standalone output)
# ---------------------------------------------------------------------------
FROM ${NODE_IMAGE} AS frontend-build

ENV PNPM_HOME=/pnpm
ENV PATH=/pnpm:$PATH
ENV NEXT_TELEMETRY_DISABLED=1

RUN npm install -g pnpm@10.27.0

WORKDIR /build

RUN apt-get update \
    && apt-get install -y --no-install-recommends openssl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY frontend/package.json frontend/pnpm-lock.yaml ./
RUN --mount=type=cache,id=pnpm,target=/pnpm/store \
    pnpm install --frozen-lockfile --ignore-scripts --store-dir /pnpm/store

COPY frontend/ ./
COPY CHANGELOG.md ./CHANGELOG.md

# Build-time NEXT_PUBLIC_* values below are defaults only; they are not used
# by any client component for URLs (verified: NEXT_PUBLIC_API_URL /
# NEXT_PUBLIC_APP_URL are referenced exclusively from server code, which
# re-reads them from process.env at runtime).
ARG NEXT_PUBLIC_API_URL=http://127.0.0.1:8000
ARG NEXT_PUBLIC_APP_URL=http://localhost:3107
ARG NEXT_PUBLIC_SELF_HOST=true
ENV NEXT_PUBLIC_API_URL=${NEXT_PUBLIC_API_URL}
ENV NEXT_PUBLIC_APP_URL=${NEXT_PUBLIC_APP_URL}
ENV NEXT_PUBLIC_SELF_HOST=${NEXT_PUBLIC_SELF_HOST}
ENV SKIP_LINT=1

RUN pnpm exec prisma generate && pnpm run build

# Prisma query engine outside src/ (mirrors frontend/Dockerfile runner stage)
RUN mkdir -p prisma-engine \
    && cp src/generated/prisma/libquery_engine-*.so.node prisma-engine/ \
    && cp server-request-timeout.cjs server-request-timeout.runtime.cjs

# ---------------------------------------------------------------------------
# Stage 2: build the backend virtualenv (CPU wheels)
# ---------------------------------------------------------------------------
FROM ${PY_IMAGE} AS backend-build

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg tesseract-ocr tesseract-ocr-eng curl unzip \
        fonts-noto-core fonts-noto-cjk fonts-noto-color-emoji fontconfig \
    && fc-cache -f \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

WORKDIR /app

COPY backend/pyproject.toml backend/uv.lock* ./
RUN uv venv .venv && uv sync --frozen --extra cpu

COPY backend/src/ ./src/
COPY backend/fonts/ ./fonts/
COPY backend/transitions/ ./transitions/

# ---------------------------------------------------------------------------
# Stage 3: runtime — everything in one container
# ---------------------------------------------------------------------------
FROM ${PY_IMAGE} AS runtime

ARG NODE_IMAGE

# Node.js runtime for the Next.js standalone server (same distro as the
# node image, so the binary just works).
COPY --from=${NODE_IMAGE} /usr/local/bin/node /usr/local/bin/node
COPY --from=${NODE_IMAGE} /usr/local/bin/npm /usr/local/bin/npm
COPY --from=${NODE_IMAGE} /usr/local/bin/npx /usr/local/bin/npx
COPY --from=${NODE_IMAGE} /usr/local/lib/node_modules /usr/local/lib/node_modules

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg tesseract-ocr tesseract-ocr-eng curl unzip ca-certificates openssl \
        fonts-noto-core fonts-noto-cjk fonts-noto-color-emoji fontconfig \
        postgresql-15 postgresql-client-15 postgresql-contrib-15 \
        redis-server nginx supervisor \
    && fc-cache -f \
    && rm -rf /var/lib/apt/lists/*

# Deno: required by yt-dlp for YouTube's JS player challenge.
RUN curl -fsSL https://deno.land/install.sh | sh
ENV DENO_DIR=/root/.deno
ENV PATH=/root/.deno/bin:/usr/local/bin:$PATH

# Prisma CLI used by the entrypoint for migration baselining/deploy.
# Keep the version in sync with frontend/package.json ("prisma": "6.x.y").
RUN npm install -g prisma@6.19.2 && prisma --version

# Backend application (venv paths are absolute: /app must stay /app).
WORKDIR /app
COPY --from=backend-build /app /app

# Frontend standalone bundle.
RUN mkdir -p /app-frontend
COPY --from=frontend-build /build/public /app-frontend/public
COPY --from=frontend-build /build/.next/standalone /app-frontend/
COPY --from=frontend-build /build/.next/static /app-frontend/.next/static
COPY --from=frontend-build /build/prisma /app-frontend/prisma
COPY --from=frontend-build /build/src/generated/prisma /app-frontend/src/generated/prisma
COPY --from=frontend-build /build/prisma-engine /app-frontend/prisma-engine
COPY --from=frontend-build /build/server-request-timeout.runtime.cjs /app-frontend/server-request-timeout.cjs

# Database baseline schema (applied on first boot only).
COPY init.sql /supoclip-init/init.sql

# All-in-one runtime configuration.
COPY render/nginx.conf /render/nginx.conf.template
COPY render/supervisord.conf /render/supervisord.conf
COPY render/entrypoint.sh /render/entrypoint.sh

RUN mkdir -p /data /run/supoclip \
    && chmod +x /render/entrypoint.sh

# Defaults that show up in the Render dashboard; runtime values that depend
# on secrets or RENDER_EXTERNAL_URL are injected by render/entrypoint.sh.
ENV SELF_HOST=true \
    TRANSCRIPTION_PROVIDER=assemblyai \
    LLM=google-gla:gemini-3-flash-preview \
    MEDIA_STORAGE=local \
    TEMP_DIR=/data/uploads \
    REDIS_HOST=127.0.0.1 \
    REDIS_PORT=6379 \
    EMBED_CRON_WORKER=1 \
    ENABLE_AI_WORKER=0 \
    VIDEO_JOB_USER_CONCURRENCY=1 \
    DATABASE_POOL_SIZE=3 \
    MAX_CLIPS=6 \
    DEFAULT_PROCESSING_MODE=fast \
    YOUTUBE_MAX_HEIGHT=720 \
    MAX_VIDEO_DURATION=2700 \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=10000

EXPOSE 10000

ENTRYPOINT ["/bin/bash", "/render/entrypoint.sh"]
