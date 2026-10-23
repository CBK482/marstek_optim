"""Home Assistant REST client (Supervisor or HA_URL/HA_TOKEN) and a mock
Home Assistant for local development (``MOCK_HA=1``)."""
from __future__ import annotations

import logging
import math
import os
import random
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

import httpx

_LOGGER = logging.getLogger(__name__)


def _to_float(v: Any) -> Optional[float]:
    try:
        f = float(v)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


class HAClient:
    """Thin async wrapper around the Home Assistant REST API."""

    mock = False

    def __init__(self, base_url: str, token: str, mode: str):
        self.base_url = base_url.rstrip("/")
        self.mode = mode  # "supervisor" | "direct"
        self.tz = ZoneInfo(os.environ.get("TZ", "Europe/Brussels"))
        self.connected = False
        self.last_error = ""
        self._client = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            timeout=httpx.Timeout(15.0, connect=5.0),
        )

    @classmethod
    def from_env(cls) -> "HAClient | MockHA":
        if os.environ.get("MOCK_HA", "").lower() in ("1", "true", "yes"):
            _LOGGER.warning("MOCK_HA enabled – using simulated Home Assistant")
            return MockHA()
        sup = os.environ.get("SUPERVISOR_TOKEN")
        if sup:
            return cls("http://supervisor/core/api", sup, "supervisor")
        url, tok = os.environ.get("HA_URL"), os.environ.get("HA_TOKEN")
        if url and tok:
            return cls(url.rstrip("/") + "/api", tok, "direct")
        _LOGGER.warning("No SUPERVISOR_TOKEN or HA_URL/HA_TOKEN – falling back to MOCK_HA")
        return MockHA()

    async def start(self) -> None:
        try:
            cfg = await self._get("/config")
            if cfg.get("time_zone"):
                self.tz = ZoneInfo(cfg["time_zone"])
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Could not read HA config: %s", err)

    async def close(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, params: dict | None = None) -> Any:
        try:
            r = await self._client.get(self.base_url + path, params=params)
            r.raise_for_status()
            self.connected, self.last_error = True, ""
            return r.json()
        except Exception as err:
            self.connected, self.last_error = False, str(err)
            raise

    async def _post(self, path: str, data: dict | None = None, params: dict | None = None) -> Any:
        try:
            r = await self._client.post(self.base_url + path, json=data or {}, params=params)
            r.raise_for_status()
            self.connected, self.last_error = True, ""
            ctype = r.headers.get("content-type", "")
            return r.json() if "json" in ctype else r.text
        except Exception as err:
            self.connected, self.last_error = False, str(err)
            raise

    async def get_states(self) -> list[dict]:
        return await self._get("/states")

    async def get_state(self, entity_id: str) -> Optional[dict]:
        if not entity_id:
            return None
        try:
            return await self._get(f"/states/{entity_id}")
        except httpx.HTTPStatusError as err:
            if err.response.status_code == 404:
                self.connected = True
                return None
            raise

    async def call_service(self, domain: str, service: str, data: dict, return_response: bool = False) -> Any:
        params = {"return_response": "true"} if return_response else None
        return await self._post(f"/services/{domain}/{service}", data, params)

    async def render_template(self, template: str) -> str:
        return str(await self._post("/template", {"template": template}))

    async def get_history(self, entity_id: str, start: datetime, end: Optional[datetime] = None) -> list[tuple[datetime, float]]:
        params = {"filter_entity_id": entity_id, "minimal_response": "", "no_attributes": ""}
        if end:
            params["end_time"] = end.isoformat()
        data = await self._get(f"/history/period/{start.isoformat()}", params)
        out = []
        for series in data or []:
            for item in series:
                v = _to_float(item.get("state"))
                ts = item.get("last_changed") or item.get("last_updated")
                if v is None or not ts:
                    continue
                out.append((datetime.fromisoformat(ts.replace("Z", "+00:00")), v))
        return out


