'use strict';

const tg = window.Telegram?.WebApp;
const $ = id => document.getElementById(id);

const STAGES = {
  available: { label: 'Свободно', full: 'Свободные', emoji: '⚪', color: '#94a3b8', bg: 'rgba(148,163,184,0.12)' },
  in_progress: { label: 'В работе', full: 'В работе', emoji: '🟡', color: '#eab308', bg: 'rgba(234,179,8,0.12)' },
  internal_review: { label: 'Проверка', full: 'На проверке лида', emoji: '🔵', color: '#38bdf8', bg: 'rgba(56,189,248,0.12)' },
  client_review: { label: 'У клиента', full: 'У заказчика', emoji: '🟣', color: '#c084fc', bg: 'rgba(192,132,252,0.12)' },
  export_ready: { label: 'Экспорт', full: 'На экспорт', emoji: '🟠', color: '#fb923c', bg: 'rgba(251,146,60,0.12)' },
  completed: { label: 'Готово', full: 'Готово', emoji: '✅', color: '#4ade80', bg: 'rgba(74,222,128,0.12)' }
};

const ACTIONS = {
  internal: { title: 'Отправить на проверку', submit: 'Отправить', endpoint: 'submit-internal', message: 'Макет отправлен на проверку' },
  client: { title: 'Передать заказчику', submit: 'Передать заказчику', endpoint: 'submit-client', message: 'Макет передан заказчику' },
  export: { title: 'Макет согласован?', submit: 'Отправить на экспорт', endpoint: 'submit-export', message: 'Задача готова к экспорту' },
  complete: { title: 'Завершить задачу', submit: 'Завершить', endpoint: 'complete', message: 'Задача завершена' },
  reject: { title: 'Вернуть на правки', submit: 'Вернуть дизайнеру', endpoint: 'reject-feedback', message: 'Задача возвращена на правки' },
  return: { title: 'Вернуть в свободные?', submit: 'Вернуть в свободные', endpoint: 'reject-feedback', message: 'Задача снова свободна' },
  images: { title: 'Количество изображений', submit: 'Сохранить', endpoint: 'images-count', message: 'Количество изображений сохранено' },
  assign: { title: 'Назначить дизайнеру', submit: 'Назначить', endpoint: 'assign', message: 'Задача успешно назначена дизайнеру' },
};

const state = {
  currentUser: null,
  isAdmin: false,
  devMode: false,
  devUserId: null,
  tasks: [],
  users: [],
  currentView: 'kanban', // 'kanban' | 'list' | 'analytics'
  currentNav: 'all',     // 'all' | 'my' | 'revisions' | 'available' | 'archived'
  stageFilter: 'all',
  poolFilter: 'all',
  searchQuery: '',
  selectedTaskId: null,
  loading: false,
  formBusy: false,
  pendingTaskIds: new Set(),
  mutationVersion: 0,
  action: null,
  collapsedPools: new Set()
};

let currentEditingPoolName = null;
let exportSelectedPools = new Set();
let exportAvailablePools = [];
let exportFormat = 'excel';
let googleScriptTemplate = '';

// --- AUTH & API ---
function getAuthHeaders() {
  const headers = { 'Content-Type': 'application/json' };
  if (tg?.initData) headers['X-Telegram-Init-Data'] = tg.initData;
  if (state.devUserId) headers['X-Dev-User-Id'] = String(state.devUserId);
  return headers;
}

async function api(path, body, method) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 20000);
  const httpMethod = method || (body === undefined ? 'GET' : 'POST');
  try {
    const res = await fetch(path, {
      method: httpMethod,
      headers: getAuthHeaders(),
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      signal: controller.signal
    });
    const data = await res.json();
    if (!res.ok) {
      const msg = typeof data.detail === 'string' ? data.detail : 'Ошибка сервера';
      throw new Error(msg);
    }
    return data;
  } finally {
    clearTimeout(timer);
  }
}

function escapeHtml(val) {
  return String(val ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function safeUrl(url) {
  try {
    const u = new URL(url);
    return ['http:', 'https:'].includes(u.protocol) ? u.href : '';
  } catch {
    return '';
  }
}

function isMine(task) {
  return state.currentUser != null && task.assigned_to_id === state.currentUser.telegram_id;
}

function assigneeName(task) {
  return (task.assigned_to_name || task.assigned_to_username || 'Исполнитель').replace(/\s*\(@[^)]*\)/g, '').trim();
}

function taskNotes(task) {
  return task.notes && !task.notes.startsWith('Исходники из папки') ? task.notes : '';
}

function isRevision(task) {
  return task.status === 'in_progress' && /^Правки:|^Требуются правки/i.test(task.notes || '');
}

function cleanPoolTitle(poolName) {
  if (!poolName) return 'Общий пул';
  return String(poolName).trim().replace(/^(?:задание|задача)\s+/i, '') || poolName;
}

function showToast(msg, type = 'info') {
  const container = $('toast-container');
  if (!container) return;
  const t = document.createElement('div');
  t.className = 'toast';
  if (type === 'error') {
    t.style.borderColor = 'rgba(239, 68, 68, 0.5)';
    t.style.color = '#fca5a5';
  }
  t.textContent = msg;
  container.appendChild(t);
  setTimeout(() => t.remove(), 3500);
}

function showFormError(elId, msg) {
  const el = $(elId);
  if (!el) return;
  el.textContent = msg;
  el.classList.remove('hidden');
}

// --- TELEGRAM & FULLSCREEN ---
function telegramSupports(ver) {
  return !!tg && (!tg.isVersionAtLeast || tg.isVersionAtLeast(ver));
}

function setupTelegram() {
  if (!tg) return;
  try {
    tg.ready?.();
    tg.expand?.();
    if (telegramSupports('6.1')) {
      tg.setHeaderColor?.('#101112');
      tg.setBackgroundColor?.('#101112');
    }
    updateFullscreenUI();
  } catch(e) {}
}

function updateFullscreenUI() {
  const btn = $('btn-fullscreen');
  if (!btn) return;
  const supported = telegramSupports('8.0') && typeof tg?.requestFullscreen === 'function';
  if (!supported) {
    btn.classList.add('hidden');
    return;
  }
  btn.classList.remove('hidden');
  const isFs = !!tg.isFullscreen;
  btn.setAttribute('title', isFs ? 'Свернуть' : 'Во весь экран');
  const use = btn.querySelector('use');
  if (use) use.setAttribute('href', isFs ? '#i-compress' : '#i-expand');
}

function toggleFullscreen() {
  if (!tg) return;
  try {
    if (tg.isFullscreen) tg.exitFullscreen?.();
    else tg.requestFullscreen?.();
  } catch (err) {}
}

// --- INITIALIZATION ---
async function init() {
  setupTelegram();

  // Keyboard shortcut Ctrl+K
  document.addEventListener('keydown', e => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
      e.preventDefault();
      $('global-search')?.focus();
    }
    if (e.key === 'Escape') {
      closeDrawer();
      document.querySelectorAll('.modal-overlay:not(.hidden)').forEach(m => m.classList.add('hidden'));
    }
  });

  // SKU Counter & Check in Add Batch modal
  let skuTimer;
  $('batch-skus')?.addEventListener('input', () => {
    updateSkuCounter();
    clearTimeout(skuTimer);
    const text = $('batch-skus').value.trim();
    if (!text) {
      renderBatchAnalysis(null);
      return;
    }
    skuTimer = setTimeout(async () => {
      try {
        const parentUrl = $('batch-parent-url').value.trim();
        const res = await api('/api/tasks/check-skus', { skus_text: text, parent_url: safeUrl(parentUrl) ? parentUrl : null });
        if (res.analysis) renderBatchAnalysis(res.analysis);
      } catch (e) {}
    }, 600);
  });

  await loadMe();
  await loadTasks();
}

