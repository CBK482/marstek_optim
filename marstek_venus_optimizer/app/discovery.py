"""Auto-discovery of Marstek/HAME batteries, P1 meters and price sensors."""
from __future__ import annotations

import re
from typing import Any

# role -> (domain, suffix regex used for device-prefixed matching)
BATTERY_ROLES: dict[str, tuple[str, str]] = {
    "soc": ("sensor", r"battery_state_of_charge|battery_soc|state_of_charge"),
    "capacity_wh": ("sensor", r"battery_capacity_wh|battery_remaining_capacity|battery_capacity"),
    "battery_power": ("sensor", r"combined_power|ac_power|battery_power|power"),
    "working_mode": ("select", r"working_mode|user_work_mode"),
    "tp0_enabled": ("switch", r"time_period_0_enabled"),
    "tp0_power": ("number", r"time_period_0_power"),
    "tp1_enabled": ("switch", r"time_period_1_enabled"),
    "tp1_power": ("number", r"time_period_1_power"),
}
BATTERY_PREFIX = re.compile(r"^(?:sensor|select|switch|number)\.((?:hame_energy|marstek)[a-z0-9_]*?)_"
                            r"(?:battery_state_of_charge|working_mode|time_period_0_enabled)$")

P1_POWER = re.compile(r"^sensor\.([a-z0-9_]*p1_meter[a-z0-9_]*?)_power$")
P1_AVG = re.compile(r"^sensor\.[a-z0-9_]*p1_meter[a-z0-9_]*_average_demand$")
P1_PEAK = re.compile(r"^sensor\.[a-z0-9_]*p1_meter[a-z0-9_]*peak_demand_current_month$")
PRICE = re.compile(r"^sensor\.(nord_pool_be[a-z0-9_]*|nordpool[a-z0-9_]*|entsoe[a-z0-9_]*)$")
PRESENCE = re.compile(r"^(?:input_select|binary_sensor|input_boolean|person)\.([a-z0-9_]*(?:presence|occupancy|away|holiday|vacation)[a-z0-9_]*)$")

ROLE_DOMAINS = {
    **{k: v[0] for k, v in BATTERY_ROLES.items()},
    "p1_power": "sensor",
    "p1_avg_demand": "sensor",
    "p1_month_peak": "sensor",
    "price": "sensor",
    "presence": "input_select",
}
ROLE_HINTS = {
    "soc": r"state_of_charge|soc",
    "capacity_wh": r"capacity|_wh",
    "battery_power": r"power",
    "working_mode": r"mode",
    "tp0_enabled": r"time_period_0",
    "tp0_power": r"time_period_0",
    "tp1_enabled": r"time_period_1",
    "tp1_power": r"time_period_1",
    "p1_power": r"p1|power",
    "p1_avg_demand": r"average_demand|demand",
    "p1_month_peak": r"peak",
    "price": r"nord_?pool|price|entsoe",
    "presence": r"presence|occupancy|away|vacation",
}



def _brief(st: dict) -> dict[str, Any]:
    a = st.get("attributes", {}) or {}
    return {
        "entity_id": st["entity_id"],
        "name": a.get("friendly_name", st["entity_id"]),
        "state": st.get("state"),
        "unit": a.get("unit_of_measurement", ""),
    }


def discover(states: list[dict]) -> dict[str, Any]:
    ids = {s["entity_id"]: s for s in states}

    # ---- batteries
    prefixes: list[str] = []
    for eid in ids:
        m = BATTERY_PREFIX.match(eid)
        if m and m.group(1) not in prefixes:
            prefixes.append(m.group(1))
    batteries = []
    for pre in sorted(prefixes):
        ents: dict[str, str] = {}
        for role, (domain, suffix) in BATTERY_ROLES.items():
            pat = re.compile(rf"^{domain}\.{re.escape(pre)}_(?:{suffix})$")
            cand = sorted(e for e in ids if pat.match(e))
            # prefer the most specific (first alternative in suffix list)
            for alt in suffix.split("|"):
                exact = f"{domain}.{pre}_{alt}"
                if exact in ids:
                    ents[role] = exact
                    break
            else:
                if cand:
                    ents[role] = cand[0]
        name = ids.get(ents.get("soc", ""), {}).get("attributes", {}).get("friendly_name", pre)
        batteries.append({"device": pre, "name": name, "entities": ents,
                          "complete": all(r in ents for r in ("soc", "working_mode", "tp0_enabled", "tp0_power",
                                                               "tp1_enabled", "tp1_power"))})

    # ---- P1 meters
    meters = []
    for eid in sorted(ids):
        m = P1_POWER.match(eid)
        if not m:
            continue
        pre = m.group(1)
        meters.append({
            "device": pre,
            "entities": {
                "p1_power": eid,
                "p1_avg_demand": next((e for e in sorted(ids) if e.startswith(f"sensor.{pre}") and P1_AVG.match(e)), ""),
                "p1_month_peak": next((e for e in sorted(ids) if e.startswith(f"sensor.{pre}") and P1_PEAK.match(e)), ""),
            },
        })

    # ---- price sensors (prefer *current_price / entities with list attributes)
    def price_score(eid: str) -> int:
        a = ids[eid].get("attributes", {}) or {}
        s = 0
        if "current_price" in eid:
            s += 2
        if any(k in a for k in ("raw_today", "prices_today", "prices")):
            s += 3
        if "_be" in eid:
            s += 1
        return -s
    prices = sorted((e for e in ids if PRICE.match(e)), key=lambda e: (price_score(e), e))

    # ---- candidate lists per role (for datalists in the UI)
    candidates: dict[str, list] = {}
    for role, domain in ROLE_DOMAINS.items():
        hint = re.compile(ROLE_HINTS[role])
        lst = [_brief(s) for eid, s in ids.items() if eid.startswith(domain + ".")]
        lst.sort(key=lambda b: (not hint.search(b["entity_id"]), b["entity_id"]))
        candidates[role] = lst[:400]

    suggestion: dict[str, str] = {}
    if batteries:
        best = next((b for b in batteries if b["complete"]), batteries[0])
        suggestion.update(best["entities"])
        suggestion["battery_device"] = best["device"]
    if meters:
        suggestion.update({k: v for k, v in meters[0]["entities"].items() if v})
    if prices:
        suggestion["price"] = prices[0]
    pres = sorted(e for e in ids if PRESENCE.match(e))
    if pres:
        suggestion["presence"] = pres[0]

    return {"batteries": batteries, "meters": meters, "prices": prices,

            "candidates": candidates, "suggestion": suggestion}
