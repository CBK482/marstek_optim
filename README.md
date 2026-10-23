# Marstek Venus Battery Optimizer Add-on

[![Home Assistant Add-on](https://img.shields.io/badge/home--assistant-add--on-blue.svg)](https://www.home-assistant.io/)
[![Optimizer](https://img.shields.io/badge/solver-scipy%20HiGHS-green.svg)](https://scipy.org)

A rolling-horizon LP / MILP optimizer add-on for **Marstek Venus / HAME** home batteries in Home Assistant. It optimizes 15-minute battery charging and discharging schedules using **Nord Pool dynamic day-ahead prices**, Belgian retail tariff formulas, and strict Belgian capacity tariff (**kwartierpiek**) peak-shaving protection.

The web UI embeds seamlessly in Home Assistant's sidebar via **Home Assistant Ingress**.

---

## Features

- **Automated Entity Discovery**: Automatically scans and detects Marstek/HAME battery devices (`hame_energy_*`, `marstek_*`), P1 smart meters (HomeWizard, etc.), and Nord Pool price sensors or actions.
- **Nord Pool Action Support**: Fetches spot prices directly via Home Assistant's `nordpool.get_prices_for_date` action, sensor attributes, or direct Energy-Charts fallback.
- **Linear Programming Engine (HiGHS)**: Evaluates a 24–36h rolling horizon in 15-minute time slots to maximize arbitrage profits while respecting battery cycle degradation and terminal SoC value.
- **Belgian Retail Tariffs**: Supports all-in calculation including supplier spot multipliers, fixed margins, volumetric grid distribution tariffs, federal excise duties, and VAT (6%).
- **Kwartierpiek (Capacity Tariff) Protection**: Constrains grid imports and dynamically throttles charging or initiates peak shaving to prevent setting expensive new monthly capacity peaks.
- **Real-Time Safety Loop**: 15-second watchdog protects against overcharge/deep-discharge limits and throttles in real-time.
- **Embedded Ingress UI**: Status dashboard with interactive 24h horizon charts, schedule tables with human-readable decision explanations, and a full configuration editor.

---

## Installation

### Method 1: Add as a Custom Add-on Repository in Home Assistant
1. In Home Assistant, go to **Settings** → **Add-ons** → **Add-on Store**.
2. Click the three dots **⋮** in the top right corner and select **Repositories**.
3. Paste the URL of this repository:
   ```text
   https://github.com/CBK482/marstek_optim
   ```
4. Click **Add**, then reload the store.
5. Select **Marstek Venus Battery Optimizer** and click **Install**.
6. Ensure **Show in sidebar** is toggled on, then click **Start**.

### Method 2: Local Add-on Folder
1. Copy or clone the `marstek_venus_optimizer` directory into your Home Assistant `/addons/` directory.
2. In the Add-on Store, click **⋮** → **Check for updates**.
3. Install **Marstek Venus Battery Optimizer (Local)**.

---

## Configuration

Open the add-on from the sidebar ("Venus Optimizer") and navigate to the **Configuration** tab:
1. Click **Auto-Discover** to match your Marstek battery and P1 meter entities.
2. Verify your battery parameters (e.g., 5120 Wh capacity, 10% min SoC, 90% max SoC, 1500W max charge).
3. Set your monthly capacity tariff target (e.g., 2500 W).
4. Click **Save & Run Optimization Preview**.

---

## Operating Modes

- **Active Control**: Solves every 5 minutes and applies optimal charging/discharging setpoints to the battery.
- **Shadow (Dry-Run)**: Solves the model and logs every action it would take without modifying any Home Assistant entity.
- **Force Charge**: Forces continuous maximum charging up to the maximum SoC limit.
- **Hold Idle (0W)**: Keeps the battery at 0 W idle setpoint.
- **Auto CT**: Switches the battery to CT self-consumption mode to follow household load.
