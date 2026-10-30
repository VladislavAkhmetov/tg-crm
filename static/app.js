'use strict';

const tg = window.Telegram?.WebApp;
const $ = id => document.getElementById(id);
const STAGES = {
  available: { label: 'Свободно', full: 'Свободные', color: '#8B9198' },
  in_progress: { label: 'В работе', full: 'В работе', color: '#FFC56B' },
  internal_review: { label: 'Проверка', full: 'Проверка лидом', color: '#7CC7FF' },
  client_review: { label: 'Заказчику', full: 'У заказчика', color: '#C9A7FF' },
  export_ready: { label: 'Экспорт', full: 'На экспорт', color: '#FFC56B' },
  completed: { label: 'Готово', full: 'Готово', color: '#C8F27A' },
};
const state = {
  currentUser: null, isAdmin: false, devMode: false, devUserId: null,
  currentTab: 'all', statusFilter: 'all', poolFilter: 'all', searchQuery: '',
  tasks: [], loaded: false, loading: false, mutationVersion: 0, pendingTaskIds: new Set(),
  justTakenTaskId: null, activeModal: null, detailTaskId: null,
  returnToTaskId: null, action: null, formBusy: false, lastFocus: null, filterKind: null,
  collapsedPools: new Set(), users: [], teamRoleFilter: 'all',
};

const AVATAR_PALETTES = [
  { bg: 'radial-gradient(circle at 35% 35%, #22c55e 0%, #064e3b 100%)', fg: '#bbf7d0' },
  { bg: 'radial-gradient(circle at 35% 35%, #0ea5e9 0%, #082f49 100%)', fg: '#bae6fd' },
  { bg: 'radial-gradient(circle at 35% 35%, #f59e0b 0%, #451a03 100%)', fg: '#fde68a' },
  { bg: 'radial-gradient(circle at 35% 35%, #a855f7 0%, #3b0764 100%)', fg: '#e9d5ff' },
  { bg: 'radial-gradient(circle at 35% 35%, #f43f5e 0%, #4c0519 100%)', fg: '#fecdd3' },
];

function getAvatarColor(name) {
  const str = String(name || 'U').trim();
  const code = str.charCodeAt(0) || 0;
  return AVATAR_PALETTES[code % AVATAR_PALETTES.length];
}

function formatSkuParts(sku) {
  const str = String(sku || '').trim();
  const lastDash = str.lastIndexOf('-');
  if (lastDash > 0 && lastDash < str.length - 1) {
    return {
      base: str.slice(0, lastDash + 1),
      variant: str.slice(lastDash + 1),
    };
  }
  return { base: str, variant: '' };
}

function isUserOnline(user) {
  if (!user?.last_active_at) return false;
  try {
    const active = new Date(user.last_active_at).getTime();
    const diff = (Date.now() - active) / (1000 * 60);
    return diff < 60;
  } catch {
    return false;
  }
}

function formatTaskDeadline(task) {
  if (!task.updated_at && !task.created_at) return '';
  try {
    const date = new Date(task.updated_at || task.created_at);
    const now = new Date();
    const isToday = date.toDateString() === now.toDateString();
    if (isToday) {
      const time = date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
      return `<span class="task-deadline-badge is-today">${icon('clock')}сегодня, ${time}</span>`;
    }
    const d = date.getDate().toString().padStart(2, '0');
    const m = (date.getMonth() + 1).toString().padStart(2, '0');
    return `<span class="task-deadline-badge">${icon('clock')}${d}.${m}</span>`;
  } catch {
    return '';
  }
}

function getHeadingSubtitle(tasks) {
  const now = new Date();
  const days = ['Вс', 'Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб'];
  const months = ['января', 'февраля', 'марта', 'апреля', 'мая', 'июня', 'июля', 'августа', 'сентября', 'октября', 'ноября', 'декабря'];
  const dateStr = `${days[now.getDay()]}, ${now.getDate()} ${months[now.getMonth()]}`;
  const onReview = (tasks || []).filter(t => !t.is_archived && (t.status === 'internal_review' || t.status === 'client_review')).length;
  return `${dateStr} · ${onReview} на проверке`;
}

function triggerHaptic(type = 'light') {
  try {
    if (tg?.HapticFeedback) {
      if (type === 'success' || type === 'error' || type === 'warning') {
        tg.HapticFeedback.notificationOccurred(type);
      } else {
        tg.HapticFeedback.impactOccurred(type);
      }
    }
  } catch {}
}

function toggleTaskMoreMenu(event) {
  if (event) event.stopPropagation();
  const menu = $('task-more-menu');
  if (menu) menu.classList.toggle('hidden');
}

function icon(name) {
  return `<svg class="icon" aria-hidden="true"><use href="#i-${name}"/></svg>`;
}
function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char]));
}
function safeUrl(value) {
  try {
    const url = new URL(value);
    return ['https:', 'http:'].includes(url.protocol) ? url.href : '';
  } catch { return ''; }
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
  return task.status === 'in_progress' && /^Правки:|^Требуются правки/.test(task.notes || '');
}
function statusHtml(task) {
  if (task.is_archived) {
    return '<span class="status-chip status-archived"><span class="status-dot"></span>В архиве</span>';
  }
  const stage = STAGES[task.status] || { label: 'Этап', color: 'var(--text-secondary)' };
  return `<span class="status-chip status-${task.status}"><span class="status-dot"></span>${escapeHtml(stage.label)}</span>`;
}
function linkHtml(url, label, symbol, className = 'resource-link') {
  const href = safeUrl(url);
  if (!href) return '';
  return `<a href="${escapeHtml(href)}" target="_blank" rel="noopener noreferrer" class="${className}">${icon(symbol)}<span>${escapeHtml(label)}</span>${className === 'detail-link' ? icon('external') : ''}</a>`;
}
function getAuthHeaders() {
  const headers = { 'Content-Type': 'application/json' };
  if (tg?.initData) headers['X-Telegram-Init-Data'] = tg.initData;
  if (state.devUserId) headers['X-Dev-User-Id'] = String(state.devUserId);
  return headers;
}
async function api(path, body, method) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 20000);
  const httpMethod = method || (body === undefined ? 'GET' : 'POST');
  try {
    const response = await fetch(path, {
      method: httpMethod, headers: getAuthHeaders(),
      ...(body === undefined ? {} : { body: JSON.stringify(body) }), signal: controller.signal,
    });
    const data = await response.json();
    if (!response.ok) {
      const message = typeof data.detail === 'string' ? data.detail : 'Не удалось сохранить. Проверьте данные и повторите.';
      throw new Error(message);
    }
    return data;
  } catch (error) {
    if (error.name === 'AbortError') throw new Error('Сервер не отвечает. Повторите попытку.');
    if (error instanceof TypeError) throw new Error('Нет связи с сервером. Проверьте подключение.');
    if (error instanceof SyntaxError) throw new Error('Сервер вернул некорректный ответ. Повторите попытку.');
    throw error;
  } finally { clearTimeout(timeout); }
}

function updateSafeArea() {
  for (const side of ['top', 'bottom', 'left', 'right']) {
    const inset = (tg?.safeAreaInset?.[side] || 0) + (tg?.contentSafeAreaInset?.[side] || 0);
    document.documentElement.style.setProperty(`--safe-${side}`, `${inset}px`);
  }
}
function telegramSupports(version) {
  return !!tg && (!tg.isVersionAtLeast || tg.isVersionAtLeast(version));
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
  btn.setAttribute('aria-label', isFs ? 'Свернуть' : 'Развернуть');
  btn.setAttribute('title', isFs ? 'Свернуть' : 'Во весь экран');
  const use = btn.querySelector('use');
  if (use) use.setAttribute('href', isFs ? '#i-compress' : '#i-expand');
}

function toggleFullscreen() {
  if (!tg) return;
  try {
    if (tg.isFullscreen) {
      tg.exitFullscreen?.();
    } else {
      tg.requestFullscreen?.();
    }
  } catch (err) {
    console.debug('toggleFullscreen error:', err);
  }
}

function setupTelegram() {
  if (!tg) return;
  try {
    tg.ready();
    tg.expand();
    if (telegramSupports('6.1')) {
      tg.setHeaderColor?.('#0A0B0D');
      tg.setBackgroundColor?.('#0A0B0D');
      tg.BackButton?.onClick(() => closeModal());
    }
    updateSafeArea();
    if (telegramSupports('8.0')) {
      tg.onEvent?.('safeAreaChanged', updateSafeArea);
      tg.onEvent?.('contentSafeAreaChanged', updateSafeArea);
      tg.onEvent?.('fullscreenChanged', () => {
        updateFullscreenUI();
        updateSafeArea();
      });
      tg.onEvent?.('fullscreenFailed', (err) => {
        console.debug('Fullscreen failed:', err);
        updateFullscreenUI();
      });
    }
    updateFullscreenUI();
  } catch (error) { console.debug('Telegram UI:', error); }
}
async function init() {
  setupTelegram();
  let skuCheckTimer;
  $('batch-skus')?.addEventListener('input', () => {
    updateSkuCounter();
    clearTimeout(skuCheckTimer);
    const text = $('batch-skus').value.trim();
    if (!text) {
      renderBatchAnalysis(null);
      return;
    }
    skuCheckTimer = setTimeout(async () => {
      try {
        const parentUrl = $('batch-parent-url').value.trim();
        const res = await api('/api/tasks/check-skus', { skus_text: text, parent_url: safeUrl(parentUrl) ? parentUrl : null });
        if (res.analysis) {
          renderBatchAnalysis(res.analysis);
        }
      } catch (e) {}
    }, 600);
  });
  document.querySelectorAll('.modal-overlay').forEach(overlay => {
    overlay.addEventListener('click', event => { if (event.target === overlay) closeModal(); });
  });
  document.addEventListener('click', event => {
    if (!event.target.closest('#btn-task-more') && !event.target.closest('#task-more-menu')) {
      $('task-more-menu')?.classList.add('hidden');
    }
  });
  document.addEventListener('keydown', handleModalKeydown);
  await retryLoad();
  setInterval(() => {
    if (!document.hidden && state.currentUser && !state.formBusy && !state.pendingTaskIds.size) loadTasks();
  }, 12000);
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden && state.currentUser && !state.formBusy) loadTasks();
  });
}
async function loadMe() {
  const data = await api('/api/me');
  state.currentUser = data.user;
  state.isAdmin = data.is_admin;
  state.devMode = data.dev_mode;
  $('btn-add-pool')?.classList.toggle('hidden', !state.isAdmin);
  $('btn-team')?.classList.toggle('hidden', !state.isAdmin);
  $('btn-export')?.classList.toggle('hidden', !state.isAdmin);
  $('dev-bar')?.classList.toggle('hidden', !state.devMode || !!tg?.initData);
  if (state.isAdmin) {
    loadDesigners().catch(() => {});
  }
}
async function retryLoad() {
  try {
    await loadMe();
    await loadTasks();
  } catch (error) { showLoadError(error.message); }
}
function showLoadError(message) {
  $('load-error-text').textContent = state.loaded ? 'Не удалось обновить задачи. Показаны последние данные.' : message;
  $('load-error').classList.remove('hidden');
  $('tasks-container').setAttribute('aria-busy', 'false');
  if (!state.loaded) $('tasks-container').innerHTML = '';
}
async function loadTasks() {
  if (state.loading) return;
  state.loading = true;
  const mutationVersion = state.mutationVersion;
  try {
    const data = await api('/api/tasks');
    const incomingTasks = data.tasks || [];
    const mergedTasks = incomingTasks.map(incoming => {
      const local = state.tasks.find(t => t.id === incoming.id);
      if (local && local.updated_at && incoming.updated_at) {
        if (new Date(local.updated_at) > new Date(incoming.updated_at)) {
          return local;
        }
      }
      return incoming;
    });
    const changed = !state.loaded || JSON.stringify(state.tasks) !== JSON.stringify(mergedTasks);
    state.tasks = mergedTasks;
    state.loaded = true;
    $('load-error').classList.add('hidden');
    $('tasks-container').setAttribute('aria-busy', 'false');
    if (changed) render();
  } catch (error) { showLoadError(error.message); }
  finally {
    state.loading = false;
    if (mutationVersion !== state.mutationVersion && !state.formBusy && !state.pendingTaskIds.size) await loadTasks();
  }
}
async function switchDevUser(role) {
  if (state.loading || state.formBusy || state.pendingTaskIds.size) return;
  closeModal(false);
  state.devUserId = { admin: 999999999, designer1: 10001, designer2: 10002 }[role];
  state.justTakenTaskId = null;
  try { await loadMe(); await loadTasks(); render(); }
  catch (error) { showLoadError(error.message); }
}

