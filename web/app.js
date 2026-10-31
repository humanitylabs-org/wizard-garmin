'use strict';
// Connector screen: status, Sync now, Disconnect, and the guided setup (a personal API key from Garmin Connect).
// Reads only this app's documented relative API. Health data itself is shown by the Health app.
const NAME = document.title;
const APP = document.documentElement.dataset.app;
const el = id => document.getElementById(id);
let S = null, SETUP = null, replacing = false, pollTimer = null;

function say(text, kind = '') {
  const s = el('status');
  s.classList.toggle('wz-sr', !kind);
  s.classList.toggle('note', kind === 'note');
  s.textContent = text;
}
async function api(path, data) {
  const init = data === undefined ? {} : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) };
  const r = await fetch(path, init);
  let body = {};
  try { body = await r.json(); } catch (e) {}
  if (!r.ok) throw new Error(body.error || `The ${NAME} app could not do that. Try again.`);
  return body;
}
function h(tag, cls, text) { const n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; }
const dayLabel = (iso, opts = { day: 'numeric', month: 'short', year: 'numeric' }) => iso ? new Date(iso + 'T12:00:00').toLocaleDateString(undefined, opts) : '';
function ago(iso) {
  if (!iso) return 'never';
  const m = Math.round((Date.now() - new Date(iso)) / 60000);
  if (m < 1) return 'just now'; if (m < 60) return m + ' min ago';
  const hrs = Math.round(m / 60); return hrs < 36 ? hrs + ' h ago' : Math.round(hrs / 24) + ' days ago';
}
const n = (v, one, many) => `${v || 0} ${(v || 0) === 1 ? one : many}`;
const setHref = (id, v) => { const e = el(id); if (e && v) e.href = v; };

// ---------- opening the Health app ----------
el('open-health').addEventListener('click', e => {
  try {
    const top = window.top;
    if (top !== window && top.wizardUX && top.location.origin === location.origin) {
      top.dispatchEvent(new CustomEvent('wizard:open', { detail: { app: 'wizard-health', path: '/wizard-health/' } }));
      e.preventDefault();
    }
  } catch (_) { /* not same-origin: follow the link */ }
});

document.addEventListener('click', e => {
  const nx = e.target.closest('[data-next]');
  if (nx) showStep(+nx.dataset.next);
});

// ---------- guided setup ----------
function showStep(k) {
  for (const li of document.querySelectorAll('.hl-step')) {
    const on = +li.dataset.step === k;
    li.classList.toggle('open', on);
    li.querySelector('.hl-step-h').setAttribute('aria-expanded', String(on));
  }
  const li = document.querySelector(`.hl-step[data-step="${k}"]`);
  if (li) requestAnimationFrame(() => li.scrollIntoView({ block: 'nearest', behavior: 'smooth' }));
}
for (const li of document.querySelectorAll('.hl-step')) li.querySelector('.hl-step-h').onclick = () => showStep(+li.dataset.step);

const lasts = d => d === 365 ? '1 year' : `${d} days`;
function renderSetup() {
  setHref('signup-link', SETUP.signup_url);
  setHref('key-page-link', SETUP.key_page_url);
  const saved = SETUP.key_saved;
  el('key-saved').hidden = !saved || replacing;
  el('key-form').hidden = saved && !replacing;
  el('key-replace').hidden = !saved || replacing;
  el('key-test').hidden = !saved || replacing;
  if (saved) {
    el('key-hint').textContent = SETUP.key_hint;
    el('key-expiry').textContent = SETUP.expires_about ? `Made to last ${lasts(SETUP.expiry_days)}, so it runs out around ${dayLabel(SETUP.expires_about)}.` : '';
    if (SETUP.verified && !el('test-result').textContent) showTest({ result: SETUP.verified, message: SETUP.verified === 'valid' ? `Last test: ${NAME} accepted this key.` : SETUP.verified === 'invalid' ? `Last test: ${NAME} did not accept this key. Replace it.` : 'Last test could not confirm the key.' });
  }
  const good = saved && SETUP.verified === 'valid';
  for (const li of document.querySelectorAll('.hl-step')) li.classList.toggle('done', (saved && +li.dataset.step < 4) || (good && +li.dataset.step === 4));
}
function showTest(r) {
  const p = el('test-result');
  p.textContent = r.message;
  p.dataset.kind = r.result;
}

