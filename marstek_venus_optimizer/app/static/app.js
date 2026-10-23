// Venus Optimizer Ingress Frontend Logic
// All paths are relative (./api/...) for Home Assistant Ingress support

let chartInstance = null;
let currentPlan = null;

// Tab Switching
document.querySelectorAll('.tab-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(c => c.style.display = 'none');
    btn.classList.add('active');
    const tabId = `tab-${btn.dataset.tab}`;
    const target = document.getElementById(tabId);
    if (target) target.style.display = 'block';
    if (btn.dataset.tab === 'logs') refreshLogs();
  });
});

// Mode Selector
const modeSelect = document.getElementById('mode-select');
modeSelect.addEventListener('change', async (e) => {
  const newMode = e.target.value;
  try {
    const res = await fetch('./api/mode', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode: newMode })
    });
    if (res.ok) {
      refreshStatus();
    }
  } catch (err) {
    console.error('Failed to change mode', err);
  }
});

// Re-run solver button
document.getElementById('btn-solve-now').addEventListener('click', async () => {
  const btn = document.getElementById('btn-solve-now');
  btn.disabled = true;
  btn.textContent = 'Solving...';
  try {
    const res = await fetch('./api/solve?apply=true', { method: 'POST' });
    if (res.ok) {
      await refreshStatus();
      await refreshPlan();
    }
  } catch (err) {
    console.error('Solve error', err);
  } finally {
    btn.disabled = false;
    btn.innerHTML = `
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
        <path d="M21.5 2v6h-6M21.34 15.57a10 10 0 1 1-.57-8.38l5.67-5.67"></path>
      </svg> Re-run Solver
    `;
  }
});

// Fetch Status & Update KPIs
async function refreshStatus() {
  try {
    const res = await fetch('./api/status');
    if (!res.ok) return;
    const data = await res.json();

    // Mode dropdown sync
    if (data.mode && modeSelect.value !== data.mode) {
      modeSelect.value = data.mode;
    }

    // Badge
    const badge = document.getElementById('conn-badge');
    if (data.connected) {
      badge.textContent = data.dry_run ? 'Connected (Shadow Mode)' : 'Connected (Active)';
      badge.style.background = data.dry_run ? 'rgba(245, 158, 11, 0.2)' : 'rgba(16, 185, 129, 0.2)';
      badge.style.color = data.dry_run ? '#fbbf24' : '#34d399';
    } else {
      badge.textContent = 'Disconnected';
      badge.style.background = 'rgba(239, 68, 68, 0.2)';
      badge.style.color = '#f87171';
    }

    // Live KPIs
    const live = data.live || {};
    const presBadge = document.getElementById('presence-badge');
    if (presBadge) {
      if (live.presence != null) {
        presBadge.style.display = 'inline-block';
        if (live.is_away) {
          presBadge.textContent = '✈️ Away';
          presBadge.style.background = 'rgba(168, 85, 247, 0.2)';
          presBadge.style.color = '#c084fc';
        } else {
          presBadge.textContent = '🏠 Home';
          presBadge.style.background = 'rgba(56, 189, 248, 0.2)';
          presBadge.style.color = '#38bdf8';
        }
      } else {
        presBadge.style.display = 'none';
      }
    }

    if (live.soc != null) {
      document.getElementById('kpi-soc').textContent = Math.round(live.soc);
      const wh = live.soc_wh != null ? Math.round(live.soc_wh) : Math.round(live.soc * 51.2);
      document.getElementById('kpi-soc-wh').textContent = `${wh} Wh / 5120 Wh`;
    }

    const cmd = data.command || {};
    const eff = cmd.effective || {};
    document.getElementById('kpi-action').textContent = eff.label || 'Idle';
    if (cmd.throttled) {
      document.getElementById('kpi-power').textContent = `Throttled: ${eff.power_w || 0} W`;
      document.getElementById('kpi-power').style.color = '#f59e0b';
    } else {
      document.getElementById('kpi-power').textContent = `Power: ${eff.power_w || 0} W`;
      document.getElementById('kpi-power').style.color = 'var(--text-muted)';
    }

    const price = data.price || {};
    if (price.spot != null) {
      document.getElementById('kpi-price-spot').textContent = `€${price.spot.toFixed(3)}/kWh`;
      document.getElementById('kpi-price-allin').textContent = `All-in: €${(price.import || 0).toFixed(3)}/kWh`;
    }

    if (live.p1_w != null) {
      document.getElementById('kpi-p1').textContent = Math.round(live.p1_w);
      const avg = live.avg_demand_w != null ? Math.round(live.avg_demand_w) : '--';
      const target = live.peak_target_w != null ? Math.round(live.peak_target_w) : '--';
      document.getElementById('kpi-avg-demand').textContent = `15m Avg: ${avg} W (Target: ${target} W)`;
    }

    const planSummary = data.plan || {};
    if (planSummary.savings_24h_eur != null) {
      document.getElementById('kpi-savings').textContent = `€${planSummary.savings_24h_eur.toFixed(2)}`;
      document.getElementById('kpi-status-msg').textContent = `${planSummary.milp ? 'MILP' : 'LP'} (${planSummary.solve_ms || 0} ms)`;
    }
  } catch (err) {
    console.error('Failed to refresh status', err);
  }
}

