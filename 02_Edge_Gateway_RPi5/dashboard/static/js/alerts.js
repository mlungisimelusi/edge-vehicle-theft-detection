// ---------------------------------------------------------------------
// Alerts page — the full incident log across the fleet, filterable by
// vehicle, with new alerts prepended live over the shared WebSocket.
// ---------------------------------------------------------------------
let allAlerts = [];
let nodeFilter = '';

function renderAlerts() {
  const container = document.getElementById('alerts-list');
  const filtered = nodeFilter ? allAlerts.filter(a => String(a.node_id) === nodeFilter) : allAlerts;
  if (!filtered.length) {
    container.innerHTML = '<div class="empty-hint">No alerts recorded yet.</div>';
    return;
  }
  container.innerHTML = filtered.map(a => {
    // (0, 0) means the vehicle had no GPS fix on record when the alert fired.
    const hasFix = window.Dashboard.hasFix(a);
    const mapsLink = `https://maps.google.com/?q=${a.latitude.toFixed(6)},${a.longitude.toFixed(6)}`;
    return `
      <div class="alert-row">
        <svg viewBox="0 0 24 24"><path d="M12 2 1 21h22zm0 4.5 7.5 13h-15zM11 10h2v5h-2zm0 6h2v2h-2z"/></svg>
        <div style="flex:1;">
          <div class="a-title">Vehicle <a href="/fleet/${a.node_id}" style="color:#fecaca;text-decoration:underline;">${a.node_id}</a> — ${window.Dashboard.reasonLabel(a.reason)}</div>
          <div class="a-meta">${new Date(a.sent_at * 1000).toLocaleString()} &middot; ${a.speed_kmph.toFixed(0)} km/h &middot; score ${a.score !== null && a.score !== undefined ? a.score.toFixed(3) : '—'} &middot; ${window.Dashboard.smsLabel(a)}</div>
          ${hasFix
            ? `<a class="maplink" href="${mapsLink}" target="_blank" rel="noopener">view last known location &rarr;</a>`
            : '<span class="a-meta">no GPS fix on record</span>'}
        </div>
      </div>`;
  }).join('');
}

async function loadNodeOptions() {
  const resp = await fetch('/api/nodes');
  if (!resp.ok) return;
  const ids = await resp.json();
  const select = document.getElementById('alert-node-filter');
  ids.forEach(id => {
    const opt = document.createElement('option');
    opt.value = id; opt.textContent = `Vehicle ${id}`;
    select.appendChild(opt);
  });
}

document.getElementById('alert-node-filter').addEventListener('change', (e) => {
  nodeFilter = e.target.value;
  renderAlerts();
});

async function loadAlerts() {
  const resp = await fetch('/api/alerts?limit=200');
  if (!resp.ok) return;
  allAlerts = await resp.json();
  renderAlerts();
}

window.Dashboard.on('alert', (alert) => {
  if (allAlerts.some(a => a.id === alert.id)) return;
  allAlerts.unshift(alert);
  renderAlerts();
});
// An alert's SMS was confirmed after the alert row was first shown.
window.Dashboard.on('alert_update', (alert) => {
  const i = allAlerts.findIndex(a => a.id === alert.id);
  if (i >= 0) { allAlerts[i] = alert; renderAlerts(); }
});
// "SMS pending" becomes "SMS not sent" after 30 min without a new message.
window.Dashboard.on('tick', renderAlerts);

loadNodeOptions();
loadAlerts();