function matchesSearch(task) {
  const query = state.searchQuery;
  if (query && ![task.sku, task.pool_name, task.assigned_to_name, task.assigned_to_username].some(value => String(value || '').toLocaleLowerCase('ru').includes(query))) return false;
  return true;
}
function matchesSearchAndPool(task) {
  if (!matchesSearch(task)) return false;
  return state.poolFilter === 'all' || (task.pool_name || 'Общий пул') === state.poolFilter;
}
function matchesScope(task, tab = state.currentTab) {
  if (state.statusFilter === 'archived') {
    if (!task.is_archived) return false;
    if (tab === 'my') return isMine(task);
    if (tab === 'available') return task.status === 'available';
    return true;
  }
  if (task.is_archived) return false;
  if (tab === 'available') return task.status === 'available' || task.id === state.justTakenTaskId;
  if (tab === 'my') return isMine(task) && (task.status !== 'completed' || state.statusFilter === 'completed');
  return true;
}
function taskPriority(task) {
  if (state.isAdmin) return { internal_review: 0, export_ready: 1, in_progress: isRevision(task) ? 2 : 3, available: 4, client_review: 5, completed: 6 }[task.status] ?? 7;
  if (isMine(task)) return { in_progress: isRevision(task) ? 0 : 1, export_ready: 2, internal_review: 4, client_review: 5, completed: 8 }[task.status] ?? 8;
  return task.status === 'available' ? 3 : 7;
}
function filteredTasks() {
  return state.tasks.filter(task => {
    if (!matchesSearchAndPool(task) || !matchesScope(task)) return false;
    if (state.statusFilter === 'archived') return true;
    return state.statusFilter === 'all' || task.status === state.statusFilter ||
      (state.currentTab === 'available' && task.id === state.justTakenTaskId && state.statusFilter === 'available');
  }).sort((a, b) => taskPriority(a) - taskPriority(b) || b.id - a.id);
}
function setTab(tab) {
  state.currentTab = tab;
  state.statusFilter = 'all';
  state.justTakenTaskId = null;
  render();
}
function setStatus(status) {
  state.statusFilter = status;
  state.justTakenTaskId = null;
  render();
}
function setPool(pool) {
  state.poolFilter = pool;
  state.justTakenTaskId = null;
  render();
}
function handleSearch(query) {
  state.searchQuery = query.toLocaleLowerCase('ru').trim();
  state.justTakenTaskId = null;
  render();
}
function clearSearch() { $('search-input').value = ''; handleSearch(''); $('search-input').focus(); }
function resetFilters() {
  Object.assign(state, { currentTab: 'all', statusFilter: 'all', poolFilter: 'all', searchQuery: '', justTakenTaskId: null });
  $('search-input').value = '';
  render();
}
function poolNames() {
  return [...new Set(state.tasks.map(task => task.pool_name || 'Общий пул'))].sort((a, b) => a.localeCompare(b, 'ru'));
}

function filterOptions(kind) {
  if (kind === 'pool') {
    const tasks = state.tasks.filter(task => matchesSearch(task) && matchesScope(task) &&
      (state.statusFilter === 'all' || state.statusFilter === 'archived' || task.status === state.statusFilter));
    return [{ value: 'all', label: 'Все пулы', count: tasks.length, symbol: 'folder' },
      ...poolNames().map(pool => ({ value: pool, label: pool, count: tasks.filter(task => (task.pool_name || 'Общий пул') === pool).length, symbol: 'folder' }))];
  }
  const base = state.tasks.filter(matchesSearchAndPool);
  const scoped = base.filter(task => !task.is_archived && matchesScope(task));
  const active = state.currentTab === 'my' ? scoped.filter(task => task.status !== 'completed') : scoped;
  const archivedCount = base.filter(task => task.is_archived && (state.currentTab === 'my' ? isMine(task) : true)).length;
  return [{ value: 'all', label: 'Все этапы', count: active.length, symbol: 'grid' },
    ...Object.entries(STAGES).map(([value, stage]) => ({ value, label: stage.full, color: stage.color,
      count: (state.currentTab === 'my' && value === 'completed' ? base.filter(task => !task.is_archived && isMine(task)) : scoped).filter(task => task.status === value).length })),
    { value: 'archived', label: 'В архиве', color: '#64748b', count: archivedCount, symbol: 'archive' }];
}

function filterOptionsHtml(kind) {
  const selected = kind === 'pool' ? state.poolFilter : state.statusFilter;
  return filterOptions(kind).map(option => `<button type="button" class="filter-option" role="option" aria-selected="${option.value === selected}" tabindex="${option.value === selected ? 0 : -1}" data-filter-value="${escapeHtml(option.value)}" onclick="chooseFilterOption(this.dataset.filterValue)">
    <span class="filter-option-marker" aria-hidden="true">${option.color ? `<i class="stage-dot" style="background:${option.color}"></i>` : icon(option.symbol)}</span>
    <span class="filter-option-label">${escapeHtml(option.label)}</span><span class="filter-option-count">${option.count}</span>
    <svg class="icon filter-option-check" aria-hidden="true"><use href="#i-check"/></svg>
  </button>`).join('');
}

function chooseFilterOption(value) {
  if (!filterOptions(state.filterKind || 'status').some(option => option.value === value)) return;
  if (state.filterKind === 'pool') setPool(value);
  else setStatus(value);
  closeModal(false);
}

function openFilterSheet() {
  renderFilterSheetContent();
  openModal('modal-filter');
}

function renderFilterSheetContent() {
  const stageChips = [
    { value: 'all', label: 'Все этапы' },
    { value: 'available', label: 'Свободно', color: '#8B9198' },
    { value: 'in_progress', label: 'В работе', color: '#FFC56B' },
    { value: 'internal_review', label: 'Проверка', color: '#7CC7FF' },
    { value: 'client_review', label: 'Заказчику', color: '#C9A7FF' },
    { value: 'export_ready', label: 'Экспорт', color: '#FFC56B' },
    { value: 'completed', label: 'Готово', color: '#C8F27A' },
    { value: 'archived', label: 'В архиве', color: '#5C6268' },
  ];

  const stageContainer = $('filter-stage-options');
  if (stageContainer) {
    stageContainer.innerHTML = stageChips.map(opt => {
      const isAct = state.statusFilter === opt.value;
      const dot = opt.color ? `<span class="stage-dot" style="background: ${opt.color}"></span>` : '';
      return `<button type="button" class="filter-chip ${isAct ? 'active' : ''}" onclick="selectFilterStage('${opt.value}')">${dot}<span>${escapeHtml(opt.label)}</span></button>`;
    }).join('');
  }

  const pools = poolNames();
  const poolSection = $('filter-pool-section');
  if (poolSection) {
    poolSection.classList.toggle('hidden', pools.length < 2);
  }
  const poolContainer = $('filter-pool-options');
  if (poolContainer) {
    const poolChips = [{ value: 'all', label: 'Все пулы' }, ...pools.map(p => ({ value: p, label: cleanPoolTitle(p) }))];
    poolContainer.innerHTML = poolChips.map(opt => {
      const isAct = state.poolFilter === opt.value;
      return `<button type="button" class="filter-chip ${isAct ? 'active' : ''}" onclick="selectFilterPool('${escapeHtml(opt.value)}')"><span>${escapeHtml(opt.label)}</span></button>`;
    }).join('');
  }
}

function selectFilterStage(value) {
  state.statusFilter = value;
  renderFilterSheetContent();
  render();
}

function selectFilterPool(value) {
  state.poolFilter = value;
  renderFilterSheetContent();
  render();
}

function resetAllFiltersFromSheet() {
  state.statusFilter = 'all';
  state.poolFilter = 'all';
  state.searchQuery = '';
  $('search-input').value = '';
  closeModal(false);
  render();
}

function openFilter(kind) {
  openFilterSheet();
}

function updateFilterExpanded() {}

function updateFilters() {
  const pools = poolNames();
  if (state.poolFilter !== 'all' && !pools.includes(state.poolFilter)) state.poolFilter = 'all';

  // Subtitle & Header count
  const countEl = $('heading-task-count');
  if (countEl) countEl.textContent = state.tasks.length ? String(state.tasks.length) : '';
  const subEl = $('heading-subtitle');
  if (subEl) subEl.textContent = getHeadingSubtitle(state.tasks);

  // Filter indicator dot on search row
  const hasActiveFilter = state.statusFilter !== 'all' || state.poolFilter !== 'all';
  const filterDot = $('filter-indicator-dot');
  if (filterDot) filterDot.classList.toggle('hidden', !hasActiveFilter);

  // Segment tabs
  const base = state.tasks.filter(matchesSearchAndPool);
  for (const tab of ['all', 'my', 'available']) {
    const count = base.filter(task => {
      if (task.is_archived) return false;
      if (tab === 'available') return task.status === 'available';
      if (tab === 'my') return isMine(task) && task.status !== 'completed';
      return true;
    }).length;
    const badge = $(`badge-${tab}`);
    if (badge) badge.textContent = count;
    const tabBtn = $(`tab-${tab}`);
    if (tabBtn) {
      tabBtn.classList.toggle('active', state.currentTab === tab);
      tabBtn.setAttribute('aria-pressed', String(state.currentTab === tab));
    }
  }

  $('search-clear-btn')?.classList.toggle('hidden', !state.searchQuery);

  // Header avatar stack
  if (state.users && state.users.length) {
    const stack = $('header-avatar-stack');
    if (stack) {
      const top3 = state.users.slice(0, 3);
      stack.innerHTML = top3.map(u => {
        const name = u.first_name || 'U';
        const pal = getAvatarColor(name);
        return `<div class="avatar-stack-item" style="background:${pal.bg}; color:${pal.fg};">${escapeHtml(name.trim()[0] || 'U')}</div>`;
      }).join('');
      const countLabel = $('header-team-count');
      if (countLabel) countLabel.textContent = `+${state.users.length}`;
    }
  }

  renderPoolActionsBar();
}
function renderPoolStageSummary(poolName) {
  const allPoolTasks = state.tasks.filter(t => (t.pool_name || 'Общий пул') === poolName && !t.is_archived);
  if (!allPoolTasks.length) return '';
  const counts = { available: 0, in_progress: 0, internal_review: 0, client_review: 0, export_ready: 0, completed: 0 };
  for (const t of allPoolTasks) {
    if (counts[t.status] !== undefined) counts[t.status]++;
  }
  const pills = [];
  if (counts.available > 0) pills.push(`<span class="pool-stat-dot" title="Свободно: ${counts.available}"><i class="stage-dot" style="background:#94a3b8"></i>${counts.available}</span>`);
  if (counts.in_progress > 0) pills.push(`<span class="pool-stat-dot" title="В работе: ${counts.in_progress}"><i class="stage-dot" style="background:#eab308"></i>${counts.in_progress}</span>`);
  if (counts.internal_review > 0) pills.push(`<span class="pool-stat-dot" title="На проверке: ${counts.internal_review}"><i class="stage-dot" style="background:#38bdf8"></i>${counts.internal_review}</span>`);
  if (counts.client_review > 0) pills.push(`<span class="pool-stat-dot" title="У заказчика: ${counts.client_review}"><i class="stage-dot" style="background:#c084fc"></i>${counts.client_review}</span>`);
  if (counts.export_ready > 0) pills.push(`<span class="pool-stat-dot" title="Экспорт: ${counts.export_ready}"><i class="stage-dot" style="background:#fb923c"></i>${counts.export_ready}</span>`);
  if (counts.completed > 0) pills.push(`<span class="pool-stat-dot" title="Готово: ${counts.completed}"><i class="stage-dot" style="background:#4ade80"></i>${counts.completed}</span>`);
  return pills.join('');
}

