const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');

function app() {
  const context = vm.createContext({
    window: { addEventListener() {} },
    document: {}, URL, AbortController, setTimeout, clearTimeout, console,
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../static/app.js'), 'utf8'), context);
  vm.runInContext(`state.currentUser = { telegram_id: 10001 }; state.tasks = [
    { id: 1, sku: '130754OUT-99', status: 'available', pool_name: 'Обувь' },
    { id: 2, sku: '130822OUT-99', status: 'in_progress', assigned_to_id: 10001, assigned_to_name: 'Анна (@anna)', pool_name: 'Обувь', notes: 'Правки: выровнять плашку' },
    { id: 3, sku: '129829OUT-99', status: 'internal_review', assigned_to_id: 10001, pool_name: 'Одежда' },
    { id: 4, sku: '129879OUT-99', status: 'client_review', assigned_to_id: 10001, pool_name: 'Одежда' },
    { id: 5, sku: '129889OUT-99', status: 'export_ready', assigned_to_id: 10001, pool_name: 'Обувь' },
    { id: 6, sku: '129930OUT-99', status: 'completed', assigned_to_id: 10001, pool_name: 'Обувь' },
    { id: 7, sku: '130754OUT-T4', status: 'in_progress', assigned_to_id: 10002, pool_name: 'Обувь' }
  ];`, context);
  return expression => vm.runInContext(expression, context);
}

test('My includes work, lead review, client review and export; completed is opt-in', () => {
  const run = app();
  run("state.currentTab = 'my'");
  assert.equal(run('filteredTasks().map(t => t.id).join(",")'), '2,5,3,4');
  run("state.statusFilter = 'completed'");
  assert.equal(run('filteredTasks().map(t => t.id).join(",")'), '6');
});

test('Each review and export stage can be selected independently', () => {
  const run = app();
  for (const [stage, id] of [['internal_review', 3], ['client_review', 4], ['export_ready', 5]]) {
    run(`state.statusFilter = '${stage}'`);
    assert.equal(run('filteredTasks().map(t => t.id).join(",")'), String(id));
  }
});

test('SKU, pool and performer search combine with scope and stage', () => {
  const run = app();
  run("state.searchQuery = '130754'");
  assert.equal(run('filteredTasks().length'), 2);
  run("state.searchQuery = 'анна'; state.currentTab = 'my'");
  assert.equal(run('filteredTasks().map(t => t.id).join(",")'), '2');
  run("state.searchQuery = ''; state.poolFilter = 'Одежда'; state.statusFilter = 'client_review'");
  assert.equal(run('filteredTasks().map(t => t.id).join(",")'), '4');
});

test('Taking a task keeps it visible in Available until the user changes filters', () => {
  const run = app();
  run("state.currentTab = 'available'; applyTask({...state.tasks[0], status: 'in_progress', assigned_to_id: 10001}); state.justTakenTaskId = 1");
  assert.equal(run('filteredTasks().map(t => t.id).join(",")'), '1');
  run('state.justTakenTaskId = null');
  assert.equal(run('filteredTasks().length'), 0);
});

test('Lead sees review first; designer sees their work and export first', () => {
  const run = app();
  assert.equal(run('filteredTasks()[0].id'), 2);
  run('state.isAdmin = true');
  assert.equal(run('filteredTasks()[0].id'), 3);
});

test('Permissions control visible actions', () => {
  const run = app();
  assert.equal(run('primaryAction(state.tasks[6])'), null);
  assert.equal(run('primaryAction(state.tasks[2])'), null);
  assert.equal(run('primaryAction(state.tasks[4]).action'), 'complete');
  run('state.isAdmin = true');
  assert.equal(run('primaryAction(state.tasks[2]).action'), 'client');
});

test('Export opens the approved client Figma from the card', () => {
  const run = app();
  const html = run("createTaskCardHtml({...state.tasks[4], internal_figma_url: 'https://www.figma.com/design/internal', client_figma_url: 'https://www.figma.com/design/approved'})");
  assert.ok(html.includes('https://www.figma.com/design/approved'));
  assert.ok(!html.includes('https://www.figma.com/design/internal'));
});

test('User content is escaped; unsupported URL schemes are suppressed', () => {
  const run = app();
  assert.equal(run(`safeUrl('javascript:alert(1)')`), '');
  assert.equal(run(`safeUrl('data:text/html,test')`), '');
  assert.equal(run(`linkHtml('javascript:alert(1)', 'Фото', 'folder')`), '');
  assert.equal(run(`escapeHtml("O'Reilly <img>")`), 'O&#39;Reilly &lt;img&gt;');
  assert.ok(!run(`createTaskCardHtml({...state.tasks[0], sku: '<img src=x>', notes: '<script>alert(1)</script>'})`).includes('<script>'));
});

test('Only revision notes receive warning styling; ordinary comments do not', () => {
  const run = app();
  assert.equal(run('isRevision(state.tasks[1])'), true);
  assert.equal(run("isRevision({...state.tasks[2], notes: 'Макет готов'})"), false);
  assert.equal(run("taskNotes({notes: 'Исходники из папки фото'})"), '');
});

test('Saved server task replaces status, owner and cleared notes together', () => {
  const run = app();
  run("applyTask({ id: 2, sku: '130822OUT-99', status: 'internal_review', assigned_to_id: 10001, notes: '' })");
  assert.equal(run('state.tasks[1].notes'), '');
  assert.equal(run('state.tasks[1].status'), 'internal_review');
  assert.equal(run('state.tasks[1].assigned_to_id'), 10001);
  assert.equal(run('state.mutationVersion'), 1);
});

test('Stage picker shows My active count and keeps completed selectable', () => {
  const run = app();
  run("state.currentTab = 'my'");
  assert.equal(run("filterOptions('status').find(option => option.value === 'all').count"), 4);
  assert.equal(run("filterOptions('status').find(option => option.value === 'completed').count"), 1);
  run("state.statusFilter = 'completed'");
  assert.equal(run("filterOptions('status').find(option => option.value === 'all').count"), 4);
});

test('Pool picker counts alternatives using search, scope and stage', () => {
  const run = app();
  run("state.poolFilter = 'Одежда'; state.statusFilter = 'in_progress'");
  assert.equal(run("filterOptions('pool').find(option => option.value === 'Обувь').count"), 2);
  assert.equal(run("filterOptions('pool').find(option => option.value === 'Одежда').count"), 0);
  run("state.currentTab = 'my'");
  assert.equal(run("filterOptions('pool').find(option => option.value === 'Обувь').count"), 1);
  run("state.searchQuery = 'нет такого артикула'");
  assert.equal(run("filterOptions('pool')[0].count"), 0);
});

test('Picker has one selected option and safely renders pool names', () => {
  const run = app();
  run("state.tasks.push({id: 8, sku: '130253OUT-90', status: 'available', pool_name: '<script>Куртки</script>'}); state.poolFilter = 'Одежда'");
  const html = run("filterOptionsHtml('pool')");
  assert.ok(html.includes('&lt;script&gt;Куртки&lt;/script&gt;'));
  assert.ok(!html.includes('<script>'));
  assert.equal((html.match(/aria-selected="true"/g) || []).length, 1);
  assert.equal((html.match(/tabindex="0"/g) || []).length, 1);
});

test('Archived tasks are excluded from active scopes and shown in archived filter', () => {
  const run = app();
  // Mark task 2 as archived
  run('state.tasks[1].is_archived = 1');
  // Task 2 should not be in 'all' when statusFilter = 'all'
  assert.ok(!run('filteredTasks().some(t => t.id === 2)'));
  // Task 2 should not be in 'my' tab
  run("state.currentTab = 'my'");
  assert.ok(!run('filteredTasks().some(t => t.id === 2)'));

  // Switch to statusFilter = 'archived'
  run("state.statusFilter = 'archived'");
  assert.equal(run('filteredTasks().map(t => t.id).join(",")'), '2');

  // Switch to tab 'all' with statusFilter = 'archived'
  run("state.currentTab = 'all'");
  assert.equal(run('filteredTasks().map(t => t.id).join(",")'), '2');

  // Verify statusHtml and primaryAction for archived task
  assert.ok(run('statusHtml(state.tasks[1])').includes('status-archived'));
  assert.equal(run('primaryAction(state.tasks[1])'), null);

  // Verify filterOptions includes archived option with count
  const archivedOpt = run("filterOptions('status').find(o => o.value === 'archived')");
  assert.ok(archivedOpt);
  assert.equal(archivedOpt.count, 1);
});

test('Batch analysis renders warnings and filters only new SKUs', () => {
  const run = app();
  run(`
    const mockElements = {
      'batch-analysis': { innerHTML: '', classList: { add() {}, remove() {}, toggle() {} } },
      'batch-skus': { value: 'OLD-01\\nNEW-01' },
      'skus-counter': { textContent: '' },
      'toast-container': { replaceChildren() {} }
    };
    globalThis.document.getElementById = id => mockElements[id] || null;
    globalThis.document.createElement = () => ({ className: '', textContent: '', remove() {} });
  `);

  run(`
    renderBatchAnalysis({
      existing_pool_name: 'Прошлый пул',
      total: 2,
      new_skus: ['NEW-01'],
      active_conflicts: [{ sku: 'OLD-01', assigned_to_name: 'Анна' }],
      completed_or_archived: []
    });
  `);

  const html = run("mockElements['batch-analysis'].innerHTML");
  assert.ok(html.includes('Прошлый пул'));
  assert.ok(html.includes('OLD-01'));
  assert.ok(html.includes('btn-filter-new'));

  // Test filterBatchOnlyNew
  run('filterBatchOnlyNew()');
  assert.equal(run("mockElements['batch-skus'].value"), 'NEW-01');
});

test('Images count badge is rendered in card HTML when images_count is present and omitted when absent', () => {
  const run = app();
  const cardWithImages = run("createTaskCardHtml({...state.tasks[0], images_count: 10})");
  assert.ok(cardWithImages.includes('badge-images'));
  assert.ok(cardWithImages.includes('10 фото'));

  const cardWithoutImages = run("createTaskCardHtml({...state.tasks[0], images_count: null})");
  assert.ok(!cardWithoutImages.includes('badge-images'));
  assert.ok(!cardWithoutImages.includes('фото'));
});

test('Task detail HTML displays images count and edit button; action sheet renders images count input', () => {
  const run = app();
  run(`
    const makeEl = () => ({
      innerHTML: '',
      textContent: '',
      className: '',
      classList: { add() {}, remove() {}, toggle() {} },
      setAttribute() {},
      getAttribute() {},
      focus() {},
      querySelector() { return makeEl(); }
    });
    const mockElements = {
      'task-detail-content': makeEl(),
      'modal-task': makeEl(),
      'modal-action': makeEl(),
      'action-title': makeEl(),
      'action-sku': makeEl(),
      'action-error': makeEl(),
      'action-submit': makeEl(),
      'action-fields': makeEl(),
      'status-filter': makeEl(),
      'pool-filter': makeEl()
    };
    globalThis.document.getElementById = id => mockElements[id] || makeEl();
    globalThis.document.querySelectorAll = () => [];
    globalThis.document.querySelector = () => ({ inert: false });
    globalThis.document.body = { classList: { add() {}, remove() {} } };
  `);

  // Render detail for task without images_count
  run("state.detailTaskId = 2; renderTaskDetail()");
  let detailHtml = run("mockElements['task-detail-content'].innerHTML");
  assert.ok(detailHtml.includes('detail-images-block'));
  assert.ok(detailHtml.includes('не указано'));
  assert.ok(detailHtml.includes("openAction('images', 2)"));

  // Now with images_count = 12
  run("state.tasks[1].images_count = 12; renderTaskDetail()");
  detailHtml = run("mockElements['task-detail-content'].innerHTML");
  assert.ok(detailHtml.includes('12 шт.'));

  // Test openAction for 'internal' (turn in for review)
  run("openAction('internal', 2)");
  let fieldsHtml = run("mockElements['action-fields'].innerHTML");
  assert.ok(fieldsHtml.includes('id="action-images-count"'));
  assert.ok(fieldsHtml.includes('value="12"'));

  // Test openAction for 'images' (direct edit on any stage)
  run("openAction('images', 2)");
  fieldsHtml = run("mockElements['action-fields'].innerHTML");
  assert.ok(fieldsHtml.includes('id="action-images-count"'));
  assert.ok(fieldsHtml.includes('Количество изображений в 1 задании'));
});

test('Assignee dot is removed, images badge is in title badges without icon, hint is removed from forms', () => {
  const run = app();
  const cardHtml = run("createTaskCardHtml({...state.tasks[1], images_count: 14})");
  // Dot must NOT be present
  assert.ok(!cardHtml.includes('assignee-dot'));
  // Assignee name present
  assert.ok(cardHtml.includes('Анна'));
  // Images badge in task-badges next to status, without icon
  assert.ok(cardHtml.includes('task-badges'));
  assert.ok(cardHtml.includes('14 фото'));
  assert.ok(!cardHtml.includes('#i-image'));

  // Action modal must not contain old hint
  run(`
    const makeEl = () => ({
      innerHTML: '', textContent: '', className: '',
      classList: { add() {}, remove() {}, toggle() {} },
      setAttribute() {}, getAttribute() {}, focus() {}, querySelector() { return makeEl(); },
      addEventListener() {}
    });
    const mockEls = {
      'action-fields': makeEl(),
      'action-title': makeEl(),
      'action-sku': makeEl(),
      'action-error': makeEl(),
      'action-submit': makeEl(),
      'modal-action': makeEl()
    };
    globalThis.document.getElementById = id => mockEls[id] || makeEl();
    globalThis.document.querySelectorAll = () => [];
    globalThis.document.querySelector = () => ({ inert: false });
    globalThis.document.body = { classList: { add() {}, remove() {} } };
  `);
  run("openAction('internal', 2)");
  // Ensure the old hint text is absent and required star is present
  const fields = run("document.getElementById('action-fields').innerHTML");
  assert.ok(!fields.includes('В среднем 8–12 изображений в паке'));
  assert.ok(fields.includes('required-star'));
});

test('Tasks are grouped by pool with stage summaries and collapse support', () => {
  const run = app();
  const groupedHtml = run("renderGroupedTasksHtml(filteredTasks())");

  // Contains pool groups
  assert.ok(groupedHtml.includes('data-pool-name="Обувь"'));
  assert.ok(groupedHtml.includes('data-pool-name="Одежда"'));

  // Contains stage breakdown pills
  assert.ok(groupedHtml.includes('pool-stat-dot'));

  // Toggle collapse
  run("state.collapsedPools.add('Обувь')");
  assert.ok(run("state.collapsedPools.has('Обувь')"));
  const collapsedHtml = run("renderGroupedTasksHtml(filteredTasks())");
  assert.ok(collapsedHtml.includes('is-collapsed'));

  run("state.collapsedPools.delete('Обувь')");
  assert.ok(!run("state.collapsedPools.has('Обувь')"));
});

test('Admin pool edit modal renders current pool SKUs', () => {
  const run = app();
  run("state.isAdmin = true");
  run(`
    const makeEl = () => ({
      innerHTML: '', textContent: '', value: '', className: '',
      classList: { add() {}, remove() {}, toggle() {} },
      setAttribute() {}, getAttribute() {}, focus() {}, querySelector() { return makeEl(); },
      addEventListener() {}
    });
    const mockEls = {
      'edit-pool-title': makeEl(),
      'edit-pool-name': makeEl(),
      'edit-pool-new-skus': makeEl(),
      'edit-pool-source-url': makeEl(),
      'edit-pool-error': makeEl(),
      'edit-pool-skus-count': makeEl(),
      'edit-pool-skus-list': makeEl(),
      'btn-edit-pool-archive-label': makeEl(),
      'modal-edit-pool': makeEl()
    };
    globalThis.document.getElementById = id => mockEls[id] || makeEl();
    globalThis.document.querySelectorAll = () => [];
    globalThis.document.querySelector = () => ({ inert: false });
    globalThis.document.body = { classList: { add() {}, remove() {} } };
  `);

  run("openEditPoolModal('Обувь')");
  const listHtml = run("document.getElementById('edit-pool-skus-list').innerHTML");
  assert.ok(listHtml.includes('130754OUT-99'));
  assert.ok(listHtml.includes('130822OUT-99'));
  assert.ok(listHtml.includes('btn-remove-user'));
});

test('Task detail HTML renders Bitrix time tracking link when present and omits when absent', () => {
  const run = app();
  run(`
    const makeEl = () => ({
      innerHTML: '', textContent: '', value: '', className: '',
      classList: { add() {}, remove() {}, toggle() {} },
      setAttribute() {}, getAttribute() {}, focus() {}, querySelector() { return makeEl(); },
      addEventListener() {}
    });
    const mockEls = {
      'task-detail-content': makeEl(),
      'modal-task': makeEl(),
      'edit-pool-title': makeEl(),
      'edit-pool-name': makeEl(),
      'edit-pool-new-skus': makeEl(),
      'edit-pool-source-url': makeEl(),
      'edit-pool-bitrix-url': makeEl(),
      'edit-pool-error': makeEl(),
      'edit-pool-skus-count': makeEl(),
      'edit-pool-skus-list': makeEl(),
      'btn-edit-pool-archive-label': makeEl(),
      'modal-edit-pool': makeEl()
    };
    globalThis.document.getElementById = id => mockEls[id] || makeEl();
    globalThis.document.querySelectorAll = () => [];
    globalThis.document.querySelector = () => ({ inert: false });
    globalThis.document.body = { classList: { add() {}, remove() {} } };
  `);

  // Task without bitrix_url
  run("state.detailTaskId = 1; renderTaskDetail()");
  let content = run("document.getElementById('task-detail-content').innerHTML");
  assert.ok(!content.includes('detail-link-bitrix'));
  assert.ok(!content.includes('Битрикс24'));

  // Task with bitrix_url
  run("state.tasks[0].bitrix_url = 'https://company.bitrix24.ru/workgroups/task/555/'; renderTaskDetail()");
  content = run("document.getElementById('task-detail-content').innerHTML");
  assert.ok(content.includes('detail-link-bitrix'));
  assert.ok(content.includes('https://company.bitrix24.ru/workgroups/task/555/'));
  assert.ok(content.includes('Битрикс24 (учет времени)'));

  // Edit pool modal populates bitrix_url input
  run("state.isAdmin = true; openEditPoolModal('Обувь')");
  assert.equal(run("document.getElementById('edit-pool-bitrix-url').value"), 'https://company.bitrix24.ru/workgroups/task/555/');
});

test('Designer and admin rework buttons in detail modal and optional revision field', () => {
  const run = app();
  run(`
    const makeEl = () => ({
      innerHTML: '', textContent: '', value: '', className: '',
      classList: { add() {}, remove() {}, toggle() {} },
      setAttribute() {}, getAttribute() {}, focus() {}, querySelector() { return makeEl(); },
      addEventListener() {}
    });
    const mockEls = {
      'task-detail-content': makeEl(),
      'modal-task': makeEl(),
      'modal-action': makeEl(),
      'action-title': makeEl(),
      'action-sku': makeEl(),
      'action-error': makeEl(),
      'action-submit': makeEl(),
      'action-fields': makeEl(),
      'action-notes': makeEl()
    };
    globalThis.document.getElementById = id => mockEls[id] || makeEl();
    globalThis.document.querySelectorAll = () => [];
    globalThis.document.querySelector = () => ({ inert: false });
    globalThis.document.body = { classList: { add() {}, remove() {} } };
  `);

  // 1. Task 3 is in internal_review and assigned to 10001
  // Designer 10001 sees "Забрать на доработку"
  run("state.currentUser = { telegram_id: 10001 }; state.isAdmin = false; state.detailTaskId = 3; renderTaskDetail()");
  let html = run("document.getElementById('task-detail-content').innerHTML");
  assert.ok(html.includes('Забрать на доработку'));
  assert.ok(html.includes("openAction('reject', 3)"));

  // Designer 10002 does NOT see rework button on task 3
  run("state.currentUser = { telegram_id: 10002 }; state.detailTaskId = 3; renderTaskDetail()");
  html = run("document.getElementById('task-detail-content').innerHTML");
  assert.ok(!html.includes('Забрать на доработку'));
  assert.ok(!html.includes("openAction('reject', 3)"));

  // Admin sees "Вернуть на правки"
  run("state.isAdmin = true; state.detailTaskId = 3; renderTaskDetail()");
  html = run("document.getElementById('task-detail-content').innerHTML");
  assert.ok(html.includes('Вернуть на правки'));

  // 2. Task 4 is in client_review and assigned to 10001
  run("state.isAdmin = false; state.currentUser = { telegram_id: 10001 }; state.detailTaskId = 4; renderTaskDetail()");
  html = run("document.getElementById('task-detail-content').innerHTML");
  assert.ok(html.includes('Забрать на доработку'));

  // 3. openAction for designer rework
  run("state.isAdmin = false; openAction('reject', 3)");
  assert.equal(run("document.getElementById('action-title').textContent"), 'Забрать на доработку');
  assert.equal(run("document.getElementById('action-submit').textContent"), 'Забрать на доработку');
  let fieldsHtml = run("document.getElementById('action-fields').innerHTML");
  assert.ok(fieldsHtml.includes('Правки'));
  assert.ok(fieldsHtml.includes('Необязательно'));
  assert.ok(!fieldsHtml.includes('required'));

  // 4. openAction for admin rework
  run("state.isAdmin = true; openAction('reject', 3)");
  assert.equal(run("document.getElementById('action-title').textContent"), 'Вернуть на правки');
  assert.equal(run("document.getElementById('action-submit').textContent"), 'Вернуть дизайнеру');
  fieldsHtml = run("document.getElementById('action-fields').innerHTML");
  assert.ok(fieldsHtml.includes('Правки'));
  assert.ok(fieldsHtml.includes('Необязательно'));
  assert.ok(!fieldsHtml.includes('required'));
});

test('Admin sees assign/reassign buttons in task detail and ACTIONS has assign endpoint', () => {
  const run = app();
  run(`
    const makeEl = () => ({
      classList: { add() {}, remove() {}, toggle() {} },
      innerHTML: '', textContent: '', value: '', dataset: {}, setAttribute() {}, removeAttribute() {}
    });
    const mockEls = {
      'task-detail-content': makeEl(),
      'action-title': makeEl(),
      'action-sku': makeEl(),
      'action-error': makeEl(),
      'action-submit': makeEl(),
      'action-fields': makeEl(),
      'action-notes': makeEl(),
      'action-designer-id': makeEl()
    };
    globalThis.document.getElementById = id => mockEls[id] || makeEl();
    globalThis.document.querySelectorAll = () => [];
    globalThis.document.querySelector = () => ({ inert: false });
    globalThis.document.body = { classList: { add() {}, remove() {} } };
  `);

  // Verify ACTIONS.assign
  assert.equal(run("ACTIONS.assign.endpoint"), 'assign');
  assert.equal(run("ACTIONS.assign.submit"), 'Назначить');

  // Admin on available task (task 1) sees "Назначить дизайнеру"
  run("state.isAdmin = true; state.detailTaskId = 1; renderTaskDetail()");
  let html = run("document.getElementById('task-detail-content').innerHTML");
  assert.ok(html.includes('Назначить дизайнеру'));
  assert.ok(html.includes("openAction('assign', 1)"));

  // Admin on in_progress task (task 2) sees "Переназначить задачу"
  run("state.isAdmin = true; state.detailTaskId = 2; renderTaskDetail()");
  html = run("document.getElementById('task-detail-content').innerHTML");
  assert.ok(html.includes('Переназначить задачу'));
  assert.ok(html.includes("openAction('assign', 2)"));

  // Non-admin does NOT see assign buttons
  run("state.isAdmin = false; state.detailTaskId = 1; renderTaskDetail()");
  html = run("document.getElementById('task-detail-content').innerHTML");
  assert.ok(!html.includes("openAction('assign', 1)"));

  // Workload map computation
  const workload = run("getActiveWorkloadMap()");
  // Task 2, 3, 4, 5 are active for 10001 -> count = 4
  assert.equal(workload[10001], 4);
  // Task 7 is active for 10002 -> count = 1
  assert.equal(workload[10002], 1);
});

test('Both designer and admin can return in_progress task to pool', () => {
  const run = app();
  run(`
    const makeEl = () => ({
      classList: { add() {}, remove() {}, toggle() {} },
      innerHTML: '', textContent: '', value: '', dataset: {}, setAttribute() {}, removeAttribute() {}
    });
    const mockEls = {
      'task-detail-content': makeEl(),
      'task-more-menu': makeEl(),
      'action-title': makeEl(),
      'action-sku': makeEl(),
      'action-error': makeEl(),
      'action-submit': makeEl(),
      'action-fields': makeEl(),
      'action-notes': makeEl(),
      'action-designer-id': makeEl()
    };
    globalThis.document.getElementById = id => mockEls[id] || makeEl();
    globalThis.document.querySelectorAll = () => [];
    globalThis.document.querySelector = () => ({ inert: false });
    globalThis.document.body = { classList: { add() {}, remove() {} } };
  `);

  // Designer assigned to task 2 (in_progress) sees return to pool button
  run("state.currentUser = { telegram_id: 10001 }; state.isAdmin = false; state.detailTaskId = 2; renderTaskDetail()");
  let html = run("document.getElementById('task-detail-content').innerHTML");
  assert.ok(html.includes("openAction('return', 2)"));
  assert.ok(html.includes("Вернуть в свободные"));

  // Admin also sees return to pool button on task 2
  run("state.isAdmin = true; state.detailTaskId = 2; renderTaskDetail()");
  html = run("document.getElementById('task-detail-content').innerHTML");
  assert.ok(html.includes("openAction('return', 2)"));
  assert.ok(html.includes("В свободные"));
  // Admin more menu also includes return to pool
  let menuHtml = run("document.getElementById('task-more-menu').innerHTML");
  assert.ok(menuHtml.includes("openAction('return', 2)"));
});





