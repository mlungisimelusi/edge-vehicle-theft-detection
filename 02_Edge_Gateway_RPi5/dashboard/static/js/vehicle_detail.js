// ---------------------------------------------------------------------
// Vehicle Detail page — one vehicle's current state, last known position
// and route, alert history, and speed/battery history charts.
// ---------------------------------------------------------------------
let historyHours = 24;
let speedChart, batteryChart, marker, routeLine;
let currentRow = null;
const MAX_CHART_POINTS = 5000;
const DEFAULT_CENTER = [-29.1211, 26.2140];   // Bloemfontein, until a fix is known

const map = L.map('vd-map', { zoomControl: true }).setView(DEFAULT_CENTER, 13);
window.Dashboard.addBaseMaps(map);
routeLine = L.polyline([], { color: '#3b82f6', weight: 3, opacity: 0.6 }).addTo(map);

function isThisVehicle(nodeId) {
  return String(nodeId) === String(NODE_ID);
}

function renderState() {
  if (!currentRow) return;
  const st = window.Dashboard.vehicleState(currentRow);
  const badge = document.getElementById('vd-status-badge');
  badge.textContent = st.label;
  badge.className = `badge ${st.cls}`;
}

function applyRow(row) {
  if (!isThisVehicle(row.node_id)) return;
  if (!row.active_alert && currentRow && currentRow.active_alert) {
    row = { ...row, active_alert: currentRow.active_alert };
  }
  currentRow = row;
  document.getElementById('vd-nodata').style.display = 'none';
  renderState();

  // Speed/heading only mean something with a fresh fix in THIS packet.
  document.getElementById('vd-speed').textContent = row.gps_valid ? `${row.speed_kmph.toFixed(1)} km/h` : '—';
  document.getElementById('vd-battery').textContent = window.Dashboard.volts(row.battery_millivolts);
  document.getElementById('vd-sats').textContent = row.satellites;
  document.getElementById('vd-signal').textContent = `${row.rssi_dbm ?? '—'} dBm / ${row.snr_db ?? '—'} dB`;
  const lastSeenEl = document.getElementById('vd-lastseen');
  lastSeenEl.dataset.ts = row.received_at;
  lastSeenEl.textContent = window.Dashboard.relativeTime(row.received_at);

  // Position = this packet's fix, else the last known fix.
  const fix = row.gps_valid
    ? { latitude: row.latitude, longitude: row.longitude, received_at: row.received_at }
    : row.last_valid_fix;
  const posEl = document.getElementById('vd-position');
  const linkEl = document.getElementById('vd-maplink');
  if (!fix) {
    posEl.textContent = 'No GPS fix received from this vehicle yet.';
    linkEl.style.display = 'none';
    return;
  }
  const latlng = [fix.latitude, fix.longitude];
  posEl.innerHTML = `${fix.latitude.toFixed(6)}, ${fix.longitude.toFixed(6)} &middot; fix
    <span data-ts="${fix.received_at}">${window.Dashboard.relativeTime(fix.received_at)}</span>`;
  linkEl.href = `https://maps.google.com/?q=${fix.latitude.toFixed(6)},${fix.longitude.toFixed(6)}`;
  linkEl.style.display = 'inline';
  if (!marker) {
    marker = L.marker(latlng).addTo(map);
    map.setView(latlng, 15);
  } else {
    marker.setLatLng(latlng);
    map.panTo(latlng);
  }
}

function renderAlerts(alerts) {
  const container = document.getElementById('vd-alerts');
  if (!alerts.length) {
    container.innerHTML = '<div class="empty-hint">No alerts recorded for this vehicle yet.</div>';
    return;
  }
  container.innerHTML = alerts.map(a => {
    const mapsLink = `https://maps.google.com/?q=${a.latitude.toFixed(6)},${a.longitude.toFixed(6)}`;
    return `
      <div class="alert-row">
        <svg viewBox="0 0 24 24"><path d="M12 2 1 21h22zm0 4.5 7.5 13h-15zM11 10h2v5h-2zm0 6h2v2h-2z"/></svg>
        <div>
          <div class="a-title">${window.Dashboard.reasonLabel(a.reason)}</div>
          <div class="a-meta">${new Date(a.sent_at * 1000).toLocaleString()} &middot; ${a.speed_kmph.toFixed(0)} km/h &middot; ${window.Dashboard.smsLabel(a)}</div>
          ${window.Dashboard.hasFix(a)
            ? `<a class="maplink" href="${mapsLink}" target="_blank" rel="noopener">view last known location &rarr;</a>`
            : '<span class="a-meta">no GPS fix on record</span>'}
        </div>
      </div>`;
  }).join('');
}

function makeGradient(ctx, colorRgb) {
  const g = ctx.createLinearGradient(0, 0, 0, 200);
  g.addColorStop(0, `rgba(${colorRgb},0.35)`);
  g.addColorStop(1, `rgba(${colorRgb},0)`);
  return g;
}