function cleanPoolTitle(poolName) {
  if (!poolName) return 'Общий пул';
  const trimmed = String(poolName).trim();
  const cleaned = trimmed.replace(/^(?:задание|задача)\s+/i, '');
  return cleaned || trimmed;
}

function renderGroupedTasksHtml(tasks) {
  if (!tasks.length) return '';
  const poolGroups = new Map();
  for (const task of tasks) {
    const poolName = task.pool_name || 'Общий пул';
    if (!poolGroups.has(poolName)) poolGroups.set(poolName, []);
    poolGroups.get(poolName).push(task);
  }

  const html = [];
  for (const [poolName, groupTasks] of poolGroups.entries()) {
    const isCollapsed = state.collapsedPools.has(poolName) && !state.searchQuery;
    const stagesHtml = renderPoolStageSummary(poolName);
    const allPoolTasks = state.tasks.filter(t => (t.pool_name || 'Общий пул') === poolName && !t.is_archived);
    const totalInPool = allPoolTasks.length;
    const doneInPool = allPoolTasks.filter(t => t.status === 'client_review' || t.status === 'export_ready' || t.status === 'completed').length;
    const pct = totalInPool ? Math.round((doneInPool / totalInPool) * 100) : 0;

    html.push(`<section class="pool-group" data-pool-name="${escapeHtml(poolName)}">
      <div class="pool-group-header">
        <button type="button" class="pool-group-toggle" onclick="togglePoolCollapse('${escapeHtml(poolName)}')" aria-expanded="${!isCollapsed}" aria-label="Свернуть или развернуть пул ${escapeHtml(poolName)}">
          <svg class="icon pool-chevron ${isCollapsed ? 'is-collapsed' : ''}"><use href="#i-chevron"/></svg>
          <span class="pool-group-name">${escapeHtml(cleanPoolTitle(poolName))}</span>
          <span class="pool-group-meta-info">${totalInPool} арт.</span>
        </button>
        <div class="pool-group-meta-right">
          <div class="pool-group-stages" style="display:none">${stagesHtml}</div>
          <div class="pool-progress-wrap" title="Завершено: ${doneInPool} из ${totalInPool}">
            <div class="pool-progress-track">
              <div class="pool-progress-fill" style="width: ${pct}%"></div>
            </div>
            <span class="pool-progress-text">${doneInPool}/${totalInPool}</span>
          </div>
          ${state.isAdmin ? `<button type="button" class="pool-edit-trigger" onclick="openEditPoolModal('${escapeHtml(poolName)}')" title="Редактировать пул" aria-label="Редактировать пул ${escapeHtml(poolName)}"><svg class="icon"><use href="#i-edit"/></svg></button>` : ''}
        </div>
      </div>
      <div class="pool-group-tasks ${isCollapsed ? 'hidden' : ''}">
        ${groupTasks.map(createTaskCardHtml).join('')}
      </div>
    </section>`);
  }
  return html.join('');
}

function togglePoolCollapse(poolName) {
  if (state.collapsedPools.has(poolName)) {
    state.collapsedPools.delete(poolName);
  } else {
    state.collapsedPools.add(poolName);
  }
  render();
}

function render() {
  updateFilters();
  const tasks = filteredTasks();
  const focusedKey = document.activeElement?.dataset.focusKey;
  $('tasks-container').innerHTML = renderGroupedTasksHtml(tasks);
  if (focusedKey) document.querySelector(`[data-focus-key="${focusedKey}"]`)?.focus({ preventScroll: true });
  $('empty-state').classList.toggle('hidden', tasks.length > 0);
  if (!tasks.length) {
    const filtered = state.searchQuery || state.statusFilter !== 'all' || state.poolFilter !== 'all';
    $('empty-title').textContent = filtered ? (state.statusFilter === 'archived' ? 'В архиве пусто' : 'Ничего не найдено') : state.currentTab === 'my' ? 'Активных задач нет' : state.currentTab === 'available' ? 'Свободных задач нет' : 'Задач пока нет';
    $('empty-description').textContent = filtered ? (state.statusFilter === 'archived' ? 'Архивированные задачи появятся здесь.' : 'Попробуйте другой запрос или снимите фильтры.') : state.currentTab === 'my' ? 'Выберите задачу из свободных.' : state.currentTab === 'available' ? 'Все задачи уже взяты в работу.' : state.isAdmin ? 'Добавьте первый пул артикулов.' : 'Админ ещё не добавил задачи.';
    $('empty-action').classList.toggle('hidden', !state.tasks.length);
  }
  if (state.activeModal === 'modal-task') renderTaskDetail();
  if (state.activeModal === 'modal-filter') renderFilterOptions();
}

function primaryAction(task) {
  if (task.is_archived) return null;
  const canEdit = isMine(task) || state.isAdmin;
  if (task.status === 'available') return { label: 'Взять в работу', action: 'take' };
  if (task.status === 'in_progress' && canEdit) return { label: 'На проверку ↗', action: 'internal' };
  if (task.status === 'internal_review' && state.isAdmin) return { label: 'Заказчику ↗', action: 'client' };
  if (task.status === 'client_review' && state.isAdmin) return { label: 'Согласовано ↗', action: 'export' };
  if (task.status === 'export_ready' && canEdit) return { label: 'Завершить', action: 'complete' };
  if (task.status === 'completed' && safeUrl(task.yandex_disk_url)) return { label: 'Файлы ↗', url: task.yandex_disk_url };
  return null;
}
function actionButtonHtml(task, detail = false) {
  const action = primaryAction(task);
  if (!action) return detail ? '' : `<button class="btn btn-secondary btn-sm" onclick="openTaskDetail(${task.id})" data-focus-key="detail-${task.id}">Открыть${icon('chevron')}</button>`;
  if (action.url) return `<a href="${escapeHtml(safeUrl(action.url))}" target="_blank" rel="noopener noreferrer" class="btn ${detail ? 'btn-primary-l' : 'btn-primary btn-sm'}" onclick="event.stopPropagation()">${action.label}</a>`;
  const disabled = state.pendingTaskIds.has(task.id);
  const clickHandler = action.action === 'take' ? `takeTask(${task.id})` : `openAction('${action.action}', ${task.id})`;
  return `<button class="btn ${detail ? 'btn-primary-l' : 'btn-primary btn-sm'}" onclick="event.stopPropagation(); ${clickHandler}" data-focus-key="action-${task.id}" ${disabled ? 'disabled' : ''}>${disabled ? 'Сохраняем…' : action.label}</button>`;
}
function createTaskCardHtml(task) {
  const parts = formatSkuParts(task.sku);
  const notes = taskNotes(task);
  const figma = ['client_review', 'export_ready', 'completed'].includes(task.status) ? task.client_figma_url || task.internal_figma_url : task.internal_figma_url || task.client_figma_url;
  const imagesBadge = task.images_count ? `<span class="chip-neutral badge-images" title="Количество фото: ${task.images_count}">${task.images_count} фото</span>` : '';
  const statusBadge = statusHtml(task);

  const assignee = task.assigned_to_id && task.status !== 'available' ? (isMine(task) ? 'Вы' : assigneeName(task)) : 'Свободно';
  const pal = getAvatarColor(assignee);
  const initial = assignee.trim()[0] || 'U';
  const deadlineBadge = formatTaskDeadline(task);

  const folderBtn = task.source_url && safeUrl(task.source_url) ?
    `<a href="${escapeHtml(safeUrl(task.source_url))}" target="_blank" rel="noopener noreferrer" class="card-icon-link" title="Исходники в облаке" onclick="event.stopPropagation()">${icon('folder')}</a>` : '';
  const figmaBtn = figma && safeUrl(figma) ?
    `<a href="${escapeHtml(safeUrl(figma))}" target="_blank" rel="noopener noreferrer" class="card-icon-link" title="Макет в Figma" onclick="event.stopPropagation()">${icon('figma')}</a>` : '';

  return `<article id="task-card-${task.id}" class="crm-card${task.id === state.justTakenTaskId ? ' just-taken' : ''}">
    <button class="task-open" onclick="openTaskDetail(${task.id})" data-focus-key="task-${task.id}" aria-label="Открыть задачу ${escapeHtml(task.sku)}">
      <div class="task-card-top">
        <div class="task-sku-display">
          <span class="task-sku-base">${escapeHtml(parts.base)}</span><span class="task-sku-variant">${escapeHtml(parts.variant || parts.base)}</span>
        </div>
        <div class="task-badges-row task-badges">
          ${statusBadge}
          ${imagesBadge}
        </div>
      </div>
      <div class="task-assignee-row" style="margin-top: 10px;">
        <div class="task-assignee-user" title="${escapeHtml(assigneeName(task))}">
          <div class="user-avatar-sm" style="background: ${pal.bg}; color: ${pal.fg};">${escapeHtml(initial)}</div>
          <span>${isMine(task) ? 'Вы' : escapeHtml(assigneeName(task))}</span>
        </div>
        ${deadlineBadge}
      </div>
    </button>
    ${notes ? `<button class="note-preview${isRevision(task) ? ' is-revision' : ''}" onclick="openTaskDetail(${task.id})" aria-label="Открыть комментарий к ${escapeHtml(task.sku)}">${icon('comment')}<span>${escapeHtml(notes)}</span></button>` : ''}
    <div class="task-footer-row">
      <div class="task-footer-icons">
        ${folderBtn}
        ${figmaBtn}
      </div>
      ${actionButtonHtml(task)}
    </div>
  </article>`;
}

function renderStepperHtml(task) {
  return '';
}