async function saveKey(keyInput, expiry, btn) {
  btn.disabled = true;
  try {
    SETUP = await api('api/key', { api_key: keyInput.value, expiry_days: +expiry.value });
    keyInput.value = ''; replacing = false;
    el('test-result').textContent = `Saved. Checking with ${NAME}\u2026`; el('test-result').dataset.kind = '';
    renderSetup();
    const r = await api('api/key/test', {});
    showTest(r);
    SETUP = await api('api/setup');
    renderSetup();
    await refresh();
    if (r.result === 'valid') poll(3000, 1200);
    return r;
  } catch (err) { el('test-result').textContent = err.message; el('test-result').dataset.kind = 'invalid'; }
  finally { btn.disabled = false; }
}
el('key-form').onsubmit = async e => { e.preventDefault(); await saveKey(el('f-key'), el('f-expiry'), el('key-save')); };
el('key-test').onclick = async () => {
  const b = el('key-test'); b.disabled = true; el('test-result').textContent = `Testing with ${NAME}\u2026`; el('test-result').dataset.kind = '';
  try { showTest(await api('api/key/test', {})); SETUP = await api('api/setup'); renderSetup(); await refresh(); }
  catch (err) { el('test-result').textContent = err.message; }
  finally { b.disabled = false; }
};
el('key-replace').onclick = () => {
  replacing = true; el('test-result').textContent = '';
  renderSetup(); el('f-key').focus();
};

// ---------- sheets ----------
el('open-key').onclick = () => WzSheet.open(el('sheet-key'));
el('rk-form').onsubmit = async e => {
  e.preventDefault();
  const r = await saveKey(el('rk-key'), el('rk-expiry'), e.submitter || el('rk-form').querySelector('button'));
  if (r) { WzSheet.close(el('sheet-key')); say(r.message, r.result === 'valid' ? 'note' : 'error'); }
};
el('open-disconnect').onclick = () => { el('sd-forget').checked = false; WzSheet.open(el('sheet-disconnect')); };
el('sd-go').onclick = async () => {
  const b = el('sd-go'); b.disabled = true;
  try {
    await api('api/disconnect', { forget_key: el('sd-forget').checked });
    WzSheet.close(el('sheet-disconnect'));
    say(`${NAME} disconnected.`, 'note');
    el('setup').dataset.opened = ''; el('test-result').textContent = '';
    await refresh();
  } catch (err) { say(err.message, 'error'); }
  finally { b.disabled = false; }
};
el('sync-now').onclick = async () => {
  const b = el('sync-now'); b.disabled = true;
  try { S = await api('api/sync', {}); render(); poll(3000, 400); } catch (err) { say(err.message, 'error'); }
  finally { b.disabled = false; }
};

// ---------- status ----------
const PILL = { not_configured: ['Not connected', 'off'], needs_login: ['New key needed', 'warn'],
  syncing: ['Syncing', 'ok'], connected: ['Connected', 'ok'], rate_limited: ['Paused', 'warn'] };
