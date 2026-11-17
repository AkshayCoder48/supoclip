#!/bin/bash
# SupoClip all-in-one entrypoint.
#
# 1. Generates (once) and loads per-install secrets.
# 2. Writes /run/supoclip/env.sh with all runtime env for every process.
# 3. On first boot: initdb + baseline schema (init.sql) + Prisma baselining.
# 4. On every boot: prisma migrate deploy (no-op after baseline).
# 5. Renders nginx.conf with the real $PORT, then exec supervisord.
#
# Data lives on /data (Render persistent disk when attached; otherwise the
# container filesystem, which is wiped on every deploy — see RENDER_DEPLOY.md).

set -euo pipefail

DATA_DIR="${DATA_DIR:-/data}"
PGDATA="$DATA_DIR/pgdata"
RUN_DIR=/run/supoclip
PG_BIN=/usr/lib/postgresql/15/bin
PUBLIC_URL="${RENDER_EXTERNAL_URL:-http://localhost:${PORT:-10000}}"

log() { echo "[entrypoint] $*"; }

mkdir -p "$DATA_DIR" "$RUN_DIR" "$DATA_DIR/uploads" "$DATA_DIR/secrets"
chmod 700 "$DATA_DIR/secrets"

# --------------------------------------------------------------------------
# 1. Per-install secrets (persisted on /data so they survive restarts/deploys
#    when a Render disk is attached).
# --------------------------------------------------------------------------
if [ ! -f "$DATA_DIR/secrets/env.sh" ]; then
    log "generating install secrets"
    cat > "$DATA_DIR/secrets/env.sh" <<SECRETS
BETTER_AUTH_SECRET=$(openssl rand -hex 32)
BACKEND_AUTH_SECRET=$(openssl rand -hex 32)
APP_SETTINGS_ENCRYPTION_KEY=$(openssl rand -hex 32)
SECRETS
    chmod 600 "$DATA_DIR/secrets/env.sh"
fi
# shellcheck disable=SC1091
. "$DATA_DIR/secrets/env.sh"
: "${BETTER_AUTH_SECRET:?}" "${BACKEND_AUTH_SECRET:?}" "${APP_SETTINGS_ENCRYPTION_KEY:?}"

# --------------------------------------------------------------------------
# 2. Runtime environment shared by backend, worker(s) and frontend.
#    Render dashboard env vars already exist in this process's environment;
#    we only add/derive what they cannot know.
# --------------------------------------------------------------------------
# DATABASE_URL is the Prisma (frontend) format; backend processes remap it
# to BACKEND_DATABASE_URL in their supervisord command lines.
cat > "$RUN_DIR/env.sh" <<ENV
export DATABASE_URL="postgresql://supoclip@127.0.0.1:5432/supoclip?connection_limit=4&pool_timeout=10"
export BACKEND_DATABASE_URL="postgresql+asyncpg://supoclip@127.0.0.1:5432/supoclip"
export REDIS_HOST="127.0.0.1"
export REDIS_PORT="6379"
export BACKEND_INTERNAL_URL="http://127.0.0.1:8000"
export NEXT_PUBLIC_API_URL="$PUBLIC_URL/b"
export NEXT_PUBLIC_APP_URL="$PUBLIC_URL"
export BETTER_AUTH_URL="$PUBLIC_URL"
export BETTER_AUTH_TRUSTED_ORIGINS="$PUBLIC_URL"
export BETTER_AUTH_SECRET="$BETTER_AUTH_SECRET"
export BACKEND_AUTH_SECRET="$BACKEND_AUTH_SECRET"
export APP_SETTINGS_ENCRYPTION_KEY="$APP_SETTINGS_ENCRYPTION_KEY"
export CORS_ORIGINS="$PUBLIC_URL"
export SELF_HOST="${SELF_HOST:-true}"
export TEMP_DIR="${TEMP_DIR:-/data/uploads}"
export MEDIA_STORAGE="local"
export PYTHONPATH="/app"
export PYTHONUNBUFFERED="1"
export PRISMA_QUERY_ENGINE_LIBRARY="/app-frontend/prisma-engine/libquery_engine-debian-openssl-3.0.x.so.node"
ENV

# --------------------------------------------------------------------------
# 3. First-boot database bootstrap.
#    PostgreSQL runs with local trust auth (it binds 127.0.0.1 only and the
#    container is single-tenant), so no password is needed.
# --------------------------------------------------------------------------
pg_as_postgres() { su -s /bin/bash postgres -c "$*"; }

