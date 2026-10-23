"""Integration tests: discovery + controller against the simulated HA."""
from __future__ import annotations

import pytest

from app.controller import Command, Controller
from app.discovery import discover
from app.ha_client import MockHA
from app.state import StateStore

B = MockHA.BAT


@pytest.fixture
def ctl(tmp_path, monkeypatch):
    monkeypatch.setattr("app.settings.CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr("app.controller.save_config", lambda cfg: None)
    ha = MockHA()
    c = Controller(ha, StateStore(tmp_path / "state.db"))
    c.cfg.control.mode = "active"
    c.cmd_delay_s = 0.0
    c.verify_delay_s = 0.0
    return c


@pytest.mark.asyncio
async def test_discovery_finds_defaults():
    ha = MockHA()
    d = discover(await ha.get_states())
    s = d["suggestion"]
    assert s["battery_device"] == B
    assert s["soc"] == f"sensor.{B}_battery_state_of_charge"
    assert s["tp0_power"] == f"number.{B}_time_period_0_power"
    assert s["p1_power"] == "sensor.p1_meter_5c2faf013360_power"
    assert s["p1_avg_demand"] == "sensor.p1_meter_5c2faf013360_average_demand"
    assert s["price"] == "sensor.nord_pool_be_current_price"
    assert d["batteries"][0]["complete"]


@pytest.mark.asyncio
async def test_apply_charge_and_idle(ctl):
    await ctl.refresh_live()
    await ctl.apply(Command("charge", 1500), "test")
    v = ctl.ha.values
    assert v[f"select.{B}_working_mode"] == "Manual"
    assert v[f"number.{B}_time_period_0_power"] == -1500
    assert v[f"switch.{B}_time_period_0_enabled"] == "on"
    assert v[f"switch.{B}_time_period_1_enabled"] == "off"
    await ctl.refresh_live()
    await ctl.apply(Command("idle"), "test")
    assert v[f"switch.{B}_time_period_0_enabled"] == "off"
    assert v[f"switch.{B}_time_period_1_enabled"] == "on"
    assert v[f"number.{B}_time_period_1_power"] == 0


@pytest.mark.asyncio
async def test_shadow_mode_does_not_touch_entities(ctl):
    ctl.cfg.control.mode = "shadow"
    await ctl.refresh_live()
    before = dict(ctl.ha.values)
    await ctl.apply(Command("charge", 1500), "test")
    assert ctl.ha.values == before
    assert any("[SHADOW]" in l["msg"] for l in ctl.logs)


@pytest.mark.asyncio
async def test_soc_cutoff_and_peak_throttle(ctl):
    ctl.cfg.control.mode = "force_charge"
    ctl.cfg.control.peak_shaving = False
    ctl.ha.soc = 95
    await ctl.watchdog()
    assert ctl.effective.kind == "idle" and ctl.flags["safety"]

    ctl.ha.soc = 50
    ctl.ha.house_load = lambda ts: 2000.0  # house close to the 2500 W target
    ctl.cfg.tariff.peak_limit_w = 2500
    ctl.cfg.entities.p1_avg_demand = ""  # own quarter avg (not enough coverage yet → instantaneous)
    await ctl.watchdog()
    assert ctl.effective.kind == "charge"
    assert ctl.effective.power_w < 1500
    assert ctl.flags["throttled"]


@pytest.mark.asyncio
async def test_full_optimization_cycle(ctl):
    res = await ctl.run_optimization(force_prices=True)
    assert res.ok, res.message
    assert len(res.slots) >= 4
    assert ctl.status()["plan"]["ok"]


@pytest.mark.asyncio
async def test_switching_order_on_first_off_second(ctl):
    await ctl.refresh_live()
    ctl.ha.calls.clear()

    # Switch to charge: tp0 ON first, tp1 OFF second
    await ctl.apply(Command("charge", 1500), "test_order")
    services = [(c["service"], c.get("entity_id")) for c in ctl.ha.calls]
    tp0_on_idx = next(i for i, (s, e) in enumerate(services) if s == "switch.turn_on" and f"time_period_0" in (e or ""))
    tp1_off_idx = next(i for i, (s, e) in enumerate(services) if s == "switch.turn_off" and f"time_period_1" in (e or ""))
    assert tp0_on_idx < tp1_off_idx, "Target period switch ON must be performed before inactive period switch OFF"

    ctl.ha.calls.clear()
    await ctl.refresh_live()

    # Switch to idle: tp1 ON first, tp0 OFF second
    await ctl.apply(Command("idle"), "test_order")
    services = [(c["service"], c.get("entity_id")) for c in ctl.ha.calls]
    tp1_on_idx = next(i for i, (s, e) in enumerate(services) if s == "switch.turn_on" and f"time_period_1" in (e or ""))
    tp0_off_idx = next(i for i, (s, e) in enumerate(services) if s == "switch.turn_off" and f"time_period_0" in (e or ""))
    assert tp1_on_idx < tp0_off_idx, "Target period switch ON must be performed before inactive period switch OFF"


@pytest.mark.asyncio
async def test_verification_retries_on_mismatch(ctl):
    await ctl.refresh_live()
    # Intercept call_service to simulate a dropped turn_on command on first attempt
    orig_call = ctl.ha.call_service
    dropped = False

    async def mock_call(domain, service, data, return_response=False):
        nonlocal dropped
        if service == "turn_on" and not dropped:
            dropped = True
            # Simulate dropped command: do not update values
            ctl.ha.calls.append({"ts": "mock", "service": f"{domain}.{service}", **data})
            return []
        return await orig_call(domain, service, data, return_response)

    ctl.ha.call_service = mock_call
    await ctl.apply(Command("charge", 1500), "test_retry")

    # Verification should have detected that tp0 was still off and issued a retry
    assert dropped is True
    assert ctl.ha.values[f"switch.{B}_time_period_0_enabled"] == "on"
    assert any("Verification mismatch" in l["msg"] for l in ctl.logs)
    assert any("Verified Charge 1500 W after retry" in l["msg"] for l in ctl.logs)


@pytest.mark.asyncio
async def test_discharge_auto_switches_to_automatic(ctl):
    await ctl.refresh_live()
    # First put in charge mode
    await ctl.apply(Command("charge", 1500), "prep")
    assert ctl.ha.values[f"select.{B}_working_mode"] == "Manual"

    # Now switch to discharge_auto (follow house load)
    await ctl.refresh_live()
    ctl.ha.calls.clear()
    await ctl.apply(Command("discharge_auto"), "follow_load")

    assert ctl.ha.values[f"select.{B}_working_mode"] == "Automatic"
    assert ctl.ha.values[f"switch.{B}_time_period_0_enabled"] == "off"
    assert ctl.ha.values[f"switch.{B}_time_period_1_enabled"] == "off"


@pytest.mark.asyncio
async def test_soft_start_and_step_up_regulation(ctl):
    ctl.cfg.control.mode = "force_charge"
    ctl.cfg.battery.max_charge_w = 2500
    ctl.cfg.tariff.peak_limit_w = 2800
    ctl.cfg.control.peak_margin_w = 100  # allowed = 2700 W
    ctl.cfg.entities.p1_avg_demand = ""  # instantaneous limit = 2700 W
    ctl.ha.soc = 50
    ctl.ha.house_load = lambda ts: 400.0

    await ctl.watchdog()

    # Initial charge command starts at 50% of 2500 W = 1250 W
    assert ctl.effective.kind == "charge"
    assert ctl.effective.power_w == 1250
    assert ctl.flags["throttled"]
    assert "Soft-start charge (50%)" in ctl.reason

    # Apply the 1250 W command
    await ctl.apply(ctl.effective, ctl.reason)
    assert ctl.applied.power_w == 1250

    # P1 meter reflects: 400 W house + 1250 W battery = 1650 W
    await ctl.watchdog()

    # Allowed = 2700 W. Headroom = 2700 - 1650 = 1050 W.
    # Step up by min(1050 * 0.5, 300, 2500 - 1250) = 300 W -> 1250 + 300 = 1550 W.
    assert ctl.effective.kind == "charge"
    assert ctl.effective.power_w == 1550
    assert "Step-up charge: 1250→1550 W" in ctl.reason

    # Assume 1550 W was applied
    await ctl.apply(ctl.effective, ctl.reason)

    # Now simulate an appliance turning on: house jumps to 1450 W
    # Total P1 will be 1450 + 1550 = 3000 W (overshoot = 300 W above allowed 2700 W)
    ctl.ha.house_load = lambda ts: 1450.0
    await ctl.watchdog()

    # Overshoot = 3000 - 2700 = 300 W. Cut = 300 + 50 = 350 W.
    # New power = 1550 - 350 = 1200 W.
    assert ctl.effective.kind == "charge"
    assert ctl.effective.power_w == 1200
    assert "reduce 1550→1200 W" in ctl.reason


@pytest.mark.asyncio
async def test_house_load_calculation_does_not_double_count_battery(ctl):
    ctl.ha.house_load = lambda ts: 800.0
    ctl.ha.values[f"number.{B}_time_period_0_power"] = -2060.0
    ctl.ha.values[f"switch.{B}_time_period_0_enabled"] = "on"
    ctl.ha.values[f"switch.{B}_time_period_1_enabled"] = "off"
    ctl.ha.values[f"select.{B}_working_mode"] = "Manual"

    await ctl.refresh_live()
    # P1 is ~2860 W, battery is charging at 2060 W.
    # House load MUST be ~800 W, NOT 2860 + 2060 = 4920 W!
    assert ctl.live["batt_charge_w"] == 2060.0
    assert abs(ctl.live["house_w"] - 800.0) < 50.0


@pytest.mark.asyncio
async def test_average_demand_triggers_automatic_peak_shaving(ctl):
    ctl.cfg.control.mode = "force_charge"
    ctl.cfg.control.peak_shaving = True
    ctl.cfg.tariff.peak_limit_w = 2500
    ctl.cfg.control.peak_margin_w = 150  # limit = 2350 W
    ctl.ha.soc = 50

    # Simulate P1 average demand reaching 2400 W (which exceeds limit 2350 W)
    ctl.ha.quarter_energy = 2400.0 * 0.25
    ctl.ha.quarter_covered = 0.25
    ctl.ha.avg_demand = 2400.0
    await ctl.watchdog()

    # Must immediately switch to Automatic to shave peak and protect capacity tariff
    assert ctl.effective.kind == "discharge_auto"
    assert ctl.flags["shaving"] is True
    assert "15-min average demand 2400 W reached limit 2350 W" in ctl.reason



