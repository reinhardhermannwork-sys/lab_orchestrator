"""FastAPI app factory, startup/shutdown hooks.

M0 scope was just the app object and a health check; every milestone
since has hooked into `lifespan` without changing this file's shape:
  - M1: load & validate config/machines.yaml on startup
  - M2: init the SQLite schema
  - M5: pick a Tux2LabClient, mount the /v1/instances router, track
    background provisioning tasks so they aren't garbage-collected
    mid-flight and can be drained at shutdown
  - M6: start the janitor background task
  - M7: run startup reconciliation against `tux2lab vm list`, before
    the janitor starts
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from lab_orchestrator.adapters.tux2lab_client import FakeTux2LabClient, SSHTux2LabClient
from lab_orchestrator.api.routes_instances import router as instances_router
from lab_orchestrator.core.config import get_settings, load_machine_definitions
from lab_orchestrator.core.janitor import janitor_loop
from lab_orchestrator.core.reconcile import reconcile_on_startup
from lab_orchestrator.db.database import get_engine
from lab_orchestrator.db.init_db import init_db, sync_machine_definitions

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # --- startup ---
    settings = get_settings()
    app.state.settings = settings
    # Raises MachineConfigError on invalid config — deliberately
    # unhandled here so startup fails fast and loud (M1 done-when).
    app.state.machines = load_machine_definitions(settings.machines_config_path)

    engine = get_engine()
    init_db(engine)
    sync_machine_definitions(engine, app.state.machines)
    app.state.db_engine = engine

    # Real client if the host-wrapper SSH settings are actually
    # configured, fake otherwise. Falling back silently would make a
    # misconfigured production deployment look like it's working while
    # quietly never touching a real VM, so this logs loudly instead.
    try:
        app.state.tux2lab = SSHTux2LabClient.from_settings(settings)
    except ValueError:
        logger.warning(
            "tux2lab SSH settings not configured (LAB_ORCH_TUX2LAB_SSH_*) — "
            "using FakeTux2LabClient. Fine for local dev, wrong for production."
        )
        app.state.tux2lab = FakeTux2LabClient()

    # Provisioning tasks (api/routes_instances.py) are tracked here so a
    # task object always has a strong referent — asyncio only holds a
    # weak reference to a bare `asyncio.create_task()` result, so an
    # untracked task can be garbage-collected mid-flight — and so
    # shutdown below can wait for whatever's still in progress.
    app.state.background_tasks: set[asyncio.Task] = set()

    app.include_router(instances_router, prefix="/v1")

    # Before the janitor starts and before any request can create a
    # provisioning task, so nothing else is writing to the table yet.
    await reconcile_on_startup(engine=engine, tux2lab=app.state.tux2lab)

    janitor_task = asyncio.create_task(
        janitor_loop(
            engine=app.state.db_engine,
            tux2lab=app.state.tux2lab,
            settings=app.state.settings,
        )
    )
    app.state.background_tasks.add(janitor_task)
    janitor_task.add_done_callback(app.state.background_tasks.discard)

    yield
    # --- shutdown ---
    for task in list(app.state.background_tasks):
        task.cancel()
    await asyncio.gather(*app.state.background_tasks, return_exceptions=True)
    await app.state.tux2lab.close()


def create_app() -> FastAPI:
    app = FastAPI(
        title="Lab Orchestrator",
        version="0.1.0",
        lifespan=lifespan,
    )

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
