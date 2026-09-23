"""FastAPI app factory, startup/shutdown hooks.

M0 scope: just the app object and a health check. Later milestones hook
into `lifespan` without touching this file's shape:
  - M1: load & validate config/machines.yaml on startup
  - M2: init the SQLite schema
  - M5: mount the /v1/instances router
  - M6: start the janitor background task
  - M7: run startup reconciliation against `tux2lab vm list`
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # --- startup ---
    # (M1) app.state.machines = load_machine_definitions()
    # (M2) init_db()
    # (M7) reconcile_on_startup()
    # (M6) janitor_task = asyncio.create_task(janitor_loop())
    yield
    # --- shutdown ---
    # (M6) janitor_task.cancel()
    # close any open SSH connections held by the tux2lab adapter


def create_app() -> FastAPI:
    app = FastAPI(
        title="Lab Orchestrator",
        version="0.1.0",
        lifespan=lifespan,
    )

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    # (M5) app.include_router(instances_router, prefix="/v1")

    return app


app = create_app()