function renderMaterialsHtml(task) {
  const rows = [];
  if (task.source_url) {
    rows.push(`
      <a href="${escapeHtml(safeUrl(task.source_url))}" target="_blank" rel="noopener noreferrer" class="material-item-row">
        <div class="material-tile-icon material-tile-amber">${icon('folder')}</div>
        <div class="material-info">
          <span class="material-name">Исходные фото</span>
          <span class="material-meta">Облако · Исходники</span>
        </div>
        ${icon('arrow-up-right')}
      </a>
    `);
  }
  if (task.internal_figma_url) {
    rows.push(`
      <a href="${escapeHtml(safeUrl(task.internal_figma_url))}" target="_blank" rel="noopener noreferrer" class="material-item-row">
        <div class="material-tile-icon material-tile-violet">${icon('figma')}</div>
        <div class="material-info">
          <span class="material-name">Макет в Figma</span>
          <span class="material-meta">Внутренний макет</span>
        </div>
        ${icon('arrow-up-right')}
      </a>
    `);
  }
  if (task.client_figma_url) {
    rows.push(`
      <a href="${escapeHtml(safeUrl(task.client_figma_url))}" target="_blank" rel="noopener noreferrer" class="material-item-row">
        <div class="material-tile-icon material-tile-violet">${icon('figma')}</div>
        <div class="material-info">
          <span class="material-name">Figma заказчика</span>
          <span class="material-meta">Внешняя ссылка</span>
        </div>
        ${icon('arrow-up-right')}
      </a>
    `);
  }
  if (task.yandex_disk_url) {
    rows.push(`
      <a href="${escapeHtml(safeUrl(task.yandex_disk_url))}" target="_blank" rel="noopener noreferrer" class="material-item-row">
        <div class="material-tile-icon material-tile-green">${icon('file')}</div>
        <div class="material-info">
          <span class="material-name">Финалы на Яндекс.Диске</span>
          <span class="material-meta">Готовые файлы</span>
        </div>
        ${icon('arrow-up-right')}
      </a>
    `);
  }
  if (task.bitrix_url) {
    rows.push(`
      <a href="${escapeHtml(safeUrl(task.bitrix_url))}" target="_blank" rel="noopener noreferrer" class="material-item-row detail-link-bitrix" onclick="handleBitrixLinkClick(event, ${task.id})">
        <div class="material-tile-icon material-tile-sky">${icon('bitrix')}</div>
        <div class="material-info">
          <span class="material-name">Битрикс24 (учет времени)</span>
          <span class="material-meta">Учёт времени · клик копирует SKU</span>
        </div>
        ${icon('arrow-up-right')}
      </a>
    `);
  }
  if (!rows.length) return '';
  return `
    <div class="detail-section-heading">
      <h3 class="detail-section-title">Материалы</h3>
    </div>
    <div class="materials-group-card">${rows.join('')}</div>
  `;
}

function renderStickyBottomBar(task) {
  const action = primaryAction(task);
  const canEdit = isMine(task) || state.isAdmin;
  const disabled = state.pendingTaskIds.has(task.id);

  const secondaryButtons = [];

  // Rework / Reject button
  if (!task.is_archived && canEdit && ['internal_review', 'client_review'].includes(task.status)) {
    const reworkLabel = state.isAdmin ? 'Вернуть на правки' : 'Забрать на доработку';
    secondaryButtons.push(`
      <button class="btn btn-secondary-l" onclick="openAction('reject', ${task.id})">
        <span>${reworkLabel}</span>
      </button>
    `);
  }

  // Admin Assign / Reassign button
  if (!task.is_archived && state.isAdmin) {
    const isMultiBtn = task.status === 'in_progress' && canEdit;
    const assignText = isMultiBtn ? 'Переназначить' : (task.status === 'available' ? 'Назначить дизайнеру' : 'Переназначить задачу');
    const assignTitle = task.status === 'available' ? 'Назначить дизайнеру' : 'Переназначить задачу';
    secondaryButtons.push(`
      <button class="btn btn-secondary-l btn-assign-task" onclick="openAction('assign', ${task.id})" title="${assignTitle}">
        ${icon('reassign')}<span>${assignText}</span>
      </button>
    `);
  }

  // Return to pool (in progress: designer or admin)
  if (!task.is_archived && task.status === 'in_progress' && canEdit) {
    const returnClass = state.isAdmin ? 'btn btn-secondary-l' : 'btn btn-ghost';
    const returnLabel = state.isAdmin ? 'В свободные' : 'Вернуть в свободные';
    secondaryButtons.push(`
      <button class="${returnClass}" onclick="openAction('return', ${task.id})" title="Вернуть в свободные">
        ${icon('refresh')}<span>${returnLabel}</span>
      </button>
    `);
  }

  let primaryBtn = '';
  if (action) {
    if (action.url) {
      primaryBtn = `<a href="${escapeHtml(safeUrl(action.url))}" target="_blank" rel="noopener noreferrer" class="btn btn-primary-l btn-wide">${action.label}</a>`;
    } else {
      const clickHandler = action.action === 'take' ? `takeTask(${task.id})` : `openAction('${action.action}', ${task.id})`;
      primaryBtn = `
        <button class="btn btn-primary-l btn-wide" onclick="${clickHandler}" ${disabled ? 'disabled' : ''}>
          <span>${disabled ? 'Сохраняем…' : action.label}</span>
        </button>
      `;
    }
  }

  if (!secondaryButtons.length && !primaryBtn) return '';
  return `
    <div class="detail-sticky-bar">
      ${secondaryButtons.length ? `<div class="detail-sticky-secondary-row">${secondaryButtons.join('')}</div>` : ''}
      ${primaryBtn}
    </div>
  `;
}

function openTaskDetail(taskId) {
  state.detailTaskId = taskId;
  renderTaskDetail();
  openModal('modal-task');
}
async function handleBitrixLinkClick(event, taskId) {
  const task = state.tasks.find(item => item.id === taskId);
  if (!task?.sku) return;
  try {
    if (navigator?.clipboard?.writeText) {
      await navigator.clipboard.writeText(task.sku);
      showToast(`Артикул ${task.sku} скопирован для учета времени`);
    }
  } catch {}
}

function renderTaskDetail() {
  const task = state.tasks.find(item => item.id === state.detailTaskId);
  if (!task) { closeModal(false); return; }
  const notes = isRevision(task) ? taskNotes(task).replace(/^Правки:\s*/, '') : taskNotes(task);
  const canEdit = isMine(task) || state.isAdmin;

  const poolBadge = $('task-sheet-pool-badge');
  if (poolBadge) {
    poolBadge.innerHTML = `${icon('folder')}<span>${escapeHtml(cleanPoolTitle(task.pool_name || 'Общий пул'))}</span>`;
  }

  const moreMenu = $('task-more-menu');
  if (moreMenu) {
    const items = [];
    if (state.isAdmin) {
      if (!task.is_archived && task.status !== 'available') {
        items.push(`
          <button type="button" class="task-more-menu-item" onclick="openAction('return', ${task.id}); toggleTaskMoreMenu();">
            ${icon('refresh')}
            <span>Вернуть в свободные</span>
          </button>
        `);
      }
      items.push(`
        <button type="button" class="task-more-menu-item" onclick="toggleTaskArchive(${task.id}, ${task.is_archived ? 0 : 1}); toggleTaskMoreMenu();">
          ${icon(task.is_archived ? 'unarchive' : 'archive')}
          <span>${task.is_archived ? 'Восстановить' : 'В архив'}</span>
        </button>
      `);
      items.push(`
        <button type="button" class="task-more-menu-item danger" onclick="confirmDeleteTask(${task.id}); toggleTaskMoreMenu();">
          ${icon('trash')}
          <span>Удалить задачу</span>
        </button>
      `);
    }
    moreMenu.innerHTML = items.length ? items.join('') : '<div class="muted" style="padding: 8px; font-size: 13px;">Нет доступных действий</div>';
  }

  const parts = formatSkuParts(task.sku);
  const assignee = task.assigned_to_id && task.status !== 'available' ? (isMine(task) ? 'Вы' : assigneeName(task)) : 'Не назначен';
  const pal = getAvatarColor(assignee);
  const initial = assignee.trim()[0] || 'U';

  const imagesBlock = `
    <div class="detail-images-block">
      <div class="detail-section-heading">
        <h3 class="detail-section-title">Изображения ${task.images_count ? task.images_count : ''}</h3>
        ${canEdit ? `<button type="button" class="btn-text-accent" onclick="openAction('images', ${task.id})">✎ Изменить</button>` : ''}
      </div>
      <div class="images-summary-box">
        <div class="images-summary-info">
          ${icon('images')}
          <span>В паке: <strong class="images-count-highlight">${task.images_count ? `${task.images_count} шт.` : 'не указано'}</strong></span>
        </div>
        <span class="muted" style="font-size:12.5px;">Исходники в облаке</span>
      </div>
    </div>
  `;

  $('task-detail-content').innerHTML = `
    <div class="task-detail-sku-row">
      <h2 id="task-detail-title" class="task-detail-sku mono">${escapeHtml(task.sku)}</h2>
      <button class="task-sku-copy-btn" onclick="copyTaskSku(${task.id})" aria-label="Скопировать артикул" title="Скопировать артикул">
        ${icon('copy')}
      </button>
    </div>

    <div class="task-detail-meta-group">
      <div class="task-detail-meta-item">
        ${statusHtml(task)}
      </div>
      <div class="task-detail-meta-item task-assignee-user">
        <div class="user-avatar-sm" style="background: ${pal.bg}; color: ${pal.fg};">${escapeHtml(initial)}</div>
        <strong style="color: var(--text-primary); font-size: 14px;">${escapeHtml(assignee)}</strong>
      </div>
      <div class="task-detail-meta-item">
        ${formatTaskDeadline(task)}
      </div>
    </div>

    ${imagesBlock}

    ${notes ? `
      <section style="margin-bottom: 20px;">
        <div class="detail-section-heading">
          <h3 class="detail-section-title">${isRevision(task) ? 'Правки' : 'Комментарий'}</h3>
        </div>
        <p class="detail-note${isRevision(task) ? ' is-revision' : ''}">${escapeHtml(notes)}</p>
      </section>
    ` : ''}

    ${renderMaterialsHtml(task)}

    ${renderStickyBottomBar(task)}
  `;
}

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
function urlField(label, value = '', required = true) {
  return `<label class="field-label" for="action-url">${label}<input id="action-url" type="url" inputmode="url" value="${escapeHtml(value)}" ${required ? 'required' : ''}></label>`;
}
async function openAction(kind, taskId) {
  if (state.formBusy) return;
  const task = state.tasks.find(item => item.id === taskId);
  const config = ACTIONS[kind];
  if (!task || !config) return;

  if (kind === 'assign' && (!state.users || !state.users.length)) {
    await loadDesigners();
  }

  state.returnToTaskId = state.activeModal === 'modal-task' ? taskId : null;
  state.action = { kind, taskId };
  const isDesignerRework = kind === 'reject' && !state.isAdmin;
  const isReassign = kind === 'assign' && task.status !== 'available';
  $('action-title').textContent = isDesignerRework ? 'Забрать на доработку' : (isReassign ? 'Переназначить задачу' : config.title);
  $('action-sku').textContent = task.sku;
  $('action-error').classList.add('hidden');
  $('action-submit').textContent = isDesignerRework ? 'Забрать на доработку' : (isReassign ? 'Переназначить' : config.submit);
  $('action-submit').className = `btn ${['reject', 'return'].includes(kind) ? 'btn-danger' : 'btn-primary'} btn-wide`;
  let fields = '';
  if (kind === 'internal') {
    fields = urlField('Ссылка на макет в Figma', task.internal_figma_url || '') +
      `<label class="field-label" for="action-images-count">Количество изображений в 1 задании <span class="required-star">*</span><input id="action-images-count" type="number" min="1" max="500" inputmode="numeric" value="${escapeHtml(task.images_count || '')}" placeholder="Например: 10" required><span id="action-images-count-error" class="field-error-msg hidden"></span></label>` +
      '<label class="field-label" for="action-notes">Комментарий<span class="field-hint">Необязательно</span><textarea id="action-notes" rows="3"></textarea></label>';
  }
  if (kind === 'images') {
    fields = `<label class="field-label" for="action-images-count">Количество изображений в 1 задании <span class="required-star">*</span><input id="action-images-count" type="number" min="1" max="500" inputmode="numeric" value="${escapeHtml(task.images_count || '')}" placeholder="Например: 10" required autofocus><span id="action-images-count-error" class="field-error-msg hidden"></span></label>`;
  }
  if (kind === 'client') fields = urlField('Figma заказчика', task.client_figma_url || '', false) + '<p class="muted">Ссылку можно оставить пустой, если макет уже передан.</p>';
  if (kind === 'complete') fields = urlField('Финалы на Яндекс.Диске', task.yandex_disk_url || '');
  if (kind === 'reject') fields = '<label class="field-label" for="action-notes">Правки<span class="field-hint">Необязательно</span><textarea id="action-notes" rows="4" placeholder="Что нужно исправить (необязательно)"></textarea></label>';
  if (kind === 'export') fields = '<p class="muted">После подтверждения дизайнер сможет загрузить финальные файлы.</p>';
  if (kind === 'return') fields = '<p class="muted">Другой дизайнер сможет взять эту задачу.</p>';
  if (kind === 'assign') {
    const currentAssigneeId = task.assigned_to_id;
    const designers = (state.users || []).filter(u => u.is_approved !== 0);
    const activeMap = getActiveWorkloadMap();

    const designerRows = designers.map(u => {
      const isSelected = u.telegram_id === currentAssigneeId;
      const count = activeMap[u.telegram_id] ?? u.active_tasks_count ?? 0;
      const countLabel = count > 0 ? `${count} в работе` : 'свободен';
      const name = `${u.first_name || 'Участник'} ${u.last_name || ''}`.trim();
      const initial = (u.first_name || 'U').trim()[0].toUpperCase();
      const username = u.username ? `@${u.username}` : '';
      const roleBadge = u.role === 'admin' ? '<span class="badge-role-admin">Админ</span>' : '';

      return `
        <label class="assignee-card${isSelected ? ' is-selected' : ''}" data-id="${u.telegram_id}">
          <input type="radio" name="assignee_radio" value="${u.telegram_id}" ${isSelected ? 'checked' : ''} onchange="onAssigneeRadioChange(this)">
          <div class="team-avatar" aria-hidden="true">${escapeHtml(initial)}</div>
          <div class="assignee-card-info">
            <div class="assignee-card-name">
              <strong>${escapeHtml(name)}</strong>
              ${roleBadge}
            </div>
            <p class="assignee-card-sub">${escapeHtml(username)}</p>
          </div>
          <div class="assignee-workload-badge ${count > 0 ? 'is-busy' : 'is-free'}">
            ${escapeHtml(countLabel)}
          </div>
        </label>
      `;
    }).join('');

    fields = `
      <div class="field-label" style="margin-bottom: 6px;">Выберите исполнителя:</div>
      <input type="hidden" id="action-designer-id" value="${currentAssigneeId || ''}">
      <div class="assignee-picker-list" role="radiogroup">
        ${designerRows || '<p class="muted">Нет доступных дизайнеров в базе</p>'}
      </div>
      ${task.status !== 'available' && currentAssigneeId ? `
        <p class="muted" style="margin-top: 10px; font-size: 13.5px;">
          ⚠️ Текущий исполнитель: <b>${escapeHtml(assigneeName(task))}</b>. Задача будет переведена в статус «В работе» новому исполнителю.
        </p>
        <div style="margin-top: 12px; text-align: center;">
          <button type="button" class="btn btn-ghost btn-sm" onclick="closeModal(); openAction('return', ${task.id});" style="width: 100%;">
            ${icon('refresh')} <span>Или вернуть задачу в свободные</span>
          </button>
        </div>
      ` : ''}
    `;
  }
  $('action-fields').innerHTML = fields;
  openModal('modal-action');

  if (kind === 'assign') {
    const currentVal = $('action-designer-id')?.value;
    const submitBtn = $('action-submit');
    if (submitBtn) {
      submitBtn.disabled = !currentVal;
      submitBtn.classList.toggle('btn-disabled-hint', !currentVal);
    }
  }

  if (['internal', 'images'].includes(kind)) {
    const imgInput = $('action-images-count');
    const submitBtn = $('action-submit');
    const updateValidity = () => {
      const val = parseInt(imgInput?.value, 10);
      const ok = !isNaN(val) && val >= 1 && val <= 500;
      submitBtn?.classList.toggle('btn-disabled-hint', !ok);
      if (ok) {
        imgInput?.classList.remove('field-error-highlight');
        const errEl = $('action-images-count-error');
        if (errEl) errEl.classList.add('hidden');
      }
      return ok;
    };
    imgInput?.addEventListener?.('input', updateValidity);
    updateValidity();
  }
}

