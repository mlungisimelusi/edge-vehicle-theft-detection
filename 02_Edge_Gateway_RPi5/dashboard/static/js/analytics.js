// ---------------------------------------------------------------------
// Analytics page — fleet-wide aggregates computed server-side from
// telemetry history (see /api/analytics/summary), re-fetched whenever the
// time window changes. Not live-streamed like the other pages: aggregates
// over an hour/day/week don't need per-packet updates.
// ---------------------------------------------------------------------
let hours = 24;
let speedBarChart, distanceBarChart;

function initCharts() {
  const opts = {
    responsive: true, maintainAspectRatio: false,
    scales: {
      x: { ticks: { color: '#8992a8' }, grid: { display: false } },
      y: { ticks: { color: '#8992a8' }, grid: { color: 'rgba(148,163,196,0.08)' } },
    },
    plugins: { legend: { display: false } },
  };
  speedBarChart = new Chart(document.getElementById('speedBarChart'), {
    type: 'bar', data: { labels: [], datasets: [{ data: [], backgroundColor: '#3b82f6', borderRadius: 6 }] }, options: opts,
  });
  distanceBarChart = new Chart(document.getElementById('distanceBarChart'), {
    type: 'bar', data: { labels: [], datasets: [{ data: [], backgroundColor: '#22d3ee', borderRadius: 6 }] }, options: opts,
  });
}

async function load() {
  const resp = await fetch(`/api/analytics/summary?hours=${hours}`);
  if (!resp.ok) return;
  const data = await resp.json();
  const vehicles = data.vehicles;

  document.getElementById('an-empty').style.display = vehicles.length ? 'none' : 'block';
  document.getElementById('an-vehicle-count').textContent = vehicles.length;
  document.getElementById('an-packet-count').textContent = vehicles.reduce((s, v) => s + v.packet_count, 0);
  document.getElementById('an-distance').textContent = `${vehicles.reduce((s, v) => s + v.distance_km, 0).toFixed(1)} km`;
  document.getElementById('an-anomaly-count').textContent = vehicles.reduce((s, v) => s + v.anomaly_count, 0);

  // Fleet-wide PDR = total received / total expected across vehicles.
  const pdrs = vehicles.map(v => v.pdr).filter(p => p);
  const received = pdrs.reduce((s, p) => s + p.received, 0);
  const expected = pdrs.reduce((s, p) => s + p.expected, 0);
  document.getElementById('an-pdr').textContent = expected ? `${(100 * received / expected).toFixed(1)}%` : '—';
  document.getElementById('an-pdr-sub').textContent = expected
    ? `${received} of ${expected} packets delivered`
    : 'needs v2 node firmware (sequence numbers)';

  const labels = vehicles.map(v => `Vehicle ${v.node_id}`);
  speedBarChart.data.labels = labels;
  speedBarChart.data.datasets[0].data = vehicles.map(v => v.avg_speed_kmph);
  speedBarChart.update();
  distanceBarChart.data.labels = labels;
  distanceBarChart.data.datasets[0].data = vehicles.map(v => v.distance_km);
  distanceBarChart.update();

  const tbody = document.getElementById('an-tbody');
  tbody.innerHTML = vehicles.map(v => `
    <tr onclick="window.location.href='/fleet/${v.node_id}'">
      <td><strong>Vehicle ${v.node_id}</strong></td>
      <td>${v.packet_count}</td>
      <td>${v.avg_speed_kmph.toFixed(1)} km/h</td>
      <td>${v.max_speed_kmph.toFixed(1)} km/h</td>
      <td>${v.distance_km.toFixed(2)} km</td>
      <td>${v.anomaly_count}</td>
      <td>${v.avg_rssi_dbm !== null ? v.avg_rssi_dbm + ' dBm' : '—'}</td>
      <td>${v.pdr ? v.pdr.pdr_percent.toFixed(1) + '%' : '—'}</td>
    </tr>`).join('');
}

document.querySelectorAll('.btn[data-hours]').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.btn[data-hours]').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    hours = parseFloat(btn.dataset.hours);
    load();
  });
});

initCharts();
load();
