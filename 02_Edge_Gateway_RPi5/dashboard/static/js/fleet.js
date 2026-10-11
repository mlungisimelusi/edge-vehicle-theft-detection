// ---------------------------------------------------------------------
// Fleet page — searchable table of every known vehicle, updated live over
// the shared WebSocket. Clicking a row opens its detail page.
// ---------------------------------------------------------------------
const rows = {}; // node_id -> latest (enriched) row
let filterText = '';

// Vehicles in trouble first, then stale ones, then healthy ones.
const STATE_ORDER = { danger: 0, stale: 1, ok: 2 };

function render() {
  const tbody = document.getElementById('fleet-tbody');
  const ids = Object.keys(rows)
    .filter(id => !filterText || id.includes(filterText))
    .sort((a, b) => {
      const sa = STATE_ORDER[window.Dashboard.vehicleState(rows[a]).cls];
      const sb = STATE_ORDER[window.Dashboard.vehicleState(rows[b]).cls];
      return sa !== sb ? sa - sb : a - b;
    });

  document.getElementById('fleet-empty').style.display = ids.length ? 'none' : 'block';
  document.getElementById('fleet-empty').textContent = Object.keys(rows).length
    ? 'No vehicle matches that search.' : 'No vehicles have reported in yet.';
  tbody.innerHTML = '';

  ids.forEach(nodeId => {
    const row = rows[nodeId];
    const st = window.Dashboard.vehicleState(row);
    const fix = row.last_valid_fix;
    const tr = document.createElement('tr');
    tr.onclick = () => { window.location.href = `/fleet/${nodeId}`; };
    tr.innerHTML = `
      <td><strong>Vehicle ${nodeId}</strong></td>
      <td><span class="badge ${st.cls}">${st.label}</span></td>
      <td>${row.gps_valid ? row.speed_kmph.toFixed(1) + ' km/h' : '—'}</td>
      <td>${window.Dashboard.volts(row.battery_millivolts)}</td>
      <td>${row.satellites}</td>
      <td>${row.rssi_dbm ?? '—'} dBm</td>
      <td>${fix ? `<span data-ts="${fix.received_at}">${window.Dashboard.relativeTime(fix.received_at)}</span>` : 'never'}</td>
      <td data-ts="${row.received_at}">${window.Dashboard.relativeTime(row.received_at)}</td>
    `;
    tbody.appendChild(tr);
  });
}

document.getElementById('fleet-search').addEventListener('input', (e) => {
  filterText = e.target.value.trim();
  render();
});

async function loadInitial() {
  const resp = await fetch('/api/latest');
  if (!resp.ok) return;
  const data = await resp.json();
  data.forEach(row => { rows[row.node_id] = row; });
  render();
}

window.Dashboard.on('snapshot', (data) => {
  data.latest.forEach(row => { rows[row.node_id] = row; });
  render();
});
window.Dashboard.on('telemetry', (row) => { rows[row.node_id] = row; render(); });
window.Dashboard.on('alert', (alert) => {
  if (!rows[alert.node_id]) return;
  rows[alert.node_id] = window.Dashboard.applyAlertToRow(rows[alert.node_id], alert);
  render();
});
window.Dashboard.on('tick', render);

loadInitial();