function onAssigneeRadioChange(radio) {
  if ($('action-designer-id')) {
    $('action-designer-id').value = radio.value;
  }
  document.querySelectorAll('.assignee-card').forEach(card => {
    card.classList.toggle('is-selected', card.dataset.id === String(radio.value));
  });
  const submitBtn = $('action-submit');
  if (submitBtn) {
    submitBtn.disabled = false;
    submitBtn.classList.remove('btn-disabled-hint');
  }
}
function applyTask(task) {
  if (!task) throw new Error('Сервер не вернул обновлённую задачу. Обновите список.');
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
    state.justTakenTaskId = taskId;
    showToast('Задача у вас в работе');
    try { if (telegramSupports('6.1')) tg.HapticFeedback?.notificationOccurred('success'); } catch {}
  } catch (error) {
    state.pendingTaskIds.delete(taskId);
    showToast(error.message, 'error');
    await loadTasks();
  }
  finally { state.pendingTaskIds.delete(taskId); render(); }
}
async function submitAction(event) {
  event.preventDefault();
  if (state.formBusy || !state.action) return;
  const { kind, taskId } = state.action;
  const config = ACTIONS[kind];
  const body = { task_id: taskId };
  if (['internal', 'images'].includes(kind)) {
    const imgInput = $('action-images-count');
    const countVal = parseInt(imgInput ? imgInput.value : '', 10);
    if (isNaN(countVal) || countVal < 1 || countVal > 500) {
      if (imgInput) {
        imgInput.classList.add('field-error-highlight');
        imgInput.classList.remove('shake-animate');
        void imgInput.offsetWidth;
        imgInput.classList.add('shake-animate');
        imgInput.focus();
      }
      const errEl = $('action-images-count-error');
      if (errEl) {
        errEl.textContent = '⚠️ Обязательно укажите количество карточек (от 1 до 500)';
        errEl.classList.remove('hidden');
      }
      try { if (telegramSupports('6.1')) tg.HapticFeedback?.notificationOccurred('error'); } catch {}
      return;
    }
    body.images_count = countVal;
    if (kind === 'internal') {
      body.internal_figma_url = $('action-url').value.trim();
      body.notes = $('action-notes').value.trim();
    }
  }
  if (kind === 'client') body.client_figma_url = $('action-url').value.trim() || null;
  if (kind === 'complete') body.yandex_disk_url = $('action-url').value.trim();
  if (kind === 'reject') { body.return_to_pool = false; body.comment = $('action-notes').value.trim(); }
  if (kind === 'return') body.return_to_pool = true;
  if (kind === 'assign') {
    const designerVal = $('action-designer-id')?.value;
    const designerId = designerVal ? parseInt(designerVal, 10) : 0;
    if (!designerId) {
      showFormError('action-error', 'Пожалуйста, выберите дизайнера из списка');
      return;
    }
    body.designer_id = designerId;
  }
  if (['internal', 'client', 'complete'].includes(kind) && $('action-url').value.trim() && !safeUrl($('action-url').value.trim())) {
    showFormError('action-error', 'Укажите ссылку, которая начинается с https:// или http://.'); return;
  }
  setFormBusy(true, 'action-submit');
  try {
    const endpoint = kind === 'images' ? `/api/tasks/${taskId}/images-count` : `/api/tasks/${config.endpoint}`;
    const data = await api(endpoint, body);
    applyTask(data.task);
    state.formBusy = false;
    closeModal();
    render();
    const isDesignerRework = kind === 'reject' && !state.isAdmin;
    showToast(isDesignerRework ? 'Задача возвращена на доработку' : config.message);
  } catch (error) { showFormError('action-error', error.message); }
  finally {
    const isDesignerRework = kind === 'reject' && !state.isAdmin;
    setFormBusy(false, 'action-submit');
    $('action-submit').textContent = isDesignerRework ? 'Забрать на доработку' : config.submit;
  }
}
function setFormBusy(busy, buttonId) {
  state.formBusy = busy;
  const button = $(buttonId);
  button.disabled = busy;
  if (busy) button.textContent = 'Сохраняем…';
  const form = button.closest('form');
  form?.querySelectorAll('input, textarea, button').forEach(field => { field.disabled = busy; });
}
function showFormError(id, message) { $(id).textContent = message; $(id).classList.remove('hidden'); }
function updateSkuCounter() {
  $('skus-counter').textContent = $('batch-skus').value.split(/[\n,;]+/).filter(value => value.trim()).length;
}

function renderBatchAnalysis(analysis, isSingle = false, singleSku = null) {
  state.lastBatchAnalysis = analysis;
  const container = $('batch-analysis');
  if (!container) return;
  if (!analysis) {
    container.innerHTML = '';
    container.classList.add('hidden');
    return;
  }

  const parts = [];
  if (analysis.existing_pool_name) {
    parts.push(`
      <div class="batch-alert batch-alert-warning">
        ${icon('alert')}
        <div>Внимание: эта ссылка уже использовалась в пуле <b>«${escapeHtml(analysis.existing_pool_name)}»</b></div>
      </div>
    `);
  }

  if (isSingle && singleSku) {
    parts.push(`
      <div class="batch-alert batch-alert-info">
        <div>🎯 Ссылка на 1 артикул: <b>${escapeHtml(singleSku)}</b> (будет создана 1 задача)</div>
      </div>
    `);
  }

  const newCount = analysis.new_skus?.length || 0;
  const activeCount = analysis.active_conflicts?.length || 0;
  const completedCount = analysis.completed_or_archived?.length || 0;

  if (newCount > 0 || activeCount > 0 || completedCount > 0) {
    const pills = [];
    if (newCount > 0) pills.push(`<span class="batch-stat-pill pill-new">🟢 Новых: ${newCount}</span>`);
    if (activeCount > 0) pills.push(`<span class="batch-stat-pill pill-active">🔴 В работе: ${activeCount}</span>`);
    if (completedCount > 0) pills.push(`<span class="batch-stat-pill pill-done">⚪ Были ранее: ${completedCount}</span>`);
    parts.push(`<div class="batch-stats-bar">${pills.join('')}</div>`);
  }

  if (activeCount > 0) {
    const conflictNames = analysis.active_conflicts
      .slice(0, 3)
      .map(c => `${escapeHtml(c.sku)} (${c.assigned_to_name ? escapeHtml(c.assigned_to_name) : 'свободна'})`)
      .join(', ');
    const more = activeCount > 3 ? ` и еще ${activeCount - 3}` : '';

    if (newCount > 0) {
      parts.push(`
        <div class="batch-alert batch-alert-warning">
          ${icon('alert')}
          <div>Уже в работе: ${conflictNames}${more}. Активные дубликаты будут пропущены.</div>
        </div>
        <button type="button" class="btn btn-secondary btn-sm btn-filter-new" onclick="filterBatchOnlyNew()">
          ✨ Оставить только новые (${newCount} шт.)
        </button>
      `);
    } else {
      parts.push(`
        <div class="batch-alert batch-alert-danger">
          ${icon('alert')}
          <div>Все указанные артикулы (${conflictNames}${more}) уже находятся в работе. Добавление заблокировано во избежание дублирования.</div>
        </div>
      `);
    }
  }

  container.innerHTML = parts.join('');
  container.classList.toggle('hidden', parts.length === 0);
}

function filterBatchOnlyNew() {
  if (!state.lastBatchAnalysis?.new_skus?.length) return;
  const newSkus = state.lastBatchAnalysis.new_skus;
  $('batch-skus').value = newSkus.join('\n');
  updateSkuCounter();
  renderBatchAnalysis({
    ...state.lastBatchAnalysis,
    new_skus: newSkus,
    active_conflicts: [],
    completed_or_archived: state.lastBatchAnalysis.completed_or_archived || []
  });
  showToast(`Оставлено новых артикулов: ${newSkus.length}`);
}

