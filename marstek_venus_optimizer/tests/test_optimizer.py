"""Unit tests for the LP/MILP optimizer, tariffs, price parsing and discovery.

Run:  cd marstek_venus_optimizer && ../.venv/bin/python -m pytest -q
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import numpy as np
import pytest

from app.optimizer import OptimizerInput, OptimizerParams, optimize
from app.prices import export_price, import_price, parse_energy_charts, parse_sensor_attributes

T0 = datetime(2026, 10, 23, 0, 0, tzinfo=timezone.utc)


def make_input(spot, load=300.0, soc=50.0, **kw):
    spot = np.asarray(spot, dtype=float)
    n = len(spot)
    load_arr = np.full(n, load) if np.isscalar(load) else np.asarray(load, dtype=float)
    return OptimizerInput(
        slot_start=[T0 + timedelta(minutes=15 * i) for i in range(n)],
        spot=spot, import_price=spot.copy(), export_price=spot.copy() - 0.05,
        load_w=load_arr, soc_pct=soc, **kw)


def params(**kw):
    base = dict(terminal_value_eur_kwh=0.0, peak_limit_w=5000, max_discharge_w=800, discharge_mode="manual")
    base.update(kw)
    return OptimizerParams(**base)


def arr(res, key):
    return np.array([getattr(s, key) for s in res.slots])


# ------------------------------------------------------------------ core LP behaviour
def test_arbitrage_charges_cheap_discharges_expensive():
    spot = [0.05] * 16 + [0.40] * 16
    res = optimize(make_input(spot, soc=10), params())
    assert res.ok
    ch, dis = arr(res, "charge_w"), arr(res, "discharge_w")
    assert ch[:16].sum() > 0 and ch[16:].sum() == 0
    assert dis[16:].sum() > 0 and dis[:16].sum() == 0
    assert res.savings_eur > 0


def test_flat_prices_do_not_cycle():
    # When starting at min_soc, flat prices with degradation will not cause any charging
    res = optimize(make_input([0.25] * 48, soc=10), params(terminal_value_eur_kwh=None))
    assert res.ok
    assert arr(res, "charge_w").sum() == 0
    assert arr(res, "discharge_w").sum() == 0
    assert all(s.action == "idle" for s in res.slots)


def test_micro_spread_below_degradation_is_ignored():
    # spread 0.02 €/kWh < losses + 2x 0.025 degradation
    spot = [0.20, 0.22] * 24
    res = optimize(make_input(spot), params(degradation_eur_per_kwh=0.025))
    assert arr(res, "charge_w").sum() == 0


def test_soc_bounds_and_dynamics():
    p = params(eta_ch=0.9, eta_dis=0.9)
    spot = [0.01] * 40 + [0.5] * 40
    res = optimize(make_input(spot, soc=50, load=1000), p)
    soc = arr(res, "soc_pct")
    assert soc.min() >= p.min_soc_pct - 1e-6 and soc.max() <= p.max_soc_pct + 1e-6
    # verify dynamics: dE = dt*(eta_c*ch - dis/eta_d)
    cap = p.capacity_wh
    e_prev = 50 / 100 * cap
    for s in res.slots:
        e = s.soc_pct / 100 * cap
        expected = e_prev + 0.25 * (0.9 * s.charge_w - s.discharge_w / 0.9)
        assert e == pytest.approx(expected, abs=1.0)
        e_prev = e
    assert soc.max() == pytest.approx(90, abs=0.5)
    assert soc[-1] == pytest.approx(10, abs=0.5)


def test_power_balance():
    rng = np.random.default_rng(1)
    load = rng.uniform(-500, 2000, 64)
    res = optimize(make_input(rng.uniform(0, 0.4, 64), load=load), params())
    for s in res.slots:
        assert s.grid_import_w - s.grid_export_w == pytest.approx(s.load_w + s.charge_w - s.discharge_w, abs=2)


def test_peak_limit_throttles_charging():
    # cheap slot but house load 1800 W with 2500 W kwartierpiek → max 700 W charge
    spot = [0.01] * 8 + [0.5] * 8
    res = optimize(make_input(spot, load=1800, soc=10),
                   params(peak_limit_w=2500, peak_penalty_eur_per_kw=50.0))
    assert res.ok
    assert arr(res, "grid_import_w").max() <= 2500 + 1
    assert arr(res, "charge_w")[:8].max() == pytest.approx(700, abs=2)
    assert res.peak_excess_w == pytest.approx(0, abs=1)


def test_peak_excess_used_when_unavoidable():
    res = optimize(make_input([0.2] * 8, load=3000, soc=10), params(peak_limit_w=2500, min_soc_pct=10))
    assert res.peak_excess_w == pytest.approx(500, abs=2)


def test_breaker_limit():
    res = optimize(make_input([0.01] * 4 + [0.6] * 4, load=8000, soc=10),
                   params(max_grid_w=9000, peak_limit_w=20000))
    assert arr(res, "grid_import_w").max() <= 9000 + 1


def test_auto_ct_never_exports_battery_energy():
    spot = [0.05] * 16 + [0.60] * 16
    res = optimize(make_input(spot, load=300, soc=90), params(discharge_mode="auto_ct"))
    dis, load = arr(res, "discharge_w"), arr(res, "load_w")
    assert (dis <= load + 1).all()
    assert arr(res, "grid_export_w").max() < 1


def test_milp_prevents_simultaneous_charge_discharge_negative_prices():
    spot = np.array([-0.30] * 8 + [0.3] * 8)
    inp = make_input(spot, soc=50)
    inp.export_price = spot - 0.01
    res = optimize(inp, params(use_milp=True))
    ch, dis = arr(res, "charge_w"), arr(res, "discharge_w")
    assert not ((ch > 1) & (dis > 1)).any()
    assert ch[:8].sum() > 0  # gets paid to charge


def test_initial_soc_outside_bounds_is_feasible():
    for soc in (3.0, 97.0):
        res = optimize(make_input([0.1, 0.3] * 8, soc=soc), params())
        assert res.ok, res.message


def test_terminal_value_controls_end_soc():
    spot = [0.10] * 32
    full = optimize(make_input(spot, soc=10), params(terminal_value_eur_kwh=0.5))
    empty = optimize(make_input(spot, soc=90, load=500), params(terminal_value_eur_kwh=0.0))
    assert full.slots[-1].soc_pct == pytest.approx(90, abs=0.5)
    assert empty.slots[-1].soc_pct == pytest.approx(10, abs=0.5)


def test_partial_first_slot_scales_energy():
    res = optimize(make_input([0.01] + [0.5] * 7, soc=10, first_slot_fraction=0.5), params())
    s0 = res.slots[0]
    e_gain_wh = (s0.soc_pct - 10) / 100 * 5120
    assert e_gain_wh == pytest.approx(0.125 * 0.9 * s0.charge_w, abs=1)


def test_current_quarter_peak_accounts_for_elapsed_import():
    # half the quarter elapsed at 3000 W average → remaining must average ≤ 2000 W
    inp = make_input([0.01] * 4 + [0.5] * 4, load=1500, soc=10, first_slot_fraction=0.5,
                     quarter_avg_so_far_w=3000)
    res = optimize(inp, params(peak_limit_w=2500, peak_penalty_eur_per_kw=50))
    assert res.slots[0].grid_import_w <= 2000 + 1


def test_reasons_present():
    res = optimize(make_input([0.05] * 8 + [0.4] * 8, soc=10), params())
    assert all(s.reason for s in res.slots)
    assert "Charge" in res.slots[0].reason


# ------------------------------------------------------------------ tariffs & parsing
TARIFF = SimpleNamespace(supplier_multiplier=1.1, supplier_margin_eur_kwh=0.01, grid_fee_eur_kwh=0.06,
                         excise_levies_eur_kwh=0.05, vat_pct=6.0, injection_multiplier=0.9,
                         injection_fee_eur_kwh=0.012)


def test_belgian_import_formula():
    assert float(import_price(0.10, TARIFF)) == pytest.approx(((0.10 * 1.1 + 0.01) + 0.06 + 0.05) * 1.06)


def test_belgian_export_formula():
    assert float(export_price(0.10, TARIFF)) == pytest.approx(0.10 * 0.9 - 0.012)


def test_parse_hourly_sensor_expands_to_quarters_and_scales_mwh():
    state = {"attributes": {"unit_of_measurement": "EUR/MWh", "raw_today": [
        {"start": "2026-10-23T00:00:00+02:00", "end": "2026-10-23T01:00:00+02:00", "value": 100.0},
        {"start": "2026-10-23T01:00:00+02:00", "end": "2026-10-23T02:00:00+02:00", "value": 50.0},
    ]}}
    out = parse_sensor_attributes(state)
    assert len(out) == 8
    vals = [out[k] for k in sorted(out)]
    assert vals[:4] == [0.1] * 4 and vals[4:] == [0.05] * 4


def test_parse_energy_charts():
    base = int(T0.timestamp())
    out = parse_energy_charts({"unix_seconds": [base, base + 900], "price": [80.0, 120.0], "unit": "EUR / MWh"})
    assert out[T0] == pytest.approx(0.08)
    assert out[T0 + timedelta(minutes=15)] == pytest.approx(0.12)


def test_forecast_load_applies_fallback_floor_when_history_suppressed():
    from app.forecast import forecast_load
    # 2 weeks of history where 18:00–20:00 (slots 72-80) were zeroed out by prior battery discharge
    slots = [T0 + timedelta(minutes=15 * i) for i in range(96)]
    samples = []
    for day in range(1, 15):
        d_start = T0 - timedelta(days=day)
        for i in range(96):
            t = d_start + timedelta(minutes=15 * i)
            # 18:00-20:00 zeroed out, rest 300 W
            val = 0.0 if 72 <= i < 80 else 300.0
            samples.append((t, val))

    preds = forecast_load(slots, samples, timezone.utc, base_w=300.0)
    # Slots 72-80 should NOT be 0 W; floor fallback should kick in (fallback profile peak is ~1000-1100W * 0.8)
    for i in range(72, 80):
        assert preds[i] >= 300.0, f"Slot {i} was {preds[i]}W, expected at least baseline 300W"