// Fetch Plan and Render Chart & Table
async function refreshPlan() {
  try {
    const res = await fetch('./api/plan');
    if (!res.ok) return;
    const plan = await res.json();
    if (!plan || !plan.ok || !plan.slots) return;
    currentPlan = plan;

    renderChart(plan.slots);
    renderSchedule(plan.slots);
  } catch (err) {
    console.error('Failed to refresh plan', err);
  }
}

function renderChart(slots) {
  const ctx = document.getElementById('opt-chart').getContext('2d');
  const labels = slots.map(s => s.label);
  const spotPrices = slots.map(s => s.spot);
  const allinPrices = slots.map(s => s.import_price);
  const batteryPowers = slots.map(s => (s.action === 'charge' ? -s.charge_w : s.discharge_w));
  const socs = slots.map(s => s.soc_pct);

  const style = getComputedStyle(document.body);
  const textPrimary = style.getPropertyValue('--text-primary').trim() || '#f9fafb';
  const textSecondary = style.getPropertyValue('--text-secondary').trim() || '#9ca3af';
  const gridColor = style.getPropertyValue('--border-color').trim() || 'rgba(55, 65, 81, 0.4)';

  const chartData = {
    labels: labels,
    datasets: [
      {
        type: 'line',
        label: 'SoC (%)',
        data: socs,
        borderColor: '#10b981',
        backgroundColor: 'rgba(16, 185, 129, 0.1)',
        yAxisID: 'ySoC',
        tension: 0.3,
        borderWidth: 2,
        pointRadius: 0
      },
      {
        type: 'line',
        label: 'All-In Buy (€/kWh)',
        data: allinPrices,
        borderColor: '#ec4899',
        borderDash: [4, 4],
        yAxisID: 'yPrice',
        borderWidth: 2,
        pointRadius: 0
      },
      {
        type: 'bar',
        label: 'Battery Power (W, -Chg/+Dis)',
        data: batteryPowers,
        backgroundColor: slots.map(s => s.action === 'charge' ? 'rgba(16, 185, 129, 0.7)' : (s.action === 'discharge' ? 'rgba(59, 130, 246, 0.7)' : 'rgba(156, 163, 175, 0.2)')),
        yAxisID: 'yPower'
      }
    ]
  };

  if (chartInstance) {
    chartInstance.data = chartData;
    chartInstance.options.scales.x.ticks.color = textSecondary;
    chartInstance.options.scales.x.grid.color = gridColor;
    chartInstance.options.scales.yPower.ticks.color = textSecondary;
    chartInstance.options.scales.yPower.title.color = textSecondary;
    chartInstance.options.scales.yPower.grid.color = gridColor;
    chartInstance.options.plugins.legend.labels.color = textPrimary;
    chartInstance.update('none'); // Update without animation
    return;
  }

  chartInstance = new Chart(ctx, {
    type: 'bar',
    data: chartData,
    options: {
      animation: false, // Switch off all animation and flashing
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      scales: {
        x: {
          ticks: { color: textSecondary, maxTicksLimit: 14 },
          grid: { color: gridColor }
        },
        yPower: {
          type: 'linear',
          position: 'left',
          title: { display: true, text: 'Power (W)', color: textSecondary },
          ticks: { color: textSecondary },
          grid: { color: gridColor }
        },
        ySoC: {
          type: 'linear',
          position: 'right',
          min: 0,
          max: 100,
          title: { display: true, text: 'SoC (%)', color: '#10b981' },
          ticks: { color: '#10b981' },
          grid: { display: false }
        },
        yPrice: {
          type: 'linear',
          position: 'right',
          title: { display: false },
          ticks: { color: '#ec4899' },
          grid: { display: false }
        }
      },
      plugins: {
        legend: { labels: { color: textPrimary } }
      }
    }
  });
}

