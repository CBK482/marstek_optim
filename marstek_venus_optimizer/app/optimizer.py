"""Rolling-horizon LP / MILP battery optimizer (scipy ``linprog`` / HiGHS).

The module is pure (no Home Assistant or I/O dependencies) so it can be unit
tested in isolation.

Units used internally: power in kW, energy in kWh, prices in EUR/kWh,
time step ``dt`` in hours. Inputs/outputs exposed to the rest of the app use W
and Wh.

Decision variables per slot t (T slots):
    ch_t   charge power (grid side)          [0, P_ch_max]
    dis_t  discharge power (AC side)          [0, P_dis_max]
    imp_t  grid import                        [0, P_grid_max]
    exp_t  grid export                        [0, P_grid_max]
    E_t    battery energy at end of slot      [SoC_min*C, SoC_max*C]
plus one scalar ``pe`` = peak excess above the kwartierpiek target and, in
MILP mode, one binary ``u_t`` per slot preventing simultaneous charge and
discharge.
"""
from __future__ import annotations

import math
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Optional, Sequence

import numpy as np
from scipy import sparse
from scipy.optimize import linprog


@dataclass
class OptimizerParams:
    capacity_wh: float = 5120.0
    min_soc_pct: float = 10.0
    target_soc_pct: float = 50.0
    max_soc_pct: float = 90.0
    max_charge_w: float = 1500.0
    max_discharge_w: float = 800.0
    eta_ch: float = 0.90
    eta_dis: float = 0.90
    degradation_eur_per_kwh: float = 0.025
    peak_limit_w: float = 2500.0
    peak_penalty_eur_per_kw: float = 3.75
    max_grid_w: float = 9200.0
    discharge_mode: str = "auto_ct"  # auto_ct: discharge <= house load (no export)
    use_milp: bool = True
    min_action_w: float = 100.0
    terminal_value_eur_kwh: Optional[float] = None
    terminal_value_percentile: float = 30.0
    target_soc_weight: float = 0.005  # EUR/kWh/h penalty for being below target SoC
    target_soc_weight_above: float = 0.001  # EUR/kWh/h penalty for being above target SoC

    @classmethod
    def from_config(cls, cfg, peak_limit_w: Optional[float] = None) -> "OptimizerParams":
        b, t, c = cfg.battery, cfg.tariff, cfg.control
        return cls(
            capacity_wh=b.capacity_wh,
            min_soc_pct=b.min_soc_pct,
            target_soc_pct=getattr(b, "target_soc_pct", 50.0),
            max_soc_pct=b.max_soc_pct,
            max_charge_w=b.max_charge_w,
            max_discharge_w=b.max_discharge_w,
            eta_ch=b.charge_eff_pct / 100.0,
            eta_dis=b.discharge_eff_pct / 100.0,
            degradation_eur_per_kwh=b.degradation_eur_per_kwh,
            peak_limit_w=t.peak_limit_w if peak_limit_w is None else peak_limit_w,
            # A new monthly peak raises the 12-month rolling average by 1/12
            # of the excess for 12 months -> cost = tariff/12 per kW.
            peak_penalty_eur_per_kw=t.capacity_tariff_eur_kw_year / 12.0,
            max_grid_w=t.max_grid_w,
            discharge_mode=b.discharge_mode,
            use_milp=c.use_milp,
            min_action_w=c.min_action_w,
            terminal_value_eur_kwh=c.terminal_value_eur_kwh,
            terminal_value_percentile=c.terminal_value_percentile,
            target_soc_weight=getattr(c, "target_soc_weight", 0.005),
            target_soc_weight_above=getattr(c, "target_soc_weight_above", 0.001),
        )


@dataclass
class OptimizerInput:
    slot_start: Sequence[datetime]
    spot: np.ndarray  # EUR/kWh
    import_price: np.ndarray  # all-in EUR/kWh
    export_price: np.ndarray  # EUR/kWh
    load_w: np.ndarray  # forecast house load (W, negative = PV surplus)
    soc_pct: float
    slot_minutes: int = 15
    first_slot_fraction: float = 1.0  # remaining fraction of the current slot
    quarter_avg_so_far_w: Optional[float] = None  # import avg in elapsed part


