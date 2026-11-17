# Deploying SupoClip on a single Render web service (all-in-one)

This directory turns the whole SupoClip self-hosted stack into **one Docker
container** that runs on a **single Render web service**:

```
Render edge (TLS) ──> nginx :10000
                        ├── /b/  ──> FastAPI backend :8000   (public REST API)
                        └── /    ──> Next.js frontend :3107  (UI + /api/* routes)

supervisord manages: PostgreSQL 15 (/data/pgdata), Redis 7,
backend (uvicorn), video worker (+embedded cron worker), frontend, nginx.
Optional: ai-worker (ENABLE_AI_WORKER=1).
```

Files:

| File | Purpose |
|---|---|
| `render/Dockerfile` | multi-stage build: frontend bundle + backend venv + runtime |
| `render/entrypoint.sh` | secrets, DB bootstrap, Prisma baselining, nginx port templating |
| `render/supervisord.conf` | process manager for all six processes |
| `render/nginx.conf` | single-port routing (`/b/` = backend, rest = frontend) |
| `render/ai-worker.conf` | optional AI-edit worker include |
| `render/render.yaml` | blueprint for creating a NEW service (skip if you have one) |

## Why `/b/`?

The browser only ever talks to the frontend origin (every backend call is
proxied server-side through the frontend's `/api/*` routes — verified in
`frontend/src/server/backend-api.ts`). But agents (like the GTA6 agency)
authenticate directly against the backend with `sk_...` API keys, so the
backend must be publicly reachable. The frontend already occupies top-level
paths (`/tasks`, `/admin` pages collide with backend route groups), so the
backend is published under the collision-free `/b/` prefix, which nginx
strips before proxying.

**Agent API base URL:** `https://<your-app>.onrender.com/b`

## Deploying onto an EXISTING Render service

Do this in the Render dashboard for your service (e.g.
`rnd_5VNlC3fNJiq92L6WUG0ukxGi4ZTN`):

1. **Fork** `FujiwaraChoki/supoclip` on GitHub, then add the files from this
   directory (and the `.dockerignore` change) to the fork's root — the
   GitHub web UI's *Add file → Upload files* works; keep the `render/`
   folder name. Commit to a branch (e.g. `render-all-in-one`).
2. Render → your service → **Settings → Build & Deploy**:
   - Repository: your fork
   - Branch: `render-all-in-one`
   - Runtime: **Docker**
   - Dockerfile Path: `render/Dockerfile`
3. **Settings → Environment**: add the env vars below.
4. (Paid plans) **Settings → Disks**: mount a disk at `/data` (15 GB is a
   good start). Without a disk everything resets on every deploy.
5. **Manual Deploy → Deploy latest commit**. First build takes ~10–15 min.
6. Wait for health check `GET /b/health/db` to pass, then open
   `https://<your-app>.onrender.com/`, create an account, and generate an
   API key under **Agents & API** (`/settings/api-keys`).

### Environment variables

**Required for the pipeline to actually produce clips:**