function renderSchedule(slots) {
  const tbody = document.querySelector('#schedule-table tbody');
  const now = new Date();
  tbody.innerHTML = '';

  slots.forEach(s => {
    const tr = document.createElement('tr');
    const stDate = new Date(s.start);
    const isCurrent = (now >= stDate && now < new Date(stDate.getTime() + 15 * 60000));
    if (isCurrent) tr.classList.add('current-slot');

    let actionClass = 'action-idle';
    let actionText = 'Idle';
    let powerDisplay = '0 W';

    if (s.action === 'charge') {
      actionClass = 'action-charge';
      actionText = 'Charge';
      powerDisplay = `-${Math.round(s.charge_w)} W`;
    } else if (s.action === 'discharge') {
      actionClass = 'action-discharge';
      actionText = 'Discharge';
      powerDisplay = `+${Math.round(s.discharge_w)} W`;
    }

    tr.innerHTML = `
      <td>${s.label}</td>
      <td><span class="action-tag ${actionClass}">${actionText}</span></td>
      <td>${powerDisplay}</td>
      <td>${Math.round(s.soc_pct)}%</td>
      <td>€${s.spot.toFixed(3)}</td>
      <td>€${s.import_price.toFixed(3)}</td>
      <td>${Math.round(s.load_w)} W</td>
      <td>${Math.round(s.grid_import_w - s.grid_export_w)} W</td>
      <td style="color: var(--text-secondary); font-size: 0.8rem;">${s.reason || ''}</td>
    `;
    tbody.appendChild(tr);
  });
}

// Config Form Handlers
async function loadConfig() {
  try {
    const res = await fetch('./api/config');
    if (!res.ok) return;
    const cfg = await res.json();
    populateForm(cfg);
    updateEntityPreviews(cfg.entities);
  } catch (err) {
    console.error('Failed to load config', err);
  }
}

function populateForm(cfg) {
  const form = document.getElementById('config-form');
  for (const [section, values] of Object.entries(cfg)) {
    for (const [key, val] of Object.entries(values)) {
      const input = form.elements[`${section}.${key}`];
      if (input) {
        input.value = val !== null ? val : '';
      }
    }
  }
}

document.getElementById('config-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const form = e.target;
  const statusEl = document.getElementById('save-status');
  statusEl.textContent = 'Saving & calculating preview...';
  statusEl.style.color = '#38bdf8';

  const payload = { entities: {}, battery: {}, tariff: {}, control: {} };
  for (const el of form.elements) {
    if (!el.name || !el.name.includes('.')) continue;
    const [sec, key] = el.name.split('.');
    let val = el.value;
    if (el.type === 'number') {
      if (typeof val === 'string') val = val.replace(',', '.');
      val = val === '' ? null : parseFloat(val);
    }
    payload[sec][key] = val;
  }

  try {
    const res = await fetch('./api/config?preview=true', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    const result = await res.json();
    if (res.ok && result.ok) {
      statusEl.textContent = 'Configuration saved successfully!';
      statusEl.style.color = '#34d399';
      if (result.plan && result.plan.slots) {
        renderChart(result.plan.slots);
        renderSchedule(result.plan.slots);
      }
      refreshStatus();
    } else {
      statusEl.textContent = `Error: ${JSON.stringify(result.errors || result.detail)}`;
      statusEl.style.color = '#ef4444';
    }
  } catch (err) {
    statusEl.textContent = `Save error: ${err.message}`;
    statusEl.style.color = '#ef4444';
  }
});

// Auto-discovery button
document.getElementById('btn-discover').addEventListener('click', async () => {
  const btn = document.getElementById('btn-discover');
  btn.disabled = true;
  btn.textContent = 'Scanning...';
  try {
    const res = await fetch('./api/discover');
    if (!res.ok) throw new Error('Discovery failed');
    const data = await res.json();
    if (data.suggestion) {
      const form = document.getElementById('config-form');
      for (const [k, v] of Object.entries(data.suggestion)) {
        const input = form.elements[`entities.${k}`];
        if (input && v) input.value = v;
      }
      updateEntityPreviews(data.suggestion);
    }
  } catch (err) {
    console.error('Discovery error', err);
  } finally {
    btn.disabled = false;
    btn.textContent = 'Auto-Discover';
  }
});

