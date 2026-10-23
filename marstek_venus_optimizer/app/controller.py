"""Real-time control loop: optimization (every N minutes), 15-second safety &
kwartierpiek watchdog and command application to the Marstek battery."""
from __future__ import annotations

import asyncio
import logging
import math
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import numpy as np

from . import prices as price_mod
from .forecast import forecast_load
from .ha_client import _to_float
from .optimizer import OptimizationResult, OptimizerInput, OptimizerParams, optimize
from .settings import AppConfig, load_config, save_config
from .state import StateStore

_LOGGER = logging.getLogger(__name__)
SLOT = timedelta(minutes=15)


@dataclass
class Command:
    kind: str  # charge | idle | discharge_auto | discharge_manual
    power_w: float = 0.0  # magnitude (W)

    @property
    def label(self) -> str:
        return {
            "charge": f"Charge {self.power_w:.0f} W",
            "idle": "Idle / Hold 0 W",
            "discharge_auto": "Discharge (Auto CT)",
            "discharge_manual": f"Discharge {self.power_w:.0f} W",
        }.get(self.kind, self.kind)

    def differs(self, other: Optional["Command"], delta_w: float) -> bool:
        if other is None or other.kind != self.kind:
            return True
        return self.kind in ("charge", "discharge_manual") and abs(self.power_w - other.power_w) >= delta_w


