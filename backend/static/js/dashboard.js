/* dashboard.js */

// ── Instant tab restore (sync, before any async — prevents flash) ─────────────
(function () {
  const t = localStorage.getItem('activeTab');
  if (!t) return;
  const btn  = document.querySelector(`.nav-item[data-tab="${t}"]`);
  const pane = document.getElementById('tab-' + t);
  if (!btn || !pane) return;
  document.querySelectorAll('.nav-item').forEach(b => b.classList.remove('active'));
  document.querySelectorAll('.tab-content').forEach(p => p.classList.remove('active'));
  btn.classList.add('active');
  pane.classList.add('active');
  const titles = { overview:'Overview', inventory:'Inventory', analytics:'Analytics',
    tags:'RFID Tags', workers:'Workers', manufacturing:'Manufacturing', alerts:'Alerts',
    audit:'Audit Trail' };
  const h = document.getElementById('page-title');
  if (h) h.textContent = titles[t] || t;
})();

// ── State ─────────────────────────────────────────────────────────────────────
let currentRole = 'viewer';
let charts = {};
let abcData = {};

// Cached data for exports / search filters
let _items        = [];
let _tags         = [];
let _workers      = [];
let _users        = [];
let _analytics    = [];
let _transactions = [];
let _alerts       = [];
let _audit        = [];
let _alertFilter  = 'all';
let _auditFilter  = 'all';

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

  // After RBAC, verify saved tab is still accessible; fetch tab-specific data
  const savedTab = localStorage.getItem('activeTab');
  if (savedTab) {
    const savedBtn = document.querySelector(`.nav-item[data-tab="${savedTab}"]`);
    if (!savedBtn || savedBtn.style.display === 'none') {
      // Tab hidden by RBAC — fall back to overview visually
      document.querySelectorAll('.nav-item').forEach(b => b.classList.remove('active'));
      document.querySelectorAll('.tab-content').forEach(p => p.classList.remove('active'));
      document.querySelector('.nav-item[data-tab="overview"]').classList.add('active');
      document.getElementById('tab-overview').classList.add('active');
      document.getElementById('page-title').textContent = 'Overview';
    } else if (savedTab === 'workers') {
      fetchWorkers(); fetchUsers(); fetchWebhooks();
    } else if (savedTab === 'manufacturing') {
      fetchPipeline(); fetchPurchaseOrders('');
    } else if (savedTab === 'audit') {
      fetchAudit();
    }
  }

  setupForms();
  clockTick();
  setInterval(clockTick, 1000);

  _kpiLoading();
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
  const d = new Date(ts.replace(' ','T') + 'Z');
  return d.toLocaleString([], { month:'short', day:'numeric',
    hour:'2-digit', minute:'2-digit' });
}

function fmtTime(ts) {
  if (!ts) return '—';
  const d = new Date(ts.replace(' ','T') + 'Z');
  return d.toLocaleTimeString([], { hour:'2-digit', minute:'2-digit', second:'2-digit' });
}

// ── UI state helpers ──────────────────────────────────────────────────────────

function _skeletonRows(cols, n = 4) {
  const widths = [
    ['70%','45%'], ['55%','35%'], ['80%','50%'], ['65%','40%']
  ];
  return Array.from({ length: n }, (_, i) => {
    const cells = Array.from({ length: cols }, (_, c) => {
      if (c === 0) {
        const [w1, w2] = widths[i % widths.length];
        return `<td class="px-4 py-3">
          <div class="skeleton sk-line sk-line-lg mb-1.5" style="width:${w1}"></div>
          <div class="skeleton sk-line sk-line-sm" style="width:${w2}"></div>
        </td>`;
      }
      const w = ['30%','50%','40%','45%','35%','30%'][c % 6];
      return `<td class="px-4 py-3"><div class="skeleton sk-line mx-auto" style="width:${w};margin:0 auto"></div></td>`;
    }).join('');
    return `<tr class="sk-row">${cells}</tr>`;
  }).join('');
}

const _ICONS = {
  box:      `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path stroke-linecap="round" stroke-linejoin="round" d="M20 7l-8-4-8 4m16 0l-8 4m8-4v10l-8 4m0-10L4 7m8 4v10M4 7v10l8 4"/></svg>`,
  tag:      `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path stroke-linecap="round" stroke-linejoin="round" d="M7 7h.01M7 3h5c.512 0 1.024.195 1.414.586l7 7a2 2 0 010 2.828l-7 7a2 2 0 01-2.828 0l-7-7A2 2 0 013 12V7a4 4 0 014-4z"/></svg>`,
  users:    `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path stroke-linecap="round" stroke-linejoin="round" d="M17 20h5v-2a3 3 0 00-5.356-1.857M17 20H7m10 0v-2c0-.656-.126-1.283-.356-1.857M7 20H2v-2a3 3 0 015.356-1.857M7 20v-2c0-.656.126-1.283.356-1.857m0 0a5.002 5.002 0 019.288 0M15 7a3 3 0 11-6 0 3 3 0 016 0z"/></svg>`,
  shield:   `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path stroke-linecap="round" stroke-linejoin="round" d="M9 12l2 2 4-4m5.618-4.016A11.955 11.955 0 0112 2.944a11.955 11.955 0 01-8.618 3.04A12.02 12.02 0 003 9c0 5.591 3.824 10.29 9 11.622 5.176-1.332 9-6.03 9-11.622 0-1.042-.133-2.052-.382-3.016z"/></svg>`,
  clipboard:`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path stroke-linecap="round" stroke-linejoin="round" d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2m-3 7h3m-3 4h3m-6-4h.01M9 16h.01"/></svg>`,
  wifi:     `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path stroke-linecap="round" stroke-linejoin="round" d="M8.111 16.404a5.5 5.5 0 017.778 0M12 20h.01m-7.08-7.071c3.904-3.905 10.236-3.905 14.141 0M1.394 9.393c5.857-5.857 15.355-5.857 21.213 0"/></svg>`,
  chart:    `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path stroke-linecap="round" stroke-linejoin="round" d="M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z"/></svg>`,
  user:     `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path stroke-linecap="round" stroke-linejoin="round" d="M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z"/></svg>`,
};

function _emptyState(icon, title, msg, btnHtml = '') {
  const svg = _ICONS[icon] || _ICONS.box;
  return `<div class="empty-state">
    <div class="empty-icon">${svg}</div>
    <div class="empty-title">${title}</div>
    <div class="empty-message">${msg}</div>
    ${btnHtml ? `<div class="empty-action">${btnHtml}</div>` : ''}
  </div>`;
}

function _tableEmpty(cols, icon, title, msg, btnHtml = '') {
  return `<tr><td colspan="${cols}" class="p-0">${_emptyState(icon, title, msg, btnHtml)}</td></tr>`;
}

function _tableError(cols, msg = 'Failed to load data. Check your connection and try again.') {
  return `<tr><td colspan="${cols}" class="p-0">
    <div class="error-state">
      <div class="error-icon">⚠</div>
      <div class="error-title">Something went wrong</div>
      <div class="error-message">${esc(msg)}</div>
    </div>
  </td></tr>`;
}

function _listEmpty(icon, title, msg) {
  return `<li class="p-0">${_emptyState(icon, title, msg)}</li>`;
}

function _listError(msg = 'Failed to load. Please refresh.') {
  return `<li class="p-0"><div class="error-state"><div class="error-icon">⚠</div>
    <div class="error-title">Load error</div>
    <div class="error-message">${esc(msg)}</div></div></li>`;
}

function _btnLoad(btn, loading, label) {
  if (loading) {
    btn._origText = btn.innerHTML;
    btn.disabled  = true;
    btn.innerHTML = `<span class="spinner"></span> ${label || 'Loading…'}`;
  } else {
    btn.disabled  = false;
    btn.innerHTML = btn._origText || label || 'Submit';
  }
}

function statusBadge(qty, threshold) {
  if (qty === 0)        return '<span class="badge badge-danger">Out of Stock</span>';
  if (qty <= threshold) return '<span class="badge badge-warning">Low Stock</span>';
  return                       '<span class="badge badge-success">In Stock</span>';
}

function tagStateBadge(state) {
  const map = {
    tagged:          'badge-info',
    in_transit:      'badge-warning',
    received:        'badge-info',
    racked:          'badge-success',
    dispatched:      'badge-neutral',
    returned:        'badge-orange',
    return_pending:  'badge-warning',
    out:             'badge-info',
    in:              'badge-success',
    consumed:        'badge-neutral',
  };
  return `<span class="badge ${map[state] || 'badge-neutral'}">${esc(state.replace(/_/g,' '))}</span>`;
}