@dataclass
class SlotPlan:
    start: str
    label: str
    spot: float
    import_price: float
    export_price: float
    load_w: float
    charge_w: float
    discharge_w: float
    grid_import_w: float
    grid_export_w: float
    soc_start_pct: float
    soc_pct: float
    peak_limit_w: float
    action: str
    power_w: float
    reason: str = ""


@dataclass
class OptimizationResult:
    ok: bool
    status: str
    message: str = ""
    slots: list = field(default_factory=list)
    objective_eur: float = 0.0
    cost_eur: float = 0.0
    baseline_cost_eur: float = 0.0
    savings_eur: float = 0.0
    savings_24h_eur: float = 0.0
    terminal_value_eur_kwh: float = 0.0
    peak_excess_w: float = 0.0
    solve_ms: float = 0.0
    milp: bool = False
    created: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


def _terminal_value(params: OptimizerParams, import_price: np.ndarray) -> float:
    if params.terminal_value_eur_kwh is not None:
        return float(params.terminal_value_eur_kwh)
    ref = float(np.percentile(import_price, params.terminal_value_percentile))
    return max(0.0, params.eta_dis * (ref - params.degradation_eur_per_kwh))


def optimize(inp: OptimizerInput, params: OptimizerParams, tz=None) -> OptimizationResult:
    t0 = time.perf_counter()
    T = len(inp.spot)
    if T == 0:
        return OptimizationResult(ok=False, status="no_data", message="No price data available")

    ci = np.asarray(inp.import_price, dtype=float)
    ce = np.minimum(np.asarray(inp.export_price, dtype=float), ci - 1e-4)
    load = np.asarray(inp.load_w, dtype=float) / 1000.0  # kW

    dt = np.full(T, inp.slot_minutes / 60.0)
    frac = float(min(1.0, max(0.05, inp.first_slot_fraction)))
    dt[0] *= frac

    cap = params.capacity_wh / 1000.0
    e0 = cap * float(np.clip(inp.soc_pct, 0, 100)) / 100.0
    e_min = cap * params.min_soc_pct / 100.0
    e_max = cap * params.max_soc_pct / 100.0
    ch_max = params.max_charge_w / 1000.0
    dis_max = params.max_discharge_w / 1000.0
    grid_max = params.max_grid_w / 1000.0
    eta_c, eta_d = params.eta_ch, params.eta_dis
    deg = params.degradation_eur_per_kwh
    tv = _terminal_value(params, ci)

    # Per-slot peak target (kW). For the running quarter the import already
    # accumulated in the elapsed part counts towards the 15-min average.
    lim = np.full(T, params.peak_limit_w / 1000.0)
    if inp.quarter_avg_so_far_w is not None and frac < 1.0:
        f_el = min(0.9, 1.0 - frac)
        lim[0] = max(0.0, (lim[0] - f_el * inp.quarter_avg_so_far_w / 1000.0) / (1.0 - f_el))

    # Variable layout:
    # CH, DIS, IMP, EXP, E, PE, DEV_BELOW, DEV_ABOVE, (MILP) U
    I_CH, I_DIS, I_IMP, I_EXP, I_E = (np.arange(T) + k * T for k in range(5))
    I_PE = 5 * T
    I_BELOW = np.arange(T) + 5 * T + 1
    I_ABOVE = np.arange(T) + 6 * T + 1
    milp = bool(params.use_milp)
    I_U = np.arange(T) + 7 * T + 1
    n = 7 * T + 1 + (T if milp else 0)

    # Objective
    c = np.zeros(n)
    c[I_CH] = dt * deg
    c[I_DIS] = dt * deg
    c[I_IMP] = dt * ci
    c[I_EXP] = -dt * ce
    c[I_E[-1]] -= tv
    c[I_PE] = params.peak_penalty_eur_per_kw
    c[I_BELOW] = dt * params.target_soc_weight
    c[I_ABOVE] = dt * params.target_soc_weight_above

    # Equalities: SoC dynamics + power balance
    rows, cols, vals = [], [], []
    b_eq = np.zeros(2 * T)
    for t in range(T):
        rows += [t, t, t]
        cols += [I_E[t], I_CH[t], I_DIS[t]]
        vals += [1.0, -dt[t] * eta_c, dt[t] / eta_d]
        if t > 0:
            rows.append(t)
            cols.append(I_E[t - 1])
            vals.append(-1.0)
        else:
            b_eq[t] = e0
        r = T + t
        rows += [r, r, r, r]
        cols += [I_IMP[t], I_EXP[t], I_CH[t], I_DIS[t]]
        vals += [1.0, -1.0, -1.0, 1.0]
        b_eq[r] = load[t]
    A_eq = sparse.csr_matrix((vals, (rows, cols)), shape=(2 * T, n))

    # Inequalities: kwartierpiek + target SoC deviations + (MILP) charge/discharge exclusivity
    rows, cols, vals, b_ub = [], [], [], []
    r = 0
    for t in range(T):
        rows += [r, r]
        cols += [I_IMP[t], I_PE]
        vals += [1.0, -1.0]
        b_ub.append(lim[t])
        r += 1

    e_target = cap * float(np.clip(params.target_soc_pct, params.min_soc_pct, params.max_soc_pct)) / 100.0
    for t in range(T):
        # -E[t] - DEV_BELOW[t] <= -e_target  <=>  e_target - E[t] <= DEV_BELOW[t]
        rows += [r, r]
        cols += [I_E[t], I_BELOW[t]]
        vals += [-1.0, -1.0]
        b_ub.append(-e_target)
        r += 1

        # E[t] - DEV_ABOVE[t] <= e_target  <=>  E[t] - e_target <= DEV_ABOVE[t]
        rows += [r, r]
        cols += [I_E[t], I_ABOVE[t]]
        vals += [1.0, -1.0]
        b_ub.append(e_target)
        r += 1

    if milp:
        for t in range(T):
            rows += [r, r]
            cols += [I_CH[t], I_U[t]]
            vals += [1.0, -ch_max]
            b_ub.append(0.0)
            r += 1
            rows += [r, r]
            cols += [I_DIS[t], I_U[t]]
            vals += [1.0, dis_max]
            b_ub.append(dis_max)
            r += 1
    A_ub = sparse.csr_matrix((vals, (rows, cols)), shape=(r, n))

    # Bounds
    bounds = [(0.0, 0.0)] * n
    dis_ub = np.full(T, dis_max)
    if params.discharge_mode == "auto_ct":
        dis_ub = np.minimum(dis_ub, np.maximum(load, 0.0))
    lo_e, hi_e = min(e_min, e0), max(e_max, e0)
    for t in range(T):
        bounds[I_CH[t]] = (0.0, ch_max)
        bounds[I_DIS[t]] = (0.0, float(dis_ub[t]))
        bounds[I_IMP[t]] = (0.0, max(grid_max, float(load[t])))
        bounds[I_EXP[t]] = (0.0, max(grid_max, float(-load[t])))
        bounds[I_E[t]] = (lo_e, hi_e)
        bounds[I_BELOW[t]] = (0.0, cap)
        bounds[I_ABOVE[t]] = (0.0, cap)
    bounds[I_PE] = (0.0, None)
    integrality = None
    if milp:
        for t in range(T):
            bounds[I_U[t]] = (0.0, 1.0)
        integrality = np.zeros(n)
        integrality[I_U] = 1

    kwargs = dict(c=c, A_ub=A_ub, b_ub=np.asarray(b_ub), A_eq=A_eq, b_eq=b_eq,
                  bounds=bounds, method="highs")
    if milp:
        kwargs["integrality"] = integrality
        kwargs["options"] = {"time_limit": 20.0, "mip_rel_gap": 1e-4}
    res = linprog(**kwargs)
    solve_ms = (time.perf_counter() - t0) * 1000.0
    if res.x is None or res.status not in (0, 1):
        return OptimizationResult(ok=False, status=f"solver_{res.status}", message=str(res.message),
                                  solve_ms=solve_ms, milp=milp)

    x = res.x
    ch = np.where(x[I_CH] * 1000 < 1, 0.0, x[I_CH] * 1000)
    dis = np.where(x[I_DIS] * 1000 < 1, 0.0, x[I_DIS] * 1000)
    # LP relaxation may (rarely) charge & discharge simultaneously: net it out
    both = (ch > 0) & (dis > 0)
    net = ch - dis
    ch = np.where(both, np.maximum(net, 0), ch)
    dis = np.where(both, np.maximum(-net, 0), dis)
    imp = x[I_IMP] * 1000
    exp_ = x[I_EXP] * 1000
    E = x[I_E]
    pe = float(x[I_PE])

    # Costs & savings
    load_w = load * 1000
    imp_b = np.maximum(load_w, 0)
    exp_b = np.maximum(-load_w, 0)

    def _cost(n_slots: int, imp_a, exp_a, ch_a, dis_a, with_deg: bool) -> float:
        s = slice(0, n_slots)
        e = float(np.sum(dt[s] * (ci[s] * imp_a[s] - ce[s] * exp_a[s]) / 1000.0))
        if with_deg:
            e += float(np.sum(dt[s] * deg * (ch_a[s] + dis_a[s]) / 1000.0))
        excess = max(0.0, float(np.max(imp_a[s] / 1000.0 - lim[s])))
        return e + params.peak_penalty_eur_per_kw * excess

    zeros = np.zeros(T)
    base_total = _cost(T, imp_b, exp_b, zeros, zeros, False)
    opt_total = _cost(T, imp, exp_, ch, dis, True)
    savings = base_total - (opt_total - tv * (E[-1] - e0))
    cum = np.cumsum(dt)
    n24 = int(np.searchsorted(cum, 24.0 + 1e-9, side="right"))
    n24 = max(1, min(T, n24))
    savings_24 = _cost(n24, imp_b, exp_b, zeros, zeros, False) - (
        _cost(n24, imp, exp_, ch, dis, True) - tv * (E[n24 - 1] - e0))

    slots: list[SlotPlan] = []
    soc_prev = e0 / cap * 100.0
    for t in range(T):
        if ch[t] >= params.min_action_w:
            action, pw = "charge", -ch[t]
        elif dis[t] >= params.min_action_w:
            action, pw = "discharge", dis[t]
        else:
            action, pw = "idle", 0.0
        st = inp.slot_start[t]
        st_local = st.astimezone(tz) if tz is not None else st
        soc = E[t] / cap * 100.0
        slots.append(SlotPlan(
            start=st.isoformat(), label=st_local.strftime("%a %H:%M"),
            spot=round(float(inp.spot[t]), 5), import_price=round(float(ci[t]), 5),
            export_price=round(float(ce[t]), 5), load_w=round(float(load_w[t]), 1),
            charge_w=round(float(ch[t]), 1), discharge_w=round(float(dis[t]), 1),
            grid_import_w=round(float(imp[t]), 1), grid_export_w=round(float(exp_[t]), 1),
            soc_start_pct=round(soc_prev, 2), soc_pct=round(float(soc), 2),
            peak_limit_w=round(float(lim[t] * 1000), 1), action=action, power_w=round(float(pw), 1),
        ))
        soc_prev = soc
    explain(slots, params, tv)

    return OptimizationResult(
        ok=True, status="optimal" if res.status == 0 else "time_limit",
        message=str(res.message), slots=slots, objective_eur=float(res.fun),
        cost_eur=round(opt_total, 4), baseline_cost_eur=round(base_total, 4),
        savings_eur=round(float(savings), 4), savings_24h_eur=round(float(savings_24), 4),
        terminal_value_eur_kwh=round(tv, 4), peak_excess_w=round(pe * 1000, 1),
        solve_ms=round(solve_ms, 1), milp=milp, created=datetime.now().astimezone().isoformat(),
    )


