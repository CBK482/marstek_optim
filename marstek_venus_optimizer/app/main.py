"""FastAPI application (served through Home Assistant Ingress on port 8099)."""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from . import __version__
from .controller import Controller
from .discovery import discover
from .ha_client import HAClient
from .settings import DB_PATH, OPERATING_MODES, AppConfig
from .state import StateStore

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "info").upper(),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
_LOGGER = logging.getLogger("venus_optimizer")
STATIC_DIR = Path(__file__).parent / "static"

ha = HAClient.from_env()
store = StateStore(DB_PATH)
controller = Controller(ha, store)
scheduler = AsyncIOScheduler()


def _schedule_jobs() -> None:
    c = controller.cfg.control
    scheduler.add_job(controller.run_optimization, "interval", minutes=c.optimize_interval_min,
                      id="optimize", replace_existing=True, max_instances=1, coalesce=True)
    scheduler.add_job(controller.watchdog, "interval", seconds=c.watchdog_interval_s,
                      id="watchdog", replace_existing=True, max_instances=1, coalesce=True)
    scheduler.add_job(store.prune, "interval", hours=6, id="prune", replace_existing=True)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await ha.start()
    controller.log(f"Venus Optimizer {__version__} started (HA mode: {ha.mode}, tz: {ha.tz}, "
                   f"operating mode: {controller.mode})")
    await controller.bootstrap_history()
    try:
        await controller.run_optimization(force_prices=True)
    except Exception as err:  # noqa: BLE001
        controller.log(f"Initial optimization failed: {err}", "error")
    _schedule_jobs()
    scheduler.start()
    yield
    scheduler.shutdown(wait=False)
    await ha.close()


class NoCacheStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope):
        resp = await super().get_response(path, scope)
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
        return resp


app = FastAPI(title="Marstek Venus Optimizer", version=__version__, lifespan=lifespan,
              docs_url="/api/docs", openapi_url="/api/openapi.json")
app.mount("/static", NoCacheStaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/api/health")
async def health():
    return {"ok": True, "version": __version__, "ha_mode": ha.mode, "connected": ha.connected}


@app.get("/api/status")
async def status():
    return controller.status()


@app.get("/api/plan")
async def plan():
    p = controller.plan
    if p is None:
        return {"ok": False, "status": "pending", "slots": []}
    return p.to_dict()


@app.post("/api/solve")
async def solve(apply: bool = Query(True)):
    res = await controller.run_optimization(apply=apply, force_prices=True)
    return res.to_dict()


@app.post("/api/mode")
async def set_mode(payload: dict = Body(...)):
    mode = payload.get("mode")
    if mode not in OPERATING_MODES:
        raise HTTPException(400, f"mode must be one of {OPERATING_MODES}")
    await controller.set_mode(mode)
    return controller.status()


@app.get("/api/config")
async def get_config():
    return controller.cfg.model_dump()


@app.get("/api/config/defaults")
async def get_defaults():
    return AppConfig().model_dump()


@app.post("/api/config")
async def save_config(payload: dict = Body(...), preview: bool = Query(True)):
    try:
        cfg = AppConfig.model_validate(payload)
    except ValidationError as err:
        return JSONResponse(status_code=422, content={"ok": False, "errors": err.errors(include_url=False)})
    old = controller.cfg.control
    if cfg.battery.min_soc_pct >= cfg.battery.max_soc_pct:
        raise HTTPException(400, "Min SoC must be lower than Max SoC")
    if not (cfg.battery.min_soc_pct <= cfg.battery.target_soc_pct <= cfg.battery.max_soc_pct):
        raise HTTPException(400, "Target SoC must be between Min SoC and Max SoC")
    controller.set_config(cfg)
    controller.prices.last_fetch = None  # price entity / source may have changed
    controller.prices._nordpool_entry = None  # noqa: SLF001
    if (old.optimize_interval_min, old.watchdog_interval_s) != (cfg.control.optimize_interval_min,
                                                                cfg.control.watchdog_interval_s):
        _schedule_jobs()
    controller.applied = None
    controller.log("Configuration saved")
    out: dict = {"ok": True, "config": cfg.model_dump()}
    if preview:
        out["plan"] = (await controller.run_optimization(apply=True, force_prices=True)).to_dict()
    return out


@app.get("/api/discover")
async def api_discover():
    try:
        states = await ha.get_states()
    except Exception as err:  # noqa: BLE001
        raise HTTPException(502, f"Home Assistant unreachable: {err}") from err
    return discover(states)


@app.get("/api/entities")
async def entity_states(ids: str = Query("")):
    wanted = [i for i in ids.split(",") if i]
    try:
        states = {s["entity_id"]: s for s in await ha.get_states()}
    except Exception as err:  # noqa: BLE001
        raise HTTPException(502, f"Home Assistant unreachable: {err}") from err
    out = {}
    for eid in wanted:
        st = states.get(eid)
        out[eid] = None if st is None else {
            "state": st["state"],
            "unit": st.get("attributes", {}).get("unit_of_measurement", ""),
            "name": st.get("attributes", {}).get("friendly_name", eid),
        }
    return out


@app.get("/api/logs")
async def logs(limit: int = 150):
    return {"logs": list(controller.logs)[:limit], "actions": store.actions(limit)}


@app.get("/api/history")
async def history(minutes: int = 360):
    return {"samples": store.recent_samples(minutes)}


@app.get("/api/mock/calls")
async def mock_calls():
    return {"mock": getattr(ha, "mock", False), "calls": list(getattr(ha, "calls", []))[-50:]}