function actionBadge(action) {
  const map = {
    scan_in:            'badge-success',
    scan_out:           'badge-info',
    manual_adjust:      'badge-neutral',
    admin_return:       'badge-purple',
    return_requested:   'badge-warning',
    return_confirmed:   'badge-orange',
    restock:            'badge-success',
    item_added:         'badge-info',
    item_updated:       'badge-neutral',
    item_deleted:       'badge-danger',
    tag_removed:        'badge-danger',
    tag_write:          'badge-info',
    factory_exit:       'badge-warning',
    warehouse_receive:  'badge-success',
    warehouse_dispatch: 'badge-neutral',
    warehouse_rack:     'badge-purple',
    customer_return:    'badge-orange',
    tag_reassigned:     'badge-warning',
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

// ── Toast notifications ───────────────────────────────────────────────────────
const _toastIcons = { success:'✓', error:'✕', info:'i', warning:'!' };
function showToast(msg, type = 'info', duration = 3500) {
  const container = document.getElementById('toast-container');
  if (!container) return;
  const el = document.createElement('div');
  el.className = `toast toast-${type}`;
  const icon = _toastIcons[type] || 'i';
  el.innerHTML = `<span class="toast-icon" style="font-weight:700;font-size:0.8rem;opacity:0.8;min-width:1rem;text-align:center">${icon}</span><span>${esc(msg)}</span>`;
  container.appendChild(el);
  const remove = () => {
    el.classList.add('toast-exit');
    setTimeout(() => el.remove(), 220);
  };
  const t = setTimeout(remove, duration);
  el.addEventListener('click', () => { clearTimeout(t); remove(); }, { once: true });
  el.style.cursor = 'pointer';
  el.title = 'Click to dismiss';
}

// ── Custom confirm dialog ─────────────────────────────────────────────────────
function customConfirm(title, message, danger = true) {
  return new Promise(resolve => {
    document.getElementById('confirm-icon').textContent  = danger ? '⚠' : '?';
    document.getElementById('confirm-title').textContent   = title;
    document.getElementById('confirm-message').textContent = message;
    const okBtn = document.getElementById('confirm-ok');
    okBtn.className = `flex-1 text-sm py-2 ${danger
      ? 'bg-red-500 hover:bg-red-600 text-white border-none cursor-pointer rounded-lg font-semibold'
      : 'btn-primary'}`;
    openModal('modal-confirm');
    const cleanup = (result) => {
      closeModal('modal-confirm');
      okBtn.replaceWith(okBtn.cloneNode(true));
      document.getElementById('confirm-cancel').replaceWith(
        document.getElementById('confirm-cancel').cloneNode(true));
      resolve(result);
    };
    document.getElementById('confirm-ok').addEventListener('click', () => cleanup(true), { once: true });
    document.getElementById('confirm-cancel').addEventListener('click', () => cleanup(false), { once: true });
  });
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
    tags:'RFID Tags', workers:'Workers', manufacturing:'Manufacturing',
    alerts:'Alerts', audit:'Audit Trail'
  };
  document.querySelectorAll('.nav-item').forEach(btn => {
    btn.addEventListener('click', () => {
      const tab = btn.dataset.tab;
      document.querySelectorAll('.nav-item').forEach(b => b.classList.remove('active'));
      document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
      btn.classList.add('active');
      document.getElementById(`tab-${tab}`).classList.add('active');
      document.getElementById('page-title').textContent = titles[tab] || tab;
      localStorage.setItem('activeTab', tab);

      if (tab === 'inventory')     fetchItems();
      if (tab === 'analytics')     fetchAnalytics();
      if (tab === 'tags')          fetchTags();
      if (tab === 'alerts')        fetchAlerts();
      if (tab === 'workers')       { fetchWorkers(); fetchUsers(); fetchWebhooks(); }
      if (tab === 'manufacturing') { fetchPipeline(); fetchPurchaseOrders(''); }
      if (tab === 'audit')         fetchAudit();
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
      dot.className = 'w-1.5 h-1.5 rounded-full shrink-0';
      dot.style.background = '#3fb950';
      label.textContent = 'MQTT Live';
      label.style.color = '#3fb950';
    } else {
      dot.className = 'w-1.5 h-1.5 rounded-full shrink-0';
      dot.style.background = '#f85149';
      label.textContent = 'MQTT Offline';
      label.style.color = '#f85149';
    }
  } catch {}
}

// ── Summary KPIs ──────────────────────────────────────────────────────────────
function _kpiLoading() {
  ['kpi-total-items','kpi-total-qty','kpi-health','kpi-low-stock',
   'kpi-out-of-stock','kpi-today-scans'].forEach(id => {
    const el = document.getElementById(id);
    if (el && el.textContent === '–') el.innerHTML = '<span class="skeleton sk-line sk-line-lg" style="width:2.5rem;display:inline-block"></span>';
  });
}

async function refreshSummary() {
  try {
    const s = await fetch('/api/analytics/summary').then(r => r.json());

    setText('kpi-total-items',   s.total_items);
    setText('kpi-health',        s.health_score + '%');
    setText('kpi-low-stock',     s.low_stock);
    setText('kpi-out-of-stock',  s.out_of_stock);
    setText('kpi-today-scans',   s.today_scans);
    setText('qs-dead-stock',     s.dead_stock);
    setText('qs-security-today', s.security_today);
    setText('qs-tags-in',        s.tags.in);
    setText('qs-tags-out',       s.tags.out);
    setText('qs-tags-consumed',  s.tags.consumed);
    setText('qs-tags-total',     s.tags.total);

    const unreadR = await fetch('/api/alerts').then(r => r.json());
    const unread  = unreadR.filter(a => !a.is_read).length;
    const badge   = document.getElementById('alert-badge');
    if (unread > 0) { badge.textContent = unread; badge.classList.remove('hidden'); }
    else badge.classList.add('hidden');

    const items   = await fetch('/api/items').then(r => r.json());
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
      datasets: [{ data: [s.healthy, s.low_stock, s.out_of_stock],
        backgroundColor: ['#22c55e','#f59e0b','#ef4444'], borderWidth: 0 }]
    },
    options: { cutout: '68%', plugins: {
      legend: { position:'bottom', labels:{ font:{ size:10 }, padding:8 } } } }
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
      datasets: [{ data: [tags.in, tags.out, tags.consumed],
        backgroundColor: ['#22c55e','#3b82f6','#9ca3af'], borderWidth: 0 }]
    },
    options: { cutout: '68%', plugins: {
      legend: { position:'bottom', labels:{ font:{ size:10 }, padding:8 } } } }
  });
}

// ── Transaction trends chart ──────────────────────────────────────────────────
async function fetchTransactionTrends() {
  try {
    const data = await fetch('/api/analytics/trends?days=7').then(r => r.json());
    const ctx  = document.getElementById('chart-trends');
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
          { label:'Received',   data: data.map(d => d.received),
            backgroundColor: '#6366f1', borderRadius: 3 },
          { label:'Dispatched', data: data.map(d => d.dispatched),
            backgroundColor: '#f59e0b', borderRadius: 3 },
        ]
      },
      options: {
        responsive: true,
        plugins: { legend:{ position:'bottom', labels:{ font:{ size:11 } } } },
        scales: {
          y: { beginAtZero:true, ticks:{ stepSize:1 } },
          x: { grid:{ display:false } }
        }
      }
    });
  } catch {}
}