class Controller:
    def __init__(self, ha, store: StateStore):
        self.ha = ha
        self.store = store
        self.cfg: AppConfig = load_config()
        self.prices = price_mod.PriceService(ha)
        self.plan: Optional[OptimizationResult] = None
        self.live: dict[str, Any] = {}
        self.planned: Optional[Command] = None
        self.effective: Optional[Command] = None
        self.applied: Optional[Command] = None
        self.reason = ""
        self.flags: dict[str, Any] = {"throttled": False, "shaving": False, "safety": ""}
        self.logs: deque = deque(maxlen=300)
        self.peak_target_w = self.cfg.tariff.peak_limit_w
        self.last_solve: Optional[datetime] = None
        self.last_error = ""
        self._solve_lock = asyncio.Lock()
        self._apply_lock = asyncio.Lock()
        self._q_start: Optional[datetime] = None
        self._q_energy_wh = 0.0
        self._q_covered_h = 0.0
        self._q_last: Optional[tuple[datetime, float]] = None
        self._shave_quarter: Optional[datetime] = None
        self._history: list[tuple[datetime, float]] = []
        self._states: dict[str, dict] = {}
        self.cmd_delay_s: float = 1.2
        self.verify_delay_s: float = 3.5
        self._last_p1_updated: Optional[str] = None
        self._p1_fresh: bool = True
        self._last_apply_ts: Optional[datetime] = None

    # ------------------------------------------------------------------ logging
    def log(self, msg: str, level: str = "info") -> None:
        getattr(_LOGGER, level if level != "action" else "info")(msg)
        self.logs.appendleft({"ts": datetime.now(timezone.utc).isoformat(), "level": level, "msg": msg})

    @property
    def mode(self) -> str:
        return self.cfg.control.mode

    @property
    def dry_run(self) -> bool:
        return self.mode == "shadow"

    def set_config(self, cfg: AppConfig, persist: bool = True) -> None:
        self.cfg = cfg
        if persist:
            save_config(cfg)

    async def set_mode(self, mode: str) -> None:
        self.cfg.control.mode = mode  # type: ignore[assignment]
        save_config(self.cfg)
        self.applied = None  # force re-apply under the new mode
        self.log(f"Operating mode → {mode}")
        await self.watchdog()

    # ------------------------------------------------------------------ live data
    async def refresh_live(self) -> None:
        e = self.cfg.entities
        try:
            states = await self.ha.get_states()
        except Exception as err:  # noqa: BLE001
            self.last_error = f"HA unreachable: {err}"
            self.live["connected"] = False
            return
        self._states = {s["entity_id"]: s for s in states}

        def val(eid: str) -> Optional[float]:
            st = self._states.get(eid) if eid else None
            return _to_float(st["state"]) if st else None

        def raw(eid: str) -> Optional[str]:
            st = self._states.get(eid) if eid else None
            return st["state"] if st else None

        now = datetime.now(timezone.utc)
        soc = val(e.soc)
        p1 = val(e.p1_power)
        batt_raw = val(e.battery_power)
        mode_state = raw(e.working_mode)
        tp0_on, tp1_on = raw(e.tp0_enabled), raw(e.tp1_enabled)
        tp0_p, tp1_p = val(e.tp0_power), val(e.tp1_power)

        p1_st = self._states.get(e.p1_power) if e.p1_power else None
        p1_updated = p1_st.get("last_updated") if p1_st else None
        if p1_updated and self._last_p1_updated:
            self._p1_fresh = (p1_updated != self._last_p1_updated)
        else:
            self._p1_fresh = True
        if p1_updated:
            self._last_p1_updated = p1_updated

        # battery AC power, + = charging (grid side)
        if batt_raw is not None:
            batt_ch = -batt_raw if self.cfg.battery.battery_power_positive == "discharge" else batt_raw
        elif mode_state == self.cfg.battery.mode_option_manual:
            p = tp0_p if tp0_on == "on" else tp1_p if tp1_on == "on" else 0.0
            batt_ch = -(p or 0.0)  # Marstek: negative period power = charging
            if soc is not None and ((batt_ch > 0 and soc >= 100) or (batt_ch < 0 and soc <= 0)):
                batt_ch = 0.0
        else:
            batt_ch = None  # Auto/CT: unknown without a power sensor
        # House load: grid import P1 = house + batt_charge => house = P1 - batt_charge
        house = max(0.0, p1 - batt_ch) if (p1 is not None and batt_ch is not None) else (max(0.0, p1) if p1 is not None else None)

        # own running quarter-hour average of grid import
        q = price_mod.floor_slot(now)
        if self._q_start != q:
            if self._q_start is not None and self._q_covered_h > 5 / 60:
                finished = self._q_energy_wh / self._q_covered_h
                self.store.update_peak(self._q_start.astimezone(self.ha.tz).strftime("%Y-%m"), finished)
            self._q_start, self._q_energy_wh, self._q_last = q, 0.0, None
            self._q_covered_h = 0.0
        if p1 is not None:
            if self._q_last is not None:
                dt_h = (now - self._q_last[0]).total_seconds() / 3600
                self._q_energy_wh += max(self._q_last[1], 0) * dt_h
                self._q_covered_h += dt_h
            self._q_last = (now, p1)
        # average over the part of the quarter we actually observed
        own_avg = self._q_energy_wh / self._q_covered_h if self._q_covered_h > 30 / 3600 else None
        avg = val(e.p1_avg_demand)
        if avg is None:
            avg = own_avg

        month = now.astimezone(self.ha.tz).strftime("%Y-%m")
        sensor_peak = val(e.p1_month_peak)
        stored_peak = self.store.get_peak(month)
        month_peak = max(x for x in (sensor_peak, stored_peak, 0.0) if x is not None)
        self.peak_target_w = max(self.cfg.tariff.peak_limit_w, month_peak)

        cap_wh = val(e.capacity_wh)
        self.live = {
            "connected": self.ha.connected,
            "ts": now.isoformat(),
            "soc": soc,
            "soc_wh": cap_wh if cap_wh is not None else (soc / 100 * self.cfg.battery.capacity_wh if soc is not None else None),
            "p1_w": p1,
            "avg_demand_w": avg,
            "own_avg_w": own_avg,
            "batt_charge_w": batt_ch,
            "house_w": house,
            "month_peak_w": month_peak,
            "peak_target_w": self.peak_target_w,
            "working_mode": mode_state,
            "tp0_enabled": tp0_on, "tp0_power": tp0_p,
            "tp1_enabled": tp1_on, "tp1_power": tp1_p,
            "quarter_fraction": min(1.0, (now - q) / SLOT),
        }
        self.last_error = "" if self.ha.connected else self.ha.last_error
        try:
            self.store.add_sample(now, soc, p1, avg, batt_ch, house, self.effective.kind if self.effective else "")
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("sample store failed: %s", err)

    async def bootstrap_history(self) -> None:
        """Seed the load forecast from HA recorder history (last 7 days)."""
        since = datetime.now(timezone.utc) - timedelta(days=7)
        if len(self.store.house_samples(datetime.now(timezone.utc) - timedelta(days=2))) > 2000:
            return
        try:
            p1 = await self.ha.get_history(self.cfg.entities.p1_power, since)
            self._history = p1
            self.log(f"Bootstrapped load forecast from {len(p1)} P1 history points")
        except Exception as err:  # noqa: BLE001
            self.log(f"History bootstrap failed: {err}", "warning")

    # ------------------------------------------------------------------ optimization
    async def run_optimization(self, apply: bool = True, cfg: Optional[AppConfig] = None,
                               force_prices: bool = False) -> OptimizationResult:
        cfg = cfg or self.cfg
        async with self._solve_lock:
            await self.refresh_live()
            now = datetime.now(timezone.utc)
            try:
                spot_map = await self.prices.get(cfg, now, force=force_prices)
            except Exception as err:  # noqa: BLE001
                spot_map = {}
                self.log(f"Price fetch error: {err}", "error")
            start = price_mod.floor_slot(now)
            end = start + timedelta(hours=cfg.control.horizon_hours)
            slots, t = [], start
            while t < end and t in spot_map:
                slots.append(t)
                t += SLOT
            if len(slots) < 4:
                res = OptimizationResult(ok=False, status="no_prices",
                                         message=f"Only {len(slots)} future price slots available "
                                                 f"({self.prices.last_error or 'no data'})")
                self.log(res.message, "warning")
                if cfg is self.cfg:
                    self.plan = res if self.plan is None or not self.plan.ok else self.plan
                return res

            spot = np.array([spot_map[s] for s in slots])
            imp = price_mod.import_price(spot, cfg.tariff)
            exp = price_mod.export_price(spot, cfg.tariff)
            samples = self._history + self.store.house_samples(now - timedelta(days=14))
            load = forecast_load(slots, samples, self.ha.tz, cfg.control.base_load_w, self.live.get("house_w"))
            soc = self.live.get("soc")
            if soc is None:
                soc = (cfg.battery.min_soc_pct + cfg.battery.max_soc_pct) / 2
                self.log("SoC unavailable – optimizing with mid-range SoC", "warning")
            frac = 1.0 - (now - start) / SLOT
            inp = OptimizerInput(
                slot_start=slots, spot=spot, import_price=imp, export_price=exp, load_w=load,
                soc_pct=float(soc), first_slot_fraction=frac,
                quarter_avg_so_far_w=self.live.get("avg_demand_w"),
            )
            params = OptimizerParams.from_config(cfg, peak_limit_w=self.peak_target_w)
            res = await asyncio.to_thread(optimize, inp, params, self.ha.tz)
            if not res.ok:
                self.log(f"Optimization failed: {res.status} {res.message}", "error")
                return res
            if cfg is self.cfg:
                self.plan = res
                self.last_solve = now
                try:
                    self.store.save_plan({"savings_24h": res.savings_24h_eur, "status": res.status},
                                         res.to_dict())
                except Exception as err:  # noqa: BLE001
                    _LOGGER.debug("plan store failed: %s", err)
                s0 = res.slots[0]
                self.log(f"Solved {len(res.slots)} slots in {res.solve_ms:.0f} ms "
                         f"({'MILP' if res.milp else 'LP'}, {res.status}); 24h savings €{res.savings_24h_eur:.2f}. "
                         f"{s0.reason}")
        if apply and cfg is self.cfg:
            await self.watchdog()
        return res

    # ------------------------------------------------------------------ command selection
    def current_slot(self):
        if not self.plan or not self.plan.ok:
            return None
        now = datetime.now(timezone.utc)
        for s in self.plan.slots:
            st = datetime.fromisoformat(s.start)
            if st <= now < st + SLOT:
                return s
        return None

    def base_command(self) -> tuple[Optional[Command], str]:
        b = self.cfg.battery
        m = self.mode
        if m == "force_charge":
            return Command("charge", b.max_charge_w), "Manual override: force charge"
        if m == "hold":
            return Command("idle"), "Manual override: hold 0 W"
        if m == "auto_ct":
            return Command("discharge_auto"), "Manual override: Auto CT self-consumption"
        s = self.current_slot()
        if s is None:
            return None, "No valid plan for the current slot"
        if s.action == "charge":
            return Command("charge", round(abs(s.power_w))), s.reason
        if s.action == "discharge":
            if b.discharge_mode == "auto_ct":
                return Command("discharge_auto"), s.reason
            return Command("discharge_manual", round(s.power_w)), s.reason
        return Command("idle"), s.reason

    # ------------------------------------------------------------------ watchdog
    async def watchdog(self) -> None:
        """Runs every ~15 s: refresh live data, apply SoC cutoffs, kwartierpiek
        throttling / shaving and push the resulting command."""
        await self.refresh_live()
        cmd, reason = self.base_command()
        self.planned = cmd
        self.flags = {"throttled": False, "shaving": False, "safety": ""}
        if cmd is None:
            self.effective, self.reason = None, reason
            return
        b, c = self.cfg.battery, self.cfg.control
        soc = self.live.get("soc")
        now = datetime.now(timezone.utc)

        # --- SoC cutoffs
        if soc is not None:
            if cmd.kind == "charge" and soc >= b.max_soc_pct:
                cmd, self.flags["safety"] = Command("idle"), f"SoC {soc:.0f}% ≥ max {b.max_soc_pct:.0f}% – charge stopped"
            elif cmd.kind.startswith("discharge") and soc <= b.min_soc_pct and self.mode != "auto_ct":
                cmd, self.flags["safety"] = Command("idle"), f"SoC {soc:.0f}% ≤ min {b.min_soc_pct:.0f}% – discharge stopped"

        # --- kwartierpiek
        p1 = self.live.get("p1_w")
        if p1 is not None:
            limit = self.peak_target_w - c.peak_margin_w
            f = min(0.95, float(self.live.get("quarter_fraction") or 0.0))
            avg = self.live.get("avg_demand_w")
            allowed = limit
            if avg is not None and f > 0.05:
                allowed = (limit - f * avg) / (1 - f)
            allowed = min(allowed, self.cfg.tariff.max_grid_w - c.peak_margin_w)
            batt_ch = self.live.get("batt_charge_w")
            house = self.live.get("house_w")
            if house is None:
                house = p1 - (batt_ch or 0.0)
            self.live["allowed_import_w"] = round(allowed)

            if cmd.kind == "charge":
                target_p = cmd.power_w
                current_p = self.applied.power_w if (self.applied and self.applied.kind == "charge") else 0.0
                can_step = self._p1_fresh or (self._last_apply_ts is None or (now - self._last_apply_ts).total_seconds() >= 20.0)

                # Case 1: Initial transition into charge from zero / non-charge
                # Start at 50% of target to avoid initial overshoot from transport delay
                if current_p <= 0.0:
                    ramp_p = math.floor((target_p * 0.5) / 10) * 10
                    if house is not None:
                        ramp_p = min(ramp_p, max(0.0, allowed - house))
                    new_p = max(0.0, ramp_p)
                    self.flags["throttled"] = (new_p < target_p)
                    reason = (f"Soft-start charge (50%): {new_p:.0f} W (target {target_p:.0f} W, "
                              f"P1 {p1:.0f} W, allowed {allowed:.0f} W)")
                    cmd = Command("charge", new_p)

                # Case 2: Already charging -> closed-loop regulation
                else:
                    if p1 > allowed:
                        # Immediate back-off proportional to overshoot + buffer (prevents wild overcompensation)
                        overshoot = p1 - allowed
                        cut_w = math.ceil((overshoot + 50.0) / 10) * 10
                        new_p = max(0.0, current_p - cut_w)
                        self.flags["throttled"] = True
                        reason = (f"Kwartierpiek throttle: P1 {p1:.0f} W > allowed {allowed:.0f} W "
                                  f"(overshoot {overshoot:.0f} W) → reduce {current_p:.0f}→{new_p:.0f} W")
                        cmd = Command("charge", new_p)
                    elif current_p < target_p:
                        # Headroom exists: step up only when fresh P1 data arrives or settles
                        headroom = max(0.0, allowed - p1)
                        if can_step and headroom > 100.0:
                            # Step up smoothly by at most 50% of headroom (capped at 300 W per step)
                            step_w = min(headroom * 0.5, 300.0, target_p - current_p)
                            step_w = math.floor(step_w / 10) * 10
                            if step_w >= 50.0:
                                new_p = min(target_p, current_p + step_w)
                                self.flags["throttled"] = (new_p < target_p)
                                reason = (f"Step-up charge: {current_p:.0f}→{new_p:.0f} W "
                                          f"(P1 {p1:.0f} W, headroom {headroom:.0f} W, target {target_p:.0f} W)")
                                cmd = Command("charge", new_p)
                            else:
                                cmd = Command("charge", current_p)
                                self.flags["throttled"] = (current_p < target_p)
                        else:
                            # Hold steady while waiting for P1 meter or if headroom is small
                            cmd = Command("charge", current_p)
                            self.flags["throttled"] = (current_p < target_p)
                    else:
                        # At target and within allowed limits
                        cmd = Command("charge", target_p)

            # Shave a live peak by discharging (Auto CT / Automatic follows the house load)
            can_dis = soc is None or soc > b.min_soc_pct + 1
            in_shave = self._shave_quarter == price_mod.floor_slot(now)
            # While shaving in Automatic without a battery power sensor the house
            # load is unobservable (P1 ≈ 0) -> hold the shave until quarter end.
            keep = in_shave and (batt_ch is None or house > allowed - 2 * c.peak_margin_w)
            avg_exceeded = (avg is not None and avg >= limit)
            if (c.peak_shaving and can_dis and cmd.kind in ("idle", "charge") and self.mode != "hold"
                    and (house > allowed or avg_exceeded or keep)):
                self._shave_quarter = price_mod.floor_slot(now)
                self.flags["shaving"] = True
                if avg_exceeded:
                    reason = (f"Peak shaving: 15-min average demand {avg:.0f} W reached limit {limit:.0f} W "
                              f"(target {self.peak_target_w:.0f} W) → switched to Automatic")
                else:
                    reason = (f"Peak shaving: house load {house:.0f} W exceeds allowed import {allowed:.0f} W "
                              f"for this quarter (target {self.peak_target_w:.0f} W)")
                cmd = Command("discharge_auto")

        if self.flags["safety"]:
            reason = self.flags["safety"]
        self.effective, self.reason = cmd, reason
        if cmd.differs(self.applied, c.min_command_delta_w):
            await self.apply(cmd, reason)

    # ------------------------------------------------------------------ actuation
    def _expected_states(self, cmd: Command) -> dict[str, Any]:
        e, b = self.cfg.entities, self.cfg.battery
        expected: dict[str, Any] = {}
        if cmd.kind == "charge":
            if e.working_mode:
                expected[e.working_mode] = b.mode_option_manual
            if e.tp0_enabled:
                expected[e.tp0_enabled] = "on"
            if e.tp1_enabled:
                expected[e.tp1_enabled] = "off"
            if e.tp0_power:
                expected[e.tp0_power] = float(int(round(-abs(cmd.power_w))))
        elif cmd.kind == "idle":
            if e.working_mode:
                expected[e.working_mode] = b.mode_option_manual
            if e.tp1_enabled:
                expected[e.tp1_enabled] = "on"
            if e.tp0_enabled:
                expected[e.tp0_enabled] = "off"
            if e.tp1_power:
                expected[e.tp1_power] = 0.0
        elif cmd.kind == "discharge_manual":
            if e.working_mode:
                expected[e.working_mode] = b.mode_option_manual
            if e.tp0_enabled:
                expected[e.tp0_enabled] = "on"
            if e.tp1_enabled:
                expected[e.tp1_enabled] = "off"
            if e.tp0_power:
                expected[e.tp0_power] = float(int(round(abs(cmd.power_w))))
        elif cmd.kind == "discharge_auto":
            if e.working_mode:
                expected[e.working_mode] = b.mode_option_auto
            if e.tp0_enabled:
                expected[e.tp0_enabled] = "off"
            if e.tp1_enabled:
                expected[e.tp1_enabled] = "off"
        return expected

    def _check_mismatches(self, expected: dict[str, Any]) -> dict[str, tuple[Any, Any]]:
        mismatches: dict[str, tuple[Any, Any]] = {}
        for eid, exp in expected.items():
            st = self._states.get(eid)
            act_str = st["state"] if st else None
            if eid.startswith("number."):
                act_num = _to_float(act_str)
                if act_num is None or abs(act_num - exp) >= 1.0:
                    mismatches[eid] = (act_num, exp)
            else:
                if act_str != exp:
                    mismatches[eid] = (act_str, exp)
        return mismatches

    def _build_calls_for_mismatches(self, mismatches: dict[str, tuple[Any, Any]]) -> list[tuple[str, str, dict]]:
        def prio(item: tuple[str, tuple[Any, Any]]) -> int:
            eid, (_act, exp) = item
            if eid.startswith("switch.") and exp == "on":
                return 0
            if eid.startswith("number."):
                return 1
            if eid.startswith("switch.") and exp == "off":
                return 2
            return 3  # select

        calls: list[tuple[str, str, dict]] = []
        for eid, (_act, target) in sorted(mismatches.items(), key=prio):
            if eid.startswith("switch."):
                calls.append(("switch", "turn_on" if target == "on" else "turn_off", {"entity_id": eid}))
            elif eid.startswith("number."):
                calls.append(("number", "set_value", {"entity_id": eid, "value": int(round(target))}))
            elif eid.startswith("select."):
                calls.append(("select", "select_option", {"entity_id": eid, "option": target}))
        return calls

    async def apply(self, cmd: Command, reason: str) -> None:
        e, b = self.cfg.entities, self.cfg.battery
        dry = self.dry_run
        calls: list[tuple[str, str, dict]] = []

        def cur(eid: str) -> Optional[str]:
            st = self._states.get(eid)
            return st["state"] if st else None

        def switch(eid: str, on: bool):
            if eid and cur(eid) != ("on" if on else "off"):
                calls.append(("switch", "turn_on" if on else "turn_off", {"entity_id": eid}))

        def number(eid: str, value: float):
            if not eid:
                return
            cv = _to_float(cur(eid))
            if cv is None or abs(cv - value) >= 1:
                calls.append(("number", "set_value", {"entity_id": eid, "value": int(round(value))}))

        def select(option: str):
            if e.working_mode and cur(e.working_mode) != option:
                calls.append(("select", "select_option", {"entity_id": e.working_mode, "option": option}))

        # Hardware rule: Sending changes in a burst causes the battery to drop commands.
        # Turning the active period switch ON first automatically switches working_mode to Manual.
        # Turning the inactive period switch OFF second prevents falling into an unmanaged limbo state.
        if cmd.kind == "charge":
            switch(e.tp0_enabled, True)
            number(e.tp0_power, -abs(cmd.power_w))
            switch(e.tp1_enabled, False)
            if cur(e.tp0_enabled) == "on" and cur(e.working_mode) != b.mode_option_manual:
                select(b.mode_option_manual)
        elif cmd.kind == "idle":
            switch(e.tp1_enabled, True)
            number(e.tp1_power, 0)
            switch(e.tp0_enabled, False)
            if cur(e.tp1_enabled) == "on" and cur(e.working_mode) != b.mode_option_manual:
                select(b.mode_option_manual)
        elif cmd.kind == "discharge_manual":
            switch(e.tp0_enabled, True)
            number(e.tp0_power, abs(cmd.power_w))
            switch(e.tp1_enabled, False)
            if cur(e.tp0_enabled) == "on" and cur(e.working_mode) != b.mode_option_manual:
                select(b.mode_option_manual)
        elif cmd.kind == "discharge_auto":
            switch(e.tp0_enabled, False)
            switch(e.tp1_enabled, False)
            select(b.mode_option_auto)

        async with self._apply_lock:
            prefix = "[SHADOW] would apply" if dry else "Applying"
            svc_txt = ", ".join(f"{d}.{s}({v.get('entity_id', '').split('.')[-1][-28:]}"
                                f"{'=' + str(v.get('option', v.get('value'))) if ('option' in v or 'value' in v) else ''})"
                                for d, s, v in calls) or "no entity changes needed"
            self.log(f"{prefix} {cmd.label} — {reason} [{svc_txt}]", "action")
            ok = True
            if not dry and calls:
                for idx, (domain, service, data) in enumerate(calls):
                    if idx > 0 and self.cmd_delay_s > 0:
                        await asyncio.sleep(self.cmd_delay_s)
                    try:
                        await self.ha.call_service(domain, service, data)
                    except Exception as err:  # noqa: BLE001
                        ok = False
                        self.log(f"Service {domain}.{service} {data} failed: {err}", "error")
                        break

            # Hardware state verification with retry
            if not dry and ok and calls:
                if self.verify_delay_s > 0:
                    await asyncio.sleep(self.verify_delay_s)
                try:
                    fresh = await self.ha.get_states()
                    self._states = {s["entity_id"]: s for s in fresh}
                except Exception as err:  # noqa: BLE001
                    self.log(f"Verification fetch failed: {err}", "warning")
                    fresh = None

                if fresh:
                    expected = self._expected_states(cmd)
                    mismatches = self._check_mismatches(expected)
                    if mismatches:
                        m_str = ", ".join(
                            f"{eid.split('.')[-1]} is {act!r} (expected {exp!r})"
                            for eid, (act, exp) in mismatches.items()
                        )
                        self.log(f"Verification mismatch: {m_str}. Retrying...", "warning")
                        retries = self._build_calls_for_mismatches(mismatches)
                        for r_idx, (domain, service, data) in enumerate(retries):
                            if r_idx > 0 and self.cmd_delay_s > 0:
                                await asyncio.sleep(self.cmd_delay_s)
                            try:
                                await self.ha.call_service(domain, service, data)
                            except Exception as err:  # noqa: BLE001
                                self.log(f"Retry {domain}.{service} {data} failed: {err}", "error")
                        if self.verify_delay_s > 0:
                            await asyncio.sleep(min(self.verify_delay_s, 2.0))
                        try:
                            fresh2 = await self.ha.get_states()
                            self._states = {s["entity_id"]: s for s in fresh2}
                            remaining = self._check_mismatches(expected)
                            if remaining:
                                rem_str = ", ".join(
                                    f"{eid.split('.')[-1]} is {act!r} (expected {exp!r})"
                                    for eid, (act, exp) in remaining.items()
                                )
                                self.log(f"Verification still failed after retry: {rem_str}", "error")
                                ok = False
                            else:
                                self.log(f"Verified {cmd.label} after retry", "info")
                        except Exception as err:  # noqa: BLE001
                            self.log(f"Re-verification fetch failed: {err}", "warning")
                    else:
                        self.log(f"Verified {cmd.label}: hardware state confirmed", "info")

            try:
                self.store.add_action(self.mode, cmd.kind, cmd.power_w, "shadow" if dry else "ha", dry, reason)
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("action store failed: %s", err)
            if ok:
                self.applied = cmd
                self._last_apply_ts = datetime.now(timezone.utc)
            else:
                self.applied = None

    # ------------------------------------------------------------------ status
    def status(self) -> dict:
        s = self.current_slot()
        p = self.plan
        spot = imp = exp = None
        if s is not None:
            spot, imp, exp = s.spot, s.import_price, s.export_price
        horizon_end = None
        if p and p.ok and p.slots:
            horizon_end = (datetime.fromisoformat(p.slots[-1].start) + SLOT).isoformat()
        return {
            "mode": self.mode,
            "dry_run": self.dry_run,
            "ha_mode": getattr(self.ha, "mode", "unknown"),
            "connected": bool(self.ha.connected),
            "last_error": self.last_error,
            "live": self.live,
            "command": {
                "planned": asdict(self.planned) | {"label": self.planned.label} if self.planned else None,
                "effective": asdict(self.effective) | {"label": self.effective.label} if self.effective else None,
                "applied": asdict(self.applied) | {"label": self.applied.label} if self.applied else None,
                "reason": self.reason,
                **self.flags,
            },
            "price": {"spot": spot, "import": imp, "export": exp, "source": self.prices.source,
                      "last_fetch": self.prices.last_fetch.isoformat() if self.prices.last_fetch else None,
                      "error": self.prices.last_error, "horizon_end": horizon_end},
            "plan": None if not p else {
                "ok": p.ok, "status": p.status, "message": p.message, "created": p.created,
                "savings_24h_eur": p.savings_24h_eur, "savings_eur": p.savings_eur,
                "cost_eur": p.cost_eur, "baseline_cost_eur": p.baseline_cost_eur,
                "solve_ms": p.solve_ms, "milp": p.milp, "terminal_value_eur_kwh": p.terminal_value_eur_kwh,
                "slots": len(p.slots), "peak_excess_w": p.peak_excess_w,
            },
            "last_solve": self.last_solve.isoformat() if self.last_solve else None,
            "tz": str(self.ha.tz),
        }