function openAddBatchModal() {
  $('form-add-batch').reset();
  renderBatchAnalysis(null);
  updateSkuCounter();
  populateAssigneeSelects();
  $('batch-error').classList.add('hidden');
  openModal('modal-add-batch');
}

async function autoScanCloudFolders() {
  const url = $('batch-parent-url').value.trim();
  if (!safeUrl(url)) { showFormError('batch-error', 'Вставьте ссылку на папку с исходниками.'); return; }
  state.formBusy = true;
  $('btn-scan').disabled = true; $('btn-submit-batch').disabled = true;
  $('btn-scan').textContent = 'Ищем…'; $('batch-error').classList.add('hidden');
  try {
    const data = await api('/api/cloud/scan', { url });
    if (!data.folders?.length && !data.single_sku) throw new Error('Артикулы не найдены. Введите их вручную.');

    if (data.is_single && data.single_sku) {
      if (!$('batch-name').value.trim()) {
        $('batch-name').value = cleanPoolTitle(data.suggested_pool_name) || data.single_sku;
      }
      $('batch-skus').value = data.single_sku;
      showToast(`Обнаружен одиночный артикул: ${data.single_sku}`);
    } else {
      if (!$('batch-name').value.trim() && data.suggested_pool_name) {
        $('batch-name').value = cleanPoolTitle(data.suggested_pool_name);
      }
      $('batch-skus').value = (data.folders || []).join('\n');
      showToast(`Найдено артикулов: ${(data.folders || []).length}`);
    }

    updateSkuCounter();
    renderBatchAnalysis(data.analysis, data.is_single, data.single_sku);
  } catch (error) { showFormError('batch-error', error.message); }
  finally { state.formBusy = false; $('btn-scan').disabled = false; $('btn-submit-batch').disabled = false; $('btn-scan').textContent = 'Найти артикулы'; }
}

async function submitAddBatch(event) {
  event.preventDefault();
  if (state.formBusy) return;
  const bitrixVal = $('batch-bitrix-url')?.value.trim();
  if (bitrixVal && !safeUrl(bitrixVal)) {
    showFormError('batch-error', 'Укажите корректную ссылку на задачу Битрикс24 (начиная с http:// или https://).');
    return;
  }
  const assigneeVal = $('batch-assignee')?.value;
  const body = {
    pool_name: $('batch-name').value.trim(),
    parent_url: $('batch-parent-url').value.trim(),
    skus_text: $('batch-skus').value.trim(),
    bitrix_url: bitrixVal || null,
    assign_to_id: assigneeVal ? parseInt(assigneeVal, 10) : null
  };
  if (!body.pool_name || !body.skus_text || !safeUrl(body.parent_url)) {
    showFormError('batch-error', 'Заполните название, ссылку на папку и артикулы.');
    return;
  }
  setFormBusy(true, 'btn-submit-batch'); $('batch-error').classList.add('hidden');
  try {
    const data = await api('/api/tasks/create-batch', body);
    state.mutationVersion++;
    state.formBusy = false; closeModal(false);
    const skippedMsg = data.skipped_active ? ` (пропущено дубликатов: ${data.skipped_active})` : '';
    showToast(`Создано задач: ${data.count}${skippedMsg}`);
    await loadTasks();
  } catch (error) { showFormError('batch-error', error.message); }
  finally { setFormBusy(false, 'btn-submit-batch'); $('btn-submit-batch').textContent = 'Создать задачи'; }
}

async function openStatsModal() {
  openModal('modal-stats'); $('stats-content').innerHTML = '<p class="sheet-subtitle">Загружаем статистику…</p>';
  try {
    const stats = await api('/api/stats');
    const total = Number(stats.total) || 0;
    const done = Number(stats.completed) || 0;
    const percent = total ? Math.min(100, Math.round(done / total * 100)) : 0;
    const totalImages = Number(stats.total_images) || 0;
    $('stats-content').innerHTML = `<div class="stats-summary"><div class="stats-tile"><div class="stats-number">${total}</div><p>Всего задач</p></div><div class="stats-tile"><div class="stats-number" style="color:#7dd3fc">${totalImages}</div><p>Изображений</p></div><div class="stats-tile"><div class="stats-number" style="color:var(--accent)">${done}</div><p>Готово · ${percent}%</p></div></div><div class="progress-track" role="progressbar" aria-label="Завершённые задачи" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${percent}"><div class="progress-fill" style="width:${percent}%"></div></div><div class="detail-section">${Object.entries(STAGES).map(([key, stage]) => `<div class="stats-stage"><span><i class="stage-dot" style="background:${stage.color}"></i>${stage.full}</span><b>${Number(stats[key]) || 0}</b></div>`).join('')}${archived ? `<div class="stats-stage"><span><i class="stage-dot" style="background:#64748b"></i>В архиве</span><b>${archived}</b></div>` : ''}</div>`;
  } catch (error) { $('stats-content').innerHTML = `<p class="form-error">${escapeHtml(error.message)}</p><button class="btn btn-wide btn-secondary" onclick="openStatsModal()">Повторить</button>`; }
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
  if (typeof $ !== 'function') return;
  const selects = [$('batch-assignee'), $('edit-pool-assignee')].filter(Boolean);
  if (!selects.length) return;
  const designers = (state.users || []).filter(u => u.is_approved !== 0);
  const activeMap = getActiveWorkloadMap();
  const optionsHtml = '<option value="">⚪ В общий пул (свободно)</option>' +
    designers.map(u => {
      const count = activeMap[u.telegram_id] ?? u.active_tasks_count ?? 0;
      const countLabel = count > 0 ? `${count} в работе` : 'свободен';
      const name = `${u.first_name || 'Участник'} ${u.last_name || ''}`.trim();
      const userLabel = u.username ? ` (@${u.username})` : '';
      return `<option value="${u.telegram_id}">🎨 ${escapeHtml(name)}${escapeHtml(userLabel)} · ${countLabel}</option>`;
    }).join('');

  selects.forEach(sel => {
    const curVal = sel.value;
    sel.innerHTML = optionsHtml;
    if (curVal) sel.value = curVal;
  });
}

async function openTeamModal() {
  if (!state.isAdmin) return;
  openModal('modal-team');
  const listEl = $('team-list');
  if (listEl) listEl.innerHTML = '<div class="muted" style="padding: 20px; text-align: center;">Загружаем участников…</div>';
  try {
    const data = await api('/api/users');
    state.users = data.users || [];
    populateAssigneeSelects();
    renderTeamList(state.users);
    updateFilters();
  } catch (error) {
    if (listEl) listEl.innerHTML = `<p class="form-error">${escapeHtml(error.message)}</p><button class="btn btn-secondary" onclick="openTeamModal()">Повторить</button>`;
  }
}

function setTeamRoleFilter(role) {
  state.teamRoleFilter = role;
  $('team-filter-all')?.classList.toggle('active', role === 'all');
  $('team-filter-designer')?.classList.toggle('active', role === 'designer');
  $('team-filter-admin')?.classList.toggle('active', role === 'admin');
  renderTeamList(state.users);
}

function renderTeamList(users) {
  const container = $('team-list');
  if (!container) return;
  const list = users || [];

  const designersCount = list.filter(u => u.role === 'designer').length;
  const adminsCount = list.filter(u => u.role === 'admin').length;
  const onlineCount = list.filter(isUserOnline).length;

  const metaEl = $('team-meta-text');
  if (metaEl) {
    metaEl.innerHTML = `${list.length} участников · <span class="online-indicator">●</span> ${onlineCount} онлайн`;
  }
  const bAll = $('team-badge-all');
  if (bAll) bAll.textContent = list.length;
  const bDes = $('team-badge-designer');
  if (bDes) bDes.textContent = designersCount;
  const bAdm = $('team-badge-admin');
  if (bAdm) bAdm.textContent = adminsCount;

  const currentRole = state.teamRoleFilter || 'all';
  const filtered = list.filter(u => currentRole === 'all' || u.role === currentRole);

  const overline = $('team-group-overline');
  if (overline) {
    overline.textContent = currentRole === 'designer' ? `ДИЗАЙНЕРЫ · ${filtered.length}` : (currentRole === 'admin' ? `МЕНЕДЖЕРЫ · ${filtered.length}` : `УЧАСТНИКИ · ${filtered.length}`);
  }

  if (!filtered.length) {
    container.innerHTML = '<div class="muted" style="padding: 24px 16px; text-align: center;">Участников в этой категории нет</div>';
    return;
  }

  container.innerHTML = filtered.map(user => {
    const name = user.first_name || 'Участник';
    const isSelf = user.telegram_id === state.currentUser?.telegram_id;
    const initial = name.trim()[0] || 'U';
    const pal = getAvatarColor(name);
    const online = isUserOnline(user);
    const roleLabel = user.role === 'admin' ? 'Менеджер' : 'Дизайнер';
    const removeBtn = isSelf ? '' : `<button type="button" class="btn-remove-user-subtle" title="Удалить из команды" aria-label="Удалить ${escapeHtml(name)}" onclick="confirmRemoveUserById(${user.telegram_id}, '${escapeHtml(name)}')">${icon('trash')}</button>`;

    return `
      <div class="team-member-row">
        <div class="team-member-avatar-wrap">
          <div class="team-member-avatar" style="background: ${pal.bg}; color: ${pal.fg};">${escapeHtml(initial)}</div>
          ${online ? '<div class="team-online-dot"></div>' : ''}
        </div>
        <div class="team-member-info">
          <span class="team-member-name">${escapeHtml(name)}${isSelf ? ' · Вы' : ''}</span>
          <span class="team-member-username">${escapeHtml(user.username ? `@${user.username}` : `ID: ${user.telegram_id}`)}</span>
        </div>
        <button type="button" class="team-member-role-pill" onclick="cycleUserRole(${user.telegram_id}, '${user.role}')" title="Сменить роль">
          <span>${roleLabel}</span>${icon('chevrons-up-down')}
        </button>
        ${removeBtn}
      </div>
    `;
  }).join('');
}

async function cycleUserRole(telegramId, currentRole) {
  const newRole = currentRole === 'admin' ? 'designer' : 'admin';
  if (telegramId === state.currentUser?.telegram_id && newRole !== 'admin') {
    if (!window.confirm('Снять с себя права администратора?')) return;
  }
  try {
    triggerHaptic('medium');
    await api(`/api/users/${telegramId}/role`, { role: newRole }, 'PUT');
    showToast(`Роль изменена: ${newRole === 'admin' ? 'Менеджер' : 'Дизайнер'}`);
    const data = await api('/api/users');
    state.users = data.users || [];
    renderTeamList(state.users);
    updateFilters();
  } catch (error) {
    showToast(error.message, 'error');
  }
}

async function confirmRemoveUserById(telegramId, name) {
  if (!window.confirm(`Удалить «${name}» из команды?`)) return;
  try {
    triggerHaptic('medium');
    await api(`/api/users/${telegramId}`, undefined, 'DELETE');
    showToast('Участник удален из команды');
    const data = await api('/api/users');
    state.users = data.users || [];
    renderTeamList(state.users);
    updateFilters();
  } catch (error) {
    showToast(error.message, 'error');
  }
}

async function syncTeamList() {
  const btn = $('btn-sync-team');
  if (btn) {
    btn.disabled = true;
    btn.classList.add('loading');
  }
  try {
    triggerHaptic('medium');
    const data = await api('/api/users/sync', {});
    state.users = data.users || [];
    populateAssigneeSelects();
    renderTeamList(state.users);
    const timeEl = $('sync-team-status');
    if (timeEl) timeEl.textContent = 'только что';
    showToast('Состав команды синхронизирован с группой');
    updateFilters();
  } catch (error) {
    showToast(error.message, 'error');
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.classList.remove('loading');
    }
  }
}