// ── Analytics tab ─────────────────────────────────────────────────────────────
async function fetchAnalytics() {
  const tbody = document.getElementById('analytics-tbody');
  if (tbody && !_analytics.length) tbody.innerHTML = _skeletonRows(7);
  try {
    const [analyticsData, abcRaw, items] = await Promise.all([
      fetch('/api/analytics').then(r => r.json()),
      fetch('/api/analytics/abc').then(r => r.json()),
      fetch('/api/items').then(r => r.json()),
    ]);
    abcData    = abcRaw;
    _analytics = analyticsData;

    const itemMap = Object.fromEntries(items.map(i => [i.id, i]));
    const labels  = analyticsData.map(d => esc(d.item_id.replace('item-','')));
    const stocks  = analyticsData.map(d => (itemMap[d.item_id] || {}).quantity ?? 0);
    const risks   = analyticsData.map(d => d.risk_score);
    const days    = analyticsData.map(d => Math.min(d.days_remaining, 30));

    const ctxS = document.getElementById('chart-stock');
    if (ctxS) {
      if (charts.stock) charts.stock.destroy();
      charts.stock = new Chart(ctxS, {
        type: 'bar',
        data: { labels, datasets: [{
          label: 'Quantity', data: stocks, borderRadius: 4,
          backgroundColor: analyticsData.map(d => {
            return d.days_remaining <= 3 ? '#ef4444' : d.days_remaining <= 7 ? '#f59e0b' : '#6366f1';
          }),
        }] },
        options: { responsive:true, plugins:{ legend:{ display:false } },
          scales:{ y:{ beginAtZero:true, ticks:{ stepSize:1 } } } }
      });
    }

    const ctxR = document.getElementById('chart-risk');
    if (ctxR) {
      if (charts.risk) charts.risk.destroy();
      charts.risk = new Chart(ctxR, {
        type: 'bar',
        data: { labels, datasets: [{
          label:'Days Remaining (capped 30)', data: days,
          backgroundColor: risks.map(riskColor), borderRadius: 4,
        }] },
        options: { indexAxis:'y', responsive:true,
          plugins:{ legend:{ display:false } },
          scales:{ x:{ beginAtZero:true, max:30 } } }
      });
    }

    const tbody = document.getElementById('analytics-tbody');
    if (tbody) {
      if (!analyticsData.length) {
        tbody.innerHTML = _tableEmpty(7, 'chart', 'No analytics data yet',
          'Analytics are calculated once items have transaction history. Add items and scan RFID tags to see insights.');
      } else tbody.innerHTML = analyticsData.map(d => {
        const item   = itemMap[d.item_id] || {};
        const abc    = abcData[d.item_id];
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
  } catch {
    const tbody = document.getElementById('analytics-tbody');
    if (tbody) tbody.innerHTML = _tableError(7);
  }
}

// ── Transactions (scan log) ───────────────────────────────────────────────────
async function fetchTransactions() {
  const ul = document.getElementById('scan-log');
  if (ul && !_transactions.length) {
    ul.innerHTML = `<li class="p-0"><div class="section-loading"><span class="spinner spinner-dark"></span> Loading activity…</div></li>`;
  }
  try {
    const txs = await fetch('/api/transactions?limit=50').then(r => r.json());
    _transactions = txs;
    if (!ul) return;
    if (!txs.length) {
      ul.innerHTML = `<li class="p-0">
        <div class="scan-empty">
          <div class="scan-pulse">
            <svg class="w-4 h-4 text-gray-300" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8.111 16.404a5.5 5.5 0 017.778 0M12 20h.01m-7.08-7.071c3.904-3.905 10.236-3.905 14.141 0M1.394 9.393c5.857-5.857 15.355-5.857 21.213 0"/>
            </svg>
          </div>
          <div class="text-sm font-medium text-gray-400">Waiting for first scan</div>
          <div class="text-xs text-gray-300">RFID activity will appear here in real time</div>
        </div>
      </li>`;
      return;
    }
    ul.innerHTML = txs.map(t => `
      <li class="py-2.5 flex items-start gap-3 hover:bg-slate-50 px-1 rounded-lg transition-colors">
        <div class="flex-1 min-w-0">
          <div class="text-sm font-medium text-gray-700 truncate">${esc(t.item_name || t.item_id)}</div>
          <div class="text-xs text-gray-400 mt-0.5">
            ${fmtDate(t.timestamp)}
            ${t.tag_uid ? `<span class="font-mono ml-1 text-gray-500">${esc(t.tag_uid)}</span>` : ''}
            ${t.performed_by && t.performed_by !== 'system' ? `<span class="ml-1 text-blue-500">· ${esc(t.performed_by)}</span>` : ''}
          </div>
        </div>
        <div class="shrink-0 mt-0.5">${actionBadge(t.action)}</div>
      </li>`).join('');
  } catch {
    if (ul) ul.innerHTML = _listError('Could not load transaction history.');
  }
}

// ── Inventory table ───────────────────────────────────────────────────────────
async function fetchItems() {
  const tbody = document.getElementById('inventory-tbody');
  if (tbody && !_items.length) tbody.innerHTML = _skeletonRows(7);
  try {
    const items = await fetch('/api/items').then(r => r.json());
    _items = items;
    renderItemsTable(items);
  } catch {
    if (tbody) tbody.innerHTML = _tableError(7);
  }
}

function renderItemsTable(items) {
  const tbody = document.getElementById('inventory-tbody');
  if (!tbody) return;
  const isManager = currentRole === 'admin' || currentRole === 'manager';
  if (!items.length) {
    const btn = isManager
      ? `<button onclick="openAddItemModal()" class="btn-primary text-sm">+ Add first item</button>`
      : '';
    tbody.innerHTML = _tableEmpty(7, 'box', 'No inventory items yet',
      'Add your first item to start tracking stock levels and receive alerts.', btn);
    return;
  }
  tbody.innerHTML = items.map(item => {
    const reserved  = item.reserved_qty || 0;
    const available = item.available_qty ?? (item.quantity - reserved);
    const rowCls = item.quantity === 0 ? 'row-danger' :
                   item.quantity <= item.low_stock_threshold ? 'row-warning' : '';
    let actions = '<span class="text-gray-300 text-xs">—</span>';
    if (isManager) {
      actions = `<div class="action-cell">
        <button onclick='openEditModal(${JSON.stringify(item)})' class="btn-sm btn-sm-edit">Edit</button>
        ${currentRole === 'admin'
          ? `<button onclick="deleteItem('${esc(item.id)}')" class="btn-sm btn-sm-danger">Delete</button>`
          : ''}
      </div>`;
    }
    return `
      <tr class="${rowCls}" data-item-id="${esc(item.id)}">
        <td class="px-4 py-3">
          <div class="font-medium text-gray-800">${esc(item.name)}</div>
          <div class="text-xs text-gray-400">${esc(item.id)}</div>
        </td>
        <td class="px-4 py-3 text-right font-mono font-semibold text-gray-700">
          ${item.quantity} <span class="text-xs font-normal text-gray-400">${esc(item.unit)}</span>
        </td>
        <td class="px-4 py-3 text-right font-mono text-sm ${reserved > 0 ? 'text-amber-600 font-semibold' : 'text-gray-400'}">
          ${reserved > 0 ? reserved : '—'}
        </td>
        <td class="px-4 py-3 text-center text-sm text-gray-500">${item.low_stock_threshold}</td>
        <td class="px-4 py-3 text-center">${statusBadge(available, item.low_stock_threshold)}</td>
        <td class="px-4 py-3 text-center text-xs text-gray-400">${fmtDate(item.updated_at)}</td>
        <td class="px-4 py-3 text-center">${actions}</td>
      </tr>`;
  }).join('');
}

function filterInventory(q) {
  const query = q.toLowerCase();
  document.querySelectorAll('#inventory-tbody tr[data-item-id]').forEach(row => {
    row.style.display = row.textContent.toLowerCase().includes(query) ? '' : 'none';
  });
}

// ── RFID Tags table ───────────────────────────────────────────────────────────
async function fetchTags() {
  const tbody = document.getElementById('tags-tbody');
  if (tbody && !_tags.length) tbody.innerHTML = _skeletonRows(6);
  try {
    const tags = await fetch('/api/tags').then(r => r.json());
    _tags = tags;
    renderTagsTable(tags);
  } catch {
    if (tbody) tbody.innerHTML = _tableError(6);
  }
}

function renderTagsTable(tags) {
  const tbody = document.getElementById('tags-tbody');
  if (!tbody) return;
  if (!tags.length) {
    tbody.innerHTML = _tableEmpty(6, 'tag', 'No RFID tags yet',
      'Tags appear automatically when scanned. In legacy mode, scan any tag at your reader to register it.');
    return;
  }
  tbody.innerHTML = tags.map(t => {
    let action = '<span class="text-gray-300 text-xs">—</span>';
    if (currentRole === 'admin') {
      if (t.state === 'return_pending') {
        action = `<div class="action-cell">
          <span class="text-xs text-amber-600 font-medium italic">Awaiting scan…</span>
          <button onclick="openReassignTagModal('${esc(t.uid)}')" class="btn-sm btn-sm-neutral">Reassign</button>
          <button onclick="deleteTag('${esc(t.uid)}')" class="btn-sm btn-sm-danger">Remove</button>
        </div>`;
      } else if (t.state === 'consumed' || t.state === 'dispatched') {
        action = `<div class="action-cell">
          <button onclick="openReturnModal('${esc(t.uid)}')" class="btn-sm btn-sm-warn">Return</button>
          <button onclick="openReassignTagModal('${esc(t.uid)}')" class="btn-sm btn-sm-neutral">Reassign</button>
          <button onclick="deleteTag('${esc(t.uid)}')" class="btn-sm btn-sm-danger">Remove</button>
        </div>`;
      } else {
        action = `<div class="action-cell">
          <button onclick="openReassignTagModal('${esc(t.uid)}')" class="btn-sm btn-sm-neutral">Reassign</button>
          <button onclick="deleteTag('${esc(t.uid)}')" class="btn-sm btn-sm-danger">Remove</button>
        </div>`;
      }
    }
    return `
      <tr data-tag-uid="${esc(t.uid)}" data-tag-state="${esc(t.state)}">
        <td class="px-4 py-3 font-mono text-sm text-gray-700">${esc(t.uid)}</td>
        <td class="px-4 py-3 text-sm text-gray-700">${esc(t.item_name || t.item_id)}</td>
        <td class="px-4 py-3 text-center">${tagStateBadge(t.state)}</td>
        <td class="px-4 py-3 text-center text-xs text-gray-400">${esc(t.rack_location) || '<span class="text-gray-300">—</span>'}</td>
        <td class="px-4 py-3 text-center text-xs text-gray-400">${fmtDate(t.last_scan)}</td>
        <td class="px-4 py-3 text-center">${action}</td>
      </tr>`;
  }).join('');
}

function filterTags() {
  const q     = (document.getElementById('tag-search-input')?.value || '').toLowerCase();
  const state = document.getElementById('tag-state-filter')?.value || '';
  document.querySelectorAll('#tags-tbody tr[data-tag-uid]').forEach(row => {
    const matchText  = !q || row.textContent.toLowerCase().includes(q);
    const matchState = !state || row.dataset.tagState === state;
    row.style.display = (matchText && matchState) ? '' : 'none';
  });
}

// ── Alerts ────────────────────────────────────────────────────────────────────
async function fetchAlerts() {
  try {
    const ul = document.getElementById('alerts-list');
    if (ul && !_alerts.length) ul.innerHTML = `<li class="p-0"><div class="section-loading"><span class="spinner spinner-dark"></span> Checking alerts…</div></li>`;
    const alerts = await fetch('/api/alerts').then(r => r.json());
    _alerts = alerts;

    const unread = alerts.filter(a => !a.is_read);
    const badge  = document.getElementById('alert-badge');
    if (unread.length > 0) {
      badge.textContent = unread.length;
      badge.classList.remove('hidden');
      showBanner(`${unread.length} unread alert${unread.length > 1 ? 's' : ''}: ${unread[0].message}`);
    } else {
      badge.classList.add('hidden');
    }

    renderAlerts(alerts);
  } catch {
    const ul = document.getElementById('alerts-list');
    if (ul) ul.innerHTML = _listError('Could not load alerts.');
  }
}

function renderAlerts(alerts) {
  const ul = document.getElementById('alerts-list');
  if (!ul) return;

  let filtered = alerts;
  if (_alertFilter === 'unread')      filtered = alerts.filter(a => !a.is_read);
  else if (_alertFilter !== 'all')    filtered = alerts.filter(a => a.alert_type === _alertFilter);

  if (!filtered.length) {
    const msgs = {
      all:          ['All clear', 'No alerts right now. You\'ll be notified when stock runs low or security events occur.'],
      unread:       ['No unread alerts', 'All alerts have been reviewed.'],
      security:     ['No security alerts', 'No unauthorized scan attempts detected.'],
      low_stock:    ['Stock levels OK', 'No items below their threshold right now.'],
      out_of_stock: ['Nothing out of stock', 'All items have available quantity.'],
    };
    const [title, msg] = msgs[_alertFilter] || ['No alerts', ''];
    ul.innerHTML = _listEmpty('shield', title, msg);
    return;
  }

  const typeIcon     = { out_of_stock:'🔴', low_stock:'🟡', security:'🔒' };
  const typeBadgeMap = { out_of_stock:'badge-danger', low_stock:'badge-warning', security:'badge-orange' };
  ul.innerHTML = filtered.map(a => {
    const icon   = typeIcon[a.alert_type] || '🔔';
    const rowCls = a.is_read ? 'opacity-50' : '';
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
        ${!a.is_read
          ? `<button onclick="markRead(${a.id})" class="btn-sm btn-sm-neutral shrink-0 mt-0.5">Dismiss</button>`
          : '<span class="text-xs text-gray-300 shrink-0 mt-1">read</span>'}
      </li>`;
  }).join('');
}

function setAlertFilter(type) {
  _alertFilter = type;
  document.querySelectorAll('#alert-filters button').forEach(btn => {
    const active = btn.dataset.filter === type;
    btn.className = `btn-sm ${active ? 'btn-sm-edit' : 'btn-sm-neutral'}`;
  });
  renderAlerts(_alerts);
}

async function markRead(id) {
  await fetch(`/api/alerts/${id}/read`, { method:'POST' });
  fetchAlerts();
  refreshSummary();
}

async function markAllRead() {
  await fetch('/api/alerts/read-all', { method:'POST' });
  fetchAlerts();
  refreshSummary();
  showToast('All alerts marked as read', 'success');
}

async function clearReadAlerts() {
  const readCount = _alerts.filter(a => a.is_read).length;
  if (!readCount) return showToast('No dismissed alerts to clear', 'info');
  const ok = await customConfirm(
    'Clear Dismissed Alerts',
    `Permanently delete ${readCount} read alert${readCount > 1 ? 's' : ''}?`,
    true
  );
  if (!ok) return;
  const r = await fetch('/api/alerts/read', { method:'DELETE' });
  if (r.ok) {
    fetchAlerts();
    showToast('Dismissed alerts cleared', 'info');
  } else {
    showToast('Failed to clear alerts (status ' + r.status + ')', 'error');
  }
}

function exportAlertsCSV() {
  if (!_alerts.length) return showToast('No alerts to export', 'warning');
  _downloadCSV(
    [['Timestamp','Type','Item','Message','Read'],
     ..._alerts.map(a => [a.timestamp, a.alert_type, a.item_name || '', a.message, a.is_read ? 'Yes' : 'No'])],
    'alerts.csv'
  );
  showToast('Alerts exported as CSV', 'success');
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
  const noteEl = document.getElementById('return-note');
  if (noteEl) noteEl.value = '';
  openModal('modal-tag-return');
}

document.querySelectorAll('.modal-overlay').forEach(el => {
  el.addEventListener('click', e => { if (e.target === el) el.classList.add('hidden'); });
});

// ── Forms ─────────────────────────────────────────────────────────────────────
function setupForms() {
  document.getElementById('form-add-item').addEventListener('submit', async e => {
    e.preventDefault();
    const btn  = e.target.querySelector('button[type=submit]');
    const fd   = new FormData(e.target);
    const body = Object.fromEntries(fd);
    body.quantity            = parseInt(body.quantity);
    body.low_stock_threshold = parseInt(body.low_stock_threshold);
    _btnLoad(btn, true, 'Adding…');
    try {
      const r = await fetch('/api/items', {
        method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify(body),
      });
      if (r.ok) {
        closeModal('modal-add-item');
        e.target.reset();
        fetchItems();
        refreshSummary();
        showToast('Item added successfully', 'success');
      } else {
        const d = await r.json();
        showToast(d.error || 'Failed to add item', 'error');
      }
    } finally { _btnLoad(btn, false, 'Add Item'); }
  });

  document.getElementById('form-edit-item').addEventListener('submit', async e => {
    e.preventDefault();
    const btn  = e.target.querySelector('button[type=submit]');
    const fd   = new FormData(e.target);
    const body = Object.fromEntries(fd);
    const id   = body.id; delete body.id;
    body.quantity            = parseInt(body.quantity);
    body.low_stock_threshold = parseInt(body.low_stock_threshold);
    _btnLoad(btn, true, 'Saving…');
    try {
      const r = await fetch(`/api/items/${encodeURIComponent(id)}`, {
        method:'PUT', headers:{'Content-Type':'application/json'},
        body: JSON.stringify(body),
      });
      if (r.ok) {
        closeModal('modal-edit-item');
        fetchItems();
        refreshSummary();
        showToast('Item updated', 'success');
      } else {
        const d = await r.json().catch(() => ({}));
        showToast(d.error || 'Failed to update item', 'error');
      }
    } finally { _btnLoad(btn, false, 'Save Changes'); }
  });

  document.getElementById('form-tag-return').addEventListener('submit', async e => {
    e.preventDefault();
    const btn  = e.target.querySelector('button[type=submit]');
    const uid  = document.getElementById('return-tag-uid').value;
    const note = (document.getElementById('return-note')?.value || '').trim() || 'Admin return';
    _btnLoad(btn, true, 'Submitting…');
    try {
      const r = await fetch(`/api/tags/${encodeURIComponent(uid)}/return`, {
        method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({ note }),
      });
      if (r.ok) {
        closeModal('modal-tag-return');
        showToast('Marked return pending — scan tag to confirm', 'warning', 5000);
      } else {
        const d = await r.json().catch(() => ({}));
        showToast(d.error || 'Return request failed', 'error');
      }
    } finally { _btnLoad(btn, false, 'Mark Return Pending'); }
  });
}

// ── CRUD ──────────────────────────────────────────────────────────────────────
async function deleteItem(id) {
  const ok = await customConfirm(
    'Delete Item',
    `Delete "${id}" and all its associated RFID tags? This cannot be undone.`,
    true
  );
  if (!ok) return;
  const r = await fetch(`/api/items/${encodeURIComponent(id)}`, { method:'DELETE' });
  if (r.ok) { showToast('Item deleted', 'info'); fetchItems(); refreshSummary(); }
  else showToast('Failed to delete item', 'error');
}

async function deleteTag(uid) {
  const ok = await customConfirm('Remove Tag', `Remove tag record for UID ${uid}?`, true);
  if (!ok) return;
  const r = await fetch(`/api/tags/${encodeURIComponent(uid)}`, { method:'DELETE' });
  if (r.ok) { showToast('Tag removed', 'info'); fetchTags(); }
  else showToast('Failed to remove tag', 'error');
}

// ── SSE ───────────────────────────────────────────────────────────────────────
function connectSSE() {
  const src = new EventSource('/api/events');

  src.onmessage = e => {
    const data = JSON.parse(e.data);

    const lb = document.getElementById('scan-live-badge');
    lb.classList.remove('hidden');
    clearTimeout(lb._timer);
    lb._timer = setTimeout(() => lb.classList.add('hidden'), 3000);

    if (data.type === 'scan' || data.type === 'rejected_scan') {
      refreshSummary();
      fetchTransactions();
      fetchItems().then(() => highlightItem(data.item_id));
      fetchTags();
      if (data.type === 'rejected_scan') {
        showBanner(`SECURITY: Consumed tag ${data.tag_uid} scanned for ${data.item_name} — possible reuse`);
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
      if (document.getElementById('tab-manufacturing').classList.contains('active')) fetchPipeline();
    }

    if (data.type === 'security_alert') {
      showBanner(`SECURITY: ${data.message}`);
      fetchAlerts();
      refreshSummary();
    }

    if (data.type === 'return_pending') {
      fetchTags();
      showToast(`Tag ${data.tag_uid} marked return pending — scan to confirm`, 'warning', 4000);
    }

    if (data.type === 'worker_auth') {
      showToast(`${data.name} authenticated at ${data.device_id}`, 'info', 2500);
      if (document.getElementById('tab-workers').classList.contains('active')) fetchWorkers();
    }

    if (data.type === 'worker_denied') {
      showToast(`Access denied: ${data.name} (${data.employee_id}) is inactive`, 'error');
    }

    if (data.type === 'job_created') {
      if (document.getElementById('tab-manufacturing').classList.contains('active')) fetchPipeline();
    }
  };

  src.onerror = () => { src.close(); setTimeout(connectSSE, 3000); };
}

function highlightItem(item_id) {
  document.querySelectorAll(`#inventory-tbody tr[data-item-id="${CSS.escape(item_id)}"]`)
    .forEach(row => {
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
  clearTimeout(banner._timer);
  banner._timer = setTimeout(() => banner.classList.add('hidden'), 6000);
}

// ── Auth ──────────────────────────────────────────────────────────────────────
async function doLogout() {
  await fetch('/api/logout', { method:'POST' });
  window.location.href = '/login';
}

// ── RBAC ──────────────────────────────────────────────────────────────────────
function applyRBAC(role) {
  const isManager = role === 'admin' || role === 'manager';
  const isAdmin   = role === 'admin';
  document.getElementById('nav-workers') && (
    document.getElementById('nav-workers').style.display = isManager ? '' : 'none'
  );
  document.querySelectorAll('[data-tab="manufacturing"]').forEach(el => {
    el.style.display = isManager ? '' : 'none';
  });
  document.querySelectorAll('[data-tab="analytics"]').forEach(el => {
    el.style.display = isManager ? '' : 'none';
  });
  // Audit trail visible to ALL roles — no hide
  const accCard = document.getElementById('dashboard-accounts-card');
  if (accCard) accCard.style.display = isAdmin ? '' : 'none';
  const regCard = document.getElementById('worker-register-card');
  if (regCard) regCard.style.display = isManager ? '' : 'none';
  const webhooksCard = document.getElementById('webhooks-card');
  if (webhooksCard) webhooksCard.style.display = isAdmin ? '' : 'none';
  const poCard = document.getElementById('purchase-orders-card');
  if (poCard) poCard.style.display = isManager ? '' : 'none';
  const importBtn = document.getElementById('btn-import-csv');
  if (importBtn) importBtn.style.display = isManager ? '' : 'none';
  const addPoBtn = document.getElementById('btn-add-po');
  if (addPoBtn) addPoBtn.style.display = isManager ? '' : 'none';
}

// ── Workers tab ───────────────────────────────────────────────────────────────
async function fetchWorkers() {
  const tbody = document.getElementById('workers-tbody');
  if (tbody && !_workers.length) tbody.innerHTML = _skeletonRows(7);
  try {
    const [workers, sessions] = await Promise.all([
      fetch('/api/workers').then(r => r.json()),
      fetch('/api/workers/sessions').then(r => r.json()),
    ]);
    _workers = workers;
    renderWorkerSessions(sessions);
    renderWorkerTable(workers);
  } catch {
    if (tbody) tbody.innerHTML = _tableError(7);
  }
}

function renderWorkerSessions(sessions) {
  const el = document.getElementById('worker-sessions');
  if (!el) return;
  const entries = Object.entries(sessions);
  if (!entries.length) {
    el.innerHTML = `<div class="flex items-center gap-2 text-sm text-slate-400 py-1">
      <span class="w-1.5 h-1.5 rounded-full bg-slate-300 shrink-0"></span>
      No workers authenticated — tap an RFID badge at any station reader
    </div>`;
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
  const tbody     = document.getElementById('workers-tbody');
  if (!tbody) return;
  const isManager = currentRole === 'admin' || currentRole === 'manager';
  if (!workers.length) {
    tbody.innerHTML = _tableEmpty(7, 'users', 'No workers registered yet',
      'Register workers so their RFID badges can authenticate at stations. Use the form above to add your first worker.');
    return;
  }
  const roleBadge = r => {
    const map = { supervisor:'badge-purple', operator:'badge-info' };
    return `<span class="badge ${map[r] || 'badge-neutral'}">${esc(r)}</span>`;
  };
  tbody.innerHTML = workers.map(w => {
    const activeChip = w.active_station
      ? `<span class="badge badge-success ml-1">@ ${esc(w.active_station)}</span>` : '';
    let actions = '<span class="text-gray-300 text-xs">—</span>';
    if (isManager) {
      const toggleBtn = w.active
        ? `<button onclick="toggleWorker(${w.id}, ${w.active})" class="btn-sm btn-sm-danger">Deactivate</button>`
        : `<button onclick="toggleWorker(${w.id}, ${w.active})" class="btn-sm btn-sm-success">Activate</button>`;
      const deleteBtn = currentRole === 'admin'
        ? `<button onclick="deleteWorker(${w.id})" class="btn-sm btn-sm-danger">Delete</button>` : '';
      actions = `<div class="action-cell">${toggleBtn}${deleteBtn}</div>`;
    }
    return `
      <tr data-worker-id="${w.id}">
        <td class="px-4 py-3">
          <div class="font-medium text-gray-800">${esc(w.name)}</div>
          <div class="text-xs text-gray-400">${esc(w.employee_id)}</div>
        </td>
        <td class="px-4 py-3 text-center">${roleBadge(w.role)}</td>
        <td class="px-4 py-3 text-center text-xs text-gray-500">${esc(w.zone) || '<span class="text-gray-300">—</span>'}</td>
        <td class="px-4 py-3 font-mono text-xs text-gray-500">
          ${esc(w.uid) || '<span class="text-gray-300 text-xs italic">not yet scanned</span>'}
        </td>
        <td class="px-4 py-3 text-center">
          ${w.active
            ? '<span class="badge badge-success">Active</span>'
            : '<span class="badge badge-neutral">Inactive</span>'}
          ${activeChip}
        </td>
        <td class="px-4 py-3 text-center text-xs text-gray-400">${fmtDate(w.last_seen)}</td>
        <td class="px-4 py-3 text-center">${actions}</td>
      </tr>`;
  }).join('');
}

function filterWorkers(q) {
  const query = q.toLowerCase();
  document.querySelectorAll('#workers-tbody tr[data-worker-id]').forEach(row => {
    row.style.display = row.textContent.toLowerCase().includes(query) ? '' : 'none';
  });
}

async function toggleWorker(id, currentActive) {
  const newActive = currentActive ? 0 : 1;
  const r = await fetch(`/api/workers/${id}`, {
    method:'PUT', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ active: newActive }),
  });
  if (r.ok) {
    showToast(newActive ? 'Worker activated' : 'Worker deactivated', newActive ? 'success' : 'warning');
    fetchWorkers();
  } else {
    showToast('Failed to update worker', 'error');
  }
}

async function deleteWorker(id) {
  const ok = await customConfirm('Delete Worker', 'Delete this worker record? Their transaction history will be preserved.', true);
  if (!ok) return;
  const r = await fetch(`/api/workers/${id}`, { method:'DELETE' });
  if (r.ok) { showToast('Worker deleted', 'info'); fetchWorkers(); }
  else showToast('Failed to delete worker', 'error');
}

document.addEventListener('DOMContentLoaded', () => {
  const form = document.getElementById('form-add-worker');
  if (form) {
    form.addEventListener('submit', async e => {
      e.preventDefault();
      const btn         = e.target.querySelector('button[type=submit]');
      const employee_id = document.getElementById('w-employee-id').value.trim().toUpperCase();
      const name        = document.getElementById('w-name').value.trim();
      const role        = document.getElementById('w-role').value;
      _btnLoad(btn, true, 'Registering…');
      try {
        const r = await fetch('/api/workers', {
          method:'POST', headers:{'Content-Type':'application/json'},
          body: JSON.stringify({ employee_id, name, role }),
        });
        const d = await r.json().catch(() => ({}));
        if (r.ok) {
          form.reset();
          fetchWorkers();
          showToast(`${esc(name)} registered as ${role}`, 'success');
        } else {
          showToast(d.error || 'Failed to register worker', 'error');
        }
      } finally { _btnLoad(btn, false, 'Register Worker'); }
    });
  }
});

setInterval(() => {
  if (document.getElementById('tab-workers').classList.contains('active')) {
    fetch('/api/workers/sessions').then(r => r.json()).then(renderWorkerSessions).catch(() => {});
  }
}, 30000);

// ── Pipeline / Manufacturing tab ──────────────────────────────────────────────
const PIPELINE_STAGES = [
  { key:'tagged',     label:'Tagged',     color:'#6366f1', desc:'Written at factory' },
  { key:'in_transit', label:'In Transit', color:'#f59e0b', desc:'Left factory floor' },
  { key:'received',   label:'Received',   color:'#3b82f6', desc:'At warehouse dock'  },
  { key:'racked',     label:'Racked',     color:'#22c55e', desc:'On warehouse shelf' },
  { key:'dispatched', label:'Dispatched', color:'#94a3b8', desc:'Sent to customer'   },
  { key:'returned',   label:'Returned',   color:'#f97316', desc:'Customer return'    },
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
    fetchPurchaseOrders('');
  } catch {}
}

function renderPipelineFlow(totals) {
  const el = document.getElementById('pipeline-flow');
  if (!el) return;
  const total = PIPELINE_STAGES.reduce((s, st) => s + (totals[st.key] || 0), 0);
  el.innerHTML = PIPELINE_STAGES.map((st, i) => `
    <div class="flex-1 min-w-20 flex flex-col items-center gap-1">
      <div class="w-full rounded-lg p-3 text-center text-white" style="background:${st.color}">
        <div class="text-2xl font-bold">${totals[st.key] || 0}</div>
        <div class="text-xs opacity-90 mt-0.5 font-semibold">${st.label}</div>
      </div>
      <div class="text-xs text-slate-400 text-center leading-tight">${esc(st.desc)}</div>
    </div>
    ${i < PIPELINE_STAGES.length - 1
      ? '<div class="self-start mt-4 text-slate-300 text-xl hidden lg:flex">&#10132;</div>' : ''}`
  ).join('');
}

function renderPipelineItems(perItem, items) {
  const tbody = document.getElementById('pipeline-items-tbody');
  if (!tbody) return;
  const itemNameMap = Object.fromEntries(items.map(i => [i.id, i.name]));
  if (!perItem.length) {
    tbody.innerHTML = _tableEmpty(7, 'tag', 'No pipeline activity yet',
      'Tags will appear here once written at the factory and scanned through the pipeline.');
    return;
  }
  tbody.innerHTML = perItem.map(row => {
    const name = itemNameMap[row.item_id] || row.item_name || row.item_id;
    const cell = key => {
      const n  = row[key] || 0;
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
    el.innerHTML = '<span class="text-slate-400 text-sm">No racked items yet</span>';
    return;
  }
  el.innerHTML = rackStats.map(r => `
    <div class="bg-green-50 border border-green-200 rounded-lg px-4 py-3 text-center min-w-20">
      <div class="text-2xl font-bold text-green-700">${r.cnt}</div>
      <div class="text-xs text-green-600 font-semibold mt-0.5">${esc(r.rack_location)}</div>
    </div>`).join('');
}

// ── Export helpers ────────────────────────────────────────────────────────────
function _downloadCSV(rows, filename) {
  const csv  = rows.map(r => r.map(c => `"${String(c ?? '').replace(/"/g,'""')}"`).join(',')).join('\n');
  const blob = new Blob([csv], { type:'text/csv;charset=utf-8;' });
  const url  = URL.createObjectURL(blob);
  const a    = Object.assign(document.createElement('a'), { href:url, download:filename });
  document.body.appendChild(a); a.click();
  setTimeout(() => { URL.revokeObjectURL(url); a.remove(); }, 100);
}

function _exportPDF(title, head, body) {
  try {
    const { jsPDF } = window.jspdf;
    const doc = new jsPDF();
    doc.setFontSize(14);
    doc.text(title, 14, 16);
    doc.setFontSize(9);
    doc.setTextColor(120);
    doc.text(`Exported ${new Date().toLocaleString()}`, 14, 22);
    doc.autoTable({ head:[head], body, startY:27, styles:{ fontSize:9 },
      headStyles:{ fillColor:[79,70,229] } });
    doc.save(`${title.toLowerCase().replace(/\s+/g,'-')}.pdf`);
    showToast('PDF exported', 'success');
  } catch(e) {
    showToast('PDF export failed — check console', 'error');
    console.error(e);
  }
}

function exportItemsCSV() {
  if (!_items.length) return showToast('No data to export', 'warning');
  _downloadCSV(
    [['ID','Name','Quantity','Reserved','Unit','Low Stock Threshold','Status','Updated'],
     ..._items.map(i => [i.id, i.name, i.quantity, i.reserved_qty || 0, i.unit, i.low_stock_threshold,
       i.quantity === 0 ? 'Out of Stock' : i.quantity <= i.low_stock_threshold ? 'Low Stock' : 'In Stock',
       i.updated_at])],
    'inventory.csv'
  );
  showToast('Inventory exported as CSV', 'success');
}

function exportItemsPDF() {
  if (!_items.length) return showToast('No data to export', 'warning');
  _exportPDF(
    'Inventory Report',
    ['ID','Name','Qty','Unit','Threshold','Status'],
    _items.map(i => [i.id, i.name, i.quantity, i.unit, i.low_stock_threshold,
      i.quantity === 0 ? 'Out of Stock' : i.quantity <= i.low_stock_threshold ? 'Low Stock' : 'In Stock'])
  );
}

function exportTagsCSV() {
  if (!_tags.length) return showToast('No data to export', 'warning');
  _downloadCSV(
    [['UID','Item','State','Rack Location','Last Scan','Registered'],
     ..._tags.map(t => [t.uid, t.item_name || t.item_id, t.state,
       t.rack_location || '', t.last_scan || '', t.registered_at])],
    'rfid-tags.csv'
  );
  showToast('Tags exported as CSV', 'success');
}

function exportWorkersCSV() {
  if (!_workers.length) return showToast('No data to export', 'warning');
  _downloadCSV(
    [['Employee ID','Name','Role','Badge UID','Status','Last Active'],
     ..._workers.map(w => [w.employee_id, w.name, w.role, w.uid || '',
       w.active ? 'Active' : 'Inactive', w.last_seen || ''])],
    'workers.csv'
  );
  showToast('Workers exported as CSV', 'success');
}

function exportAnalyticsCSV() {
  if (!_analytics.length) return showToast('No data to export', 'warning');
  _downloadCSV(
    [['Item ID','Avg Daily Usage','Forecast Demand','EOQ','Days Remaining','Risk Score','ABC Class'],
     ..._analytics.map(d => [d.item_id, d.avg_daily_usage, d.forecast_demand, d.eoq,
       d.days_remaining >= 999 ? 'Unlimited' : d.days_remaining, d.risk_score,
       (abcData[d.item_id] || {}).class || '—'])],
    'analytics.csv'
  );
  showToast('Analytics exported as CSV', 'success');
}

function exportAnalyticsPDF() {
  if (!_analytics.length) return showToast('No data to export', 'warning');
  _exportPDF(
    'Inventory Analytics Report',
    ['Item ID','Avg Daily','Forecast','EOQ','Days Left','Risk','ABC'],
    _analytics.map(d => [d.item_id, d.avg_daily_usage, d.forecast_demand, d.eoq,
      d.days_remaining >= 999 ? '∞' : d.days_remaining, d.risk_score,
      (abcData[d.item_id] || {}).class || '—'])
  );
}

function exportTransactionsCSV() {
  if (!_transactions.length) return showToast('No data to export', 'warning');
  _downloadCSV(
    [['Timestamp','Item','Action','Qty Change','Previous Qty','New Qty','Tag UID','Performed By'],
     ..._transactions.map(t => [t.timestamp, t.item_name || t.item_id, t.action,
       t.quantity_change, t.previous_quantity, t.new_quantity,
       t.tag_uid || '', t.performed_by || 'system'])],
    'transactions.csv'
  );
  showToast('Transactions exported as CSV', 'success');
}

// ── Audit Trail ───────────────────────────────────────────────────────────────
async function fetchAudit() {
  const tbody = document.getElementById('audit-tbody');
  if (tbody) tbody.innerHTML = _skeletonRows(8);
  try {
    const rows = await fetch(`/api/audit?limit=200&filter=${_auditFilter}`).then(r => r.json());
    _audit = rows;
    renderAuditTable(rows);
  } catch {
    if (tbody) tbody.innerHTML = _tableError(8);
  }
}

function renderAuditTable(rows) {
  const tbody = document.getElementById('audit-tbody');
  if (!tbody) return;
  if (!rows.length) {
    const filterMsgs = {
      all:       ['No audit entries yet', 'All system actions will be logged here as they happen — scans, admin changes, and more.'],
      dashboard: ['No dashboard actions yet', 'Actions performed via this dashboard will appear here.'],
      physical:  ['No physical scans yet', 'RFID scans from ESP32 stations will appear here.'],
      admin:     ['No admin actions yet', 'Item and tag management actions performed by admins will appear here.'],
    };
    const [title, msg] = filterMsgs[_auditFilter] || filterMsgs.all;
    tbody.innerHTML = _tableEmpty(8, 'clipboard', title, msg);
    return;
  }
  tbody.innerHTML = rows.map(t => {
    const qtyChange = t.quantity_change > 0
      ? `<span class="text-green-600 font-mono font-semibold">+${t.quantity_change}</span>`
      : t.quantity_change < 0
      ? `<span class="text-red-600 font-mono font-semibold">${t.quantity_change}</span>`
      : `<span class="text-gray-400 font-mono">0</span>`;
    const deviceLabel = t.device_id === 'dashboard'
      ? '<span class="badge badge-info">dashboard</span>'
      : t.device_id === 'unknown' || !t.device_id
      ? '<span class="text-gray-300 text-xs">—</span>'
      : `<span class="badge badge-purple">${esc(t.device_id)}</span>`;
    return `
      <tr>
        <td class="px-4 py-2.5 text-xs text-gray-400 whitespace-nowrap">${fmtDate(t.timestamp)}</td>
        <td class="px-4 py-2.5">${actionBadge(t.action)}</td>
        <td class="px-4 py-2.5 text-sm text-gray-700">
          ${t.item_name ? `<div>${esc(t.item_name)}</div><div class="text-xs text-gray-400">${esc(t.item_id)}</div>`
            : `<span class="text-gray-400 text-xs">${esc(t.item_id)}</span>`}
        </td>
        <td class="px-4 py-2.5 text-center">${qtyChange}</td>
        <td class="px-4 py-2.5 text-sm text-gray-600">${esc(t.performed_by) || '<span class="text-gray-300">system</span>'}</td>
        <td class="px-4 py-2.5 text-center">${deviceLabel}</td>
        <td class="px-4 py-2.5 font-mono text-xs text-gray-500">${t.tag_uid ? esc(t.tag_uid) : '<span class="text-gray-300">—</span>'}</td>
        <td class="px-4 py-2.5 text-xs text-gray-500 max-w-xs truncate">${esc(t.note) || ''}</td>
      </tr>`;
  }).join('');
}

function setAuditFilter(type) {
  _auditFilter = type;
  document.querySelectorAll('#audit-filters button').forEach(btn => {
    const active = btn.dataset.filter === type;
    btn.className = `btn-sm ${active ? 'btn-sm-edit' : 'btn-sm-neutral'}`;
  });
  fetchAudit();
}

function exportAuditCSV() {
  if (!_audit.length) return showToast('No audit data to export', 'warning');
  _downloadCSV(
    [['Timestamp','Action','Item','Qty Change','Performed By','Device/Station','Tag UID','Note'],
     ..._audit.map(t => [t.timestamp, t.action, t.item_name || t.item_id,
       t.quantity_change, t.performed_by || 'system', t.device_id || '',
       t.tag_uid || '', t.note || ''])],
    'audit-trail.csv'
  );
  showToast('Audit trail exported as CSV', 'success');
}

// ── Dashboard Accounts ────────────────────────────────────────────────────────
async function fetchUsers() {
  if (currentRole !== 'admin') return;
  const tbody = document.getElementById('dashboard-accounts-tbody');
  if (tbody && !_users.length) tbody.innerHTML = _skeletonRows(6);
  try {
    const users = await fetch('/api/users').then(r => r.json());
    _users = users;
    renderDashboardAccounts(users);
  } catch {
    if (tbody) tbody.innerHTML = _tableError(6);
  }
}

function renderDashboardAccounts(users) {
  const tbody = document.getElementById('dashboard-accounts-tbody');
  if (!tbody) return;
  if (!users.length) {
    tbody.innerHTML = _tableEmpty(6, 'user', 'No dashboard accounts',
      'Create accounts for staff who need dashboard access. You can link physical RFID badges to accounts for full audit traceability.',
      `<button onclick="openAddUserModal()" class="btn-primary text-sm">+ Create first account</button>`);
    return;
  }
  const roleBadgeMap = { admin:'badge-danger', manager:'badge-warning', viewer:'badge-neutral' };
  tbody.innerHTML = users.map(u => `
    <tr data-user-id="${u.id}">
      <td class="px-4 py-3 font-medium text-gray-800">${esc(u.username)}</td>
      <td class="px-4 py-3 text-center">
        <span class="badge ${roleBadgeMap[u.role] || 'badge-neutral'}">${esc(u.role)}</span>
      </td>
      <td class="px-4 py-3 font-mono text-xs text-gray-500">
        ${u.badge_uid ? esc(u.badge_uid) : '<span class="text-gray-300 italic">not linked</span>'}
      </td>
      <td class="px-4 py-3 text-xs text-gray-500">${u.employee_id ? esc(u.employee_id) : '<span class="text-gray-300">—</span>'}</td>
      <td class="px-4 py-3 text-center text-xs text-gray-400">${fmtDate(u.created_at)}</td>
      <td class="px-4 py-3 text-center">
        <div class="action-cell">
          <button onclick='openEditUserModal(${JSON.stringify(u)})' class="btn-sm btn-sm-edit">Edit</button>
          <button onclick="deleteUser(${u.id})" class="btn-sm btn-sm-danger">Delete</button>
        </div>
      </td>
    </tr>`).join('');
}

function openAddUserModal() {
  document.getElementById('form-add-user').reset();
  openModal('modal-add-user');
}

function openEditUserModal(u) {
  document.getElementById('edit-user-id').value    = u.id;
  document.getElementById('edit-u-role').value     = u.role;
  document.getElementById('edit-u-badge').value    = u.badge_uid || '';
  document.getElementById('edit-u-employee-id').value = u.employee_id || '';
  openModal('modal-edit-user');
}

async function deleteUser(id) {
  const ok = await customConfirm('Delete Account', 'Permanently delete this dashboard account?', true);
  if (!ok) return;
  const r = await fetch(`/api/users/${id}`, { method:'DELETE' });
  if (r.ok) { showToast('Account deleted', 'info'); fetchUsers(); }
  else { const d = await r.json(); showToast('Error: ' + (d.error || 'Failed'), 'error'); }
}

document.addEventListener('DOMContentLoaded', () => {
  const formAdd = document.getElementById('form-add-user');
  if (formAdd) {
    formAdd.addEventListener('submit', async e => {
      e.preventDefault();
      const btn  = e.target.querySelector('button[type=submit]');
      const body = {
        username: document.getElementById('u-username').value.trim(),
        password: document.getElementById('u-password').value,
        role:     document.getElementById('u-role').value,
      };
      if (!body.username || !body.password) return showToast('Username and password are required', 'warning');
      _btnLoad(btn, true, 'Creating…');
      try {
        const r = await fetch('/api/users', {
          method:'POST', headers:{'Content-Type':'application/json'},
          body: JSON.stringify(body),
        });
        if (r.ok) {
          closeModal('modal-add-user');
          formAdd.reset();
          fetchUsers();
          showToast(`Account "${esc(body.username)}" created`, 'success');
        } else {
          const d = await r.json().catch(() => ({}));
          showToast(d.error || 'Failed to create account', 'error');
        }
      } finally { _btnLoad(btn, false, 'Create Account'); }
    });
  }

  const formEdit = document.getElementById('form-edit-user');
  if (formEdit) {
    formEdit.addEventListener('submit', async e => {
      e.preventDefault();
      const btn  = e.target.querySelector('button[type=submit]');
      const id   = document.getElementById('edit-user-id').value;
      const body = {
        role:        document.getElementById('edit-u-role').value,
        badge_uid:   document.getElementById('edit-u-badge').value.trim() || null,
        employee_id: document.getElementById('edit-u-employee-id').value.trim().toUpperCase() || null,
      };
      _btnLoad(btn, true, 'Saving…');
      try {
        const r = await fetch(`/api/users/${id}`, {
          method:'PUT', headers:{'Content-Type':'application/json'},
          body: JSON.stringify(body),
        });
        if (r.ok) {
          closeModal('modal-edit-user');
          fetchUsers();
          showToast('Account updated', 'success');
        } else {
          const d = await r.json().catch(() => ({}));
          showToast(d.error || 'Failed to update account', 'error');
        }
      } finally { _btnLoad(btn, false, 'Save Changes'); }
    });
  }
});

// ── CSV Import ────────────────────────────────────────────────────────────────
function openImportCSVModal() {
  document.getElementById('form-import-csv').reset();
  const res = document.getElementById('import-csv-result');
  if (res) res.innerHTML = '';
  openModal('modal-import-csv');
}

document.addEventListener('DOMContentLoaded', () => {
  const form = document.getElementById('form-import-csv');
  if (!form) return;
  form.addEventListener('submit', async e => {
    e.preventDefault();
    const btn  = form.querySelector('button[type=submit]');
    const file = form.querySelector('input[type=file]').files[0];
    if (!file) return showToast('Select a CSV file first', 'warning');
    const fd = new FormData();
    fd.append('file', file);
    _btnLoad(btn, true, 'Importing…');
    try {
      const r = await fetch('/api/import/items', { method:'POST', body: fd });
      const d = await r.json().catch(() => ({}));
      const res = document.getElementById('import-csv-result');
      if (r.ok) {
        if (res) res.innerHTML = `<div class="text-sm text-green-700 bg-green-50 border border-green-200 rounded p-2 mt-2">
          Created: ${d.created} &nbsp;|&nbsp; Updated: ${d.updated} &nbsp;|&nbsp; Errors: ${d.errors}
        </div>`;
        fetchItems(); refreshSummary();
        showToast(`Import complete: ${d.created} created, ${d.updated} updated`, 'success');
      } else {
        if (res) res.innerHTML = `<div class="text-sm text-red-600 mt-2">${esc(d.error || 'Import failed')}</div>`;
        showToast(d.error || 'Import failed', 'error');
      }
    } finally { _btnLoad(btn, false, 'Import'); }
  });
});

// ── Tag Reassignment ──────────────────────────────────────────────────────────
function openReassignTagModal(uid) {
  const display = document.getElementById('reassign-old-uid');
  const input   = document.getElementById('reassign-old-uid-input');
  const newUid  = document.getElementById('reassign-new-uid');
  if (display) display.textContent = uid;
  if (input)   input.value = uid;
  if (newUid)  newUid.value = '';
  openModal('modal-reassign-tag');
}

document.addEventListener('DOMContentLoaded', () => {
  const form = document.getElementById('form-reassign-tag');
  if (!form) return;
  form.addEventListener('submit', async e => {
    e.preventDefault();
    const btn     = form.querySelector('button[type=submit]');
    const old_uid = document.getElementById('reassign-old-uid-input').value;
    const new_uid = document.getElementById('reassign-new-uid').value.trim();
    if (!new_uid) return showToast('Enter the new tag UID', 'warning');
    _btnLoad(btn, true, 'Reassigning…');
    try {
      const r = await fetch(`/api/tags/${encodeURIComponent(old_uid)}/reassign`, {
        method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({ new_uid }),
      });
      const d = await r.json().catch(() => ({}));
      if (r.ok) {
        closeModal('modal-reassign-tag');
        fetchTags();
        showToast(`Tag reassigned to ${new_uid}`, 'success');
      } else {
        showToast(d.error || 'Reassignment failed', 'error');
      }
    } finally { _btnLoad(btn, false, 'Reassign Tag'); }
  });
});

// ── Change Password ───────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  const form = document.getElementById('form-change-password');
  if (!form) return;
  form.addEventListener('submit', async e => {
    e.preventDefault();
    const btn     = form.querySelector('button[type=submit]');
    const current = document.getElementById('cp-current').value;
    const newPw   = document.getElementById('cp-new').value;
    const confirm = document.getElementById('cp-confirm').value;
    if (newPw !== confirm) return showToast('New passwords do not match', 'warning');
    if (newPw.length < 6)  return showToast('Password must be at least 6 characters', 'warning');
    _btnLoad(btn, true, 'Saving…');
    try {
      const meR = await fetch('/api/me').then(r => r.json()).catch(() => null);
      if (!meR) return showToast('Session error — please log in again', 'error');
      const r = await fetch(`/api/users/${meR.id}/password`, {
        method:'PUT', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({ current_password: current, new_password: newPw }),
      });
      const d = await r.json().catch(() => ({}));
      if (r.ok) {
        form.reset();
        showToast('Password changed successfully', 'success');
      } else {
        showToast(d.error || 'Failed to change password', 'error');
      }
    } finally { _btnLoad(btn, false, 'Change Password'); }
  });
});

// ── Purchase Orders ───────────────────────────────────────────────────────────
let _pos = [];
let _poFilter = '';

async function fetchPurchaseOrders(statusFilter) {
  _poFilter = statusFilter;
  const tbody = document.getElementById('po-tbody');
  if (tbody) tbody.innerHTML = _skeletonRows(7, 3);
  try {
    const url = '/api/purchase-orders' + (statusFilter ? `?status=${encodeURIComponent(statusFilter)}` : '');
    const pos  = await fetch(url).then(r => r.json());
    _pos = pos;
    renderPOTable(pos);
    document.querySelectorAll('#po-filters button').forEach(btn => {
      btn.className = `btn-sm ${btn.dataset.filter === statusFilter ? 'btn-sm-edit' : 'btn-sm-neutral'}`;
    });
  } catch {
    if (tbody) tbody.innerHTML = _tableError(7);
  }
}

function renderPOTable(pos) {
  const tbody = document.getElementById('po-tbody');
  if (!tbody) return;
  if (!pos.length) {
    tbody.innerHTML = _tableEmpty(7, 'clipboard', 'No purchase orders',
      'Create a PO to track expected deliveries and auto-match against warehouse receives.');
    return;
  }
  const statusBadgePO = s => {
    const map = { open:'badge-info', partial:'badge-warning', fulfilled:'badge-success', cancelled:'badge-neutral' };
    return `<span class="badge ${map[s] || 'badge-neutral'}">${esc(s)}</span>`;
  };
  const canEdit = currentRole === 'admin' || currentRole === 'manager';
  tbody.innerHTML = pos.map(p => {
    const progress = p.expected_qty > 0 ? Math.min(100, Math.round(p.received_qty / p.expected_qty * 100)) : 0;
    return `
      <tr>
        <td class="px-4 py-3 text-xs text-gray-400 whitespace-nowrap">${fmtDate(p.created_at)}</td>
        <td class="px-4 py-3">
          <div class="font-medium text-gray-800">${esc(p.item_name || p.item_id)}</div>
          <div class="text-xs text-gray-400">${esc(p.item_id)}</div>
        </td>
        <td class="px-4 py-3 text-right font-mono text-sm">${p.expected_qty}</td>
        <td class="px-4 py-3 text-right font-mono text-sm text-green-600">${p.received_qty}</td>
        <td class="px-4 py-3">
          <div class="w-full bg-gray-100 rounded-full h-1.5">
            <div class="h-1.5 rounded-full ${progress >= 100 ? 'bg-green-500' : 'bg-blue-500'}" style="width:${progress}%"></div>
          </div>
          <div class="text-xs text-gray-400 text-right mt-0.5">${progress}%</div>
        </td>
        <td class="px-4 py-3 text-center">${statusBadgePO(p.status)}</td>
        <td class="px-4 py-3 text-center">
          ${canEdit
            ? `<div class="action-cell"><button onclick="deletePO(${p.id})" class="btn-sm btn-sm-danger">Delete</button></div>`
            : '<span class="text-gray-300 text-xs">—</span>'}
        </td>
      </tr>`;
  }).join('');
}

function openAddPOModal() {
  const sel = document.getElementById('po-item-id');
  if (sel) {
    sel.innerHTML = '<option value="">Select item…</option>' +
      _items.map(i => `<option value="${esc(i.id)}">${esc(i.name)}</option>`).join('');
  }
  const form = document.getElementById('form-add-po');
  if (form) form.reset();
  openModal('modal-add-po');
}

async function deletePO(id) {
  const ok = await customConfirm('Delete Purchase Order', 'Delete this purchase order?', true);
  if (!ok) return;
  const r = await fetch(`/api/purchase-orders/${id}`, { method:'DELETE' });
  if (r.ok) { showToast('Purchase order deleted', 'info'); fetchPurchaseOrders(_poFilter); }
  else showToast('Failed to delete PO', 'error');
}

document.addEventListener('DOMContentLoaded', () => {
  const form = document.getElementById('form-add-po');
  if (!form) return;
  form.addEventListener('submit', async e => {
    e.preventDefault();
    const btn = form.querySelector('button[type=submit]');
    const body = {
      item_id:      document.getElementById('po-item-id').value,
      expected_qty: parseInt(document.getElementById('po-expected-qty').value),
      note:         (document.getElementById('po-note')?.value || '').trim() || null,
    };
    if (!body.item_id)         return showToast('Select an item', 'warning');
    if (body.expected_qty < 1) return showToast('Expected quantity must be at least 1', 'warning');
    _btnLoad(btn, true, 'Creating…');
    try {
      const r = await fetch('/api/purchase-orders', {
        method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify(body),
      });
      if (r.ok) {
        closeModal('modal-add-po');
        fetchPurchaseOrders('');
        showToast('Purchase order created', 'success');
      } else {
        const d = await r.json().catch(() => ({}));
        showToast(d.error || 'Failed to create PO', 'error');
      }
    } finally { _btnLoad(btn, false, 'Create PO'); }
  });
});

// ── Webhooks ──────────────────────────────────────────────────────────────────
let _webhooks = [];

async function fetchWebhooks() {
  if (currentRole !== 'admin') return;
  const tbody = document.getElementById('webhooks-tbody');
  if (tbody) tbody.innerHTML = _skeletonRows(5, 2);
  try {
    const whs = await fetch('/api/webhooks').then(r => r.json());
    _webhooks = whs;
    renderWebhooksTable(whs);
  } catch {
    if (tbody) tbody.innerHTML = _tableError(5);
  }
}

function renderWebhooksTable(whs) {
  const tbody = document.getElementById('webhooks-tbody');
  if (!tbody) return;
  if (!whs.length) {
    tbody.innerHTML = _tableEmpty(5, 'wifi', 'No webhooks configured',
      'Add a webhook URL to receive real-time POST notifications for low stock and security events.');
    return;
  }
  tbody.innerHTML = whs.map(w => `
    <tr>
      <td class="px-4 py-3 font-medium text-gray-800">${esc(w.name)}</td>
      <td class="px-4 py-3 text-xs font-mono text-gray-500 max-w-xs truncate" title="${esc(w.url)}">${esc(w.url)}</td>
      <td class="px-4 py-3 text-xs text-gray-500">${esc(w.events)}</td>
      <td class="px-4 py-3 text-center">
        ${w.active ? '<span class="badge badge-success">Active</span>' : '<span class="badge badge-neutral">Inactive</span>'}
      </td>
      <td class="px-4 py-3 text-center">
        <div class="action-cell">
          <button onclick='openEditWebhookModal(${JSON.stringify(w)})' class="btn-sm btn-sm-edit">Edit</button>
          <button onclick="testWebhook(${w.id})" class="btn-sm btn-sm-neutral">Test</button>
          <button onclick="deleteWebhook(${w.id})" class="btn-sm btn-sm-danger">Delete</button>
        </div>
      </td>
    </tr>`).join('');
}

function openAddWebhookModal() {
  document.getElementById('webhook-modal-title').textContent = 'Add Webhook';
  document.getElementById('webhook-id').value = '';
  document.getElementById('form-webhook').reset();
  openModal('modal-webhook');
}

function openEditWebhookModal(wh) {
  document.getElementById('webhook-modal-title').textContent = 'Edit Webhook';
  document.getElementById('webhook-id').value      = wh.id;
  document.getElementById('webhook-name').value    = wh.name;
  document.getElementById('webhook-url').value     = wh.url;
  document.getElementById('webhook-events').value  = wh.events;
  const activeEl = document.getElementById('webhook-active');
  if (activeEl) activeEl.checked = !!wh.active;
  openModal('modal-webhook');
}

async function deleteWebhook(id) {
  const ok = await customConfirm('Delete Webhook', 'Remove this webhook endpoint?', true);
  if (!ok) return;
  const r = await fetch(`/api/webhooks/${id}`, { method:'DELETE' });
  if (r.ok) { showToast('Webhook deleted', 'info'); fetchWebhooks(); }
  else showToast('Failed to delete webhook', 'error');
}

async function testWebhook(id) {
  const r = await fetch(`/api/webhooks/${id}/test`, { method:'POST' });
  if (r.ok) showToast('Test payload sent', 'success');
  else showToast('Test failed — check the endpoint URL', 'error');
}

document.addEventListener('DOMContentLoaded', () => {
  const form = document.getElementById('form-webhook');
  if (!form) return;
  form.addEventListener('submit', async e => {
    e.preventDefault();
    const btn    = form.querySelector('button[type=submit]');
    const id     = document.getElementById('webhook-id').value;
    const active = document.getElementById('webhook-active');
    const body   = {
      name:   document.getElementById('webhook-name').value.trim(),
      url:    document.getElementById('webhook-url').value.trim(),
      events: document.getElementById('webhook-events').value.trim() || 'low_stock,security',
      active: (active ? active.checked : true) ? 1 : 0,
    };
    if (!body.name) return showToast('Webhook name is required', 'warning');
    if (!body.url)  return showToast('Webhook URL is required', 'warning');
    _btnLoad(btn, true, id ? 'Saving…' : 'Adding…');
    try {
      const r = await fetch(id ? `/api/webhooks/${id}` : '/api/webhooks', {
        method: id ? 'PUT' : 'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify(body),
      });
      if (r.ok) {
        closeModal('modal-webhook');
        fetchWebhooks();
        showToast(id ? 'Webhook updated' : 'Webhook added', 'success');
      } else {
        const d = await r.json().catch(() => ({}));
        showToast(d.error || 'Failed to save webhook', 'error');
      }
    } finally { _btnLoad(btn, false, id ? 'Save Changes' : 'Add Webhook'); }
  });
});

// ── Init overview charts ──────────────────────────────────────────────────────
fetchTransactionTrends();
setInterval(fetchTransactionTrends, 60000);
