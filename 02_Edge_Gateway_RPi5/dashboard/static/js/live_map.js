// ---------------------------------------------------------------------
// Live Map page — Leaflet map with animated, status-coloured vehicle
// markers, each vehicle's recent trail, and a compact fleet sidebar. Full
// per-vehicle detail lives on the Fleet page; this page is the live picture.
// ---------------------------------------------------------------------
const nodes = {};
let selectedNodeId = null;
let initialCenterDone = false;
let followMode = true;
const MAX_LIVE_TRAIL_POINTS = 300;
const TRAIL_HISTORY_HOURS = 2;
const DEFAULT_CENTER = [-29.1211, 26.2140];   // Bloemfontein, until a vehicle reports

const map = L.map('map', { zoomControl: true }).setView(DEFAULT_CENTER, 13);
window.Dashboard.addBaseMaps(map);

const recenterBtn = document.getElementById('recenter-btn');
map.on('dragstart', () => { followMode = false; recenterBtn.classList.add('visible'); });
recenterBtn.addEventListener('click', () => {
  followMode = true;
  recenterBtn.classList.remove('visible');
  const entry = selectedNodeId && nodes[selectedNodeId];
  if (entry && entry.lastValidLatLng) map.flyTo(entry.lastValidLatLng, Math.max(map.getZoom(), 15), { duration: 0.9 });
});

function buildVehicleIcon(row) {
  const status = window.Dashboard.vehicleState(row).cls;
  // Heading is only meaningful with a fresh fix while moving.
  const heading = (row.gps_valid && Number.isFinite(row.heading_deg)) ? row.heading_deg : 0;
  const html = `
    <div class="vmarker vmarker-${status}">
      <div class="vmarker-pulse"></div>
      <svg class="vmarker-arrow" viewBox="0 0 24 24" style="transform: rotate(${heading}deg)">
        <path d="M12 1.5 L20 21 L12 16.5 L4 21 Z"/>
      </svg>
    </div>`;
  return L.divIcon({ html, className: 'vmarker-container', iconSize: [36, 36], iconAnchor: [18, 18] });
}

function tooltipText(nodeId, row) {
  return `Vehicle ${nodeId} — ${window.Dashboard.vehicleState(row).label}`;
}

function animateMarkerMove(entry, from, to, duration = 800) {
  if (entry.animFrame) cancelAnimationFrame(entry.animFrame);
  const start = performance.now();
  const [lat1, lon1] = from, [lat2, lon2] = to;
  function step(now) {
    const t = Math.min(1, (now - start) / duration);
    const ease = t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2;
    entry.marker.setLatLng([lat1 + (lat2 - lat1) * ease, lon1 + (lon2 - lon1) * ease]);
    if (t < 1) entry.animFrame = requestAnimationFrame(step); else entry.animFrame = null;
  }
  entry.animFrame = requestAnimationFrame(step);
}

function ensureMarker(nodeId, entry, latlng) {
  if (entry.marker) return;
  entry.marker = L.marker(latlng, { icon: buildVehicleIcon(entry.lastRow) }).addTo(map);
  entry.marker.on('click', () => selectNode(nodeId));
  entry.marker.bindTooltip(tooltipText(nodeId, entry.lastRow), { direction: 'top', offset: [0, -14] });
  entry.trail = L.polyline(entry.trailPoints, { color: '#3b82f6', weight: 3, opacity: 0.5 }).addTo(map);
}

function refreshMarker(nodeId, entry) {
  if (!entry.marker) return;
  entry.marker.setIcon(buildVehicleIcon(entry.lastRow));
  entry.marker.setTooltipContent(tooltipText(nodeId, entry.lastRow));
}

// Pre-fill the trail with the last couple of hours, so opening the page
// shows where the vehicle has been, not just a single dot.
async function loadTrail(nodeId, entry) {
  entry.trailRequested = true;
  try {
    const resp = await fetch(`/api/history?node_id=${nodeId}&hours=${TRAIL_HISTORY_HOURS}`);
    if (!resp.ok) return;
    const history = (await resp.json()).filter(r => r.gps_valid).map(r => [r.latitude, r.longitude]);
    entry.trailPoints = history.concat(entry.trailPoints).slice(-MAX_LIVE_TRAIL_POINTS);
    if (entry.trail) entry.trail.setLatLngs(entry.trailPoints);
  } catch (err) {
    console.warn('Could not load trail for vehicle', nodeId, err);
  }
}

