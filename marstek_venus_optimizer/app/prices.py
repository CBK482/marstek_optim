"""Price ingestion (Nord Pool sensor / Nord Pool service / Energy-Charts) and
Belgian retail tariff formulas."""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

import httpx
import numpy as np

_LOGGER = logging.getLogger(__name__)

SLOT = timedelta(minutes=15)
ENERGY_CHARTS_URL = "https://api.energy-charts.info/price"


# --------------------------------------------------------------------------- tariffs
def import_price(spot, tariff) -> np.ndarray:
    """All-in import price (EUR/kWh) from spot (EUR/kWh)."""
    spot = np.asarray(spot, dtype=float)
    energy = spot * tariff.supplier_multiplier + tariff.supplier_margin_eur_kwh
    return (energy + tariff.grid_fee_eur_kwh + tariff.excise_levies_eur_kwh) * (1 + tariff.vat_pct / 100.0)


def export_price(spot, tariff) -> np.ndarray:
    """Injection price (EUR/kWh) from spot (EUR/kWh)."""
    spot = np.asarray(spot, dtype=float)
    return spot * tariff.injection_multiplier - tariff.injection_fee_eur_kwh


# --------------------------------------------------------------------------- helpers
def floor_slot(ts: datetime) -> datetime:
    ts = ts.astimezone(timezone.utc)
    return ts.replace(minute=ts.minute - ts.minute % 15, second=0, microsecond=0)


def _parse_ts(v: Any) -> Optional[datetime]:
    if isinstance(v, datetime):
        dt = v
    elif isinstance(v, (int, float)):
        dt = datetime.fromtimestamp(v, tz=timezone.utc)
    elif isinstance(v, str):
        try:
            dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def to_slots(points: list[tuple[datetime, Optional[datetime], float]]) -> dict[datetime, float]:
    """Expand (start, end, price €/kWh) points to a 15-min UTC grid."""
    out: dict[datetime, float] = {}
    points = sorted(points, key=lambda p: p[0])
    for i, (start, end, price) in enumerate(points):
        if price is None or (isinstance(price, float) and np.isnan(price)):
            continue
        if end is None:
            end = points[i + 1][0] if i + 1 < len(points) else start + timedelta(hours=1)
            if end - start > timedelta(hours=1):
                end = start + timedelta(hours=1)
        t = floor_slot(start)
        while t < end:
            out[t] = float(price)
            t += SLOT
    return out


def _scale(values: list[float], unit_hint: str = "") -> float:
    u = (unit_hint or "").lower()
    if "mwh" in u:
        return 1 / 1000.0
    if "kwh" in u and ("cent" in u or "ct" in u):
        return 1 / 100.0
    if "kwh" in u:
        return 1.0
    finite = [abs(v) for v in values if v is not None]
    # heuristic: day-ahead prices > 5 are almost certainly EUR/MWh
    return 1 / 1000.0 if finite and np.median(finite) > 5 else 1.0


_LIST_ATTRS = ("raw_today", "raw_tomorrow", "prices_today", "prices_tomorrow", "prices", "data", "forecast")
_START_KEYS = ("start", "time", "start_time", "startsAt", "from", "hour")
_END_KEYS = ("end", "end_time", "endsAt", "till", "to")
_VAL_KEYS = ("value", "price", "price_per_kwh", "total", "price_ct_per_kwh")


def parse_sensor_attributes(state: dict) -> dict[datetime, float]:
    """Parse HACS nordpool / entsoe-style list attributes into a 15-min grid."""
    attrs = state.get("attributes", {}) or {}
    unit = str(attrs.get("unit_of_measurement") or attrs.get("unit") or "")
    if attrs.get("price_in_cents"):
        unit = "ct/kWh"
    raw: list[tuple[datetime, Optional[datetime], float]] = []
    for key in _LIST_ATTRS:
        items = attrs.get(key)
        if not isinstance(items, list):
            continue
        for it in items:
            if not isinstance(it, dict):
                continue
            start = next((_parse_ts(it[k]) for k in _START_KEYS if k in it), None)
            end = next((_parse_ts(it[k]) for k in _END_KEYS if k in it), None)
            val = next((it[k] for k in _VAL_KEYS if k in it and it[k] is not None), None)
            if start is None or val is None:
                continue
            try:
                raw.append((start, end, float(val)))
            except (TypeError, ValueError):
                continue
    if not raw:
        return {}
    f = _scale([r[2] for r in raw], unit)
    return to_slots([(s, e, v * f) for s, e, v in raw])


def parse_nordpool_service(resp: dict, area: str) -> dict[datetime, float]:
    """Parse ``nordpool.get_prices_for_date`` response (prices in EUR/MWh)."""
    data = resp.get("service_response", resp) or {}
    items = data.get(area)
    if not isinstance(items, list):
        # Fallback if areas are keyed differently or nested in prices / entries
        for k, v in data.items():
            if isinstance(v, list) and v and isinstance(v[0], dict) and "price" in v[0]:
                items = v
                break
            if isinstance(v, dict) and area in v and isinstance(v[area], list):
                items = v[area]
                break
    items = items or []
    pts = []
    for it in items:
        if not isinstance(it, dict):
            continue
        s = _parse_ts(it.get("start") or it.get("startsAt") or it.get("time"))
        e = _parse_ts(it.get("end") or it.get("endsAt"))
        price = it.get("price") if it.get("price") is not None else it.get("value")
        if s is not None and price is not None:
            # Nord Pool integration returns EUR/MWh for get_prices_for_date
            pts.append((s, e, float(price) / 1000.0))
    return to_slots(pts)