def _eur(v: float) -> str:
    return f"€{v:.3f}/kWh"


def explain(slots: list[SlotPlan], params: OptimizerParams, tv: float) -> None:
    """Attach a human-readable reason to every slot (in place)."""
    T = len(slots)
    prices = np.array([s.import_price for s in slots])
    next_dis = [None] * T
    next_ch = [None] * T
    nd = nc = None
    for t in range(T - 1, -1, -1):
        next_dis[t], next_ch[t] = nd, nc
        if slots[t].action == "discharge":
            nd = t
        if slots[t].action == "charge":
            nc = t
    ch_cost_num = ch_cost_den = 0.0

    for t, s in enumerate(slots):
        cheaper = float(np.mean(prices < s.import_price)) * 100.0
        soc_txt = f"{s.soc_start_pct:.0f}%"
        at_peak = s.grid_import_w >= s.peak_limit_w - 1 and s.peak_limit_w > 0
        if s.action == "charge":
            ch_cost_num += s.charge_w * s.import_price
            ch_cost_den += s.charge_w
            lead = f"Charge ({abs(s.power_w):.0f}W)"
            if s.import_price < 0:
                why = f"negative all-in price {_eur(s.import_price)} – paid to consume"
            else:
                why = f"all-in {_eur(s.import_price)} is in the cheapest {max(cheaper, 1):.0f}% of the horizon"
            k = next_dis[t]
            if k is not None:
                why += f"; energy is planned for discharge at {slots[k].label[4:]} ({_eur(slots[k].import_price)})"
            elif s.soc_pct <= params.target_soc_pct + 1.0:
                why += f"; charging to target {params.target_soc_pct:.0f}% SoC reserve for Fluvius peak protection"
            else:
                why += f"; carried beyond the horizon (valued {_eur(tv)})"
            if at_peak:
                why += f"; import capped at kwartierpiek target {s.peak_limit_w:.0f}W"
        elif s.action == "discharge":
            lead = f"Discharge ({s.power_w:.0f}W)"
            avg = ch_cost_num / ch_cost_den if ch_cost_den > 0 else None
            target = "covering house load (Auto CT)" if params.discharge_mode == "auto_ct" else "manual discharge"
            why = f"{target}; all-in {_eur(s.import_price)}"
            if avg is not None:
                why += f" vs. avg. charge cost {_eur(avg)}"
            else:
                why += f" vs. stored-energy value {_eur(tv)}"
            if s.load_w > s.peak_limit_w:
                why += f"; shaving forecast peak ({s.load_w:.0f}W > {s.peak_limit_w:.0f}W)"
        else:
            lead = "Idle (0W)"
            k, j = next_dis[t], next_ch[t]
            rt = 1 - params.eta_ch * params.eta_dis
            if s.soc_start_pct <= params.min_soc_pct + 0.5:
                why = f"at minimum SoC {soc_txt}"
                if j is not None:
                    why += f"; waiting for cheaper charging at {slots[j].label[4:]} ({_eur(slots[j].import_price)})"
            elif s.soc_start_pct >= params.max_soc_pct - 0.5 and k is not None:
                why = (f"battery full ({soc_txt}); holding for discharge at "
                       f"{slots[k].label[4:]} ({_eur(slots[k].import_price)})")
            elif abs(s.soc_start_pct - params.target_soc_pct) <= 2.0 and k is None:
                why = (f"holding target {soc_txt} SoC reserve for Fluvius peak shaving and "
                       f"overcapacity protection ({rt*100:.0f}% round-trip loss barrier)")
            elif k is not None:
                why = (f"holding {soc_txt} SoC; current all-in {_eur(s.import_price)}, saving discharge "
                       f"for {slots[k].label[4:]} at {_eur(slots[k].import_price)}")
            else:
                why = (f"holding {soc_txt} SoC; no spread beats round-trip losses ({rt*100:.0f}%) "
                       f"+ degradation (€{params.degradation_eur_per_kwh:.3f}/kWh)")
        s.reason = f"{s.label[4:]} — {lead}: {why}"