const live = () => S && ['connected', 'syncing', 'rate_limited'].includes(S.status);
function connLine() {
  const st = S.status, su = S.setup || {};
  if (st === 'not_configured') return !su.key_saved ? 'Set up below so Health can show your data.'
    : su.key_verified === 'invalid' ? `${NAME} did not accept the saved key. Replace it in step 4.` : 'Key saved. Test it in step 4.';
  if (st === 'needs_login') return S.error || `${NAME} no longer accepts your key. Create a new one and paste it in step 4.`;
  if (st === 'syncing') {
    const p = su.progress;
    return (su.sync_kind === 'backfill' ? `Downloading your ${NAME} history` : 'Syncing') + (p ? ` (${p.kind}: ${p.records})` : '') + '\u2026';
  }
  if (st === 'rate_limited') return `${NAME} asked for a pause; syncing resumes on its own.`;
  return 'Synced ' + ago(S.last_sync);
}
function render() {
  const [label, cls] = PILL[S.status] || ['Unknown', 'off'];
  const pill = el('conn-pill'); pill.textContent = label; pill.className = 'wz-pill ' + cls;
  el('conn-line').textContent = connLine();
  const c = S.counts || {}, su = S.setup || {};
  el('account').hidden = !live();
  if (live()) {
    el('acct-sub').textContent = S.history_from ? `History from ${dayLabel(S.history_from)} \u00b7 ${n(c.days, 'day', 'days')}, ${n(c.sleeps, 'sleep', 'sleeps')}, ${n(c.workouts, 'activity', 'activities')}` : 'Downloading history\u2026';
    el('sync-title').textContent = S.status === 'syncing' ? 'Syncing now' : 'Synced ' + ago(S.last_sync);
    el('sync-sub').textContent = `Checks ${NAME} every ${S.sync_every_minutes} minutes` + (S.next_sync && S.status !== 'syncing' ? ' \u00b7 next ' + new Date(S.next_sync).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' }) : '');
    el('sync-now').disabled = S.status === 'syncing';
  }
  el('key-warn').hidden = !(live() && su.key_expiring);
  el('key-warn').textContent = su.key_days_left != null && su.key_days_left >= 0
    ? `Your Garmin key runs out in about ${n(su.key_days_left, 'day', 'days')}. Create a new key on Garmin Connect and use Replace key below.`
    : 'Your Garmin key may have run out. Create a new key on Garmin Connect and use Replace key below.';
  el('sync-error').hidden = !(live() && S.error);
  el('sync-error').textContent = S.error || '';
  el('more').hidden = !su.key_saved && !live();
  const dl = el('details'); dl.replaceChildren();
  const rows = [['Status', label], ['Last sync', S.last_sync ? new Date(S.last_sync).toLocaleString() : 'never'],
    ['History from', S.history_from ? dayLabel(S.history_from) : '\u2013'], ['Latest day', S.latest_date ? dayLabel(S.latest_date) : '\u2013'],
    ['Saved here', `${n(c.days, 'day', 'days')}, ${n(c.sleeps, 'sleep', 'sleeps')}, ${n(c.workouts, 'activity', 'activities')}`],
    ['Key runs out', su.key_expires_about ? 'about ' + dayLabel(su.key_expires_about) : '\u2013'],
    ['Recovery score', 'Body Battery when you wake (Garmin does not share Training Readiness)'],
    ['Shared as', 'health data for Health (read-only, box-only)'], ['App version', S.version]];
  if (S.error) rows.push(['Last problem', S.error]);
  for (const [k, v] of rows) dl.append(h('dt', '', k), h('dd', '', v));
  const setup = !live();
  el('setup').hidden = !setup;
  if (setup && SETUP) {
    renderSetup();
    if (!el('setup').dataset.opened) { el('setup').dataset.opened = '1'; showStep(su.key_saved ? 4 : 1); }
  }
}

async function refresh() {
  try {
    S = await api('api/status');
    if (!SETUP || !live()) SETUP = await api('api/setup');
    render();
  } catch (err) { say(err.message, 'error'); }
}
function poll(ms, times) {
  clearTimeout(pollTimer);
  let k = 0;
  const tick = async () => {
    await refresh();
    k++;
    const fast = S && S.status === 'syncing' && k < times;
    pollTimer = setTimeout(tick, fast ? 3000 : 60000);
  };
  pollTimer = setTimeout(tick, ms);
}

// Domain-only Back: sheets push their own entries through wizard-sheet.js; the screen itself is the root.
if (!window.WizardApp || WizardApp.version !== 1) {
  say(`Wizard system functions are unavailable. Open ${NAME} through Wizard.`, 'error');
} else {
  WizardApp.ready({ name: APP, version: document.documentElement.dataset.version || '' });
  WizardApp.back.register({ canGoBack: () => false, back: () => false });
}
refresh().then(() => poll(S && S.status === 'syncing' ? 3000 : 60000, 1200));
document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
