# Marstek Venus Battery Optimizer – Documentation

## Home Assistant Ingress Add-on

This add-on connects directly to Home Assistant's Core API using the Supervisor token (`SUPERVISOR_TOKEN`) to automate and optimize the charging and discharging of Marstek Venus batteries.

### Prerequisites & Integrations

- **HAME / Marstek Integration**: Either via the HAME Energy integration or MQTT / Modbus entities exposing:
  - Battery State of Charge (`sensor.*_battery_state_of_charge`)
  - Working Mode (`select.*_working_mode`)
  - Time Period 0 Switch & Power (`switch.*_time_period_0_enabled`, `number.*_time_period_0_power`)
  - Time Period 1 Switch & Power (`switch.*_time_period_1_enabled`, `number.*_time_period_1_power`)
- **P1 Smart Meter**: HomeWizard or DSMR integration providing:
  - Instantaneous power (`sensor.*p1_meter*_power`)
  - 15-minute average demand (`sensor.*p1_meter*_average_demand`)
- **Nord Pool Integration**:
  - Exposes the action `nordpool.get_prices_for_date`, or a price sensor (`sensor.nord_pool_be*`).

### Optimization Architecture

- **Frequency**: Re-solves every 5 minutes (configurable).
- **Time Slots**: 15 minutes aligned with day-ahead market settlement and Belgian capacity tariff intervals.
- **Watchdog**: 15-second loop enforcing hard SoC limits (10% min, 90% max) and throttling charging if household consumption threatens to trigger a new 15-minute monthly peak demand.

### Standalone Local Development Outside Home Assistant

You can run the optimizer locally on your workstation:
```bash
cd marstek_venus_optimizer
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt

# Run with simulated Home Assistant environment
export MOCK_HA=1
python -m uvicorn app.main:app --port 8099 --reload
```
Open [http://localhost:8099](http://localhost:8099) in your browser.