if [ ! -s "$PGDATA/PG_VERSION" ]; then
    log "initialising PostgreSQL at $PGDATA"
    chown -R postgres:postgres "$DATA_DIR"
    pg_as_postgres "$PG_BIN/initdb -D $PGDATA -E UTF8 --auth-local=trust --auth-host=trust" >/dev/null

    # Small-footprint config (the whole stack shares one container; on the
    # 512 MB free plan every megabyte counts - the cgroup OOM killer takes
    # the whole instance down otherwise).
    cat >> "$PGDATA/postgresql.conf" <<PGCONF
listen_addresses = '127.0.0.1'
shared_buffers = 24MB
effective_cache_size = 64MB
max_connections = 25
work_mem = 4MB
maintenance_work_mem = 24MB
PGCONF
    chown postgres:postgres "$PGDATA/postgresql.conf"

    log "starting temporary PostgreSQL for bootstrap"
    pg_as_postgres "$PG_BIN/pg_ctl -D $PGDATA -w -t 60 -l $PGDATA/bootstrap.log start"

    log "creating role/database"
    $PG_BIN/createuser -h 127.0.0.1 -U postgres --superuser supoclip 2>/dev/null \
        || log "role supoclip already exists"
    $PG_BIN/createdb -h 127.0.0.1 -U postgres -O supoclip supoclip 2>/dev/null \
        || log "database supoclip already exists"

    log "applying baseline schema (init.sql)"
    $PG_BIN/psql -h 127.0.0.1 -U postgres -v ON_ERROR_STOP=1 -d supoclip \
        -f /supoclip-init/init.sql

    # Prisma baselining: init.sql IS the current baseline, so record every
    # shipped migration as applied instead of replaying non-idempotent SQL.
    # Rows are inserted directly (checksum = sha256 of migration.sql, the
    # format prisma uses) - spawning `prisma migrate resolve` once per
    # migration costs ~10 s each on a throttled CPU and fails anyway when
    # the table is missing prisma 6's newer columns.
    log "baselining Prisma migrations"
    $PG_BIN/psql -h 127.0.0.1 -U postgres -d supoclip -v ON_ERROR_STOP=1 <<'PSQL'
        CREATE TABLE IF NOT EXISTS "_prisma_migrations" (
            id VARCHAR(36) PRIMARY KEY NOT NULL,
            checksum VARCHAR(64) NOT NULL,
            finished_at TIMESTAMPTZ,
            migration_name VARCHAR(255) NOT NULL,
            logs TEXT,
            rolled_back_at TIMESTAMPTZ,
            started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            applied_steps_count INTEGER NOT NULL DEFAULT 0
        );
PSQL
    for m in /app-frontend/prisma/migrations/*/; do
        name=$(basename "$m")
        [ -f "$m/migration.sql" ] || continue
        checksum=$(sha256sum "$m/migration.sql" | cut -d' ' -f1)
        $PG_BIN/psql -h 127.0.0.1 -U postgres -d supoclip -q -c \
            "INSERT INTO \"_prisma_migrations\" (id, checksum, finished_at, migration_name, logs, rolled_back_at, started_at, applied_steps_count) \
             SELECT gen_random_uuid()::text, '$checksum', now(), '$name', NULL, NULL, now(), 1 \
             WHERE NOT EXISTS (SELECT 1 FROM \"_prisma_migrations\" WHERE migration_name = '$name')" \
            || log "WARN: could not record migration $name (continuing)"
    done

    log "stopping temporary PostgreSQL"
    pg_as_postgres "$PG_BIN/pg_ctl -D $PGDATA -w -t 60 -m fast stop"
    log "bootstrap complete"
fi

# --------------------------------------------------------------------------
# 4. Prisma migrations added after the baseline are applied by the frontend
#    program at start (see supervisord.conf) once PostgreSQL is up.
#    The backend applies its own backend/src/migrations/sql/*.sql at start
#    via init_db(), tracked in the schema_migrations table.
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# 5. Optional processes (video worker, AI worker) selected by env vars, then
#    nginx with real $PORT and supervisord.
# --------------------------------------------------------------------------
mkdir -p /render/programs
if [ "${ENABLE_VIDEO_WORKER:-1}" != "0" ]; then
    cp /render/worker.conf /render/programs/worker.conf
else
    log "video worker DISABLED (ENABLE_VIDEO_WORKER=0) - tasks will queue"
fi
if [ "${ENABLE_AI_WORKER:-0}" = "1" ]; then
    cp /render/ai-worker.conf /render/programs/ai-worker.conf
    log "AI worker enabled"
fi

PORT_NUM="${PORT:-10000}"
sed "s/__PORT__/${PORT_NUM}/g" /render/nginx.conf.template > "$RUN_DIR/nginx.conf"
log "nginx listening on :${PORT_NUM}; public URL: ${PUBLIC_URL}"
log "public REST API base for agents: ${PUBLIC_URL}/b"

exec /usr/bin/supervisord -n -c /render/supervisord.conf