async function loadMe() {
  try {
    const data = await api('/api/me');
    state.currentUser = data.user;
    state.isAdmin = data.is_admin;
    state.devMode = data.dev_mode;

    const initials = (data.user?.first_name || 'U')[0].toUpperCase();
    $('user-avatar').textContent = initials;
    $('user-name').textContent = data.user?.first_name || data.user?.username || 'Пользователь';
    $('user-role-badge').textContent = state.isAdmin ? '👑 Тимлид / Админ' : '🎨 Дизайнер';

    $('btn-top-add-pool').classList.toggle('hidden', !state.isAdmin);
    $('btn-sidebar-add-pool').classList.toggle('hidden', !state.isAdmin);
    $('nav-export-item').classList.toggle('hidden', !state.isAdmin);
    $('nav-team-item').classList.toggle('hidden', !state.isAdmin);
    $('nav-backup-item').classList.toggle('hidden', !state.isAdmin);
    $('dev-bar').classList.toggle('hidden', !state.devMode || !!tg?.initData);

    if (state.isAdmin) {
      loadDesigners().catch(() => {});
    }
  } catch (err) {
    console.error('loadMe error:', err);
    showToast('Ошибка авторизации: ' + err.message, 'error');
  }
}

async function loadDesigners() {
  if (!state.isAdmin) return [];
  try {
    const data = await api('/api/users');
    state.users = data.users || [];
    populateAssigneeSelects();
    return state.users;
  } catch (e) {
    return [];
  }
}

function populateAssigneeSelects() {
  const selects = [$('batch-assignee'), $('edit-pool-assignee')];
  const optionsHtml = '<option value="">⚪ В общий пул (свободно)</option>' +
    state.users.map(u => `<option value="${u.telegram_id}">🎨 ${escapeHtml(u.first_name || u.username)}</option>`).join('');
  selects.forEach(sel => { if (sel) sel.innerHTML = optionsHtml; });
}

function getActiveWorkloadMap() {
  const map = {};
  for (const t of (state.tasks || [])) {
    if (!t.is_archived && t.assigned_to_id && ['in_progress', 'internal_review', 'client_review', 'export_ready'].includes(t.status)) {
      map[t.assigned_to_id] = (map[t.assigned_to_id] || 0) + 1;
    }
  }
  return map;
}

async function switchDevUser(role) {
  state.devUserId = { admin: 999999999, designer1: 10001, designer2: 10002 }[role];
  state.selectedTaskId = null;
  closeDrawer();
  await loadMe();
  await loadTasks(true);
}

async function loadTasks(notify = false) {
  if (state.loading) return;
  state.loading = true;
  try {
    const data = await api('/api/tasks');
    state.tasks = data.tasks || [];
    render();
    if (notify) showToast('Данные обновлены');
  } catch (err) {
    showToast('Ошибка загрузки задач: ' + err.message, 'error');
  } finally {
    state.loading = false;
  }
}

// --- FILTERING & VIEWS ---
function getFilteredTasks() {
  return state.tasks.filter(task => {
    // 1. Navigation Focus
    if (state.currentNav === 'archived') {
      if (!task.is_archived) return false;
    } else {
      if (task.is_archived) return false;
      if (state.currentNav === 'my') {
        if (!isMine(task) || task.status === 'completed') return false;
      } else if (state.currentNav === 'revisions') {
        if (!isRevision(task)) return false;
      } else if (state.currentNav === 'available') {
        if (task.status !== 'available') return false;
      }
    }

    // 2. Pool filter
    if (state.poolFilter !== 'all') {
      const pName = task.pool_name || 'Общий пул';
      if (pName !== state.poolFilter) return false;
    }

    // 3. Stage filter
    if (state.stageFilter !== 'all' && task.status !== state.stageFilter) {
      return false;
    }

    // 4. Search query
    if (state.searchQuery) {
      const q = state.searchQuery.toLowerCase();
      const matchSku = (task.sku || '').toLowerCase().includes(q);
      const matchPool = (task.pool_name || '').toLowerCase().includes(q);
      const matchUser = (task.assigned_to_name || task.assigned_to_username || '').toLowerCase().includes(q);
      if (!matchSku && !matchPool && !matchUser) return false;
    }

    return true;
  });
}

function setScopeTab(nav) {
  state.currentNav = nav;
  ['all', 'my', 'revisions', 'available', 'archived'].forEach(n => {
    $(`nav-${n}`)?.classList.toggle('active', n === nav);
  });
  render();
}

function filterByPool(poolName) {
  state.poolFilter = poolName;
  document.querySelectorAll('.pool-nav-card').forEach(el => {
    el.classList.toggle('active', el.dataset.pool === poolName);
  });
  render();
}

function filterByStage(stage) {
  state.stageFilter = (state.stageFilter === stage) ? 'all' : stage;
  document.querySelectorAll('.kpi-tile').forEach(t => t.classList.remove('active'));
  $(`kpi-tile-${state.stageFilter}`)?.classList.add('active');
  render();
}

function handleSearch(val) {
  state.searchQuery = val.trim();
  $('search-clear-btn')?.classList.toggle('hidden', !state.searchQuery);
  render();
}

function clearSearch() {
  const inp = $('global-search');
  if (inp) inp.value = '';
  handleSearch('');
}

function switchView(viewName) {
  state.currentView = viewName;
  ['kanban', 'list', 'analytics'].forEach(v => {
    $(`view-${v}`)?.classList.toggle('hidden', v !== viewName);
    $(`view-btn-${v}`)?.classList.toggle('active', v === viewName);
  });
  if (viewName === 'list') renderListView();
  if (viewName === 'analytics') renderAnalytics();
}

// --- RENDER MAIN ---
function render() {
  updateKPIs();
  updateSidebar();
  renderKanban();
  if (state.currentView === 'list') renderListView();
  if (state.currentView === 'analytics') renderAnalytics();
  if (state.selectedTaskId) renderDrawer();
}

function updateKPIs() {
  const activeTasks = state.tasks.filter(t => !t.is_archived);
  const counts = { available: 0, in_progress: 0, internal_review: 0, client_review: 0, export_ready: 0, completed: 0 };
  for (const t of activeTasks) {
    if (counts[t.status] !== undefined) counts[t.status]++;
  }
  $('kpi-total').textContent = activeTasks.length;
  $('kpi-available').textContent = counts.available;
  $('kpi-in-progress').textContent = counts.in_progress;
  $('kpi-internal').textContent = counts.internal_review;
  $('kpi-client').textContent = counts.client_review;
  $('kpi-completed').textContent = counts.completed;
}