async function triggerBackup() {
  if (state.formBusy) return;
  state.formBusy = true;
  const btn = $('btn-backup-db');
  if (btn) {
    btn.disabled = true;
    btn.classList.add('loading');
  }
  showToast('Создаем резервную копию базы данных…');
  try {
    triggerHaptic('medium');
    const res = await api('/api/admin/backup', {});
    const timeEl = $('backup-db-status');
    if (timeEl) timeEl.textContent = 'только что';
    showToast(res.message || 'Резервная копия отправлена в ваш Telegram!');
  } catch (error) {
    showToast(error.message, 'error');
  } finally {
    state.formBusy = false;
    if (btn) {
      btn.disabled = false;
      btn.classList.remove('loading');
    }
  }
}

function renderPoolActionsBar() {
  const bar = $('pool-actions-bar');
  if (!bar) return;
  if (!state.isAdmin || state.poolFilter === 'all') {
    bar.classList.add('hidden');
    return;
  }
  const currentPool = state.poolFilter;
  const poolTasks = state.tasks.filter(task => (task.pool_name || 'Общий пул') === currentPool);
  if (!poolTasks.length) {
    bar.classList.add('hidden');
    return;
  }
  bar.classList.remove('hidden');
  $('pool-actions-title').textContent = `${cleanPoolTitle(currentPool)} (${poolTasks.length})`;
  const bitrixBtn = $('btn-pool-bitrix');
  const bitrixUrl = poolTasks.find(t => t.bitrix_url)?.bitrix_url;
  if (bitrixBtn) {
    if (bitrixUrl && safeUrl(bitrixUrl)) {
      bitrixBtn.href = escapeHtml(safeUrl(bitrixUrl));
      bitrixBtn.classList.remove('hidden');
    } else {
      bitrixBtn.classList.add('hidden');
    }
  }
  const allArchived = poolTasks.length > 0 && poolTasks.every(t => t.is_archived);
  const archiveBtn = $('btn-pool-archive');
  const archiveLabel = $('btn-pool-archive-label');
  const archiveIcon = archiveBtn?.querySelector('use');
  if (allArchived) {
    if (archiveLabel) archiveLabel.textContent = 'Восстановить';
    if (archiveBtn) archiveBtn.title = 'Восстановить все задачи пула из архива';
    if (archiveIcon) archiveIcon.setAttribute('href', '#i-unarchive');
  } else {
    if (archiveLabel) archiveLabel.textContent = 'В архив';
    if (archiveBtn) archiveBtn.title = 'Отправить все задачи пула в архив';
    if (archiveIcon) archiveIcon.setAttribute('href', '#i-archive');
  }
}

async function toggleCurrentPoolArchive() {
  if (!state.isAdmin || state.poolFilter === 'all' || state.formBusy) return;
  const currentPool = state.poolFilter;
  const poolTasks = state.tasks.filter(task => (task.pool_name || 'Общий пул') === currentPool);
  if (!poolTasks.length) return;
  const allArchived = poolTasks.every(t => t.is_archived);
  const targetArchived = allArchived ? 0 : 1;
  const actionWord = targetArchived ? 'отправить в архив' : 'восстановить из архива';
  if (!window.confirm(`Вы уверены, что хотите ${actionWord} все задачи пула "${currentPool}" (${poolTasks.length} шт.)?`)) return;

  state.formBusy = true;
  try {
    const data = await api('/api/pools/archive', { pool_name: currentPool, is_archived: targetArchived });
    for (const updated of data.tasks || []) {
      applyTask(updated);
    }
    showToast(targetArchived ? `Пул "${currentPool}" отправлен в архив` : `Пул "${currentPool}" восстановлен`);
    render();
  } catch (error) {
    showToast(error.message, 'error');
  } finally {
    state.formBusy = false;
  }
}

async function confirmDeleteCurrentPool() {
  if (!state.isAdmin || state.poolFilter === 'all' || state.formBusy) return;
  const currentPool = state.poolFilter;
  const poolTasks = state.tasks.filter(task => (task.pool_name || 'Общий пул') === currentPool);
  if (!poolTasks.length) return;
  if (!window.confirm(`ВНИМАНИЕ: Удалить пул "${currentPool}" и все его задачи (${poolTasks.length} шт.) безвозвратно? Это действие нельзя отменить.`)) return;

  state.formBusy = true;
  try {
    await api('/api/pools/delete', { pool_name: currentPool });
    state.tasks = state.tasks.filter(task => (task.pool_name || 'Общий пул') !== currentPool);
    state.mutationVersion++;
    state.poolFilter = 'all';
    render();
    showToast(`Пул "${currentPool}" удалён`);
  } catch (error) {
    showToast(error.message, 'error');
  } finally {
    state.formBusy = false;
  }
}

let currentEditingPoolName = null;

function openEditPoolModal(poolName) {
  if (!state.isAdmin) return;
  currentEditingPoolName = poolName;
  $('edit-pool-title').textContent = `Пул: ${poolName}`;
  $('edit-pool-name').value = poolName;
  $('edit-pool-new-skus').value = '';
  $('edit-pool-source-url').value = '';
  const poolTasks = state.tasks.filter(t => (t.pool_name || 'Общий пул') === poolName);
  const currentBitrix = poolTasks.find(t => t.bitrix_url)?.bitrix_url || '';
  if ($('edit-pool-bitrix-url')) {
    $('edit-pool-bitrix-url').value = currentBitrix;
  }
  if ($('edit-pool-assignee')) {
    $('edit-pool-assignee').value = '';
  }
  populateAssigneeSelects();
  $('edit-pool-error').classList.add('hidden');
  renderEditingPoolSkus();
  openModal('modal-edit-pool');
}

async function submitSavePoolBitrix() {
  if (!state.isAdmin || !currentEditingPoolName) return;
  const bitrixUrl = $('edit-pool-bitrix-url')?.value.trim();
  if (bitrixUrl && !safeUrl(bitrixUrl)) {
    showFormError('edit-pool-error', 'Укажите корректную ссылку на Битрикс24 (начиная с http:// или https://)');
    return;
  }
  setFormBusy(true, 'btn-save-pool-bitrix');
  $('edit-pool-error').classList.add('hidden');
  try {
    await api('/api/pools/update-bitrix', {
      pool_name: currentEditingPoolName,
      bitrix_url: bitrixUrl || null
    });
    for (const t of state.tasks) {
      if ((t.pool_name || 'Общий пул') === currentEditingPoolName) {
        t.bitrix_url = bitrixUrl || null;
      }
    }
    state.mutationVersion++;
    render();
    showToast(bitrixUrl ? 'Ссылка на Битрикс24 сохранена' : 'Ссылка на Битрикс24 очищена');
  } catch (error) {
    showFormError('edit-pool-error', error.message);
  } finally {
    setFormBusy(false, 'btn-save-pool-bitrix');
    $('btn-save-pool-bitrix').textContent = 'Сохранить';
  }
}

function renderEditingPoolSkus() {
  if (!currentEditingPoolName) return;
  const poolTasks = state.tasks.filter(t => (t.pool_name || 'Общий пул') === currentEditingPoolName);
  $('edit-pool-skus-count').textContent = poolTasks.length;

  const allArchived = poolTasks.length > 0 && poolTasks.every(t => t.is_archived);
  $('btn-edit-pool-archive-label').textContent = allArchived ? 'Из архива' : 'В архив';

  if (!poolTasks.length) {
    $('edit-pool-skus-list').innerHTML = '<p class="muted" style="padding: 8px 0;">В этом пуле нет артикулов</p>';
    return;
  }

  $('edit-pool-skus-list').innerHTML = poolTasks.map(task => {
    const stage = STAGES[task.status] || { label: task.status, color: '#64748b' };
    const assignee = task.assigned_to_name ? escapeHtml(task.assigned_to_name) : 'Не назначен';
    return `<div class="edit-pool-sku-row" id="edit-sku-row-${task.id}">
      <div class="edit-pool-sku-info">
        <span class="edit-pool-sku-code">${escapeHtml(task.sku)}</span>
        <span class="edit-pool-sku-status" style="color: ${stage.color || '#94a3b8'}">• ${escapeHtml(stage.label)}</span>
        <span class="edit-pool-sku-assignee muted">${assignee}</span>
      </div>
      <button type="button" class="btn-remove-user" onclick="deleteSkuFromPool(${task.id}, '${escapeHtml(task.sku)}')" title="Удалить артикул из пула">
        <svg class="icon"><use href="#i-trash"/></svg>
      </button>
    </div>`;
  }).join('');
}

async function submitRenamePool() {
  if (!state.isAdmin || !currentEditingPoolName) return;
  const newName = $('edit-pool-name').value.trim();
  if (!newName) {
    showFormError('edit-pool-error', 'Название пула не может быть пустым');
    return;
  }
  if (newName === currentEditingPoolName) {
    showToast('Название не изменилось');
    return;
  }
  setFormBusy(true, 'btn-save-pool-name');
  try {
    const res = await api('/api/pools/rename', {
      old_pool_name: currentEditingPoolName,
      new_pool_name: newName
    });
    for (const t of state.tasks) {
      if ((t.pool_name || 'Общий пул') === currentEditingPoolName) {
        t.pool_name = newName;
      }
    }
    if (state.collapsedPools.has(currentEditingPoolName)) {
      state.collapsedPools.delete(currentEditingPoolName);
      state.collapsedPools.add(newName);
    }
    currentEditingPoolName = newName;
    $('edit-pool-title').textContent = `Пул: ${newName}`;
    render();
    renderEditingPoolSkus();
    showToast(`Пул переименован в «${newName}»`);
  } catch (err) {
    showFormError('edit-pool-error', err.message);
  } finally {
    setFormBusy(false, 'btn-save-pool-name');
  }
}

async function submitAddSkusToPool() {
  if (!state.isAdmin || !currentEditingPoolName) return;
  const text = $('edit-pool-new-skus').value.trim();
  if (!text) {
    showFormError('edit-pool-error', 'Введите хотя бы один артикул');
    return;
  }
  const sourceUrl = $('edit-pool-source-url').value.trim();
  setFormBusy(true, 'btn-add-skus-to-pool');
  try {
    const bitrixVal = $('edit-pool-bitrix-url')?.value.trim();
    const assigneeVal = $('edit-pool-assignee')?.value;
    const res = await api('/api/pools/add-skus', {
      pool_name: currentEditingPoolName,
      skus_text: text,
      source_url: safeUrl(sourceUrl) ? sourceUrl : null,
      bitrix_url: bitrixVal ? (safeUrl(bitrixVal) ? bitrixVal : null) : undefined,
      assign_to_id: assigneeVal ? parseInt(assigneeVal, 10) : null
    });
    showToast(`Добавлено артикулов: ${res.created_count}`);
    $('edit-pool-new-skus').value = '';
    if ($('edit-pool-assignee')) $('edit-pool-assignee').value = '';
    await loadTasks();
    renderEditingPoolSkus();
    render();
  } catch (err) {
    showFormError('edit-pool-error', err.message);
  } finally {
    setFormBusy(false, 'btn-add-skus-to-pool');
  }
}

async function deleteSkuFromPool(taskId, sku) {
  if (!state.isAdmin) return;
  if (!window.confirm(`Удалить артикул ${sku} из пула?`)) return;
  try {
    await api(`/api/tasks/${taskId}`, {}, 'DELETE');
    state.tasks = state.tasks.filter(t => t.id !== taskId);
    render();
    renderEditingPoolSkus();
    showToast(`Артикул ${sku} удален`);
  } catch (err) {
    showToast(err.message, 'error');
  }
}

