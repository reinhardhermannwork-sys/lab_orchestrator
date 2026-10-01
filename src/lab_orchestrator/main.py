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
  - M8: log to stdout, pick the tux2lab backend explicitly
"""

from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from lab_orchestrator.adapters.tux2lab_client import (
    FakeTux2LabClient,
    SSHTux2LabClient,
    Tux2LabClient,
)
from lab_orchestrator.api.routes_instances import router as instances_router
from lab_orchestrator.api.routes_machines import router as machines_router
from lab_orchestrator.core.config import Settings, get_settings, load_machine_definitions
from lab_orchestrator.core.janitor import janitor_loop
from lab_orchestrator.core.reconcile import reconcile_on_startup
from lab_orchestrator.db.database import get_engine
from lab_orchestrator.db.init_db import init_db, sync_machine_definitions

logger = logging.getLogger(__name__)

_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def configure_logging(level: str) -> None:
    """Send lab_orchestrator.* logs to stdout at `level`.

    uvicorn only configures its own loggers, so without this only WARNING
    and above from this package reached stderr (Python's last-resort
    handler) and every INFO line -- janitor, reconciliation -- was lost.
    Only the package logger is touched, never the root logger, and it
    keeps propagating so pytest's caplog still sees records. Idempotent:
    the lifespan runs once per app, and tests build many apps.
    """
    package_logger = logging.getLogger("lab_orchestrator")
    package_logger.setLevel(level)
    if not any(getattr(h, "_lab_orchestrator", False) for h in package_logger.handlers):
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter(_LOG_FORMAT))
        handler._lab_orchestrator = True  # type: ignore[attr-defined]
        package_logger.addHandler(handler)


def make_tux2lab_client(settings: Settings) -> Tux2LabClient:
    """Pick the Tux2LabClient named by `settings.tux2lab_backend`.

    "ssh" raises ValueError (failing startup) if the SSH settings are
    missing. "auto" falls back to the fake client with a loud warning,
    which is fine for local dev and wrong for a deployment -- a deployment
    sets the backend explicitly (M8).
    """
    if settings.tux2lab_backend == "fake":
        logger.warning("using FakeTux2LabClient (LAB_ORCH_TUX2LAB_BACKEND=fake) -- no real VMs")
        return FakeTux2LabClient()
    if settings.tux2lab_backend == "ssh":
        return SSHTux2LabClient.from_settings(settings)
    try:
        return SSHTux2LabClient.from_settings(settings)
    except ValueError:
        logger.warning(
            "tux2lab SSH settings not configured (LAB_ORCH_TUX2LAB_SSH_*) — "
            "using FakeTux2LabClient. Fine for local dev, wrong for production."
        )
        return FakeTux2LabClient()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # --- startup ---
    settings = get_settings()
    configure_logging(settings.log_level)
    app.state.settings = settings
    # Raises MachineConfigError on invalid config — deliberately
    # unhandled here so startup fails fast and loud (M1 done-when).
    app.state.machines = load_machine_definitions(settings.machines_config_path)

    engine = get_engine()
    init_db(engine)
    sync_machine_definitions(engine, app.state.machines)
    app.state.db_engine = engine

    # Raises ValueError for backend "ssh" without SSH settings --
    # deliberately unhandled, like the config error above.
    app.state.tux2lab = make_tux2lab_client(settings)

    # Provisioning tasks (api/routes_instances.py) are tracked here so a
    # task object always has a strong referent — asyncio only holds a
    # weak reference to a bare `asyncio.create_task()` result, so an
    # untracked task can be garbage-collected mid-flight — and so
    # shutdown below can wait for whatever's still in progress.
    app.state.background_tasks: set[asyncio.Task] = set()

    app.include_router(instances_router, prefix="/v1")
    app.include_router(machines_router, prefix="/v1")

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