# =========================================================================== mock
class MockHA:
    """Simulated Home Assistant with a Marstek Venus E, HomeWizard P1 meter and
    a Nord Pool sensor – used for local development and UI testing."""

    mock = True
    mode = "mock"

    BAT = "hame_energy_hmg_50_009b08782bb2"
    P1 = "p1_meter_5c2faf013360"

    def __init__(self):
        self.tz = ZoneInfo(os.environ.get("TZ", "Europe/Brussels"))
        self.connected = True
        self.last_error = ""
        self.capacity_wh = 5120.0
        self.soc = 55.0
        self.last_step = datetime.now(timezone.utc)
        self.quarter_energy = 0.0
        self.quarter_covered = 0.0
        self.quarter_start = self._quarter(self.last_step)
        self.avg_demand = 0.0
        self.month_peak = 2210.0
        self.batt_power = 0.0  # + discharge, - charge (AC side)
        self.load = 400.0
        self.p1 = 400.0
        self.calls: list[dict] = []
        self.values: dict[str, Any] = {
            f"select.{self.BAT}_working_mode": "Manual",
            f"switch.{self.BAT}_time_period_0_enabled": "off",
            f"switch.{self.BAT}_time_period_1_enabled": "on",
            f"number.{self.BAT}_time_period_0_power": -1500.0,
            f"number.{self.BAT}_time_period_1_power": 0.0,
        }
        self._rng = random.Random(42)

    async def start(self) -> None:
        return None

    async def close(self) -> None:
        return None

    @staticmethod
    def _quarter(ts: datetime) -> datetime:
        return ts.replace(minute=ts.minute - ts.minute % 15, second=0, microsecond=0)

    def house_load(self, ts: datetime) -> float:
        h = ts.astimezone(self.tz).hour + ts.astimezone(self.tz).minute / 60
        base = 280 + 250 * math.exp(-((h - 7.5) ** 2) / 1.5) + 900 * math.exp(-((h - 18.8) ** 2) / 2.2)
        base += 150 * math.exp(-((h - 12.5) ** 2) / 3)
        return base

    def spot(self, ts: datetime) -> float:
        """Synthetic Belgian day-ahead curve in EUR/MWh (duck curve)."""
        loc = ts.astimezone(self.tz)
        h = loc.hour + loc.minute / 60
        day = loc.timetuple().tm_yday
        p = 85 + 55 * math.exp(-((h - 8) ** 2) / 2.5) + 95 * math.exp(-((h - 19) ** 2) / 3)
        p -= 70 * math.exp(-((h - 13.5) ** 2) / 4) + 35 * math.exp(-((h - 3.5) ** 2) / 4)
        p += 12 * math.sin(day + h)
        return round(p, 2)

    def _step(self) -> None:
        now = datetime.now(timezone.utc)
        dt_h = max(0.0, (now - self.last_step).total_seconds() / 3600)
        self.last_step = now
        self.load = self.house_load(now) + self._rng.uniform(-60, 60)
        mode = self.values[f"select.{self.BAT}_working_mode"]
        p = 0.0
        if mode == "Auto":
            p = min(self.load, 800.0) if self.soc > 10 else 0.0
        elif self.values[f"switch.{self.BAT}_time_period_0_enabled"] == "on":
            p = float(self.values[f"number.{self.BAT}_time_period_0_power"])
        elif self.values[f"switch.{self.BAT}_time_period_1_enabled"] == "on":
            p = float(self.values[f"number.{self.BAT}_time_period_1_power"])
        # Marstek convention: negative = charging
        if p < 0 and self.soc >= 100:
            p = 0.0
        if p > 0 and self.soc <= 0:
            p = 0.0
        self.batt_power = p
        eff = 0.9
        delta_wh = (-p * eff if p < 0 else -p / eff) * dt_h
        self.soc = min(100.0, max(0.0, self.soc + delta_wh / self.capacity_wh * 100))
        self.p1 = self.load - p
        q = self._quarter(now)
        if q != self.quarter_start:
            finished = self.quarter_energy / max(self.quarter_covered, 1e-6) if self.quarter_energy else 0
            self.month_peak = max(self.month_peak, finished)
            self.quarter_start, self.quarter_energy, self.quarter_covered = q, 0.0, 0.0
        self.quarter_energy += max(self.p1, 0) * dt_h
        self.quarter_covered += dt_h
        self.avg_demand = self.quarter_energy / self.quarter_covered if self.quarter_covered > 0 else max(self.p1, 0)

    def _st(self, eid: str, state: Any, name: str, unit: str | None = None, **attrs) -> dict:
        a = {"friendly_name": name, **attrs}
        if unit:
            a["unit_of_measurement"] = unit
        return {"entity_id": eid, "state": str(state), "attributes": a,
                "last_changed": self.last_step.isoformat(), "last_updated": self.last_step.isoformat()}

    def _raw_prices(self, day_offset: int) -> list[dict]:
        loc_mid = datetime.now(self.tz).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=day_offset)
        out = []
        for i in range(96):
            s = loc_mid + timedelta(minutes=15 * i)
            out.append({"start": s.isoformat(), "end": (s + timedelta(minutes=15)).isoformat(),
                        "value": round(self.spot(s) / 1000, 5)})
        return out

    async def get_states(self) -> list[dict]:
        self._step()
        b, p = self.BAT, self.P1
        now_loc = datetime.now(self.tz)
        tomorrow_valid = now_loc.hour >= 13
        sts = [
            self._st(f"sensor.{b}_battery_state_of_charge", round(self.soc, 1), "Venus Battery SoC", "%", device_class="battery"),
            self._st(f"sensor.{b}_battery_capacity_wh", round(self.soc / 100 * self.capacity_wh), "Venus Battery Capacity", "Wh"),
            self._st(f"sensor.{b}_ac_power", round(self.batt_power), "Venus AC Power", "W", device_class="power"),
            self._st(f"select.{b}_working_mode", self.values[f"select.{b}_working_mode"], "Venus Working Mode",
                     options=["Auto", "Manual", "AI", "Trading"]),
            self._st(f"switch.{b}_time_period_0_enabled", self.values[f"switch.{b}_time_period_0_enabled"], "Venus Time Period 0 Enabled"),
            self._st(f"switch.{b}_time_period_1_enabled", self.values[f"switch.{b}_time_period_1_enabled"], "Venus Time Period 1 Enabled"),
            self._st(f"number.{b}_time_period_0_power", self.values[f"number.{b}_time_period_0_power"], "Venus Time Period 0 Power", "W", min=-2500, max=2500),
            self._st(f"number.{b}_time_period_1_power", self.values[f"number.{b}_time_period_1_power"], "Venus Time Period 1 Power", "W", min=-2500, max=2500),
            self._st(f"sensor.{p}_power", round(self.p1), "P1 Meter Power", "W", device_class="power"),
            self._st(f"sensor.{p}_power_phase_1", round(self.p1), "P1 Meter Power phase 1", "W", device_class="power"),
            self._st(f"sensor.{p}_average_demand", round(self.avg_demand), "P1 Meter Average demand", "W", device_class="power"),
            self._st(f"sensor.{p}_peak_demand_current_month", round(self.month_peak), "P1 Meter Peak demand current month", "W"),
            self._st("sensor.nord_pool_be_current_price", round(self.spot(datetime.now(timezone.utc)) / 1000, 5),
                     "Nord Pool BE Current price", "EUR/kWh",
                     raw_today=self._raw_prices(0), raw_tomorrow=self._raw_prices(1) if tomorrow_valid else [],
                     tomorrow_valid=tomorrow_valid),
            self._st("sensor.living_room_temperature", 20.5, "Living room temperature", "°C"),
        ]
        return sts

    async def get_state(self, entity_id: str) -> Optional[dict]:
        for st in await self.get_states():
            if st["entity_id"] == entity_id:
                return st
        return None

    async def call_service(self, domain: str, service: str, data: dict, return_response: bool = False) -> Any:
        self._step()
        eid = data.get("entity_id")
        self.calls.append({"ts": datetime.now(timezone.utc).isoformat(), "service": f"{domain}.{service}", **data})
        self.calls = self.calls[-200:]
        if domain == "select" and service == "select_option":
            self.values[eid] = data["option"]
        elif domain == "switch":
            self.values[eid] = "on" if service == "turn_on" else "off"
            if service == "turn_on" and "time_period" in (eid or ""):
                self.values[f"select.{self.BAT}_working_mode"] = "Manual"
        elif domain == "number" and service == "set_value":
            self.values[eid] = float(data["value"])
        elif domain == "nordpool":
            raise httpx.HTTPError("nordpool integration not available in mock")
        return []

    async def render_template(self, template: str) -> str:
        return "None"

    async def get_history(self, entity_id: str, start: datetime, end: Optional[datetime] = None) -> list[tuple[datetime, float]]:
        end = end or datetime.now(timezone.utc)
        out, t = [], start
        rng = random.Random(7)
        while t < end:
            out.append((t, self.house_load(t) + rng.uniform(-80, 80)))
            t += timedelta(minutes=5)
        return out
