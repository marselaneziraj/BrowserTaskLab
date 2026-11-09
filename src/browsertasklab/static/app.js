const $ = id => document.getElementById(id);
const examples = {
  catalog: 'Search for Python books under $40 and return structured results with source URLs.',
  form: 'Prepare a contact draft for Ada Lovelace, email ada@example.test, message "I would like to learn about your fictional mentoring program."',
  appointments: 'Book a simulated appointment for Ada Lovelace, email ada@example.test, on 2030-05-20 10:00 UTC.'
};
const descriptions = {catalog: 'Search a fictional catalog and extract results with source URLs.', form: 'Validate fictional contact details and prepare a draft for review.', appointments: 'Choose a fixed UTC slot on a simulated appointment website.'};
let activeRun = null, snapshot = null, shots = [], shotIndex = -1, followLatest = true;
let reviewDigest = null, lastEvaluation = '', polling = false;
const terminal = new Set(['succeeded', 'blocked', 'budget_exceeded', 'stopped', 'failed']);

function element(tag, text, className) {
  const e = document.createElement(tag);
  if (text !== undefined) e.textContent = text;
  if (className) e.className = className;
  return e;
}
function notify(message) { $('notice').textContent = message; $('notice').hidden = false; clearTimeout(notify.timer); notify.timer = setTimeout(() => $('notice').hidden = true, 6500); }
async function api(path, body) {
  const response = await fetch(path, body === undefined ? {} : {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
  const value = await response.json();
  if (!response.ok) throw new Error(typeof value.detail === 'string' ? value.detail : JSON.stringify(value.detail));
  return value;
}
function switchView(view) {
  $('runs-view').hidden = view !== 'runs'; $('evaluations-view').hidden = view !== 'evaluations';
  $('breadcrumb').textContent = view === 'runs' ? 'Agent runs' : 'Evaluations';
  document.querySelectorAll('[data-view]').forEach(b => b.classList.toggle('active', b.dataset.view === view));
  if (view === 'evaluations') refreshEvaluations().catch(e => notify(e.message));
}
document.querySelectorAll('[data-view]').forEach(b => b.addEventListener('click', () => switchView(b.dataset.view)));
$('workflow').addEventListener('change', () => { $('task').value = examples[$('workflow').value]; $('site-description').textContent = descriptions[$('workflow').value]; });
$('use-example').addEventListener('click', () => $('task').value = examples[$('workflow').value]);
$('task').value = examples.catalog;
$('mode').addEventListener('change', () => {
  $('mode-note').textContent = {mock: 'Scripted policy for demonstrations. No model calls or model performance claims.', baseline: 'Fixed action sequence. A deterministic comparison with no model calls.', llm: 'Actual model calls. Sends your synthetic task and page observations to the configured provider; API charges may apply.'}[$('mode').value];
});
$('task-form').addEventListener('submit', async event => {
  event.preventDefault(); $('start').disabled = true;
  try {
    const data = await api('/api/runs', {task: $('task').value, workflow: $('workflow').value, mode: $('mode').value, max_actions: Number($('max-actions').value), max_seconds: Number($('max-seconds').value), require_review: $('require-review').checked, variant: $('variant').value});
    activeRun = data.id; snapshot = null; shots = []; shotIndex = -1; followLatest = true; reviewDigest = null;
    $('result-data').replaceChildren(); $('result-summary').textContent = 'Working toward a verified outcome…';
    await poll(); await refreshRuns();
  } catch (e) { notify(e.message); $('start').disabled = false; }
});
$('stop').addEventListener('click', async () => { if (activeRun) { try { await api('/api/runs/' + activeRun + '/stop', {}); await poll(); } catch(e) { notify(e.message); } } });

function showShot() {
  if (!shots.length) return;
  shotIndex = Math.max(0, Math.min(shotIndex, shots.length - 1));
  const img = element('img'); img.src = '/api/screenshots/' + encodeURIComponent(shots[shotIndex]); img.alt = 'Masked local demo page after browser observation';
  $('browser-content').replaceChildren(img); $('screenshot-controls').hidden = false;
  $('shot-label').textContent = 'Observation ' + (shotIndex + 1) + ' / ' + shots.length;
  $('previous-shot').disabled = shotIndex === 0; $('next-shot').disabled = shotIndex === shots.length - 1;
}
$('previous-shot').onclick = () => { followLatest = false; shotIndex--; showShot(); };
$('next-shot').onclick = () => { followLatest = false; shotIndex++; showShot(); };
$('latest-shot').onclick = () => { followLatest = true; shotIndex = shots.length - 1; showShot(); };

function renderResult(result) {
  $('result-data').replaceChildren();
  if (!result) return;
  $('result-summary').textContent = result.completed + (result.unresolved.length ? ' · ' + result.unresolved.join(' ') : '');
  if (Array.isArray(result.data) && result.data.length) {
    const table = element('table', undefined, 'result-table');
    const head = element('tr'); ['Name', 'Category', 'USD', 'Source'].forEach(x => head.append(element('th', x))); table.append(head);
    result.data.forEach(row => { const tr = element('tr'); tr.append(element('td', row.name), element('td', row.category), element('td', row.price)); const td = element('td');
      // Only this run's local demo sources become links.
      try { const url = new URL(row.source_url); if (url.origin === location.origin && url.pathname.startsWith('/demo/' + activeRun + '/')) { const a = element('a', 'View source ↗'); a.href = url.href; a.target = '_blank'; a.rel = 'noopener'; td.append(a); } else td.textContent = row.source_url; } catch (_) { td.textContent = 'Invalid source'; }
      tr.append(td); table.append(tr); }); $('result-data').append(table);
  } else if (result.data) $('result-data').append(element('pre', JSON.stringify(result.data, null, 2)));
  if (Array.isArray(result.data) && !result.data.length) $('result-data').append(element('p', 'No matching products. The empty result was verified.'));
}

async function render(state) {
  snapshot = state;
  $('status').textContent = state.status.replaceAll('_', ' '); $('status').className = 'status ' + state.status;
  $('run-mode').textContent = state.request.mode === 'llm' ? 'ACTUAL MODEL · ' + (state.model || 'configuring') : state.request.mode.toUpperCase() + ' · NO MODEL CALLS';
  $('timer').textContent = state.elapsed_seconds.toFixed(1) + 's'; $('stop').disabled = terminal.has(state.status); $('start').disabled = !terminal.has(state.status);
  $('page-url').textContent = state.observation?.url || 'Opening local demo…';
  document.querySelectorAll('[data-phase]').forEach(e => e.classList.toggle('current', state.phase.startsWith(e.dataset.phase)));
  $('action-count').textContent = state.actions + ' / ' + state.request.max_actions + ' actions';
  $('history').replaceChildren();
  state.history.forEach(h => { const li = element('li', undefined, h.ok ? '' : 'error'); const action = h.action;
    const description = action.tool.replaceAll('_', ' ') + (action.target ? ' · ' + action.target.name : '');
    li.append(element('span', String(h.step).padStart(2, '0'), 'action-number'), element('span', description), element('small', h.ok ? '✓' : h.failure_category)); $('history').append(li); });
  $('history').scrollTop = $('history').scrollHeight;
  const newShots = state.screenshots;
  if (newShots.join() !== shots.join()) { shots = newShots; if (followLatest) shotIndex = shots.length - 1; showShot(); }
  $('trace').textContent = JSON.stringify(state, null, 2);
  if (terminal.has(state.status)) refreshRuns().catch(() => {});
  $('result-mode').textContent = state.request.mode === 'llm' ? 'Actual model run' : 'Offline ' + state.request.mode + ' run';
  if (state.result) renderResult(state.result);
  $('review-panel').hidden = state.status !== 'awaiting_review';
  if (state.status === 'awaiting_review' && reviewDigest !== state.pending_review?.digest) {
    const review = await api('/api/runs/' + activeRun + '/review'); reviewDigest = review.digest;
    $('review-data').textContent = JSON.stringify({effect: review.effect, url: review.url, details: review.fields}, null, 2);
    $('approve').disabled = $('decline').disabled = false;
  }
}
async function poll() {
  if (!activeRun || polling) return;
  polling = true;
  try { await render(await api('/api/runs/' + activeRun)); } catch(e) { notify(e.message); } finally { polling = false; }
}
async function decideReview(approve) {
  try { $('approve').disabled = $('decline').disabled = true; await api('/api/runs/' + activeRun + '/review', {approve, digest: reviewDigest}); await poll(); } catch(e) { notify(e.message); }
}
$('approve').onclick = () => decideReview(true); $('decline').onclick = () => decideReview(false);

async function refreshRuns() {
  const data = await api('/api/runs'); $('recent-runs').replaceChildren();
  if (!data.length) $('recent-runs').append(element('p', 'Your runs will appear here.'));
  data.slice(0, 7).forEach(run => { const b = element('button', run.request.workflow + ' · ' + run.status.replaceAll('_', ' ')); b.title = run.request.mode + ' · ' + run.id; b.onclick = async () => { activeRun = run.id; shots = []; followLatest = true; reviewDigest = null; switchView('runs'); await poll(); }; $('recent-runs').append(b); });
}
$('refresh-runs').onclick = () => refreshRuns().catch(e => notify(e.message));

async function refreshEvaluations() {
  const data = await api('/api/evaluations'); $('eval-status').textContent = data.status; $('run-eval').disabled = data.status === 'running';
  const fingerprint = JSON.stringify(data.reports.map(r => r.id)); if (fingerprint === lastEvaluation) return; lastEvaluation = fingerprint;
  if (!data.reports.length) return;
  $('eval-reports').replaceChildren();
  data.reports.forEach(report => {
    const panel = element('section', undefined, 'panel report'); const heading = element('div', undefined, 'panel-heading');
    heading.append(element('h2', report.metadata.planner_mode.toUpperCase() + ' · ' + report.metadata.split.replaceAll('_', ' ')), element('span', report.metadata.model || 'No model calls', 'muted')); panel.append(heading);
    panel.append(element('p', report.metadata.measured_at + ' · ' + report.metadata.python + ' · ' + report.metadata.platform + ' · ' + report.rows.length + ' measured runs', 'report-meta'));
    const grid = element('div', undefined, 'metric-grid');
    const metrics = [['Task success', report.summary.task_success_rate === null ? 'N/A' : Math.round(report.summary.task_success_rate * 100) + '%'], ['Expected behavior', Math.round(report.summary.scenario_pass_rate * 100) + '%'], ['Median latency', report.summary.median_latency_seconds.toFixed(2) + 's'], ['p95 latency', report.summary.p95_latency_seconds.toFixed(2) + 's']];
    metrics.forEach(([label, value]) => { const m = element('div', undefined, 'metric'); m.append(element('strong', value), element('span', label)); grid.append(m); }); panel.append(grid);
    const table = element('table'); const head = element('tr'); ['Scenario', 'Status', 'Independent check', 'Actions', 'Latency'].forEach(x => head.append(element('th', x))); table.append(head);
    report.rows.forEach(row => { const tr = element('tr'); tr.append(element('td', row.scenario), element('td', row.status), element('td', row.scenario_pass ? 'Pass' : 'Fail', row.scenario_pass ? 'pass' : 'fail'), element('td', row.actions), element('td', row.latency_seconds.toFixed(2) + 's')); table.append(tr); }); panel.append(table);
    const detail = element('details'); detail.append(element('summary', 'Full metrics & reproducibility metadata'), element('pre', JSON.stringify({summary: report.summary, metadata: report.metadata, rows: report.rows}, null, 2))); panel.append(detail); $('eval-reports').append(panel);
  });
}
$('run-eval').onclick = async () => { try { await api('/api/evaluations', {mode: $('eval-mode').value, split: $('eval-split').value}); $('eval-status').textContent = 'running'; $('run-eval').disabled = true; } catch(e) { notify(e.message); } };
setInterval(() => { if (activeRun && (!snapshot || !terminal.has(snapshot.status))) poll(); if (!$('evaluations-view').hidden) refreshEvaluations().catch(() => {}); }, 800);
Promise.all([refreshRuns(), api('/api/config').then(config => { if (!config.llm_configured) $('mode').querySelector('[value=llm]').textContent = 'LLM agent · API key needed'; })]).catch(e => notify(e.message));