function updateSidebar() {
  const activeTasks = state.tasks.filter(t => !t.is_archived);
  const myCount = activeTasks.filter(t => isMine(t) && t.status !== 'completed').length;
  const revisionsCount = activeTasks.filter(t => isRevision(t)).length;
  const availCount = activeTasks.filter(t => t.status === 'available').length;
  const archivedCount = state.tasks.filter(t => t.is_archived).length;

  $('badge-nav-all').textContent = activeTasks.length;
  $('badge-nav-my').textContent = myCount;
  $('badge-nav-revisions').textContent = revisionsCount;
  $('badge-nav-available').textContent = availCount;
  $('badge-nav-archived').textContent = archivedCount;

  // Pools list
  const poolsMap = new Map();
  for (const t of activeTasks) {
    const p = t.pool_name || 'Общий пул';
    if (!poolsMap.has(p)) poolsMap.set(p, { total: 0, done: 0 });
    const stat = poolsMap.get(p);
    stat.total++;
    if (t.status === 'completed') stat.done++;
  }

  const container = $('sidebar-pools-list');
  if (!container) return;

  let html = `<div class="pool-nav-card ${state.poolFilter === 'all' ? 'active' : ''}" data-pool="all" onclick="filterByPool('all')">
    <div class="pool-nav-title">
      <span class="pool-nav-name">📁 Все пулы</span>
      <span class="nav-item-count">${activeTasks.length}</span>
    </div>
  </div>`;

  for (const [poolName, stat] of poolsMap.entries()) {
    const percent = stat.total ? Math.round((stat.done / stat.total) * 100) : 0;
    const isActive = state.poolFilter === poolName;
    html += `
      <div class="pool-nav-card ${isActive ? 'active' : ''}" data-pool="${escapeHtml(poolName)}" onclick="filterByPool('${escapeHtml(poolName)}')">
        <div class="pool-nav-title">
          <span class="pool-nav-name" title="${escapeHtml(poolName)}">📁 ${escapeHtml(cleanPoolTitle(poolName))}</span>
          <div class="pool-nav-actions" onclick="event.stopPropagation()">
            <span style="font-size:11px; color:#94a3b8;">${stat.total}</span>
            ${state.isAdmin ? `<button type="button" class="btn-pool-edit-mini" onclick="openEditPoolModal('${escapeHtml(poolName)}')" title="Редактировать пул"><svg class="icon" style="width:13px; height:13px;"><use href="#i-edit"/></svg></button>` : ''}
          </div>
        </div>
        <div class="pool-progress-mini">
          <div class="pool-progress-bar" style="width: ${percent}%;"></div>
        </div>
      </div>
    `;
  }
  container.innerHTML = html;
}

// --- KANBAN RENDER ---
function renderKanban() {
  const filtered = getFilteredTasks();
  const stages = ['available', 'in_progress', 'internal_review', 'client_review', 'export_ready', 'completed'];

  for (const st of stages) {
    const colCards = $(`col-cards-${st}`);
    const colCount = $(`kanban-count-${st}`);
    const inStage = filtered.filter(t => t.status === st);

    if (colCount) colCount.textContent = inStage.length;
    if (!colCards) continue;

    if (!inStage.length) {
      colCards.innerHTML = `<div style="text-align:center; padding:30px 10px; color:#475569; font-size:12px;">Пусто</div>`;
      continue;
    }

    colCards.innerHTML = inStage.map(task => {
      const isSelected = task.id === state.selectedTaskId;
      const initials = (task.assigned_to_name || task.assigned_to_username || '??').slice(0, 2).toUpperCase();
      const photosBadge = task.images_count ? `<span class="badge-photos">📷 ${task.images_count}</span>` : '';
      const notes = taskNotes(task);
      const figmaUrl = ['client_review', 'export_ready', 'completed'].includes(task.status) ? task.client_figma_url || task.internal_figma_url : task.internal_figma_url || task.client_figma_url;

      return `
        <article class="task-card ${isSelected ? 'active-selected' : ''}" onclick="selectTask(${task.id})">
          <div class="card-top">
            <span class="card-sku">${escapeHtml(task.sku)}</span>
            <div class="card-badges">${photosBadge}</div>
          </div>
          <div class="card-pool-name">📁 ${escapeHtml(cleanPoolTitle(task.pool_name || 'Общий пул'))}</div>
          ${notes ? `
            <div class="card-comment ${isRevision(task) ? 'is-revision' : ''}">
              <span>💬</span>
              <span style="overflow:hidden; text-overflow:ellipsis; display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical;">${escapeHtml(notes)}</span>
            </div>
          ` : ''}
          <div class="card-footer">
            <div class="card-assignee">
              ${task.assigned_to_id ? `
                <span class="assignee-dot">${initials}</span>
                <span title="${escapeHtml(assigneeName(task))}">${isMine(task) ? 'Вы' : escapeHtml(assigneeName(task).split(' ')[0])}</span>
              ` : `<span style="color:#64748b; font-size:11px;">⚪ Не назначен</span>`}
            </div>
            <div class="card-quick-actions">
              ${task.source_url ? `<a class="quick-link" href="${escapeHtml(safeUrl(task.source_url))}" target="_blank" onclick="event.stopPropagation()" title="Папка фото">📁</a>` : ''}
              ${figmaUrl ? `<a class="quick-link" href="${escapeHtml(safeUrl(figmaUrl))}" target="_blank" onclick="event.stopPropagation()" title="Макет Figma">🎨</a>` : ''}
              ${task.bitrix_url ? `<a class="quick-link" href="${escapeHtml(safeUrl(task.bitrix_url))}" target="_blank" onclick="event.stopPropagation()" title="Битрикс24">⏱️</a>` : ''}
            </div>
          </div>
        </article>
      `;
    }).join('');
  }
}

// --- LIST VIEW RENDER ---
function renderListView() {
  const container = $('list-pools-container');
  if (!container) return;
  const filtered = getFilteredTasks();

  const pools = new Map();
  for (const t of filtered) {
    const p = t.pool_name || 'Общий пул';
    if (!pools.has(p)) pools.set(p, []);
    pools.get(p).push(t);
  }

  if (!pools.size) {
    container.innerHTML = `<div style="text-align:center; padding:40px; color:#64748b;">Нет задач для отображения</div>`;
    return;
  }

  container.innerHTML = Array.from(pools.entries()).map(([pName, tasks]) => {
    return `
      <div class="pool-list-block">
        <div class="pool-list-header">
          <div class="pool-title-main">
            <span>📁 ${escapeHtml(cleanPoolTitle(pName))}</span>
            <span class="nav-item-count">${tasks.length} артикулов</span>
          </div>
          <div style="display:flex; gap:6px;">
            ${state.isAdmin ? `
              <button class="btn btn-secondary btn-sm" onclick="openEditPoolModal('${escapeHtml(pName)}')">✏️ Настроить пул</button>
              <button class="btn btn-secondary btn-sm" onclick="openExportModal(['${escapeHtml(pName)}'])">📥 Экспорт</button>
            ` : ''}
          </div>
        </div>
        <table class="pool-table">
          <thead>
            <tr>
              <th>Артикул</th>
              <th>Этап</th>
              <th>Исполнитель</th>
              <th>Фото</th>
              <th>Замечания</th>
              <th>Действие</th>
            </tr>
          </thead>
          <tbody>
            ${tasks.map(t => {
              const conf = STAGES[t.status] || STAGES.available;
              return `
                <tr onclick="selectTask(${t.id})">
                  <td style="font-family:monospace; font-weight:700;">${escapeHtml(t.sku)}</td>
                  <td><span style="display:inline-flex; align-items:center; gap:5px; font-size:11.5px; font-weight:600; padding:2px 7px; border-radius:6px; background:${conf.bg}; color:${conf.color};">${conf.emoji} ${conf.label}</span></td>
                  <td>${escapeHtml(t.assigned_to_name ? (isMine(t) ? 'Вы' : assigneeName(t)) : '—')}</td>
                  <td>${t.images_count ? `${t.images_count} шт.` : '—'}</td>
                  <td style="max-width:220px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; color:${isRevision(t) ? 'var(--stage-revision)' : 'inherit'};">${escapeHtml(taskNotes(t) || '—')}</td>
                  <td><button class="btn btn-secondary btn-sm" onclick="event.stopPropagation(); selectTask(${t.id})">Открыть</button></td>
                </tr>
              `;
            }).join('')}
          </tbody>
        </table>
      </div>
    `;
  }).join('');
}

