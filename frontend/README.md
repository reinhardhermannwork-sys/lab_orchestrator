# Lab frontend (M9)

React + Tailwind client (built with Vite) and a thin Fastify server, all
TypeScript. The server serves the built client and a fixed list of
`/api/...` routes that forward to the orchestrator. It is the **only** path
from the browser to the orchestrator, and the only place `user` is set:
from the identity header traefik copies from authentik
(`X-authentik-username`). See `server/app.ts` and architecture doc §4, §17.

## Develop

Needs Node.js 24 (on the dev VM: `export PATH=$HOME/.local/node/bin:$PATH`).

```bash
npm ci
# terminal 1: orchestrator (repo root), fake tux2lab
LAB_ORCH_TUX2LAB_BACKEND=fake ../.venv/bin/uvicorn lab_orchestrator.main:app --port 8000
# terminal 2: frontend server, with a dev user instead of authentik
LAB_FRONTEND_ORCHESTRATOR_URL=http://127.0.0.1:8000 LAB_FRONTEND_DEV_USER=hermann npm run dev:server
# terminal 3: client with hot reload on http://localhost:5173 (proxies /api)
npm run dev:client
```

`LAB_FRONTEND_DEV_USER` is for local development only. With it set, a
request without the identity header runs as that user, so it must never be
set in a deployment (compose doesn't).

## Check

```bash
npm run typecheck
npm test
npm run build      # typecheck + client build into dist/client
```

## Settings

| Env var | Default | |
|---|---|---|
| `LAB_FRONTEND_ORCHESTRATOR_URL` | `http://lab-orchestrator:8000` | orchestrator on the lab network |
| `LAB_FRONTEND_USER_HEADER` | `x-authentik-username` | identity header from traefik/authentik |
| `LAB_FRONTEND_DEV_USER` | *(unset)* | local dev only, see above |
| `LAB_FRONTEND_PORT` / `LAB_FRONTEND_HOST` | `3000` / `0.0.0.0` | |
| `LAB_FRONTEND_CLIENT_DIR` | `dist/client` | built client to serve |