function upsertNodeData(nodeId, row) {
  let entry = nodes[nodeId];
  if (!entry) {
    entry = { marker: null, trail: null, trailPoints: [], lastRow: row, lastValidLatLng: null,
              animFrame: null, trailRequested: false };
    nodes[nodeId] = entry;
  }
  // Keep a still-active alert if this row doesn't carry one (e.g. snapshot
  // race), since alerts arrive on a separate message.
  if (!row.active_alert && entry.lastRow && entry.lastRow.active_alert) {
    row = { ...row, active_alert: entry.lastRow.active_alert };
  }
  entry.lastRow = row;

  // Where to draw the vehicle: this packet's fix if it has one, otherwise
  // its last known fix — a motion packet sent before the GPS locks must not
  // make the vehicle vanish from the map.
  let latlng = null;
  if (row.gps_valid) latlng = [row.latitude, row.longitude];
  else if (row.last_valid_fix) latlng = [row.last_valid_fix.latitude, row.last_valid_fix.longitude];

  if (latlng) {
    const moved = !entry.lastValidLatLng ||
      entry.lastValidLatLng[0] !== latlng[0] || entry.lastValidLatLng[1] !== latlng[1];
    if (!entry.marker) {
      ensureMarker(nodeId, entry, latlng);
    } else if (moved) {
      animateMarkerMove(entry, entry.lastValidLatLng || latlng, latlng);
    }
    refreshMarker(nodeId, entry);

    if (row.gps_valid && moved) {
      entry.trailPoints.push(latlng);
      if (entry.trailPoints.length > MAX_LIVE_TRAIL_POINTS) entry.trailPoints.shift();
      entry.trail.setLatLngs(entry.trailPoints);
    }
    entry.lastValidLatLng = latlng;

    if (!initialCenterDone) {
      map.setView(latlng, 16);
      initialCenterDone = true;
    } else if (moved && followMode && (!selectedNodeId || String(nodeId) === String(selectedNodeId))) {
      map.panTo(latlng, { animate: true, duration: 0.8 });
    }
  }

  if (!entry.trailRequested) loadTrail(nodeId, entry);
  renderVehicleList();
}

function renderVehicleList() {
  const container = document.getElementById('vehicle-list');
  const ids = Object.keys(nodes);
  document.getElementById('empty-hint').style.display = ids.length ? 'none' : 'block';
  container.querySelectorAll('.mini-vehicle-card').forEach(el => el.remove());

  ids.sort((a, b) => a - b).forEach(nodeId => {
    const entry = nodes[nodeId];
    const row = entry.lastRow;
    const state = window.Dashboard.vehicleState(row);
    const where = entry.lastValidLatLng ? '' : ' &middot; no position yet';

    const card = document.createElement('div');
    card.className = 'mini-vehicle-card' + (String(nodeId) === String(selectedNodeId) ? ' selected' : '');
    card.onclick = () => selectNode(nodeId);
    card.innerHTML = `
      <div class="mvc-row"><span class="status-dot ${state.cls}"></span>Vehicle ${nodeId}
        <span class="badge ${state.cls}" style="margin-left:auto;">${state.label}</span></div>
      <div class="mvc-meta">${row.gps_valid ? row.speed_kmph.toFixed(0) + ' km/h &middot; ' : ''}<span data-ts="${row.received_at}">${window.Dashboard.relativeTime(row.received_at)}</span>${where}</div>
      <div class="mvc-meta"><a href="/fleet/${nodeId}" style="color:var(--accent-2)" onclick="event.stopPropagation()">view details &rarr;</a></div>
    `;
    container.appendChild(card);
  });
}

function selectNode(nodeId) {
  selectedNodeId = String(nodeId);
  followMode = true;
  recenterBtn.classList.remove('visible');
  const entry = nodes[nodeId];
  if (entry && entry.lastValidLatLng) map.flyTo(entry.lastValidLatLng, Math.max(map.getZoom(), 15), { duration: 0.9 });
  renderVehicleList();
}

window.Dashboard.on('snapshot', (data) => {
  data.latest.forEach(row => upsertNodeData(row.node_id, row));
  if (!selectedNodeId && data.latest.length) selectNode(data.latest[0].node_id);
});
window.Dashboard.on('telemetry', (row) => upsertNodeData(row.node_id, row));
window.Dashboard.on('alert', (alert) => {
  const entry = nodes[alert.node_id];
  if (!entry || !window.Dashboard.isThreat(alert)) return;
  entry.lastRow = window.Dashboard.applyAlertToRow(entry.lastRow, alert);
  refreshMarker(alert.node_id, entry);
  renderVehicleList();
});
// Time-based state (no signal, gps lost, alert expiry) needs re-evaluating
// even when nothing new arrives.
window.Dashboard.on('tick', () => {
  Object.keys(nodes).forEach(id => refreshMarker(id, nodes[id]));
  renderVehicleList();
});