// --- ANALYTICS VIEW RENDER ---
function renderAnalytics() {
  const activeTasks = state.tasks.filter(t => !t.is_archived);
  $('funnel-total').textContent = activeTasks.length;

  // Workload
  const workload = {};
  for (const t of activeTasks) {
    if (t.assigned_to_id && ['in_progress', 'internal_review', 'client_review', 'export_ready'].includes(t.status)) {
      const name = assigneeName(t);
      workload[name] = (workload[name] || 0) + 1;
    }
  }

  const workloadContainer = $('analytics-workload-list');
  if (workloadContainer) {
    const entries = Object.entries(workload).sort((a,b) => b[1] - a[1]);
    if (!entries.length) {
      workloadContainer.innerHTML = `<div style="color:var(--muted); font-size:12px;">Нет активных задач в работе</div>`;
    } else {
      const maxCount = Math.max(...entries.map(e => e[1]), 1);
      workloadContainer.innerHTML = entries.map(([name, count]) => {
        const percent = Math.round((count / maxCount) * 100);
        return `
          <div class="team-load-row">
            <div style="width:130px; font-weight:600; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">${escapeHtml(name)}</div>
            <div class="team-load-bar-wrap"><div class="team-load-bar" style="width:${percent}%;"></div></div>
            <div style="width:50px; text-align:right; font-weight:700;">${count} арт.</div>
          </div>
        `;
      }).join('');
    }
  }

  // Funnel
  const funnelContainer = $('analytics-funnel-list');
  if (funnelContainer) {
    const total = activeTasks.length || 1;
    funnelContainer.innerHTML = Object.entries(STAGES).map(([k, s]) => {
      const cnt = activeTasks.filter(t => t.status === k).length;
      const pct = Math.round((cnt / total) * 100);
      return `
        <div>
          <div style="display:flex; justify-content:space-between; font-size:12px; margin-bottom:4px;">
            <span>${s.emoji} ${s.label}</span>
            <strong>${cnt} (${pct}%)</strong>
          </div>
          <div class="team-load-bar-wrap"><div class="team-load-bar" style="width:${pct}%; background:${s.color};"></div></div>
        </div>
      `;
    }).join('');
  }

  // Total images
  const totalImg = activeTasks.reduce((sum, t) => sum + (Number(t.images_count) || 0), 0);
  $('analytics-total-images').textContent = totalImg;
}

// --- DRAWER DETAILS & ACTIONS ---
function selectTask(taskId) {
  state.selectedTaskId = taskId;
  document.querySelectorAll('.task-card').forEach(c => c.classList.remove('active-selected'));
  renderDrawer();
}

function closeDrawer() {
  state.selectedTaskId = null;
  $('task-drawer')?.classList.add('closed');
  document.querySelectorAll('.task-card').forEach(c => c.classList.remove('active-selected'));
}

function renderDrawer() {
  const task = state.tasks.find(t => t.id === state.selectedTaskId);
  const drawer = $('task-drawer');
  if (!task || !drawer) {
    drawer?.classList.add('closed');
    return;
  }
  drawer.classList.remove('closed');

  const conf = STAGES[task.status] || STAGES.available;
  $('drawer-sku').textContent = task.sku;
  $('drawer-pool-name').textContent = cleanPoolTitle(task.pool_name || 'Общий пул');
  $('drawer-assignee').textContent = task.assigned_to_id ? `${assigneeName(task)}${isMine(task) ? ' · Вы' : ''}` : '⚪ Не назначен';
  $('drawer-images-count').textContent = task.images_count ? `${task.images_count} шт.` : 'Не указано';

  const pill = $('drawer-status-pill');
  pill.style.background = conf.bg;
  pill.style.color = conf.color;
  $('drawer-status-dot').textContent = conf.emoji;
  $('drawer-status-name').textContent = conf.full || conf.label;

  // Edit images button
  const canEdit = isMine(task) || state.isAdmin;
  $('btn-drawer-edit-images').classList.toggle('hidden', !canEdit);

  // Links
  const linkSource = $('drawer-link-source');
  if (task.source_url) {
    linkSource.href = safeUrl(task.source_url);
    linkSource.classList.remove('hidden');
  } else {
    linkSource.classList.add('hidden');
  }

  const linkFigma = $('drawer-link-figma');
  if (task.internal_figma_url) {
    linkFigma.href = safeUrl(task.internal_figma_url);
    linkFigma.classList.remove('hidden');
  } else {
    linkFigma.classList.add('hidden');
  }

  const linkClientFigma = $('drawer-link-client-figma');
  if (task.client_figma_url) {
    linkClientFigma.href = safeUrl(task.client_figma_url);
    linkClientFigma.classList.remove('hidden');
  } else {
    linkClientFigma.classList.add('hidden');
  }

  const linkDisk = $('drawer-link-disk');
  if (task.yandex_disk_url) {
    linkDisk.href = safeUrl(task.yandex_disk_url);
    linkDisk.classList.remove('hidden');
  } else {
    linkDisk.classList.add('hidden');
  }

  const linkBitrix = $('drawer-link-bitrix');
  if (task.bitrix_url) {
    linkBitrix.href = safeUrl(task.bitrix_url);
    linkBitrix.classList.remove('hidden');
  } else {
    linkBitrix.classList.add('hidden');
  }

  // Notes
  const notesText = taskNotes(task);
  const notesSec = $('drawer-notes-section');
  if (notesText) {
    notesSec.classList.remove('hidden');
    $('drawer-notes-text').textContent = isRevision(task) ? notesText.replace(/^Правки:\s*/, '') : notesText;
    $('drawer-notes-badge').textContent = isRevision(task) ? '⚠️ Правки' : 'Комментарий';
  } else {
    notesSec.classList.add('hidden');
  }

  // Action buttons
  renderDrawerActions(task);
}

function primaryAction(task) {
  if (task.is_archived) return null;
  const canEdit = isMine(task) || state.isAdmin;
  if (task.status === 'available') return { label: 'Взять в работу', action: 'take' };
  if (task.status === 'in_progress' && canEdit) return { label: 'Отправить на проверку', action: 'internal' };
  if (task.status === 'internal_review' && state.isAdmin) return { label: 'Передать заказчику', action: 'client' };
  if (task.status === 'client_review' && state.isAdmin) return { label: 'Согласовано (На экспорт)', action: 'export' };
  if (task.status === 'export_ready' && canEdit) return { label: 'Завершить (Финалы на Я.Диске)', action: 'complete' };
  if (task.status === 'completed' && safeUrl(task.yandex_disk_url)) return { label: 'Файлы на Яндекс.Диске', url: task.yandex_disk_url };
  return null;
}

function renderDrawerActions(task) {
  const container = $('drawer-actions');
  if (!container) return;
  const canEdit = isMine(task) || state.isAdmin;
  const act = primaryAction(task);

  let html = '';

  if (act) {
    if (act.url) {
      html += `<a class="btn btn-secondary btn-wide" href="${escapeHtml(safeUrl(act.url))}" target="_blank">📁 ${escapeHtml(act.label)}</a>`;
    } else {
      const disabled = state.pendingTaskIds.has(task.id);
      html += `<button class="btn btn-primary btn-wide" onclick="${act.action === 'take' ? `takeTask(${task.id})` : `openAction('${act.action}', ${task.id})`}" ${disabled ? 'disabled' : ''}>${disabled ? 'Сохраняем…' : act.label}</button>`;
    }
  }

  // Assign / Reassign (Admin)
  if (!task.is_archived && state.isAdmin) {
    html += `<button class="btn btn-secondary btn-wide" onclick="openAction('assign', ${task.id})">👥 ${task.status === 'available' ? 'Назначить дизайнеру' : 'Переназначить задачу'}</button>`;
  }

  // Reject / Rework
  if (!task.is_archived && canEdit && ['internal_review', 'client_review'].includes(task.status)) {
    html += `<button class="btn btn-danger btn-wide" onclick="openAction('reject', ${task.id})">${state.isAdmin ? '↩ Вернуть на правки' : '↩ Забрать на доработку'}</button>`;
  }

  // Return to available
  if (!task.is_archived && task.status === 'in_progress' && canEdit) {
    html += `<button class="btn btn-ghost btn-wide" onclick="openAction('return', ${task.id})">Вернуть в свободные</button>`;
  }

  // Admin: Archive & Delete
  if (state.isAdmin) {
    html += `
      <div style="display:flex; gap:8px; margin-top:4px;">
        <button class="btn btn-secondary btn-sm" style="flex:1;" onclick="toggleTaskArchive(${task.id}, ${task.is_archived ? 0 : 1})">${task.is_archived ? 'Восстановить' : 'В архив'}</button>
        <button class="btn btn-danger btn-sm" onclick="confirmDeleteTask(${task.id})">Удалить</button>
      </div>
    `;
  }

  container.innerHTML = html;
}