def parse_energy_charts(data: dict) -> dict[datetime, float]:
    secs = data.get("unix_seconds") or []
    prices = data.get("price") or []
    f = _scale(prices, str(data.get("unit", "EUR/MWh")))
    pts = [(datetime.fromtimestamp(s, tz=timezone.utc), None, p * f)
           for s, p in zip(secs, prices) if p is not None]
    return to_slots(pts)


async def fetch_energy_charts(area: str, day_from: date, day_to: date) -> dict[datetime, float]:
    params = {"bzn": area, "start": day_from.isoformat(), "end": day_to.isoformat()}
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(ENERGY_CHARTS_URL, params=params)
        r.raise_for_status()
        return parse_energy_charts(r.json())


class PriceService:
    """Collects spot prices from the configured source chain and caches them."""

    def __init__(self, ha):
        self.ha = ha
        self.cache: dict[datetime, float] = {}
        self.source: str = "none"
        self.last_fetch: Optional[datetime] = None
        self.last_error: str = ""
        self._nordpool_entry: Optional[str] = None

    def _needs_refresh(self, now: datetime) -> bool:
        if not self.last_fetch or not self.cache:
            return True
        age = now - self.last_fetch
        latest = max(self.cache)
        # Check how far into the future our price horizon currently extends
        horizon_remaining = latest - now
        loc_hour = now.astimezone(self.ha.tz).hour
        loc_minute = now.astimezone(self.ha.tz).minute
        
        # Nord Pool day-ahead auction publishes ~12:45 - 13:00 CET.
        # If we only have today's prices (horizon < 18h) and it's after 12:45 local time,
        # poll on every solver loop (every 5 min) until tomorrow's prices arrive.
        is_after_noon = (loc_hour > 12) or (loc_hour == 12 and loc_minute >= 45)
        if horizon_remaining < timedelta(hours=18) and is_after_noon:
            return age >= timedelta(minutes=5)
            
        # If horizon has less than 8 hours left at any other time, poll every 10 min
        if horizon_remaining < timedelta(hours=8):
            return age >= timedelta(minutes=10)

        # Once tomorrow's full curve is locked in (horizon > 24h), refresh hourly
        return age >= timedelta(minutes=60)

    async def get(self, cfg, now: Optional[datetime] = None, force: bool = False) -> dict[datetime, float]:
        now = now or datetime.now(timezone.utc)
        if not force and not self._needs_refresh(now):
            return self.cache
        t = cfg.tariff
        order = [t.price_source] if t.price_source != "auto" else ["sensor", "nordpool_service", "energy_charts"]
        errors = []
        for src in order:
            try:
                prices = await getattr(self, f"_from_{src}")(cfg, now)
            except Exception as err:  # noqa: BLE001
                errors.append(f"{src}: {err}")
                continue
            future = {k: v for k, v in prices.items() if k >= floor_slot(now)}
            if len(future) >= 4:
                # keep a bit of history for the chart / debugging
                self.cache = {k: v for k, v in prices.items() if k >= floor_slot(now) - timedelta(hours=6)}
                self.source = src
                self.last_fetch = now
                self.last_error = "; ".join(errors)
                return self.cache
            errors.append(f"{src}: no future prices")
        self.last_error = "; ".join(errors)
        _LOGGER.warning("Price fetch failed: %s", self.last_error)
        return {k: v for k, v in self.cache.items() if k >= floor_slot(now)}

    async def _from_sensor(self, cfg, now):
        ent = cfg.entities.price
        if not ent:
            raise ValueError("no price entity configured")
        st = await self.ha.get_state(ent)
        if not st:
            raise ValueError(f"{ent} not found")
        return parse_sensor_attributes(st)

    async def _from_nordpool_service(self, cfg, now):
        if not self._nordpool_entry:
            ent = cfg.entities.price
            # Try to resolve via template with device/entity or config_entries('nordpool')
            tpl = (
                f"{{% set e = '{ent}' %}}"
                f"{{{{ config_entry_id(e) if e else '' }}}}"
            )
            entry = await self.ha.render_template(tpl) if ent else ""
            entry = (entry or "").strip()
            if not entry or entry == "None":
                # Try finding any nordpool config entry directly via template
                entry = await self.ha.render_template("{{ (config_entries('nordpool') | first) if config_entries('nordpool') else '' }}")
                entry = (entry or "").strip()
            if entry and entry != "None":
                self._nordpool_entry = entry

        out: dict[datetime, float] = {}
        area = cfg.tariff.price_area or "BE"
        local_today = now.astimezone(self.ha.tz).date()
        for d in (local_today, local_today + timedelta(days=1)):
            try:
                payload: dict[str, Any] = {
                    "date": d.isoformat(),
                    "areas": [area] if isinstance(area, str) else area,
                    "currency": "EUR"
                }
                if self._nordpool_entry and self._nordpool_entry != "None":
                    payload["config_entry"] = self._nordpool_entry

                resp = await self.ha.call_service(
                    "nordpool", "get_prices_for_date",
                    payload,
                    return_response=True)
                out.update(parse_nordpool_service(resp or {}, area))
            except Exception as err:  # noqa: BLE001 - tomorrow not yet published or call failed
                _LOGGER.debug("nordpool action for %s failed: %s", d, err)
        return out

    async def _from_energy_charts(self, cfg, now):
        local_today = now.astimezone(self.ha.tz).date()
        return await fetch_energy_charts(cfg.tariff.price_area, local_today, local_today + timedelta(days=1))