function timeLabel(epoch) {
  const d = new Date(epoch * 1000);
  // Over a multi-day window a bare time is ambiguous — include the date.
  return historyHours > 24
    ? d.toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
    : d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

function initCharts() {
  const commonOptions = {
    responsive: true, maintainAspectRatio: false,
    scales: {
      x: { ticks: { color: '#8992a8', maxTicksLimit: 7, font: { size: 10.5 } }, grid: { color: 'rgba(148,163,196,0.08)' } },
      y: { ticks: { color: '#8992a8', font: { size: 10.5 } }, grid: { color: 'rgba(148,163,196,0.08)' } },
    },
    plugins: { legend: { display: false } },
    animation: false, elements: { point: { radius: 0, hoverRadius: 4 } },
  };
  const speedCtx = document.getElementById('speedChart').getContext('2d');
  speedChart = new Chart(speedCtx, {
    type: 'line',
    data: { labels: [], datasets: [{ data: [], borderColor: '#3b82f6', backgroundColor: makeGradient(speedCtx, '59,130,246'), fill: true, tension: 0.3, borderWidth: 2 }] },
    options: commonOptions,
  });
  const battCtx = document.getElementById('batteryChart').getContext('2d');
  batteryChart = new Chart(battCtx, {
    type: 'line',
    data: { labels: [], datasets: [{ data: [], borderColor: '#22c55e', backgroundColor: makeGradient(battCtx, '34,197,94'), fill: true, tension: 0.3, borderWidth: 2 }] },
    options: commonOptions,
  });
}

function pushPoint(chart, label, value) {
  chart.data.labels.push(label);
  chart.data.datasets[0].data.push(value);
  if (chart.data.labels.length > MAX_CHART_POINTS) {
    chart.data.labels.shift();
    chart.data.datasets[0].data.shift();
  }
}

async function loadHistory() {
  const resp = await fetch(`/api/history?node_id=${NODE_ID}&hours=${historyHours}`);
  if (!resp.ok) return;
  const rowsData = await resp.json();
  // Speed is only plotted from packets with a fresh fix — stale-GPS packets
  // report 0 km/h, which would draw false "stopped" dips.
  const withFix = rowsData.filter(r => r.gps_valid);
  speedChart.data.labels = withFix.map(r => timeLabel(r.received_at));
  speedChart.data.datasets[0].data = withFix.map(r => r.speed_kmph);
  speedChart.update();
  // 0 mV = battery not measured — leave those out rather than plot 0 V.
  const withBattery = rowsData.filter(r => r.battery_millivolts > 0);
  batteryChart.data.labels = withBattery.map(r => timeLabel(r.received_at));
  batteryChart.data.datasets[0].data = withBattery.map(r => r.battery_millivolts / 1000);
  batteryChart.update();

  routeLine.setLatLngs(withFix.map(r => [r.latitude, r.longitude]));
  document.getElementById('vd-route-note').textContent = withFix.length
    ? `Route over the selected window: ${withFix.length} GPS fixes.`
    : 'No GPS fixes in the selected window.';
}

async function loadAlerts() {
  const resp = await fetch(`/api/alerts?node_id=${NODE_ID}&limit=50`);
  if (!resp.ok) return;
  renderAlerts(await resp.json());
}

document.querySelectorAll('.btn[data-hours]').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.btn[data-hours]').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    historyHours = parseFloat(btn.dataset.hours);
    loadHistory();
  });
});

window.Dashboard.on('snapshot', (data) => {
  const row = data.latest.find(r => isThisVehicle(r.node_id));
  if (row) applyRow(row);
});
window.Dashboard.on('telemetry', (row) => {
  if (!isThisVehicle(row.node_id)) return;
  applyRow(row);
  const label = timeLabel(row.received_at);
  if (row.gps_valid) {
    pushPoint(speedChart, label, row.speed_kmph);
    routeLine.addLatLng([row.latitude, row.longitude]);
  }
  if (row.battery_millivolts > 0) pushPoint(batteryChart, label, row.battery_millivolts / 1000);
  speedChart.update();
  batteryChart.update();
});
window.Dashboard.on('alert', (alert) => {
  if (!isThisVehicle(alert.node_id)) return;
  if (currentRow) currentRow = window.Dashboard.applyAlertToRow(currentRow, alert);
  renderState();
  loadAlerts();
});
window.Dashboard.on('alert_update', (alert) => {
  if (isThisVehicle(alert.node_id)) loadAlerts();
});
window.Dashboard.on('tick', renderState);

initCharts();
loadHistory();
loadAlerts();

fetch(`/api/latest?node_id=${NODE_ID}`).then(r => r.ok ? r.json() : []).then(rowsData => {
  if (rowsData.length) applyRow(rowsData[0]);
  else document.getElementById('vd-nodata').style.display = 'block';
});