// --- TASK ACTIONS & MUTATIONS ---
function applyTask(task) {
  if (!task) return;
  state.mutationVersion++;
  const index = state.tasks.findIndex(item => item.id === task.id);
  if (index >= 0) state.tasks[index] = task;
  else state.tasks.unshift(task);
}

async function takeTask(taskId) {
  if (state.pendingTaskIds.has(taskId)) return;
  state.pendingTaskIds.add(taskId);
  render();
  try {
    const data = await api('/api/tasks/take', { task_id: taskId });
    applyTask(data.task);
    showToast('Задача у вас в работе! 🎨');
    try { if (telegramSupports('6.1')) tg.HapticFeedback?.notificationOccurred('success'); } catch {}
  } catch (error) {
    showToast(error.message, 'error');
    await loadTasks();
  } finally {
    state.pendingTaskIds.delete(taskId);
    render();
  }
}

function openImagesCountAction() {
  if (state.selectedTaskId) openAction('images', state.selectedTaskId);
}

function copyCurrentSku() {
  const task = state.tasks.find(t => t.id === state.selectedTaskId);
  if (task?.sku) {
    navigator.clipboard?.writeText(task.sku);
    showToast(`Скопировано: ${task.sku}`);
  }
}

async function toggleTaskArchive(taskId, archiveStatus) {
  try {
    const res = await api(`/api/tasks/${taskId}/archive`, { is_archived: archiveStatus });
    if (res.task) applyTask(res.task);
    showToast(archiveStatus ? 'Задача перемещена в архив' : 'Задача восстановлена из архива');
    render();
  } catch (e) {
    showToast(e.message, 'error');
  }
}

async function confirmDeleteTask(taskId) {
  const task = state.tasks.find(t => t.id === taskId);
  if (!confirm(`Удалить задачу ${task?.sku || ''}? Это действие необратимо.`)) return;
  try {
    await api(`/api/tasks/${taskId}`, undefined, 'DELETE');
    state.tasks = state.tasks.filter(t => t.id !== taskId);
    closeDrawer();
    render();
    showToast('Задача удалена');
  } catch (e) {
    showToast(e.message, 'error');
  }
}

// --- ACTION MODAL (ALL 8 ACTIONS) ---
function urlField(label, value = '', required = true) {
  return `<div class="form-group"><label for="action-url">${label}</label><input id="action-url" class="form-control" type="url" value="${escapeHtml(value)}" ${required ? 'required' : ''}></div>`;
}

async function openAction(kind, taskId) {
  if (state.formBusy) return;
  const task = state.tasks.find(item => item.id === taskId);
  const config = ACTIONS[kind];
  if (!task || !config) return;

  if (kind === 'assign' && (!state.users || !state.users.length)) {
    await loadDesigners();
  }

  state.action = { kind, taskId };
  const isDesignerRework = kind === 'reject' && !state.isAdmin;
  const isReassign = kind === 'assign' && task.status !== 'available';

  $('action-title').textContent = isDesignerRework ? 'Забрать на доработку' : (isReassign ? 'Переназначить задачу' : config.title);
  $('action-sku').textContent = `Артикул: ${task.sku}`;
  $('action-error').classList.add('hidden');
  $('action-submit').textContent = isDesignerRework ? 'Забрать на доработку' : (isReassign ? 'Переназначить' : config.submit);
  $('action-submit').className = `btn ${['reject', 'return'].includes(kind) ? 'btn-danger' : 'btn-primary'} btn-wide`;

  let fields = '';
  if (kind === 'internal') {
    fields = urlField('Ссылка на макет в Figma *', task.internal_figma_url || '') +
      `<div class="form-group"><label for="action-images-count">Количество изображений в 1 задании *</label><input id="action-images-count" class="form-control" type="number" min="1" max="500" value="${escapeHtml(task.images_count || '')}" placeholder="Например: 10" required></div>` +
      `<div class="form-group"><label for="action-notes">Комментарий для лида</label><textarea id="action-notes" class="form-control" rows="3"></textarea></div>`;
  }
  if (kind === 'images') {
    fields = `<div class="form-group"><label for="action-images-count">Количество изображений в 1 задании *</label><input id="action-images-count" class="form-control" type="number" min="1" max="500" value="${escapeHtml(task.images_count || '')}" placeholder="Например: 10" required autofocus></div>`;
  }
  if (kind === 'client') {
    fields = urlField('Figma заказчика', task.client_figma_url || '', false) +
      `<div class="form-group"><label for="action-notes">Комментарий</label><textarea id="action-notes" class="form-control" rows="2"></textarea></div>`;
  }
  if (kind === 'complete') {
    fields = urlField('Финалы на Яндекс.Диске *', task.yandex_disk_url || '') +
      `<div class="form-group"><label for="action-notes">Комментарий</label><textarea id="action-notes" class="form-control" rows="2"></textarea></div>`;
  }
  if (kind === 'reject') {
    fields = `<div class="form-group"><label for="action-notes">Что нужно исправить? *</label><textarea id="action-notes" class="form-control" rows="4" required placeholder="Опишите замечания к макету..."></textarea></div>`;
  }
  if (kind === 'export') {
    fields = `<p class="muted" style="margin-bottom:10px;">Заказчик согласовал макет. Задача перейдет в статус «На экспорт».</p>`;
  }
  if (kind === 'return') {
    fields = `<p class="muted" style="margin-bottom:10px;">Задача вернется в общий свободный пул, и любой дизайнер сможет взять её.</p>`;
  }
  if (kind === 'assign') {
    const currentAssigneeId = task.assigned_to_id;
    const designers = (state.users || []).filter(u => u.is_approved !== 0);
    const activeMap = getActiveWorkloadMap();

    const designerRows = designers.map(u => {
      const isSelected = u.telegram_id === currentAssigneeId;
      const count = activeMap[u.telegram_id] ?? 0;
      const countLabel = count > 0 ? `${count} в работе` : 'свободен';
      const name = `${u.first_name || 'Участник'} ${u.last_name || ''}`.trim();
      const initial = (u.first_name || 'U').trim()[0].toUpperCase();

      return `
        <label class="assignee-card${isSelected ? ' is-selected' : ''}" data-id="${u.telegram_id}">
          <input type="radio" name="assignee_radio" value="${u.telegram_id}" ${isSelected ? 'checked' : ''} onchange="onAssigneeRadioChange(this)">
          <div class="team-avatar">${escapeHtml(initial)}</div>
          <div class="assignee-card-info">
            <div class="assignee-card-name">
              <strong>${escapeHtml(name)}</strong>
              ${u.role === 'admin' ? '<span style="font-size:10px; background:rgba(200,238,148,0.15); color:var(--accent); padding:1px 5px; border-radius:4px;">Админ</span>' : ''}
            </div>
            <p class="assignee-card-sub">${escapeHtml(u.username ? `@${u.username}` : '')}</p>
          </div>
          <div class="assignee-workload-badge ${count > 0 ? 'is-busy' : 'is-free'}">
            ${escapeHtml(countLabel)}
          </div>
        </label>
      `;
    }).join('');

    fields = `
      <div style="font-size:12.5px; font-weight:600; margin-bottom:6px;">Выберите дизайнера:</div>
      <input type="hidden" id="action-designer-id" value="${currentAssigneeId || ''}">
      <div class="assignee-picker-list">${designerRows || '<p class="muted">Нет доступных дизайнеров</p>'}</div>
    `;
  }

  $('action-fields').innerHTML = fields;
  openModal('modal-action');
}