| Variable | Notes |
|---|---|
| `ASSEMBLY_AI_API_KEY` | free sign-up at assemblyai.com (~$50 free credit). Alternative: `TRANSCRIPTION_PROVIDER=youtube_captions` needs no key (uses YouTube's captions; weaker for music-heavy sources) |
| `LLM` | default `google-gla:gemini-3-flash-preview` |
| `GOOGLE_API_KEY` | free tier at aistudio.google.com (matches the default `LLM`). Or switch to `openai:*`/`anthropic:*`/`openrouter:*` + the matching key |

**Strongly recommended — YouTube downloads from Render's datacenter IP:**

Render's IPs face the exact "Sign in to confirm you're not a bot" wall the
GTA6 agency hits from its sandbox. SupoClip's fallback chain is:
`YOUTUBE_PROXY_URL` → RapidAPI → Apify → direct yt-dlp. Configure at least
one of:

| Variable | Notes |
|---|---|
| `APIFY_API_TOKEN` | apify.com free account; the primary Apify YouTube actor runs per video (cents). Same tier the agency already prefers |
| `YOUTUBE_PROXY_URL` | sticky residential proxy template, e.g. IPPeak: `http://USER-session-{session}:PASS@HOST:PORT` (the `{session}` placeholder is per-job sticky), or DataImpulse format; optionally `YOUTUBE_PROXY_COUNTRY=us` |

**Useful overrides** (defaults baked into the image, all optional):

| Variable | Default | Notes |
|---|---|---|
| `YOUTUBE_MAX_HEIGHT` | `720` | raise to `1080` on ≥2 GB plans |
| `MAX_VIDEO_DURATION` | `2700` | source length cap in seconds |
| `MAX_CLIPS` | `6` | matches the GTA6 agency's max |
| `DISABLE_SIGN_UP` | `false` | set to `true` after creating your account |
| `ENABLE_AI_WORKER` | `0` | AI editor queue (adds RAM) |
| `TRANSCRIPTION_PROVIDER` | `assemblyai` | or `whisper` (local, slower, no key) / `youtube_captions` |

Secrets (`BETTER_AUTH_SECRET`, `BACKEND_AUTH_SECRET`,
`APP_SETTINGS_ENCRYPTION_KEY`) are generated on first boot and stored at
`/data/secrets/env.sh` — no action needed.

### First-run checklist

1. Open the app URL, create an account.
2. API keys require a **verified email**; self-host without SES can't send
   verification mail. Use the Render **Shell** (paid plans) instead:

   ```bash
   su -s /bin/bash postgres -c \
     "psql supoclip -c 'UPDATE users SET \"emailVerified\" = true;'"
   ```

3. Settings → **Agents & API** → create key → copy the `sk_...` once.
4. Set `DISABLE_SIGN_UP=true` (the URL is public).
5. Submit one short YouTube video as a canary before relying on it.

### Plan sizing (honest numbers)

| Plan | RAM | Verdict |
|---|---|---|
| Free | 512 MB | boots, UI works, light renders only; DB wiped on each deploy; add a keep-alive pinger (see below) |
| Starter | 512 MB + disk | persistent but renders still tight; keep `YOUTUBE_MAX_HEIGHT=480` |
| Standard | 2 GB | comfortable: 720p–1080p renders, uploads, concurrent task + UI |
| Standard 2× | 4 GB | headroom for 1080p + faster-whisper local transcription |

A 1080p render (ffmpeg + face detection + caption burn-in) spikes well past
512 MB. The image defaults (`YOUTUBE_MAX_HEIGHT=720`, `MAX_CLIPS=6`,
`DATABASE_POOL_SIZE=3`, single worker) are chosen to fit ~2 GB.

### Keep-alive polling

Free/instance services sleep after 15 min without traffic. Point an
UptimeRobot / cron-job.org HTTPS monitor at:

```
https://<your-app>.onrender.com/b/health/db
```

every 5–10 minutes. That endpoint exercises nginx → backend → PostgreSQL.

## Using the deployment from the GTA6 agency

Everything the agency needs is the API base + a key:

```bash
BASE="https://<your-app>.onrender.com/b"
# create a clipping task from a YouTube URL
curl -X POST "$BASE/tasks/" \
  -H "Authorization: Bearer sk_..." -H "Content-Type: application/json" \
  -d '{"source":{"url":"https://youtu.be/VIDEO_ID"},"clip_length":"short"}'
# poll task state (or subscribe to $BASE/tasks/{id}/progress SSE)
curl "$BASE/tasks/{id}" -H "Authorization: Bearer sk_..."
```

Clip video files and burned-in-caption exports come back as URLs under the
same `/b/` base. This offloads YouTube downloading AND rendering from the
agency sandbox — the sandbox's burned datacenter-IP never touches YouTube
when `APIFY_API_TOKEN` or `YOUTUBE_PROXY_URL` is configured on SupoClip.

## Upgrades

1. Sync your fork with upstream.
2. Re-apply this directory if it didn't merge cleanly (it only adds files,
   plus the one-line `!backend` un-ignore in `.dockerignore`).
3. Deploy. `prisma migrate deploy` (new frontend migrations) runs on every
   boot; the backend applies its own `backend/src/migrations/sql/*.sql`
   automatically. Both are tracked tables, so reboots are safe.

## What is NOT included

- MCP server (`mcp/`) — add an `mcp` program to supervisord later if wanted;
  the REST API covers the agency's needs.
- bgutil PO-token sidecar — only useful together with a configured proxy;
  the image already ships the yt-dlp plugin and Deno, so it can be added as
  another program + `YOUTUBE_POT_PROVIDER_URL=http://127.0.0.1:4416`.
- Observability (vector/Axiom) — logs stream to Render's log tab already.
