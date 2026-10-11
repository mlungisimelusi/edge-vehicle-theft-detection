// ---------------------------------------------------------------------
// Shared across every page: one WebSocket connection, a tiny pub/sub so
// each page's own script only has to care about the message types it
// actually uses, the single definition of "what state is this vehicle in",
// plus small formatting helpers.
//
// Message types emitted to pages:
//   snapshot      initial state on (re)connect
//   telemetry     a new packet (row enriched with last_valid_fix/active_alert)
//   alert         a new alert row
//   alert_update  an existing alert changed (its SMS was confirmed sent)
//   tick          every 15 s, so pages re-evaluate time-based state (a
//                 vehicle that stops reporting must turn "no signal" even
//                 though no new message arrives to trigger a re-render)
// ---------------------------------------------------------------------
window.Dashboard = (function () {
  const STALE_AFTER_SECONDS = 900;      // no packet for 15 min -> "no signal"
  const GPS_STALE_SECONDS = 600;        // no fresh fix for 10 min -> "gps lost"
  const ALERT_ACTIVE_SECONDS = 1800;    // stays red for 30 min after a threat alert
  const TICK_MS = 15000;

  const listeners = {};
  let ws = null;
  let reconnectDelay = 1000;

  function relativeTime(epochSeconds) {
    if (epochSeconds === null || epochSeconds === undefined) return '—';
    const diff = Math.max(0, (Date.now() / 1000) - epochSeconds);
    if (diff < 5) return 'just now';
    if (diff < 60) return `${Math.floor(diff)}s ago`;
    if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
    if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
    return `${Math.floor(diff / 86400)}d ago`;
  }

  function durationFromSeconds(totalSeconds) {
    if (totalSeconds === null || totalSeconds === undefined) return '—';
    const d = Math.floor(totalSeconds / 86400);
    const h = Math.floor((totalSeconds % 86400) / 3600);
    const m = Math.floor((totalSeconds % 3600) / 60);
    if (d > 0) return `${d}d ${h}h`;
    if (h > 0) return `${h}h ${m}m`;
    return `${m}m`;
  }

  // 0 mV means the node isn't measuring its battery (no divider wired).
  function volts(millivolts) {
    if (millivolts === null || millivolts === undefined) return '—';
    if (millivolts <= 0) return 'not measured';
    return `${(millivolts / 1000).toFixed(2)} V`;
  }

  // One definition of vehicle state, used by the map, fleet table and
  // detail page so they can never disagree.
  //   danger  a threat alert in the last 30 min (theft alert / contact lost)
  //   stale   no packet for 15 min, or no fresh GPS fix for 10 min
  //   ok      reporting, with a recent GPS fix
  // State comes from ALERTS, not the per-packet is_anomaly flag: with 5%
  // contamination the model flags the odd normal packet, while an alert is
  // what the gateway actually acted on. And it stays red for a while, so a
  // stolen car doesn't turn green on its next normal-looking packet.
  function vehicleState(row) {
    const now = Date.now() / 1000;
    const alert = row.active_alert;
    if (alert && now - alert.sent_at < ALERT_ACTIVE_SECONDS) {
      if (alert.reason !== 'loss_of_contact') return { cls: 'danger', label: 'theft alert' };
      if (row.received_at <= alert.sent_at) return { cls: 'danger', label: 'contact lost' };
      // Contact lost, but the vehicle has reported again since — fall through.
    }
    if (now - row.received_at > STALE_AFTER_SECONDS) return { cls: 'stale', label: 'no signal' };
    const fix = row.last_valid_fix;
    if (!fix) return { cls: 'stale', label: 'no gps fix' };
    if (now - fix.received_at > GPS_STALE_SECONDS) return { cls: 'stale', label: 'gps lost' };
    return { cls: 'ok', label: 'live' };
  }

  // Merge a new alert into a vehicle row so vehicleState() sees it at once.
  function applyAlertToRow(row, alert) {
    if (!row || !isThreat(alert)) return row;
    return { ...row, active_alert: { id: alert.id, reason: alert.reason, sent_at: alert.sent_at } };
  }

  // Human-readable labels for the gateway's alert reason codes.
  const REASON_LABELS = {
    coldstart_hardware_motion_flag: 'Motion detected (learning period)',
    isolation_forest: 'Unusual movement pattern (Isolation Forest)',
    isolation_forest_context: 'Motion at an unusual time (Isolation Forest)',
    loss_of_contact: 'Contact lost — out of range or jammed?',
    location_update: 'Location update (follow-up)',
  };

  function reasonLabel(reason) {
    return REASON_LABELS[reason] || reason;
  }

  // The gateway records an alert the moment it is raised and flips sms_sent
  // once the modem confirms delivery, so a recent unsent alert is "pending".
  function smsLabel(alert) {
    if (alert.sms_sent) return 'SMS sent';
    const ageSeconds = (Date.now() / 1000) - alert.sent_at;
    return ageSeconds < 1800 ? 'SMS pending' : 'SMS not sent';
  }

  // Location-update rows are follow-up SMSes, not new threats.
  function isThreat(alert) {
    return alert.reason !== 'location_update';
  }

  function hasFix(alert) {
    return !(alert.latitude === 0 && alert.longitude === 0);
  }

  function escapeHtml(value) {
    return String(value).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  // Base maps that need no API key. CARTO's basemaps started requiring a
  // key, so the map fell back to an "API KEY REQUIRED" placeholder.
  //   Dark       OpenStreetMap tiles, darkened with a CSS filter to match
  //              the dashboard theme (see .tiles-dark in style.css)
  //   Streets    standard OpenStreetMap
  //   Satellite  Esri World Imagery
  // The user's choice is remembered per browser.
  const BASEMAP_STORAGE_KEY = 'vtd.basemap';

  function addBaseMaps(map) {
    const osmAttribution = '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors';
    const layers = {
      'Dark': L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
        maxZoom: 19, attribution: osmAttribution, className: 'tiles-dark',
      }),
      'Streets': L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
        maxZoom: 19, attribution: osmAttribution,
      }),
      'Satellite': L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}', {
        maxZoom: 19, maxNativeZoom: 18,
        attribution: 'Imagery &copy; Esri, Vantor, Earthstar Geographics, and the GIS User Community',
      }),
    };

    let chosen = 'Dark';
    try {
      const saved = localStorage.getItem(BASEMAP_STORAGE_KEY);
      if (saved && layers[saved]) chosen = saved;
    } catch (err) { /* storage unavailable — use the default */ }
    layers[chosen].addTo(map);

    L.control.layers(layers, null, { position: 'topright' }).addTo(map);
    map.on('baselayerchange', (e) => {
      try { localStorage.setItem(BASEMAP_STORAGE_KEY, e.name); } catch (err) { /* ignore */ }
    });
    return layers;
  }

  function on(type, handler) {
    if (!listeners[type]) listeners[type] = [];
    listeners[type].push(handler);
  }

  function emit(type, data) {
    (listeners[type] || []).forEach(fn => {
      try { fn(data); } catch (err) { console.error('Dashboard handler error:', err); }
    });
  }

  function setConnUI(connected, reconnecting) {
    const dot = document.getElementById('conn-dot');
    const label = document.getElementById('conn-status');
    if (!dot || !label) return;
    if (connected) {
      dot.classList.add('connected');
      label.textContent = 'live';
    } else {
      dot.classList.remove('connected');
      label.textContent = reconnecting ? 'reconnecting…' : 'connecting…';
    }
  }

  function connect() {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    ws = new WebSocket(`${proto}://${location.host}/ws`);

    ws.onopen = () => { setConnUI(true); reconnectDelay = 1000; };
    ws.onclose = () => {
      setConnUI(false, true);
      // Back off up to 15 s so a stopped server isn't hammered by every tab.
      setTimeout(connect, reconnectDelay);
      reconnectDelay = Math.min(reconnectDelay * 2, 15000);
    };
    ws.onerror = () => ws.close();
    ws.onmessage = (event) => {
      const msg = JSON.parse(event.data);
      emit(msg.type, msg.data);
    };
  }

  document.addEventListener('DOMContentLoaded', connect);
  setInterval(() => emit('tick'), TICK_MS);

  return {
    on, relativeTime, durationFromSeconds, volts, vehicleState, applyAlertToRow,
    reasonLabel, smsLabel, isThreat, hasFix, escapeHtml, addBaseMaps,
  };
})();

// Keep every "…ago" timestamp on the page fresh without page-specific code.
setInterval(() => {
  document.querySelectorAll('[data-ts]').forEach(el => {
    const ts = parseFloat(el.dataset.ts);
    if (Number.isFinite(ts)) el.textContent = window.Dashboard.relativeTime(ts);
  });
}, 1000);