function onAssigneeRadioChange(radio) {
  if ($('action-designer-id')) $('action-designer-id').value = radio.value;
  document.querySelectorAll('.assignee-card').forEach(card => {
    card.classList.toggle('is-selected', card.dataset.id === String(radio.value));
  });
}

async function submitAction(event) {
  event.preventDefault();
  if (state.formBusy || !state.action) return;
  const { kind, taskId } = state.action;
  const config = ACTIONS[kind];
  const body = { task_id: taskId };

  if (['internal', 'images'].includes(kind)) {
    const val = parseInt($('action-images-count')?.value, 10);
    if (isNaN(val) || val < 1 || val > 500) {
      showFormError('action-error', 'Укажите число от 1 до 500');
      return;
    }
    body.images_count = val;
  }
  if (kind === 'internal') {
    body.internal_figma_url = $('action-url').value.trim();
    body.notes = $('action-notes')?.value.trim() || null;
  }
  if (kind === 'client') {
    const u = $('action-url')?.value.trim();
    body.client_figma_url = u ? safeUrl(u) : null;
    body.notes = $('action-notes')?.value.trim() || null;
  }
  if (kind === 'complete') {
    body.yandex_disk_url = $('action-url').value.trim();
    body.notes = $('action-notes')?.value.trim() || null;
  }
  if (kind === 'reject') {
    const n = $('action-notes').value.trim();
    if (!n) {
      showFormError('action-error', 'Опишите замечания');
      return;
    }
    body.notes = 'Правки: ' + n;
    body.return_to = 'in_progress';
  }
  if (kind === 'return') {
    body.return_to = 'available';
  }
  if (kind === 'assign') {
    const dId = $('action-designer-id')?.value;
    if (!dId) {
      showFormError('action-error', 'Выберите дизайнера');
      return;
    }
    body.assignee_id = parseInt(dId, 10);
  }

  state.formBusy = true;
  const btn = $('action-submit');
  btn.disabled = true;

  try {
    const res = await api(`/api/tasks/${config.endpoint}`, body);
    applyTask(res.task);
    closeModal('modal-action');
    showToast(config.message);
    render();
  } catch (e) {
    showFormError('action-error', e.message);
  } finally {
    state.formBusy = false;
    btn.disabled = false;
  }
}

// --- ADD BATCH POOL MODAL ---
function openAddBatchModal() {
  if (!state.isAdmin) return;
  $('form-add-batch').reset();
  $('batch-error').classList.add('hidden');
  $('batch-analysis').classList.add('hidden');
  updateSkuCounter();
  openModal('modal-add-batch');
}

function updateSkuCounter() {
  const text = $('batch-skus')?.value || '';
  const lines = text.split(/[\n,;]+/).map(s => s.trim()).filter(Boolean);
  const counter = $('skus-counter');
  if (counter) counter.textContent = `${lines.length} арт.`;
}

function renderBatchAnalysis(analysis) {
  const box = $('batch-analysis');
  if (!box) return;
  if (!analysis) {
    box.classList.add('hidden');
    box.innerHTML = '';
    return;
  }
  let msg = `Найдено артикулов: <b>${analysis.total_parsed}</b>. `;
  if (analysis.existing_in_db?.length) {
    msg += `<br>⚠️ Уже есть в базе: <b>${analysis.existing_in_db.length}</b> (${analysis.existing_in_db.slice(0, 3).join(', ')}...)`;
  }
  box.innerHTML = msg;
  box.className = 'batch-analysis ' + (analysis.existing_in_db?.length ? 'form-error' : '');
  box.classList.remove('hidden');
}

