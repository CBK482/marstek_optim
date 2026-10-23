"""Configuration model, defaults and persistence (``/data/config.json``)."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

_LOGGER = logging.getLogger(__name__)

def _get_data_dir() -> Path:
    env_dir = os.environ.get("DATA_DIR")
    if env_dir:
        return Path(env_dir)
    default_dir = Path("/data")
    try:
        if default_dir.exists() and os.access(str(default_dir), os.W_OK):
            return default_dir
    except Exception:
        pass
    # Fallback for local development outside Home Assistant
    local_dir = Path(__file__).resolve().parent.parent / "data"
    return local_dir


DATA_DIR = _get_data_dir()
CONFIG_PATH = DATA_DIR / "config.json"
OPTIONS_PATH = DATA_DIR / "options.json"  # Supervisor-managed add-on options
DB_PATH = DATA_DIR / "state.db"

DEFAULT_BATTERY = "hame_energy_hmg_50_009b08782bb2"
DEFAULT_P1 = "p1_meter_5c2faf013360"

OperatingMode = Literal["active", "shadow", "force_charge", "hold", "auto_ct"]
OPERATING_MODES = ("active", "shadow", "force_charge", "hold", "auto_ct")


class EntityConfig(BaseModel):
    battery_device: str = DEFAULT_BATTERY
    soc: str = f"sensor.{DEFAULT_BATTERY}_battery_state_of_charge"
    capacity_wh: str = f"sensor.{DEFAULT_BATTERY}_battery_capacity_wh"
    battery_power: str = ""
    working_mode: str = f"select.{DEFAULT_BATTERY}_working_mode"
    tp0_enabled: str = f"switch.{DEFAULT_BATTERY}_time_period_0_enabled"
    tp0_power: str = f"number.{DEFAULT_BATTERY}_time_period_0_power"
    tp1_enabled: str = f"switch.{DEFAULT_BATTERY}_time_period_1_enabled"
    tp1_power: str = f"number.{DEFAULT_BATTERY}_time_period_1_power"
    p1_power: str = f"sensor.{DEFAULT_P1}_power"
    p1_avg_demand: str = f"sensor.{DEFAULT_P1}_average_demand"
    p1_month_peak: str = f"sensor.{DEFAULT_P1}_peak_demand_current_month"
    price: str = "sensor.nord_pool_be_current_price"


class BatteryConfig(BaseModel):
    capacity_wh: float = Field(5120, gt=0)
    min_soc_pct: float = Field(10, ge=0, le=100)
    max_soc_pct: float = Field(90, ge=0, le=100)
    max_charge_w: float = Field(1500, ge=0)
    max_discharge_w: float = Field(800, ge=0)
    charge_eff_pct: float = Field(90, gt=0, le=100)
    discharge_eff_pct: float = Field(90, gt=0, le=100)
    degradation_eur_per_kwh: float = Field(0.025, ge=0)
    discharge_mode: Literal["auto_ct", "manual"] = "auto_ct"
    battery_power_positive: Literal["discharge", "charge"] = "discharge"
    mode_option_manual: str = "Manual"
    mode_option_auto: str = "Automatic"

    @field_validator("mode_option_auto", mode="before")
    @classmethod
    def _coerce_auto(cls, v: object) -> str:
        if v == "Auto":
            return "Automatic"
        return str(v) if v is not None else "Automatic"


class TariffConfig(BaseModel):
    supplier_multiplier: float = 1.0
    supplier_margin_eur_kwh: float = 0.012
    grid_fee_eur_kwh: float = 0.058
    excise_levies_eur_kwh: float = 0.0525
    vat_pct: float = 6.0
    injection_multiplier: float = 1.0
    injection_fee_eur_kwh: float = 0.012
    peak_limit_w: float = Field(2500, ge=0)
    capacity_tariff_eur_kw_year: float = Field(45.0, ge=0)
    max_grid_w: float = Field(9200, gt=0)
    price_area: str = "BE"
    price_source: Literal["auto", "sensor", "nordpool_service", "energy_charts"] = "auto"


class ControlConfig(BaseModel):
    mode: OperatingMode = "shadow"
    optimize_interval_min: int = Field(5, ge=1, le=60)
    watchdog_interval_s: int = Field(15, ge=5, le=300)
    horizon_hours: int = Field(36, ge=4, le=48)
    use_milp: bool = True
    min_action_w: float = Field(100, ge=0)
    min_command_delta_w: float = Field(50, ge=0)
    peak_margin_w: float = Field(150, ge=0)
    peak_shaving: bool = True
    terminal_value_percentile: float = Field(30, ge=0, le=100)
    terminal_value_eur_kwh: Optional[float] = None
    base_load_w: float = Field(350, ge=0)


class AppConfig(BaseModel):
    entities: EntityConfig = EntityConfig()
    battery: BatteryConfig = BatteryConfig()
    tariff: TariffConfig = TariffConfig()
    control: ControlConfig = ControlConfig()


def load_config() -> AppConfig:
    if CONFIG_PATH.exists():
        try:
            return AppConfig.model_validate(json.loads(CONFIG_PATH.read_text()))
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Invalid %s (%s) – using defaults", CONFIG_PATH, err)
    return AppConfig()


def save_config(cfg: AppConfig) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg.model_dump(), indent=2))
    tmp.replace(CONFIG_PATH)


def load_addon_options() -> dict:
    try:
        return json.loads(OPTIONS_PATH.read_text())
    except Exception:  # noqa: BLE001
        return {}