async function updateEntityPreviews(entities) {
  const ids = [
    entities.soc, entities.working_mode,
    entities.p1_power, entities.p1_avg_demand, entities.price, entities.presence
  ].filter(Boolean).join(',');

  if (!ids) return;
  try {
    const res = await fetch(`./api/entities?ids=${encodeURIComponent(ids)}`);
    if (!res.ok) return;
    const states = await res.json();

    const setPrev = (id, elId) => {
      const el = document.getElementById(elId);
      if (!el) return;
      const st = states[id];
      if (st && st.state != null) {
        el.textContent = `Live: ${st.state} ${st.unit || ''}`;
      } else {
        el.textContent = 'Live: Not found';
      }
    };

    setPrev(entities.soc, 'prev-soc');
    setPrev(entities.working_mode, 'prev-mode');
    setPrev(entities.p1_power, 'prev-p1-power');
    setPrev(entities.p1_avg_demand, 'prev-p1-avg');
    setPrev(entities.price, 'prev-price');
    setPrev(entities.presence, 'prev-presence');
  } catch (err) {

    console.error('Preview error', err);
  }
}

// Logs tab
async function refreshLogs() {
  try {
    const res = await fetch('./api/logs?limit=80');
    if (!res.ok) return;
    const data = await res.json();
    const tbody = document.querySelector('#logs-table tbody');
    tbody.innerHTML = '';
    (data.logs || []).forEach(l => {
      const tr = document.createElement('tr');
      const timeStr = new Date(l.ts).toLocaleTimeString();
      let color = 'var(--text-primary)';
      if (l.level === 'action') color = '#38bdf8';
      else if (l.level === 'error') color = '#ef4444';
      else if (l.level === 'warning') color = '#f59e0b';

      tr.innerHTML = `
        <td style="color: var(--text-muted); font-size: 0.8rem;">${timeStr}</td>
        <td style="font-weight: 600; text-transform: uppercase; font-size: 0.75rem; color: ${color};">${l.level}</td>
        <td style="font-size: 0.82rem; color: ${color};">${l.msg}</td>
      `;
      tbody.appendChild(tr);
    });
  } catch (err) {
    console.error('Failed to refresh logs', err);
  }
}

document.getElementById('btn-refresh-logs').addEventListener('click', refreshLogs);

// Home Assistant Theme Sync (Parent frame or system preference)
function syncHATheme() {
  let theme = null;

  // 1. Check if embedded inside Home Assistant iframe and accessible
  try {
    if (window.parent && window.parent !== window) {
      const parentDoc = window.parent.document;
      const haEl = parentDoc.querySelector('home-assistant') || parentDoc.documentElement;
      if (haEl) {
        // HA sets dark attribute or class on <html> or <home-assistant>
        const isDark = haEl.classList.contains('dark') ||
                       haEl.hasAttribute('dark') ||
                       (window.parent.hass && window.parent.hass.themes && window.parent.hass.themes.darkMode);
        if (isDark != null) {
          theme = isDark ? 'dark' : 'light';
        }
      }
    }
  } catch (e) {
    // Cross-origin restriction fallback
  }

  // 2. If not detected from parent, check OS / browser media query (Bert's profile "Auto")
  if (!theme) {
    theme = window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  }

  const currentTheme = document.documentElement.getAttribute('data-theme');
  if (currentTheme !== theme) {
    document.documentElement.setAttribute('data-theme', theme);
    // Re-render chart with new theme colors only when theme changed
    if (chartInstance && currentPlan && currentPlan.slots) {
      renderChart(currentPlan.slots);
    }
  }
}

// Listen for system / HA theme changes
window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', syncHATheme);
window.addEventListener('message', (ev) => {
  if (ev.data && (ev.data.theme || ev.data.darkMode != null)) {
    syncHATheme();
  }
});

// Initial Load & Refresh Intervals
window.addEventListener('DOMContentLoaded', () => {
  syncHATheme();
  refreshStatus();
  refreshPlan();
  loadConfig();

  setInterval(syncHATheme, 3000);
  setInterval(refreshStatus, 10000);
  setInterval(refreshPlan, 60000);
});
