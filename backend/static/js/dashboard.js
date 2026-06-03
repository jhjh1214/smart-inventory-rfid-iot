/* dashboard.js */

// ── State ─────────────────────────────────────────────────────────────────────
let currentRole = 'viewer';
let charts = {};
let abcData = {};

// ── Boot ──────────────────────────────────────────────────────────────────────
(async function init() {
  try {
    const r = await fetch('/api/me');
    if (!r.ok) { window.location.href = '/login'; return; }
    const me = await r.json();
    currentRole = me.role;
    document.getElementById('user-info').textContent = `${me.username} (${me.role})`;
  } catch {
    window.location.href = '/login';
    return;
  }

  applyRBAC(currentRole);
  setupTabs();
  setupForms();
  clockTick();
  setInterval(clockTick, 1000);

  // Initial data load
  await Promise.all([refreshSummary(), fetchTransactions(), fetchItems()]);
  await Promise.all([fetchAnalytics(), fetchTags(), fetchAlerts()]);
  fetchStatus();

  connectSSE();
  setInterval(fetchStatus, 5000);
  setInterval(refreshSummary, 20000);
  setInterval(fetchTransactions, 15000);
  setInterval(fetchAlerts, 20000);
})();

// ── Helpers ───────────────────────────────────────────────────────────────────
function esc(s) {
  if (s == null) return '';
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;')
                  .replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function fmtDate(ts) {
  if (!ts) return '—';
  const d = new Date(ts.replace(' ','T'));
  return d.toLocaleString([], { month:'short', day:'numeric',
    hour:'2-digit', minute:'2-digit' });
}

function fmtTime(ts) {
  if (!ts) return '—';
  const d = new Date(ts.replace(' ','T'));
  return d.toLocaleTimeString([], { hour:'2-digit', minute:'2-digit', second:'2-digit' });
}

function statusBadge(qty, threshold) {
  if (qty === 0)        return '<span class="badge badge-danger">Out of Stock</span>';
  if (qty <= threshold) return '<span class="badge badge-warning">Low Stock</span>';
  return                       '<span class="badge badge-success">In Stock</span>';
}

function tagStateBadge(state) {
  const map = { out:'badge-info', in:'badge-success', consumed:'badge-neutral' };
  return `<span class="badge ${map[state] || 'badge-neutral'}">${esc(state)}</span>`;
}

function actionBadge(action) {
  const map = {
    scan_in:       'badge-success',
    scan_out:      'badge-info',
    manual_adjust: 'badge-neutral',
    admin_return:  'badge-purple',
  };
  return `<span class="badge ${map[action] || 'badge-neutral'}">${esc(action.replace(/_/g,' '))}</span>`;
}

function abcBadge(cls) {
  const map = { A:'badge-danger', B:'badge-warning', C:'badge-success' };
  return `<span class="badge ${map[cls] || 'badge-neutral'}">${cls}</span>`;
}

function riskColor(score) {
  if (score >= 80) return '#ef4444';
  if (score >= 50) return '#f59e0b';
  return '#22c55e';
}

// ── Clock ─────────────────────────────────────────────────────────────────────
function clockTick() {
  document.getElementById('clock').textContent =
    new Date().toLocaleTimeString([], { hour:'2-digit', minute:'2-digit', second:'2-digit' });
}

// ── Tab navigation ────────────────────────────────────────────────────────────
function setupTabs() {
  const titles = {
    overview:'Overview', inventory:'Inventory', analytics:'Analytics',
    tags:'RFID Tags', workers:'Workers', manufacturing:'Manufacturing', alerts:'Alerts'
  };
  document.querySelectorAll('.nav-item').forEach(btn => {
    btn.addEventListener('click', () => {
      const tab = btn.dataset.tab;
      document.querySelectorAll('.nav-item').forEach(b => b.classList.remove('active'));
      document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
      btn.classList.add('active');
      document.getElementById(`tab-${tab}`).classList.add('active');
      document.getElementById('page-title').textContent = titles[tab] || tab;

      // Lazy-refresh on tab switch
      if (tab === 'inventory') fetchItems();
      if (tab === 'analytics') fetchAnalytics();
      if (tab === 'tags')      fetchTags();
      if (tab === 'alerts')         fetchAlerts();
      if (tab === 'workers')        fetchWorkers();
      if (tab === 'manufacturing')  fetchPipeline();
    });
  });
}

// ── MQTT status ───────────────────────────────────────────────────────────────
async function fetchStatus() {
  try {
    const d = await fetch('/api/status').then(r => r.json());
    const dot   = document.getElementById('mqtt-dot');
    const label = document.getElementById('mqtt-label');
    if (d.connected) {
      dot.className = 'w-2 h-2 rounded-full bg-green-400 shrink-0';
      label.textContent = 'MQTT Live';
    } else {
      dot.className = 'w-2 h-2 rounded-full bg-red-400 shrink-0';
      label.textContent = 'MQTT Offline';
    }
  } catch {}
}

// ── Summary KPIs ──────────────────────────────────────────────────────────────
async function refreshSummary() {
  try {
    const s = await fetch('/api/analytics/summary').then(r => r.json());

    setText('kpi-total-items',  s.total_items);
    setText('kpi-health',       s.health_score + '%');
    setText('kpi-low-stock',    s.low_stock);
    setText('kpi-out-of-stock', s.out_of_stock);
    setText('kpi-today-scans',  s.today_scans);
    setText('qs-dead-stock',    s.dead_stock);
    setText('qs-security-today', s.security_today);
    setText('qs-tags-in',       s.tags.in);
    setText('qs-tags-out',      s.tags.out);
    setText('qs-tags-consumed', s.tags.consumed);
    setText('qs-tags-total',    s.tags.total);

    // unread alerts badge
    const unreadR = await fetch('/api/alerts').then(r => r.json());
    const unread = unreadR.filter(a => !a.is_read).length;
    const badge = document.getElementById('alert-badge');
    if (unread > 0) {
      badge.textContent = unread;
      badge.classList.remove('hidden');
    } else {
      badge.classList.add('hidden');
    }

    // Total qty from items
    const items = await fetch('/api/items').then(r => r.json());
    const totalQty = items.reduce((sum, i) => sum + i.quantity, 0);
    setText('kpi-total-qty', totalQty);

    renderStatusDonut(s);
    renderTagsDonut(s.tags);
  } catch {}
}

function setText(id, val) {
  const el = document.getElementById(id);
  if (el) el.textContent = val ?? '–';
}

// ── Donut charts ──────────────────────────────────────────────────────────────
function renderStatusDonut(s) {
  const ctx = document.getElementById('chart-status-donut');
  if (!ctx) return;
  if (charts.statusDonut) charts.statusDonut.destroy();
  charts.statusDonut = new Chart(ctx, {
    type: 'doughnut',
    data: {
      labels: ['Healthy','Low Stock','Out of Stock'],
      datasets: [{
        data: [s.healthy, s.low_stock, s.out_of_stock],
        backgroundColor: ['#22c55e','#f59e0b','#ef4444'],
        borderWidth: 0,
      }]
    },
    options: {
      cutout: '68%',
      plugins: {
        legend: { position: 'bottom', labels: { font: { size: 10 }, padding: 8 } }
      }
    }
  });
}

function renderTagsDonut(tags) {
  const ctx = document.getElementById('chart-tags-donut');
  if (!ctx) return;
  if (charts.tagsDonut) charts.tagsDonut.destroy();
  charts.tagsDonut = new Chart(ctx, {
    type: 'doughnut',
    data: {
      labels: ['In Warehouse','With Product','Consumed'],
      datasets: [{
        data: [tags.in, tags.out, tags.consumed],
        backgroundColor: ['#22c55e','#3b82f6','#9ca3af'],
        borderWidth: 0,
      }]
    },
    options: {
      cutout: '68%',
      plugins: {
        legend: { position: 'bottom', labels: { font: { size: 10 }, padding: 8 } }
      }
    }
  });
}

// ── Transaction trends chart ──────────────────────────────────────────────────
async function fetchTransactionTrends() {
  try {
    const data = await fetch('/api/analytics/trends?days=7').then(r => r.json());
    const ctx = document.getElementById('chart-trends');
    if (!ctx) return;
    if (charts.trends) charts.trends.destroy();
    charts.trends = new Chart(ctx, {
      type: 'bar',
      data: {
        labels: data.map(d => {
          const dt = new Date(d.day);
          return dt.toLocaleDateString([], { month:'short', day:'numeric' });
        }),
        datasets: [
          {
            label: 'Received (scan in)',
            data: data.map(d => d.received),
            backgroundColor: '#6366f1',
            borderRadius: 3,
          },
          {
            label: 'Dispatched (scan out)',
            data: data.map(d => d.dispatched),
            backgroundColor: '#f59e0b',
            borderRadius: 3,
          }
        ]
      },
      options: {
        responsive: true,
        plugins: {
          legend: { position: 'bottom', labels: { font: { size: 11 } } }
        },
        scales: {
          y: { beginAtZero: true, ticks: { stepSize: 1 } },
          x: { grid: { display: false } }
        }
      }
    });
  } catch {}
}

// ── Analytics tab ─────────────────────────────────────────────────────────────
async function fetchAnalytics() {
  try {
    const [analyticsData, abcRaw, items] = await Promise.all([
      fetch('/api/analytics').then(r => r.json()),
      fetch('/api/analytics/abc').then(r => r.json()),
      fetch('/api/items').then(r => r.json()),
    ]);
    abcData = abcRaw;

    const itemMap = Object.fromEntries(items.map(i => [i.id, i]));
    const labels  = analyticsData.map(d => esc(d.item_id.replace('item-','')));
    const stocks  = analyticsData.map(d => (itemMap[d.item_id] || {}).quantity ?? 0);
    const risks   = analyticsData.map(d => d.risk_score);
    const days    = analyticsData.map(d => Math.min(d.days_remaining, 30));

    // Stock bar chart
    const ctxS = document.getElementById('chart-stock');
    if (ctxS) {
      if (charts.stock) charts.stock.destroy();
      charts.stock = new Chart(ctxS, {
        type: 'bar',
        data: {
          labels,
          datasets: [{
            label: 'Quantity',
            data: stocks,
            backgroundColor: analyticsData.map(d => {
              const dr = d.days_remaining;
              return dr <= 3 ? '#ef4444' : dr <= 7 ? '#f59e0b' : '#6366f1';
            }),
            borderRadius: 4,
          }]
        },
        options: {
          responsive: true,
          plugins: { legend: { display: false } },
          scales: { y: { beginAtZero: true, ticks: { stepSize: 1 } } }
        }
      });
    }

    // Risk horizontal bar
    const ctxR = document.getElementById('chart-risk');
    if (ctxR) {
      if (charts.risk) charts.risk.destroy();
      charts.risk = new Chart(ctxR, {
        type: 'bar',
        data: {
          labels,
          datasets: [{
            label: 'Days Remaining (capped 30)',
            data: days,
            backgroundColor: risks.map(riskColor),
            borderRadius: 4,
          }]
        },
        options: {
          indexAxis: 'y',
          responsive: true,
          plugins: { legend: { display: false } },
          scales: { x: { beginAtZero: true, max: 30 } }
        }
      });
    }

    // Per-item table
    const tbody = document.getElementById('analytics-tbody');
    if (tbody) {
      tbody.innerHTML = analyticsData.map(d => {
        const item = itemMap[d.item_id] || {};
        const abc  = abcData[d.item_id];
        const riskCls = d.risk_score >= 80 ? 'text-red-600 font-semibold' :
                        d.risk_score >= 50 ? 'text-amber-600 font-semibold' : 'text-green-600';
        return `
          <tr>
            <td class="px-4 py-3">
              <div class="font-medium text-gray-800">${esc(item.name || d.item_id)}</div>
              <div class="text-xs text-gray-400">${esc(d.item_id)}</div>
            </td>
            <td class="px-4 py-3 text-center">
              ${abc ? abcBadge(abc.class) : '<span class="text-gray-400 text-xs">—</span>'}
            </td>
            <td class="px-4 py-3 text-right font-mono text-sm">${d.avg_daily_usage}</td>
            <td class="px-4 py-3 text-right font-mono text-sm">${d.forecast_demand}</td>
            <td class="px-4 py-3 text-right font-mono text-sm">${d.eoq}</td>
            <td class="px-4 py-3 text-right font-mono text-sm">${d.days_remaining >= 999 ? '∞' : d.days_remaining}</td>
            <td class="px-4 py-3 text-center">
              <span class="${riskCls} text-sm">${d.risk_score}</span>
            </td>
          </tr>`;
      }).join('');
    }
  } catch {}
}

// ── Transactions (scan log) ───────────────────────────────────────────────────
async function fetchTransactions() {
  try {
    const txs = await fetch('/api/transactions?limit=30').then(r => r.json());
    const ul = document.getElementById('scan-log');
    if (!ul) return;
    if (!txs.length) {
      ul.innerHTML = '<li class="py-4 text-slate-400 text-sm text-center">No scans yet</li>';
      return;
    }
    ul.innerHTML = txs.map(t => `
      <li class="py-2.5 flex items-center gap-3">
        <div class="flex-1 min-w-0">
          <div class="text-sm font-medium text-gray-700 truncate">${esc(t.item_name || t.item_id)}</div>
          <div class="text-xs text-gray-400">${fmtDate(t.timestamp)}
            ${t.tag_uid ? `<span class="font-mono ml-1">${esc(t.tag_uid)}</span>` : ''}
          </div>
        </div>
        <div class="shrink-0">${actionBadge(t.action)}</div>
      </li>`).join('');
  } catch {}
}

// ── Inventory table ───────────────────────────────────────────────────────────
async function fetchItems() {
  try {
    const items = await fetch('/api/items').then(r => r.json());
    const tbody = document.getElementById('inventory-tbody');
    if (!tbody) return;
    if (!items.length) {
      tbody.innerHTML = '<tr><td colspan="6" class="px-4 py-8 text-center text-slate-400">No items found</td></tr>';
      return;
    }
    tbody.innerHTML = items.map(item => {
      const rowCls = item.quantity === 0 ? 'row-danger' :
                     item.quantity <= item.low_stock_threshold ? 'row-warning' : '';
      const adminActions = currentRole === 'admin'
        ? `<button onclick='openEditModal(${JSON.stringify(item)})' class="text-indigo-600 hover:underline text-xs mr-2">Edit</button>
           <button onclick="deleteItem('${esc(item.id)}')" class="text-red-500 hover:underline text-xs">Delete</button>`
        : `<button onclick='openEditModal(${JSON.stringify(item)})' class="text-indigo-600 hover:underline text-xs">Edit</button>`;
      return `
        <tr class="${rowCls}" data-item-id="${esc(item.id)}">
          <td class="px-4 py-3">
            <div class="font-medium text-gray-800">${esc(item.name)}</div>
            <div class="text-xs text-gray-400">${esc(item.id)}</div>
          </td>
          <td class="px-4 py-3 text-right font-mono font-semibold text-gray-700">
            ${item.quantity} <span class="text-xs font-normal text-gray-400">${esc(item.unit)}</span>
          </td>
          <td class="px-4 py-3 text-center text-sm text-gray-500">${item.low_stock_threshold}</td>
          <td class="px-4 py-3 text-center">${statusBadge(item.quantity, item.low_stock_threshold)}</td>
          <td class="px-4 py-3 text-center text-xs text-gray-400">${fmtDate(item.updated_at)}</td>
          <td class="px-4 py-3 text-center">${adminActions}</td>
        </tr>`;
    }).join('');
  } catch {}
}

// ── RFID Tags table ───────────────────────────────────────────────────────────
async function fetchTags() {
  try {
    const tags = await fetch('/api/tags').then(r => r.json());
    const tbody = document.getElementById('tags-tbody');
    if (!tbody) return;
    if (!tags.length) {
      tbody.innerHTML = '<tr><td colspan="6" class="px-4 py-8 text-center text-slate-400">No tags registered</td></tr>';
      return;
    }
    tbody.innerHTML = tags.map(t => {
      let action = '';
      if (t.state === 'consumed' && currentRole === 'admin') {
        action = `<button onclick="openReturnModal('${esc(t.uid)}')"
          class="text-amber-600 hover:underline text-xs font-semibold">Return</button>`;
      } else if (currentRole === 'admin') {
        action = `<button onclick="deleteTag('${esc(t.uid)}')"
          class="text-red-500 hover:underline text-xs">Remove</button>`;
      } else {
        action = '<span class="text-gray-300 text-xs">—</span>';
      }
      return `
        <tr>
          <td class="px-4 py-3 font-mono text-sm text-gray-700">${esc(t.uid)}</td>
          <td class="px-4 py-3 text-sm text-gray-700">${esc(t.item_name || t.item_id)}</td>
          <td class="px-4 py-3 text-center">${tagStateBadge(t.state)}</td>
          <td class="px-4 py-3 text-center text-xs text-gray-400">${fmtDate(t.last_scan)}</td>
          <td class="px-4 py-3 text-center text-xs text-gray-400">${fmtDate(t.registered_at)}</td>
          <td class="px-4 py-3 text-center">${action}</td>
        </tr>`;
    }).join('');
  } catch {}
}

// ── Alerts ────────────────────────────────────────────────────────────────────
async function fetchAlerts() {
  try {
    const alerts = await fetch('/api/alerts').then(r => r.json());
    const ul = document.getElementById('alerts-list');
    if (!ul) return;

    const unread = alerts.filter(a => !a.is_read);
    const badge  = document.getElementById('alert-badge');
    if (unread.length > 0) {
      badge.textContent = unread.length;
      badge.classList.remove('hidden');
      const banner = document.getElementById('alert-banner');
      const bannerText = document.getElementById('alert-banner-text');
      bannerText.textContent = `${unread.length} unread alert${unread.length > 1 ? 's' : ''}: ${unread[0].message}`;
      banner.classList.remove('hidden');
    } else {
      badge.classList.add('hidden');
    }

    if (!alerts.length) {
      ul.innerHTML = '<li class="py-6 text-slate-400 text-sm text-center">No alerts</li>';
      return;
    }

    const typeIcon = { out_of_stock: '🔴', low_stock: '🟡', security: '🔒' };
    ul.innerHTML = alerts.map(a => {
      const icon   = typeIcon[a.alert_type] || '🔔';
      const rowCls = a.is_read ? 'opacity-50' : '';
      const typeBadgeMap = {
        out_of_stock: 'badge-danger',
        low_stock:    'badge-warning',
        security:     'badge-orange',
      };
      return `
        <li class="py-3.5 flex items-start gap-3 ${rowCls}">
          <span class="text-xl leading-none mt-0.5">${icon}</span>
          <div class="flex-1 min-w-0">
            <div class="flex items-center gap-2 mb-0.5">
              <span class="badge ${typeBadgeMap[a.alert_type] || 'badge-neutral'}">${esc(a.alert_type.replace(/_/g,' '))}</span>
              ${a.item_name ? `<span class="text-xs text-gray-500">${esc(a.item_name)}</span>` : ''}
            </div>
            <div class="text-sm text-gray-700">${esc(a.message)}</div>
            <div class="text-xs text-gray-400 mt-0.5">${fmtDate(a.timestamp)}</div>
          </div>
          ${!a.is_read ? `<button onclick="markRead(${a.id})" class="text-xs text-indigo-600 hover:underline shrink-0 mt-0.5">Dismiss</button>` : ''}
        </li>`;
    }).join('');
  } catch {}
}

async function markRead(id) {
  await fetch(`/api/alerts/${id}/read`, { method: 'POST' });
  fetchAlerts();
  refreshSummary();
}

async function markAllRead() {
  await fetch('/api/alerts/read-all', { method: 'POST' });
  fetchAlerts();
  refreshSummary();
}

function dismissBanner() {
  document.getElementById('alert-banner').classList.add('hidden');
}

// ── Modals ────────────────────────────────────────────────────────────────────
function openModal(id)  { document.getElementById(id).classList.remove('hidden'); }
function closeModal(id) { document.getElementById(id).classList.add('hidden'); }

function openAddItemModal() { openModal('modal-add-item'); }

function openEditModal(item) {
  const form = document.getElementById('form-edit-item');
  form.id.value                  = item.id;
  form.name.value                = item.name;
  form.quantity.value            = item.quantity;
  form.unit.value                = item.unit;
  form.low_stock_threshold.value = item.low_stock_threshold;
  openModal('modal-edit-item');
}

function openReturnModal(uid) {
  document.getElementById('return-tag-uid').value = uid;
  document.getElementById('return-note').value = '';
  openModal('modal-tag-return');
}

document.querySelectorAll('.modal-overlay').forEach(el => {
  el.addEventListener('click', e => { if (e.target === el) el.classList.add('hidden'); });
});

// ── Forms ─────────────────────────────────────────────────────────────────────
function setupForms() {
  document.getElementById('form-add-item').addEventListener('submit', async e => {
    e.preventDefault();
    const fd = new FormData(e.target);
    const body = Object.fromEntries(fd);
    body.quantity            = parseInt(body.quantity);
    body.low_stock_threshold = parseInt(body.low_stock_threshold);
    await fetch('/api/items', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    closeModal('modal-add-item');
    e.target.reset();
    fetchItems();
    refreshSummary();
  });

  document.getElementById('form-edit-item').addEventListener('submit', async e => {
    e.preventDefault();
    const fd = new FormData(e.target);
    const body = Object.fromEntries(fd);
    const id = body.id; delete body.id;
    body.quantity            = parseInt(body.quantity);
    body.low_stock_threshold = parseInt(body.low_stock_threshold);
    await fetch(`/api/items/${encodeURIComponent(id)}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    closeModal('modal-edit-item');
    fetchItems();
    refreshSummary();
  });

  document.getElementById('form-tag-return').addEventListener('submit', async e => {
    e.preventDefault();
    const uid  = document.getElementById('return-tag-uid').value;
    const note = document.getElementById('return-note').value.trim() || 'Admin return';
    const r = await fetch(`/api/tags/${encodeURIComponent(uid)}/return`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ note }),
    });
    if (r.ok) {
      closeModal('modal-tag-return');
      fetchTags();
      fetchItems();
      refreshSummary();
    } else {
      const d = await r.json();
      alert('Error: ' + (d.error || 'Return failed'));
    }
  });
}

// ── CRUD ──────────────────────────────────────────────────────────────────────
async function deleteItem(id) {
  if (!confirm(`Delete item "${id}"? This will also remove associated tags.`)) return;
  await fetch(`/api/items/${encodeURIComponent(id)}`, { method: 'DELETE' });
  fetchItems();
  refreshSummary();
}

async function deleteTag(uid) {
  if (!confirm(`Remove tag ${uid}?`)) return;
  await fetch(`/api/tags/${encodeURIComponent(uid)}`, { method: 'DELETE' });
  fetchTags();
}

// ── SSE ───────────────────────────────────────────────────────────────────────
function connectSSE() {
  const src = new EventSource('/api/events');

  src.onmessage = e => {
    const data = JSON.parse(e.data);

    // Flash live badge
    const lb = document.getElementById('scan-live-badge');
    lb.classList.remove('hidden');
    clearTimeout(lb._timer);
    lb._timer = setTimeout(() => lb.classList.add('hidden'), 3000);

    if (data.type === 'scan' || data.type === 'rejected_scan') {
      refreshSummary();
      fetchTransactions();
      fetchItems().then(() => highlightItem(data.item_id));
      if (data.type === 'rejected_scan') {
        showBanner(`SECURITY: Consumed tag ${data.tag_uid} scanned for ${data.item_name} — possible reuse`, 'red');
        fetchAlerts();
      } else if (data.action === 'scan_out') {
        fetchAlerts();
      }
    }

    if (data.type === 'pipeline') {
      refreshSummary();
      fetchTransactions();
      fetchItems().then(() => highlightItem(data.item_id));
      if (data.stage === 'received' || data.stage === 'dispatched') fetchAlerts();
      // Refresh manufacturing tab if open
      if (document.getElementById('tab-manufacturing').classList.contains('active')) {
        fetchPipeline();
      }
    }

    if (data.type === 'security_alert') {
      showBanner(`SECURITY: ${data.message}`, 'red');
      fetchAlerts();
      refreshSummary();
    }

    if (data.type === 'worker_auth') {
      if (document.getElementById('tab-workers').classList.contains('active')) {
        fetchWorkers();
      }
      // Flash a subtle notice in the topbar
      const lb = document.getElementById('scan-live-badge');
      lb.classList.remove('hidden');
      clearTimeout(lb._timer2);
      lb._timer2 = setTimeout(() => lb.classList.add('hidden'), 2000);
    }

    if (data.type === 'worker_denied') {
      showBanner(`Access denied: worker ${data.employee_id} (${data.name}) is inactive`, 'red');
    }

    if (data.type === 'job_created') {
      if (document.getElementById('tab-manufacturing').classList.contains('active')) {
        fetchPipeline();
      }
    }
  };

  src.onerror = () => {
    src.close();
    setTimeout(connectSSE, 3000);
  };
}

function highlightItem(item_id) {
  document.querySelectorAll(`#inventory-tbody tr[data-item-id="${CSS.escape(item_id)}"]`).forEach(row => {
    row.classList.add('row-flash');
    setTimeout(() => row.classList.remove('row-flash'), 2000);
  });
}

function showBanner(msg, color = 'red') {
  const banner = document.getElementById('alert-banner');
  const text   = document.getElementById('alert-banner-text');
  banner.className = `${color === 'red' ? 'bg-red-500' : 'bg-amber-500'} text-white px-6 py-2.5 flex items-center justify-between text-sm`;
  text.textContent = msg;
  banner.classList.remove('hidden');
}

// ── Auth ──────────────────────────────────────────────────────────────────────
async function doLogout() {
  await fetch('/api/logout', { method: 'POST' });
  window.location.href = '/login';
}

// ── RBAC visibility ───────────────────────────────────────────────────────────

function applyRBAC(role) {
  const isAdmin   = role === 'admin';
  const isManager = role === 'admin' || role === 'manager';

  // Tabs hidden from viewers
  ['nav-workers'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.style.display = isManager ? '' : 'none';
  });
  // Manufacturing tab visible to managers+
  document.querySelectorAll('[data-tab="manufacturing"]').forEach(el => {
    el.style.display = isManager ? '' : 'none';
  });
  // Analytics tab visible to managers+
  document.querySelectorAll('[data-tab="analytics"]').forEach(el => {
    el.style.display = isManager ? '' : 'none';
  });
}


// ── Workers tab ───────────────────────────────────────────────────────────────

async function fetchWorkers() {
  try {
    const [workers, sessions] = await Promise.all([
      fetch('/api/workers').then(r => r.json()),
      fetch('/api/workers/sessions').then(r => r.json()),
    ]);
    renderWorkerSessions(sessions);
    renderWorkerTable(workers);
  } catch {}
}

function renderWorkerSessions(sessions) {
  const el = document.getElementById('worker-sessions');
  if (!el) return;
  const entries = Object.entries(sessions);
  if (!entries.length) {
    el.innerHTML = '<span class="text-slate-400 text-sm">No workers authenticated at any station</span>';
    return;
  }
  el.innerHTML = entries.map(([did, s]) => `
    <div class="flex items-center gap-3 bg-green-50 border border-green-200 rounded-lg px-4 py-3">
      <div class="w-2 h-2 rounded-full bg-green-500 shrink-0"></div>
      <div>
        <div class="font-semibold text-sm text-green-800">${esc(s.name)}</div>
        <div class="text-xs text-green-600">${esc(s.employee_id)} · ${esc(did)}</div>
        <div class="text-xs text-green-500">expires in ${s.expires_in}s</div>
      </div>
      <span class="badge badge-purple ml-2">${esc(s.role)}</span>
    </div>`).join('');
}

function renderWorkerTable(workers) {
  const tbody = document.getElementById('workers-tbody');
  if (!tbody) return;
  const isManager = currentRole === 'admin' || currentRole === 'manager';
  if (!workers.length) {
    tbody.innerHTML = '<tr><td colspan="6" class="px-4 py-8 text-center text-slate-400">No workers registered</td></tr>';
    return;
  }
  const roleBadge = r => {
    const map = { supervisor:'badge-purple', operator:'badge-info' };
    return `<span class="badge ${map[r] || 'badge-neutral'}">${esc(r)}</span>`;
  };
  tbody.innerHTML = workers.map(w => {
    const activeChip = w.active_station
      ? `<span class="badge badge-success">Active @ ${esc(w.active_station)}</span>`
      : '';
    const actions = isManager
      ? `<button onclick="toggleWorker(${w.id}, ${w.active})"
           class="text-xs ${w.active ? 'text-red-500' : 'text-green-600'} hover:underline mr-2">
           ${w.active ? 'Deactivate' : 'Activate'}</button>
         ${currentRole === 'admin' ? `<button onclick="deleteWorker(${w.id})" class="text-xs text-red-500 hover:underline">Delete</button>` : ''}`
      : '—';
    return `
      <tr>
        <td class="px-4 py-3">
          <div class="font-medium text-gray-800">${esc(w.name)}</div>
          <div class="text-xs text-gray-400">${esc(w.employee_id)}</div>
        </td>
        <td class="px-4 py-3 text-center">${roleBadge(w.role)}</td>
        <td class="px-4 py-3 font-mono text-xs text-gray-500">${esc(w.uid) || '<span class="text-gray-300">not yet scanned</span>'}</td>
        <td class="px-4 py-3 text-center">
          ${w.active ? '<span class="badge badge-success">Active</span>' : '<span class="badge badge-neutral">Inactive</span>'}
          ${activeChip}
        </td>
        <td class="px-4 py-3 text-center text-xs text-gray-400">${fmtDate(w.last_seen)}</td>
        <td class="px-4 py-3 text-center">${actions}</td>
      </tr>`;
  }).join('');
}

async function toggleWorker(id, currentActive) {
  await fetch(`/api/workers/${id}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ active: currentActive ? 0 : 1 }),
  });
  fetchWorkers();
}

async function deleteWorker(id) {
  if (!confirm('Delete this worker record?')) return;
  await fetch(`/api/workers/${id}`, { method: 'DELETE' });
  fetchWorkers();
}

// Worker registration form
document.addEventListener('DOMContentLoaded', () => {
  const form = document.getElementById('form-add-worker');
  if (form) {
    form.addEventListener('submit', async e => {
      e.preventDefault();
      const employee_id = document.getElementById('w-employee-id').value.trim().toUpperCase();
      const name        = document.getElementById('w-name').value.trim();
      const role        = document.getElementById('w-role').value;
      const r = await fetch('/api/workers', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ employee_id, name, role }),
      });
      const d = await r.json();
      if (r.ok) { form.reset(); fetchWorkers(); }
      else alert('Error: ' + (d.error || 'Failed'));
    });
  }
});

// Update worker session display every 30s if tab is open
setInterval(() => {
  if (document.getElementById('tab-workers').classList.contains('active')) {
    fetch('/api/workers/sessions').then(r => r.json()).then(renderWorkerSessions).catch(() => {});
  }
}, 30000);


// ── Pipeline / Manufacturing tab ──────────────────────────────────────────────

const PIPELINE_STAGES = [
  { key: 'tagged',      label: 'Tagged',      color: '#6366f1', desc: 'Written at factory' },
  { key: 'in_transit',  label: 'In Transit',  color: '#f59e0b', desc: 'Left factory floor' },
  { key: 'received',    label: 'Received',    color: '#3b82f6', desc: 'At warehouse dock'  },
  { key: 'racked',      label: 'Racked',      color: '#22c55e', desc: 'On warehouse shelf'  },
  { key: 'dispatched',  label: 'Dispatched',  color: '#94a3b8', desc: 'Sent to customer'   },
  { key: 'returned',    label: 'Returned',    color: '#f97316', desc: 'Customer return'    },
];

async function fetchPipeline() {
  try {
    const [data, items] = await Promise.all([
      fetch('/api/pipeline').then(r => r.json()),
      fetch('/api/items').then(r => r.json()),
    ]);

    renderPipelineFlow(data.totals);
    renderPipelineItems(data.per_item, items);
    renderRackStats(data.rack_stats);
    renderWriteJobs(data.jobs);
    populateJobItemSelect(items);

    // Show/hide write job form based on role
    const card = document.getElementById('write-job-card');
    if (card) card.style.display = currentRole === 'admin' ? '' : 'none';
  } catch {}
}

function renderPipelineFlow(totals) {
  const el = document.getElementById('pipeline-flow');
  if (!el) return;
  const total = PIPELINE_STAGES.reduce((s, st) => s + (totals[st.key] || 0), 0);
  el.innerHTML = PIPELINE_STAGES.map((st, i) => {
    const count = totals[st.key] || 0;
    const pct   = total > 0 ? Math.round(count / total * 100) : 0;
    return `
      <div class="flex-1 min-w-24 flex flex-col items-center gap-1">
        <div class="w-full rounded-lg p-3 text-center text-white text-sm font-semibold"
             style="background:${st.color}">
          <div class="text-2xl font-bold">${count}</div>
          <div class="text-xs opacity-90 mt-0.5">${st.label}</div>
        </div>
        <div class="text-xs text-slate-400 text-center leading-tight">${esc(st.desc)}</div>
        ${i < PIPELINE_STAGES.length - 1 ? '<div class="text-slate-300 text-lg self-center hidden lg:block">&#8594;</div>' : ''}
      </div>
      ${i < PIPELINE_STAGES.length - 1 ? '<div class="self-center text-slate-300 text-xl hidden lg:flex">&#10132;</div>' : ''}`;
  }).join('');
}

function renderPipelineItems(perItem, items) {
  const tbody = document.getElementById('pipeline-items-tbody');
  if (!tbody) return;
  const itemNameMap = Object.fromEntries(items.map(i => [i.id, i.name]));
  if (!perItem.length) {
    tbody.innerHTML = '<tr><td colspan="7" class="px-4 py-8 text-center text-slate-400">No tags tracked yet</td></tr>';
    return;
  }
  tbody.innerHTML = perItem.map(row => {
    const name = itemNameMap[row.item_id] || row.item_name || row.item_id;
    const cell = (key) => {
      const n = row[key] || 0;
      const st = PIPELINE_STAGES.find(s => s.key === key);
      return n > 0
        ? `<td class="px-4 py-3 text-center font-semibold" style="color:${st ? st.color : '#000'}">${n}</td>`
        : `<td class="px-4 py-3 text-center text-slate-300">—</td>`;
    };
    return `
      <tr>
        <td class="px-4 py-3">
          <div class="font-medium text-gray-800">${esc(name)}</div>
          <div class="text-xs text-gray-400">${esc(row.item_id)}</div>
        </td>
        ${cell('tagged')}${cell('in_transit')}${cell('received')}
        ${cell('racked')}${cell('dispatched')}${cell('returned')}
      </tr>`;
  }).join('');
}

function renderRackStats(rackStats) {
  const el = document.getElementById('rack-stats');
  if (!el) return;
  if (!rackStats.length) {
    el.innerHTML = '<span class="text-slate-400 text-sm">No racked items</span>';
    return;
  }
  el.innerHTML = rackStats.map(r => `
    <div class="bg-green-50 border border-green-200 rounded-lg px-4 py-3 text-center min-w-20">
      <div class="text-2xl font-bold text-green-700">${r.cnt}</div>
      <div class="text-xs text-green-600 font-semibold mt-0.5">${esc(r.rack_location)}</div>
    </div>`).join('');
}

function renderWriteJobs(jobs) {
  const tbody = document.getElementById('jobs-tbody');
  if (!tbody) return;
  if (!jobs.length) {
    tbody.innerHTML = '<tr><td colspan="4" class="px-4 py-6 text-center text-slate-400">No jobs yet</td></tr>';
    return;
  }
  const statusBadge = (s) => {
    const map = { pending:'badge-warning', in_progress:'badge-info', complete:'badge-success' };
    return `<span class="badge ${map[s] || 'badge-neutral'}">${esc(s)}</span>`;
  };
  tbody.innerHTML = jobs.map(j => `
    <tr>
      <td class="px-4 py-3">
        <div class="text-sm font-medium text-gray-800">${esc(j.item_name || j.item_id)}</div>
        <div class="text-xs text-gray-400">${fmtDate(j.created_at)}</div>
      </td>
      <td class="px-4 py-3 text-right font-mono text-sm">${j.quantity}</td>
      <td class="px-4 py-3 text-right font-mono text-sm text-green-600">${j.written}</td>
      <td class="px-4 py-3 text-center">${statusBadge(j.status)}</td>
    </tr>`).join('');
}

function populateJobItemSelect(items) {
  const sel = document.getElementById('job-item-id');
  if (!sel) return;
  const current = sel.value;
  sel.innerHTML = '<option value="">Select item…</option>' +
    items.map(i => `<option value="${esc(i.id)}" ${i.id === current ? 'selected' : ''}>${esc(i.name)}</option>`).join('');
}

// Write job form submit
document.addEventListener('DOMContentLoaded', () => {
  const form = document.getElementById('form-write-job');
  if (form) {
    form.addEventListener('submit', async e => {
      e.preventDefault();
      const item_id  = document.getElementById('job-item-id').value;
      const quantity = parseInt(document.getElementById('job-quantity').value);
      if (!item_id || quantity < 1) return;
      const btn = form.querySelector('button[type=submit]');
      btn.disabled = true;
      btn.textContent = 'Sending…';
      try {
        const r = await fetch('/api/factory/jobs', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ item_id, quantity }),
        });
        const d = await r.json();
        if (r.ok) {
          form.reset();
          fetchPipeline();
        } else {
          alert('Error: ' + (d.error || 'Failed'));
        }
      } finally {
        btn.disabled = false;
        btn.textContent = 'Send Job to ESP32';
      }
    });
  }
});

// ── Load overview charts on start ─────────────────────────────────────────────
fetchTransactionTrends();
setInterval(fetchTransactionTrends, 60000);
