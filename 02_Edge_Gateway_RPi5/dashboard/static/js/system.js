// ---------------------------------------------------------------------
// System page — polls a snapshot REST endpoint rather than the WebSocket,
// since these are slow-moving health metrics, not per-packet telemetry.
// ---------------------------------------------------------------------
function fmtBytes(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

async function load() {
  const resp = await fetch('/api/system/status');
  if (!resp.ok) return;
  const s = await resp.json();

  // --- Gateway heartbeat (written by pi_gateway.py every ~10 s) ---
  const gw = s.gateway;
  const gwEl = document.getElementById('sys-gw-status');
  const gwSub = document.getElementById('sys-gw-sub');
  const gsmEl = document.getElementById('sys-gsm-status');
  const gsmSub = document.getElementById('sys-gsm-sub');
  if (!gw) {
    gwEl.innerHTML = '<span class="status-dot muted"></span> unknown';
    gwSub.textContent = 'gateway has not reported yet';
    gsmEl.innerHTML = '<span class="status-dot muted"></span> unknown';
    gsmSub.textContent = '—';
    document.getElementById('sys-pending').textContent = '—';
    document.getElementById('sys-lora-packets').textContent = '—';
  } else if (!gw.running) {
    gwEl.innerHTML = '<span class="status-dot danger"></span> not running';
    gwSub.textContent = `last heartbeat ${window.Dashboard.relativeTime(Date.now() / 1000 - gw.seconds_since_heartbeat)} — no detection or alerts`;
    gsmEl.innerHTML = '<span class="status-dot muted"></span> unknown';
    gsmSub.textContent = 'gateway is down';
    document.getElementById('sys-pending').textContent = '—';
    document.getElementById('sys-lora-packets').textContent = '—';
  } else {
    gwEl.innerHTML = '<span class="status-dot ok"></span> running';
    gwSub.textContent = `up ${window.Dashboard.durationFromSeconds(gw.uptime_seconds)} · loss-of-contact alerts ${gw.loss_of_contact_alerts ? 'on' : 'off'}`;
    if (!gw.alert_numbers) {
      gsmEl.innerHTML = '<span class="status-dot stale"></span> no numbers';
      gsmSub.textContent = 'add alert numbers to gateway_config.json';
    } else if (gw.gsm_ready) {
      gsmEl.innerHTML = '<span class="status-dot ok"></span> ready';
      gsmSub.textContent = `${gw.gsm_port || 'serial'} · ${gw.alert_numbers} number(s)`;
    } else {
      gsmEl.innerHTML = '<span class="status-dot danger"></span> not ready';
      gsmSub.textContent = 'modem not responding or not registered — retrying';
    }
    document.getElementById('sys-pending').textContent = gw.pending_alerts;
    document.getElementById('sys-lora-packets').textContent = gw.lora_packets.toLocaleString();
    document.getElementById('sys-lora-crc').textContent =
      `packets since gateway start · ${gw.lora_crc_errors} CRC error(s)`;
  }

  const loraEl = document.getElementById('sys-lora-status');
  const loraSub = document.getElementById('sys-lora-sub');
  const dotClass = s.lora_link_status === 'linked' ? 'ok' : (s.lora_link_status === 'stale' ? 'stale' : 'muted');
  loraEl.innerHTML = `<span class="status-dot ${dotClass}"></span> ${s.lora_link_status}`;
  loraSub.textContent = s.seconds_since_last_packet !== null
    ? `last packet ${window.Dashboard.relativeTime(Date.now() / 1000 - s.seconds_since_last_packet)}`
    : 'no packets received yet';

  document.getElementById('sys-vehicle-count').textContent = s.known_vehicle_count;
  document.getElementById('sys-clients').textContent = s.connected_dashboard_clients;
  document.getElementById('sys-uptime').textContent = window.Dashboard.durationFromSeconds(s.dashboard_uptime_seconds);

  document.getElementById('sys-telemetry-count').textContent = s.telemetry_row_count.toLocaleString();
  document.getElementById('sys-alert-count').textContent = s.alert_row_count.toLocaleString();
  document.getElementById('sys-db-size').textContent = fmtBytes(s.db_size_bytes);
  document.getElementById('sys-db-path').textContent = s.db_path;
  document.getElementById('sys-poll-interval').textContent = `${s.poll_interval_seconds}s`;

  const lastAlertEl = document.getElementById('sys-last-alert');
  if (s.last_alert) {
    const a = s.last_alert;
    lastAlertEl.innerHTML = `
      <div class="alert-row">
        <svg viewBox="0 0 24 24"><path d="M12 2 1 21h22zm0 4.5 7.5 13h-15zM11 10h2v5h-2zm0 6h2v2h-2z"/></svg>
        <div>
          <div class="a-title">Vehicle <a href="/fleet/${a.node_id}" style="color:#fecaca;text-decoration:underline;">${a.node_id}</a> — ${window.Dashboard.reasonLabel(a.reason)}</div>
          <div class="a-meta">${new Date(a.sent_at * 1000).toLocaleString()} &middot; ${window.Dashboard.smsLabel(a)}</div>
        </div>
      </div>`;
  } else {
    lastAlertEl.innerHTML = '<div class="empty-hint">No alerts recorded yet.</div>';
  }
}

load();
setInterval(load, 5000);