async function toggleCurrentEditingPoolArchive() {
  if (!state.isAdmin || !currentEditingPoolName) return;
  const poolTasks = state.tasks.filter(t => (t.pool_name || 'Общий пул') === currentEditingPoolName);
  const allArchived = poolTasks.length > 0 && poolTasks.every(t => t.is_archived);
  const targetArchived = allArchived ? 0 : 1;
  try {
    await api('/api/pools/archive', { pool_name: currentEditingPoolName, is_archived: targetArchived === 1 });
    for (const t of state.tasks) {
      if ((t.pool_name || 'Общий пул') === currentEditingPoolName) {
        t.is_archived = targetArchived;
      }
    }
    render();
    renderEditingPoolSkus();
    showToast(targetArchived ? `Пул «${currentEditingPoolName}» перенесен в архив` : `Пул «${currentEditingPoolName}» восстановлен`);
  } catch (err) {
    showFormError('edit-pool-error', err.message);
  }
}

async function confirmDeleteEditingPool() {
  if (!state.isAdmin || !currentEditingPoolName) return;
  if (!window.confirm(`Вы действительно хотите безвозвратно удалить пул «${currentEditingPoolName}» и все его задачи?`)) return;
  try {
    await api('/api/pools/delete', { pool_name: currentEditingPoolName });
    state.tasks = state.tasks.filter(t => (t.pool_name || 'Общий пул') !== currentEditingPoolName);
    closeModal();
    render();
    showToast(`Пул «${currentEditingPoolName}» удален`);
  } catch (err) {
    showFormError('edit-pool-error', err.message);
  }
}

async function toggleTaskArchive(taskId, isArchived) {
  if (state.formBusy) return;
  state.formBusy = true;
  try {
    const data = await api(`/api/tasks/${taskId}/archive`, { is_archived: isArchived });
    applyTask(data.task);
    showToast(isArchived ? 'Задача перемещена в архив' : 'Задача восстановлена из архива');
    render();
  } catch (error) {
    showToast(error.message, 'error');
  } finally {
    state.formBusy = false;
  }
}

async function confirmDeleteTask(taskId) {
  const task = state.tasks.find(item => item.id === taskId);
  if (!task) return;
  if (!window.confirm(`Удалить задачу "${task.sku}" безвозвратно?`)) return;
  if (state.formBusy) return;
  state.formBusy = true;
  try {
    await api(`/api/tasks/${taskId}`, undefined, 'DELETE');
    state.tasks = state.tasks.filter(item => item.id !== taskId);
    state.mutationVersion++;
    closeModal(false);
    render();
    showToast('Задача удалена');
  } catch (error) {
    showToast(error.message, 'error');
  } finally {
    state.formBusy = false;
  }
}

function openModal(id) {
  if (state.formBusy) return;
  if (!state.activeModal) state.lastFocus = document.activeElement;
  document.querySelectorAll('.modal-overlay').forEach(overlay => overlay.classList.toggle('hidden', overlay.id !== id));
  state.activeModal = id;
  updateFilterExpanded();
  document.body.classList.add('modal-open');
  document.querySelector('.app-shell').inert = true;
  $('dev-bar').inert = true;
  if (telegramSupports('6.1')) tg.BackButton?.show();
  // Focus the dialog without opening the phone keyboard before the user taps a field.
  $(id).querySelector('.modal-dialog').focus({ preventScroll: true });
}
function closeModal(returnToTask = true) {
  if (state.formBusy) return;
  const backTaskId = state.activeModal === 'modal-action' && returnToTask ? state.returnToTaskId : null;
  state.returnToTaskId = null;
  if (backTaskId) { openTaskDetail(backTaskId); return; }
  document.querySelectorAll('.modal-overlay').forEach(overlay => overlay.classList.add('hidden'));
  state.activeModal = null;
  updateFilterExpanded();
  document.body.classList.remove('modal-open');
  document.querySelector('.app-shell').inert = false;
  $('dev-bar').inert = false;
  if (telegramSupports('6.1')) tg.BackButton?.hide();
  if (state.lastFocus?.isConnected) state.lastFocus.focus({ preventScroll: true });
}
function handleModalKeydown(event) {
  if (!state.activeModal) return;
  if (event.key === 'Escape') { event.preventDefault(); closeModal(); }
  if (state.activeModal === 'modal-filter' && event.target.closest('#filter-options') && ['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) {
    event.preventDefault();
    const options = [...$('filter-options').querySelectorAll('.filter-option')];
    const current = options.indexOf(document.activeElement);
    const index = event.key === 'Home' ? 0 : event.key === 'End' ? options.length - 1 : (current + (event.key === 'ArrowDown' ? 1 : -1) + options.length) % options.length;
    options.forEach((option, position) => { option.tabIndex = position === index ? 0 : -1; });
    options[index]?.focus();
    return;
  }
  if (event.key !== 'Tab') return;
  const focusable = [...$(state.activeModal).querySelectorAll('button:not(:disabled), a[href], input:not(:disabled), textarea:not(:disabled), select:not(:disabled)')].filter(element => element.tabIndex >= 0 && !element.closest('.hidden'));
  const first = focusable[0]; const last = focusable[focusable.length - 1];
  if (!first) { event.preventDefault(); return; }
  if (event.shiftKey && (document.activeElement === first || document.activeElement.classList.contains('modal-dialog'))) { event.preventDefault(); last.focus(); }
  else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
}
async function copyTaskSku(taskId) {
  const task = state.tasks.find(item => item.id === taskId);
  if (!task) return;
  try { await navigator.clipboard.writeText(task.sku); showToast('Артикул скопирован'); }
  catch { showToast('Не удалось скопировать артикул', 'error'); }
}
let toastTimer;
function showToast(message, type = 'info') {
  clearTimeout(toastTimer);
  const toast = document.createElement('div');
  toast.className = `crm-toast${type === 'error' ? ' is-error' : ''}`;
  toast.textContent = message;
  $('toast-container').replaceChildren(toast);
  toastTimer = setTimeout(() => toast.remove(), type === 'error' ? 5000 : 3000);
}
// --- Export Functionality ---
let exportAvailablePools = [];
let exportSelectedPools = new Set();
let exportFormat = 'excel';
let googleScriptTemplate = '';

async function openExportModal(preSelectedPoolNames = null) {
  if (!state.isAdmin) return;
  $('export-error')?.classList.add('hidden');
  $('export-google-section')?.classList.toggle('hidden', exportFormat !== 'google');
  $('export-guide-section')?.classList.add('hidden');
  $('export-pools-list').innerHTML = '<div class="muted" style="padding: 12px; text-align: center;">Загрузка пулов...</div>';
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
    $('export-pools-list').innerHTML = `<div class="form-error" style="display:block;">${escapeHtml(err.message)}</div>`;
  }
}

function renderExportPoolsList() {
  if (!exportAvailablePools.length) {
    $('export-pools-list').innerHTML = '<div class="muted" style="padding: 12px; text-align: center;">Нет доступных пулов для экспорта</div>';
    $('export-pools-summary').textContent = 'Выбрано пулов: 0';
    return;
  }

  $('export-pools-list').innerHTML = exportAvailablePools.map(pool => {
    const isSelected = exportSelectedPools.has(pool.pool_name);
    const countInfo = `${pool.total_tasks} арт. · ${pool.total_images || 0} фото`;
    const archivedBadge = pool.all_archived ? ' <span class="muted">(архив)</span>' : '';
    return `<label class="export-pool-item${isSelected ? ' is-selected' : ''}">
      <span class="export-pool-checkbox">
        <input type="checkbox" ${isSelected ? 'checked' : ''} onchange="toggleExportPoolSelection('${escapeHtml(pool.pool_name)}', this.checked)">
        <span class="export-pool-name" title="${escapeHtml(pool.pool_name)}">${escapeHtml(pool.pool_name)}${archivedBadge}</span>
      </span>
      <span class="export-pool-badge">${countInfo}</span>
    </label>`;
  }).join('');

  updateExportSummary();
}

function updateExportSummary() {
  const selectedCount = exportSelectedPools.size;
  const totalAvailable = exportAvailablePools.length;
  const toggleBtn = $('btn-export-toggle-all');
  if (toggleBtn) {
    toggleBtn.textContent = (selectedCount === totalAvailable && totalAvailable > 0) ? 'Снять все' : 'Выбрать все';
  }

  let totalTasks = 0;
  let totalImages = 0;
  for (const pool of exportAvailablePools) {
    if (exportSelectedPools.has(pool.pool_name)) {
      totalTasks += pool.total_tasks;
      totalImages += (pool.total_images || 0);
    }
  }

  $('export-pools-summary').textContent = `Выбрано: ${selectedCount} из ${totalAvailable} пулов (${totalTasks} задач, ${totalImages} фото)`;
}

function toggleExportPoolSelection(poolName, isChecked) {
  if (isChecked) {
    exportSelectedPools.add(poolName);
  } else {
    exportSelectedPools.delete(poolName);
  }
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
    showToast('Не удалось скопировать код', 'error');
  }
}

async function executeExport() {
  if (!exportSelectedPools.size) {
    showFormError('export-error', 'Выберите хотя бы один пул для выгрузки');
    return;
  }
  $('export-error')?.classList.add('hidden');
  const btn = $('btn-run-export');
  const originalText = btn.textContent;
  btn.disabled = true;
  btn.textContent = exportFormat === 'google' ? 'Синхронизация с Google...' : 'Формируем Excel...';

  const selectedPoolsList = [...exportSelectedPools];

  try {
    if (exportFormat === 'excel') {
      const response = await fetch('/api/export/excel', {
        method: 'POST',
        headers: getAuthHeaders(),
        body: JSON.stringify({ pool_names: selectedPoolsList, send_to_telegram: true })
      });
      if (!response.ok) {
        const errJson = await response.json().catch(() => ({}));
        throw new Error(errJson.detail || 'Ошибка генерации Excel файла');
      }

      let filename = 'report_pools.xlsx';
      const disposition = response.headers.get('Content-Disposition');
      if (disposition && disposition.includes("filename*=UTF-8''")) {
        filename = decodeURIComponent(disposition.split("filename*=UTF-8''")[1].split(';')[0]);
      } else if (disposition && disposition.includes('filename=')) {
        filename = disposition.split('filename=')[1].replace(/"/g, '').split(';')[0];
      }

      const sentToTg = response.headers.get('X-Sent-To-Telegram') === 'true';

      try {
        const blob = await response.blob();
        const url = window.URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = filename;
        document.body.appendChild(a);
        a.click();
        a.remove();
        window.URL.revokeObjectURL(url);
      } catch (dlErr) {
        console.debug('Browser download fallback:', dlErr);
      }

      closeModal();
      if (sentToTg) {
        showToast('Отчет Excel отправлен вам в чат с ботом! 📄');
      } else {
        showToast(`Отчет Excel сформирован (${selectedPoolsList.length} пулов)`);
      }
    } else {
      const webhookUrl = $('export-google-webhook').value.trim();
      if (!webhookUrl) {
        throw new Error('Укажите ссылку на веб-приложение Google Apps Script');
      }
      const saveDefault = $('export-google-save-default')?.checked ?? true;
      const res = await api('/api/export/google-sheets', {
        webhook_url: webhookUrl,
        pool_names: selectedPoolsList,
        save_as_default: saveDefault
      });
      closeModal();
      showToast(`Успешно выгружено в Google Таблицу (${res.sheets_count} листов)`);
    }
  } catch (err) {
    showFormError('export-error', err.message);
    setTimeout(() => {
      $('export-error')?.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }, 50);
  } finally {
    btn.disabled = false;
    btn.textContent = originalText;
  }
}

function exportCurrentPool() {
  if (state.poolFilter !== 'all') {
    openExportModal([state.poolFilter]);
  } else {
    openExportModal();
  }
}


window.addEventListener('DOMContentLoaded', init);