async function autoScanCloudFolders() {
  const url = $('batch-parent-url').value.trim();
  if (!url) {
    alert('Укажите ссылку на папку с исходниками');
    return;
  }
  const btn = $('btn-scan');
  btn.disabled = true;
  btn.textContent = 'Сканируем…';
  try {
    const res = await api('/api/cloud/scan', { url });
    if (res.skus?.length) {
      $('batch-skus').value = res.skus.join('\n');
      updateSkuCounter();
      showToast(`Найдено ${res.skus.length} артикулов в папке!`);
    } else {
      showToast('В папке не обнаружено артикулов');
    }
  } catch (e) {
    alert('Ошибка сканирования: ' + e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = 'Найти артикулы';
  }
}

async function submitAddBatch(e) {
  e.preventDefault();
  const btn = $('btn-submit-batch');
  btn.disabled = true;

  const poolName = $('batch-name').value.trim();
  const parentUrl = $('batch-parent-url').value.trim();
  const bitrixUrl = $('batch-bitrix-url').value.trim();
  const skusText = $('batch-skus').value.trim();
  const assigneeVal = $('batch-assignee').value;

  try {
    const res = await api('/api/tasks/create-batch', {
      pool_name: poolName,
      parent_url: parentUrl,
      bitrix_url: bitrixUrl ? (safeUrl(bitrixUrl) ? bitrixUrl : null) : null,
      skus_text: skusText,
      assigned_to_id: assigneeVal ? parseInt(assigneeVal, 10) : null
    });
    closeModal('modal-add-batch');
    await loadTasks();
    showToast(`Пул «${poolName}» создан (${res.created_count} задач)!`);
  } catch (err) {
    showFormError('batch-error', err.message);
  } finally {
    btn.disabled = false;
  }
}

// --- EDIT POOL MODAL ---
function openEditPoolModal(poolName) {
  if (!state.isAdmin) return;
  currentEditingPoolName = poolName;
  $('edit-pool-title').textContent = `Пул: ${poolName}`;
  $('edit-pool-name').value = poolName;
  $('edit-pool-new-skus').value = '';
  $('edit-pool-source-url').value = '';

  const poolTasks = state.tasks.filter(t => (t.pool_name || 'Общий пул') === poolName);
  const currentBitrix = poolTasks.find(t => t.bitrix_url)?.bitrix_url || '';
  if ($('edit-pool-bitrix-url')) $('edit-pool-bitrix-url').value = currentBitrix;
  if ($('edit-pool-assignee')) $('edit-pool-assignee').value = '';

  $('edit-pool-error').classList.add('hidden');
  renderEditingPoolSkus();
  openModal('modal-edit-pool');
}

function renderEditingPoolSkus() {
  if (!currentEditingPoolName) return;
  const poolTasks = state.tasks.filter(t => (t.pool_name || 'Общий пул') === currentEditingPoolName);
  $('edit-pool-skus-count').textContent = poolTasks.length;

  const allArchived = poolTasks.length > 0 && poolTasks.every(t => t.is_archived);
  $('btn-edit-pool-archive-label').textContent = allArchived ? 'Из архива' : 'В архив';

  const container = $('edit-pool-skus-list');
  if (!poolTasks.length) {
    container.innerHTML = '<p class="muted" style="padding:8px 0;">В этом пуле нет артикулов</p>';
    return;
  }

  container.innerHTML = poolTasks.map(task => {
    const stage = STAGES[task.status] || { label: task.status, color: '#94a3b8' };
    const assignee = task.assigned_to_name ? escapeHtml(assigneeName(task)) : 'Не назначен';
    return `
      <div class="edit-pool-sku-row">
        <div>
          <span class="edit-pool-sku-code">${escapeHtml(task.sku)}</span>
          <span style="color:${stage.color}; margin-left:6px; font-size:11px;">• ${escapeHtml(stage.label)}</span>
          <span class="muted" style="margin-left:6px; font-size:11px;">(${assignee})</span>
        </div>
        <button type="button" class="btn-remove-user" onclick="deleteSkuFromPool(${task.id}, '${escapeHtml(task.sku)}')" title="Удалить артикул">
          <svg class="icon" style="width:14px; height:14px;"><use href="#i-trash"/></svg>
        </button>
      </div>
    `;
  }).join('');
}

async function submitRenamePool() {
  if (!state.isAdmin || !currentEditingPoolName) return;
  const newName = $('edit-pool-name').value.trim();
  if (!newName) {
    showFormError('edit-pool-error', 'Название не может быть пустым');
    return;
  }
  if (newName === currentEditingPoolName) return;

  try {
    await api('/api/pools/rename', { old_pool_name: currentEditingPoolName, new_pool_name: newName });
    for (const t of state.tasks) {
      if ((t.pool_name || 'Общий пул') === currentEditingPoolName) t.pool_name = newName;
    }
    currentEditingPoolName = newName;
    $('edit-pool-title').textContent = `Пул: ${newName}`;
    render();
    renderEditingPoolSkus();
    showToast(`Пул переименован в «${newName}»`);
  } catch (err) {
    showFormError('edit-pool-error', err.message);
  }
}

async function submitSavePoolBitrix() {
  if (!state.isAdmin || !currentEditingPoolName) return;
  const bitrixUrl = $('edit-pool-bitrix-url')?.value.trim();
  try {
    await api('/api/pools/update-bitrix', {
      pool_name: currentEditingPoolName,
      bitrix_url: bitrixUrl ? (safeUrl(bitrixUrl) ? bitrixUrl : null) : null
    });
    for (const t of state.tasks) {
      if ((t.pool_name || 'Общий пул') === currentEditingPoolName) t.bitrix_url = bitrixUrl || null;
    }
    render();
    showToast(bitrixUrl ? 'Ссылка Битрикс24 сохранена' : 'Ссылка Битрикс24 очищена');
  } catch (err) {
    showFormError('edit-pool-error', err.message);
  }
}

async function submitAddSkusToPool() {
  if (!state.isAdmin || !currentEditingPoolName) return;
  const text = $('edit-pool-new-skus').value.trim();
  if (!text) {
    showFormError('edit-pool-error', 'Введите артикулы');
    return;
  }
  const sourceUrl = $('edit-pool-source-url').value.trim();
  const assigneeVal = $('edit-pool-assignee')?.value;

  try {
    const res = await api('/api/pools/add-skus', {
      pool_name: currentEditingPoolName,
      skus_text: text,
      source_url: safeUrl(sourceUrl) ? sourceUrl : null,
      assign_to_id: assigneeVal ? parseInt(assigneeVal, 10) : null
    });
    $('edit-pool-new-skus').value = '';
    await loadTasks();
    renderEditingPoolSkus();
    showToast(`Добавлено ${res.created_count} артикулов`);
  } catch (err) {
    showFormError('edit-pool-error', err.message);
  }
}

async function deleteSkuFromPool(taskId, sku) {
  if (!confirm(`Удалить артикул ${sku} из базы?`)) return;
  try {
    await api(`/api/tasks/${taskId}`, undefined, 'DELETE');
    state.tasks = state.tasks.filter(t => t.id !== taskId);
    renderEditingPoolSkus();
    render();
    showToast(`Артикул ${sku} удален`);
  } catch (e) {
    showFormError('edit-pool-error', e.message);
  }
}

async function toggleCurrentEditingPoolArchive() {
  if (!currentEditingPoolName) return;
  const poolTasks = state.tasks.filter(t => (t.pool_name || 'Общий пул') === currentEditingPoolName);
  const allArchived = poolTasks.length > 0 && poolTasks.every(t => t.is_archived);
  const targetArchive = !allArchived;

  try {
    await api('/api/pools/archive', { pool_name: currentEditingPoolName, is_archived: targetArchive ? 1 : 0 });
    for (const t of state.tasks) {
      if ((t.pool_name || 'Общий пул') === currentEditingPoolName) t.is_archived = targetArchive ? 1 : 0;
    }
    renderEditingPoolSkus();
    render();
    showToast(targetArchive ? 'Пул отправлен в архив' : 'Пул восстановлен из архива');
  } catch (err) {
    showFormError('edit-pool-error', err.message);
  }
}

async function confirmDeleteEditingPool() {
  if (!currentEditingPoolName) return;
  if (!confirm(`Удалить пул «${currentEditingPoolName}» и ВСЕ его артикулы? Действие необратимо.`)) return;

  try {
    await api('/api/pools/delete', { pool_name: currentEditingPoolName });
    state.tasks = state.tasks.filter(t => (t.pool_name || 'Общий пул') !== currentEditingPoolName);
    closeModal('modal-edit-pool');
    render();
    showToast(`Пул «${currentEditingPoolName}» удален`);
  } catch (err) {
    showFormError('edit-pool-error', err.message);
  }
}

// --- EXPORT MODAL (EXCEL & GOOGLE SHEETS) ---
async function openExportModal(preSelectedPoolNames = null) {
  if (!state.isAdmin) return;
  $('export-error')?.classList.add('hidden');
  $('export-google-section')?.classList.toggle('hidden', exportFormat !== 'google');
  $('export-guide-section')?.classList.add('hidden');
  $('export-pools-list').innerHTML = '<div class="muted" style="padding:10px; text-align:center;">Загрузка пулов...</div>';
  openModal('modal-export');

  try {
    const data = await api('/api/export/pools');
    exportAvailablePools = data.pools || [];

    if (preSelectedPoolNames && preSelectedPoolNames.length) {
      exportSelectedPools = new Set(preSelectedPoolNames);
    } else if (state.poolFilter !== 'all') {
      exportSelectedPools = new Set([state.poolFilter]);
    } else {
      const nonArchived = exportAvailablePools.filter(p => !p.all_archived).map(p => p.pool_name);
      exportSelectedPools = new Set(nonArchived.length ? nonArchived : exportAvailablePools.map(p => p.pool_name));
    }

    renderExportPoolsList();

    const gsSettings = await api('/api/settings/google-sheets');
    if (gsSettings.webhook_url && $('export-google-webhook')) {
      $('export-google-webhook').value = gsSettings.webhook_url;
    }
    googleScriptTemplate = gsSettings.script_template || '';
  } catch (err) {
    $('export-pools-list').innerHTML = `<div class="form-error">${escapeHtml(err.message)}</div>`;
  }
}

function renderExportPoolsList() {
  const container = $('export-pools-list');
  if (!exportAvailablePools.length) {
    container.innerHTML = '<div class="muted" style="padding:8px; text-align:center;">Нет доступных пулов</div>';
    $('export-pools-summary').textContent = 'Выбрано: 0';
    return;
  }

  container.innerHTML = exportAvailablePools.map(pool => {
    const isSelected = exportSelectedPools.has(pool.pool_name);
    const countInfo = `${pool.total_tasks} арт. · ${pool.total_images || 0} фото`;
    const archivedBadge = pool.all_archived ? ' <span class="muted">(архив)</span>' : '';
    return `
      <label style="display:flex; align-items:center; justify-content:space-between; padding:4px 6px; border-radius:6px; cursor:pointer; background:${isSelected ? 'rgba(200,238,148,0.06)' : 'transparent'};">
        <span style="display:flex; align-items:center; gap:8px;">
          <input type="checkbox" ${isSelected ? 'checked' : ''} onchange="toggleExportPoolSelection('${escapeHtml(pool.pool_name)}', this.checked)">
          <span style="font-size:12.5px; font-weight:600;">${escapeHtml(pool.pool_name)}${archivedBadge}</span>
        </span>
        <span class="muted" style="font-size:11px;">${countInfo}</span>
      </label>
    `;
  }).join('');

  updateExportSummary();
}

function updateExportSummary() {
  const selectedCount = exportSelectedPools.size;
  const total = exportAvailablePools.length;
  $('btn-export-toggle-all').textContent = (selectedCount === total && total > 0) ? 'Снять все' : 'Выбрать все';

  let totalTasks = 0, totalImages = 0;
  for (const p of exportAvailablePools) {
    if (exportSelectedPools.has(p.pool_name)) {
      totalTasks += p.total_tasks;
      totalImages += (p.total_images || 0);
    }
  }
  $('export-pools-summary').textContent = `Выбрано: ${selectedCount} из ${total} пулов (${totalTasks} задач, ${totalImages} фото)`;
}

function toggleExportPoolSelection(poolName, isChecked) {
  if (isChecked) exportSelectedPools.add(poolName);
  else exportSelectedPools.delete(poolName);
  renderExportPoolsList();
}

function toggleExportAllPools() {
  if (exportSelectedPools.size === exportAvailablePools.length) {
    exportSelectedPools.clear();
  } else {
    exportSelectedPools = new Set(exportAvailablePools.map(p => p.pool_name));
  }
  renderExportPoolsList();
}

function handleExportFormatChange(format) {
  exportFormat = format;
  $('export-google-section')?.classList.toggle('hidden', format !== 'google');
  $('btn-run-export').textContent = format === 'google' ? 'Выгрузить в Google Таблицу' : 'Скачать Excel (.xlsx)';
  if (format === 'google' && !$('export-google-webhook')?.value.trim()) {
    $('export-guide-section')?.classList.remove('hidden');
  } else if (format !== 'google') {
    $('export-guide-section')?.classList.add('hidden');
  }
}

function openGoogleScriptGuide() {
  $('export-guide-section')?.classList.toggle('hidden');
}

async function copyGoogleScriptCode() {
  if (!googleScriptTemplate) {
    try {
      const gsSettings = await api('/api/settings/google-sheets');
      googleScriptTemplate = gsSettings.script_template || '';
    } catch (e) {}
  }
  if (!googleScriptTemplate) return;
  try {
    await navigator.clipboard.writeText(googleScriptTemplate);
    showToast('Код скрипта скопирован в буфер обмена');
  } catch (e) {
    showToast('Не удалось скопировать', 'error');
  }
}

async function executeExport() {
  if (!exportSelectedPools.size) {
    showFormError('export-error', 'Выберите хотя бы один пул');
    return;
  }
  const btn = $('btn-run-export');
  btn.disabled = true;
  btn.textContent = exportFormat === 'google' ? 'Синхронизация с Google...' : 'Формируем Excel...';

  const selectedList = [...exportSelectedPools];
  try {
    if (exportFormat === 'excel') {
      const response = await fetch('/api/export/excel', {
        method: 'POST',
        headers: getAuthHeaders(),
        body: JSON.stringify({ pool_names: selectedList, send_to_telegram: true })
      });
      if (!response.ok) {
        const errJson = await response.json().catch(() => ({}));
        throw new Error(errJson.detail || 'Ошибка генерации Excel');
      }

      let filename = 'report_pools.xlsx';
      const disposition = response.headers.get('Content-Disposition');
      if (disposition && disposition.includes("filename*=UTF-8''")) {
        filename = decodeURIComponent(disposition.split("filename*=UTF-8''")[1].split(';')[0]);
      }
      const sentToTg = response.headers.get('X-Sent-To-Telegram') === 'true';

      const blob = await response.blob();
      const url = window.URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      a.remove();
      window.URL.revokeObjectURL(url);

      closeModal('modal-export');
      showToast(sentToTg ? 'Отчет Excel отправлен вам в чат с ботом! 📄' : 'Отчет Excel скачан');
    } else {
      const webhookUrl = $('export-google-webhook').value.trim();
      if (!webhookUrl) throw new Error('Укажите ссылку на веб-приложение Google Apps Script');
      const saveDefault = $('export-google-save-default')?.checked ?? true;
      const res = await api('/api/export/google-sheets', {
        webhook_url: webhookUrl,
        pool_names: selectedList,
        save_as_default: saveDefault
      });
      closeModal('modal-export');
      showToast(`Успешно выгружено в Google Таблицу (${res.sheets_count} листов)`);
    }
  } catch (err) {
    showFormError('export-error', err.message);
  } finally {
    btn.disabled = false;
    btn.textContent = exportFormat === 'google' ? 'Выгрузить в Google Таблицу' : 'Скачать Excel (.xlsx)';
  }
}

// --- TEAM MODAL & BACKUP ---
async function openTeamModal() {
  if (!state.isAdmin) return;
  openModal('modal-team');
  $('team-list').innerHTML = '<p class="muted" style="padding:10px; text-align:center;">Загрузка участников…</p>';
  try {
    const data = await api('/api/users');
    state.users = data.users || [];
    populateAssigneeSelects();
    renderTeamList(state.users);
  } catch (error) {
    $('team-list').innerHTML = `<p class="form-error">${escapeHtml(error.message)}</p>`;
  }
}

function renderTeamList(users) {
  const container = $('team-list');
  if (!users.length) {
    container.innerHTML = '<p class="muted">Участников пока нет</p>';
    return;
  }
  container.innerHTML = users.map(user => {
    const name = user.first_name || 'Участник';
    const isSelf = user.telegram_id === state.currentUser?.telegram_id;
    const removeBtn = isSelf ? '' : `<button type="button" class="btn-remove-user" title="Удалить" onclick="confirmRemoveUser(${user.telegram_id}, '${escapeHtml(name)}')"><svg class="icon" style="width:14px; height:14px;"><use href="#i-trash"/></svg></button>`;
    return `
      <div class="team-row">
        <div class="team-avatar">${escapeHtml(name.trim()[0] || 'U')}</div>
        <div class="team-info">
          <strong>${escapeHtml(name)}${isSelf ? ' · Вы' : ''}</strong>
          <p>${escapeHtml(user.username ? `@${user.username}` : String(user.telegram_id))}</p>
        </div>
        <div class="team-actions">
          <select onchange="updateUserRole(${user.telegram_id}, this.value)">
            <option value="designer" ${user.role === 'designer' ? 'selected' : ''}>Дизайнер</option>
            <option value="admin" ${user.role === 'admin' ? 'selected' : ''}>Админ</option>
          </select>
          ${removeBtn}
        </div>
      </div>
    `;
  }).join('');
}

async function updateUserRole(userId, newRole) {
  try {
    await api(`/api/users/${userId}/role`, { role: newRole });
    showToast('Роль участника обновлена');
    await openTeamModal();
  } catch (e) {
    showToast(e.message, 'error');
  }
}

async function confirmRemoveUser(userId, name) {
  if (!confirm(`Удалить ${name} из состава команды?`)) return;
  try {
    await api(`/api/users/${userId}`, undefined, 'DELETE');
    showToast('Участник удален');
    await openTeamModal();
  } catch (e) {
    showToast(e.message, 'error');
  }
}

async function syncTeamList() {
  const btn = $('btn-sync-team');
  btn.disabled = true;
  try {
    const data = await api('/api/users/sync', {});
    state.users = data.users || [];
    populateAssigneeSelects();
    renderTeamList(state.users);
    showToast('Состав команды синхронизирован с Telegram-группой');
  } catch (e) {
    showToast(e.message, 'error');
  } finally {
    btn.disabled = false;
  }
}

async function triggerBackup() {
  showToast('Создаем резервную копию базы данных…');
  try {
    const res = await api('/api/admin/backup', {});
    showToast(res.message || 'Резервная копия отправлена в ваш Telegram!');
  } catch (e) {
    showToast(e.message, 'error');
  }
}

// --- UTILS ---
function openModal(id) {
  $(id)?.classList.remove('hidden');
}

function closeModal(id) {
  $(id)?.classList.add('hidden');
}

window.addEventListener('DOMContentLoaded', init);
