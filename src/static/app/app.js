// The app at /: a phone layout over /api/app/. Plain JS, no build step.
// Screens are rendered with h``, which escapes every value it's given
// unless it's wrapped in raw(). Routes live in the URL hash:
//   main tabs  #/trans/<daily|calendar|monthly|total>/<YYYY-MM>
//              #/stats/<week|month|year>/<YYYY-MM-DD>/<0|1>, #/accounts, #/more
//              (a custom period: #/stats/custom/<from>/<0|1>/<days after from>)
//   pages      #/cat/<0|1>/<root uid>, #/account/<uid>/<daily|monthly|annually>/<YYYY-MM-DD>, #/search,
//              #/bookmarks, #/manage/accounts, #/manage/categories/<0|1>
//   form       #/add[/<YYYY-MM-DD>[/<account uid>]], #/edit/<uid>
// Pages and the form sit over the main screen, and the manage pages over the
// form when its pickers open them; an open calendar day is
// history.state.sheet. Back closes whichever is on top.
'use strict';

const CFG = JSON.parse(document.getElementById('app-config').textContent);
const LOCALE = CFG.locale || undefined;

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

const ESC = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
class Raw { constructor(s) { this.s = s; } toString() { return this.s; } }
const raw = s => new Raw(s);
function piece(v) {
  if (v instanceof Raw) return v.s;
  if (Array.isArray(v)) return v.map(piece).join('');
  if (v === null || v === undefined || v === false) return '';
  return String(v).replace(/[&<>"']/g, c => ESC[c]);
}
function h(strings, ...values) {
  let out = strings[0];
  values.forEach((v, i) => { out += piece(v) + strings[i + 1]; });
  return raw(out);
}
const $ = (sel, root = document) => root.querySelector(sel);

const ICONS = {
  left: '<path d="M15 5l-7 7 7 7"/>',
  right: '<path d="M9 5l7 7-7 7"/>',
  down: '<path d="M6 9l6 6 6-6"/>',
  back: '<path d="M20 12H5M11 5l-7 7 7 7"/>',
  plus: '<path d="M12 4v16M4 12h16"/>',
  close: '<path d="M6 6l12 12M18 6L6 18"/>',
  trans: '<path d="M6 3h12v18l-2-1.4-2 1.4-2-1.4-2 1.4-2-1.4-2 1.4z"/><path d="M9.5 8h5M9.5 11.5h5M9.5 15h3"/>',
  stats: '<path d="M11 3.6a8.5 8.5 0 109.4 9.4H11z"/><path d="M14 3.5a7 7 0 016.5 6.5H14z"/>',
  accounts: '<path d="M4.5 8V6.8A1.8 1.8 0 016.3 5H17v3"/><rect x="4.5" y="8" width="16" height="11" rx="2"/>'
    + '<path d="M15.5 13.5h2.2"/>',
  more: '<rect x="4.5" y="4.5" width="6" height="6" rx="1.5"/><rect x="13.5" y="4.5" width="6" height="6" rx="1.5"/>'
    + '<rect x="4.5" y="13.5" width="6" height="6" rx="1.5"/><rect x="13.5" y="13.5" width="6" height="6" rx="1.5"/>',
  backspace: '<path d="M21 6H9l-6 6 6 6h12z"/><path d="M12.5 9.5l5 5M17.5 9.5l-5 5"/>',
  trash: '<path d="M4 7h16M10 11v6M14 11v6M9 7V4h6v3M6 7l1 13h10l1-13"/>',
  copy: '<rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V5a1 1 0 00-1-1H5a1 1 0 00-1 1v10a1 1 0 001 1h3"/>',
  editor: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 9h18M3 14h18M9 9v11"/>',
  settings: '<path d="M4 6h9M17 6h3M4 12h3M11 12h9M4 18h11M19 18h1"/>'
    + '<circle cx="15" cy="6" r="2"/><circle cx="9" cy="12" r="2"/><circle cx="17" cy="18" r="2"/>',
  sync: '<path d="M20 12a8 8 0 01-14.3 4.9M4 12a8 8 0 0114.3-4.9"/><path d="M18.5 3v4.2h-4.2M5.5 21v-4.2h4.2"/>',
  users: '<circle cx="9" cy="8" r="3.5"/><path d="M2.5 20a6.5 6.5 0 0113 0"/><path d="M16 4.5a3.5 3.5 0 010 7M18 14a6 6 0 013.5 6"/>',
  logout: '<path d="M10 4H5v16h5M14 8l4 4-4 4M18 12H9"/>',
  search: '<circle cx="11" cy="11" r="7"/><path d="M20 20l-4-4"/>',
  filter: '<path d="M4 7h10M18 7h2M4 17h4M12 17h8"/><circle cx="16" cy="7" r="2"/><circle cx="10" cy="17" r="2"/>',
  budget: '<path d="M4.5 16.5a7.5 7.5 0 1115 0"/><path d="M12 16.5l3.5-4.5"/>',
  star: '<path d="M12 3.8l2.5 5.1 5.6.8-4 3.9.9 5.6-5-2.6-5 2.6.9-5.6-4-3.9 5.6-.8z"/>',
  chart: '<path d="M4 19.5h16"/><path d="M5 15.5l4-5 4 3 6-7"/>',
  pencil: '<path d="M4.5 19.5h4l10-10-4-4-10 10z"/><path d="M13 7l4 4"/>',
  up: '<path d="M12 18.5v-13M6.5 11L12 5.5 17.5 11"/>',
  dn: '<path d="M12 5.5v13M6.5 13l5.5 5.5 5.5-5.5"/>',
  eye: '<path d="M2.5 12S6 5.8 12 5.8 21.5 12 21.5 12 18 18.2 12 18.2 2.5 12 2.5 12z"/><circle cx="12" cy="12" r="2.8"/>',
  eyeoff: '<path d="M2.5 12S6 5.8 12 5.8 21.5 12 21.5 12 18 18.2 12 18.2 2.5 12 2.5 12z"/><path d="M4 4l16 16"/>',
  list: '<path d="M9 6.5h11M9 12h11M9 17.5h11"/><circle cx="5" cy="6.5" r="1" fill="currentColor" stroke="none"/>'
    + '<circle cx="5" cy="12" r="1" fill="currentColor" stroke="none"/><circle cx="5" cy="17.5" r="1" fill="currentColor" stroke="none"/>',
};
const icon = name => raw(`<svg class="i" viewBox="0 0 24 24" aria-hidden="true">${ICONS[name]}</svg>`);

const pad2 = n => String(n).padStart(2, '0');
const iso = d => `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`;
const ym = d => `${d.getFullYear()}-${pad2(d.getMonth() + 1)}`;
function parseIso(s) {
  const [y, m, d] = s.split('-').map(Number);
  return new Date(y, (m || 1) - 1, d || 1);
}
const isIsoDate = s => /^\d{4}-\d\d-\d\d$/.test(s || '');
const addDays = (d, n) => new Date(d.getFullYear(), d.getMonth(), d.getDate() + n);
const monthStart = d => new Date(d.getFullYear(), d.getMonth(), 1);
const monthEnd = d => new Date(d.getFullYear(), d.getMonth() + 1, 0);
let WEEK_START = 1;  // the app's week_start_day: 0 Sunday .. 6 Saturday
const weekStart = d => addDays(d, -((d.getDay() - WEEK_START + 7) % 7));
const mmdd = d => `${pad2(d.getMonth() + 1)}.${pad2(d.getDate())}`;

const monthFmt = new Intl.DateTimeFormat(LOCALE, { month: 'short', year: 'numeric' });
const monthNameFmt = new Intl.DateTimeFormat(LOCALE, { month: 'short' });
const weekdayFmt = new Intl.DateTimeFormat(LOCALE, { weekday: 'short' });
const shortDateFmt = new Intl.DateTimeFormat(LOCALE, { year: '2-digit', month: 'numeric', day: 'numeric' });
const numFmts = {};
function num(v, decimals = 2) {
  numFmts[decimals] ??= new Intl.NumberFormat(LOCALE, { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
  return numFmts[decimals].format(v || 0);
}

let META = null;
const currency = uid => META.currencies[uid] || META.currencies[META.main_currency] || { symbol: '', decimals: 2 };
const money = (v, uid) => `${currency(uid).symbol} ${num(v, currency(uid).decimals)}`.trim();
const mainMoney = v => money(v, META.main_currency);
const mainNum = v => num(v, currency(META.main_currency).decimals);
const accountBy = uid => META.accounts.find(a => a.uid === uid);

let toastTimer;
function toast(message) {
  const el = $('#toast');
  el.textContent = message;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, 2600);
}

// A small form over everything. fields: [{name, label, type (text, number,
// date, select), value, options: [[value, label]]}]. Resolves with
// {name: value}, or null if cancelled.
function dialog(title, fields, ok = 'Save', note = '') {
  return new Promise(resolve => {
    const el = $('#dialog');
    el.innerHTML = h`<form class="dlg" novalidate><div class="dhead">${title}</div>${note && h`<div class="dnote">${note}</div>`}
      ${fields.map(f => h`<label class="dfield"><span>${f.label}</span>${f.type === 'select'
        ? h`<select name="${f.name}">${f.options.map(([v, l]) => h`<option value="${v}" ${v === f.value ? raw('selected') : ''}>${l}</option>`)}</select>`
        : h`<input name="${f.name}" type="${f.type === 'date' ? 'date' : 'text'}" ${f.type === 'number' ? raw('inputmode="decimal"') : ''}
            value="${f.value ?? ''}" autocomplete="off">`}</label>`)}
      <div class="dbtns"><button type="button" class="cancel">Cancel</button><button type="submit" class="ok">${ok}</button></div></form>`;
    el.hidden = false;
    const form = el.querySelector('form');
    const done = v => {
      el.hidden = true;
      el.innerHTML = '';
      resolve(v);
    };
    form.addEventListener('submit', e => {
      e.preventDefault();
      done(Object.fromEntries(new FormData(form)));
    });
    form.querySelector('.cancel').addEventListener('click', () => done(null));
    form.querySelector('input, select').focus();
  });
}
const parseNumber = text => parseFloat(String(text).replace(/\s/g, '').replace(',', '.'));

async function api(path, { method = 'GET', body } = {}) {
  const init = { method, headers: { Accept: 'application/json' }, credentials: 'same-origin' };
  if (body !== undefined) {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(body);
  }
  if (method !== 'GET') init.headers['X-CSRF-Token'] = CFG.csrf;
  let r;
  try {
    r = await fetch(path, init);
  } catch (e) {
    throw new Error("Can't reach the server.");
  }
  if (r.status === 401) {
    location.href = '/login?next=' + encodeURIComponent('/' + location.hash);
    throw new Error('Not logged in.');
  }
  const data = await r.json().catch(() => ({}));
  if (!r.ok || data.ok === false) throw new Error(data.error || `The server answered ${r.status}.`);
  return data;
}

// GET answers, kept until anything is written.
const cache = new Map();
function cached(path) {
  if (!cache.has(path)) {
    cache.set(path, api(path).catch(e => {
      cache.delete(path);
      throw e;
    }));
  }
  return cache.get(path);
}
const query = params => new URLSearchParams(params).toString();
const loadDays = (from, to, extra = {}) => cached(`/api/app/days?${query({ from, to, ...extra })}`).then(d => d.days);
const loadSums = (from, to, extra = {}) => cached(`/api/app/sums?${query({ from, to, ...extra })}`)
  .then(d => Object.fromEntries(d.days.map(x => [x.date, x])));
async function reloadData() {
  cache.clear();
  META = await api('/api/app/meta');
  WEEK_START = META.week_start;
}
const currencyOptions = () => Object.entries(META.currencies)
  .sort((a, b) => (a[1].order ?? 1e9) - (b[1].order ?? 1e9)).map(([uid, c]) => [uid, `${c.symbol} (${c.iso})`]);

// Income and expense of the days in [from, to] of a loadSums() map.
function sumRange(byDate, from, to) {
  let income = 0;
  let expense = 0;
  for (let d = from; d <= to; d = addDays(d, 1)) {
    const x = byDate[iso(d)];
    if (x) {
      income += x.income;
      expense += x.expense;
    }
  }
  return [income, expense];
}

// ---------------------------------------------------------------------------
// State and routing
// ---------------------------------------------------------------------------

const TABS = ['trans', 'stats', 'accounts', 'more'];
const SUBS = ['daily', 'calendar', 'monthly', 'total'];
const PERIODS = { week: 'Weekly', month: 'Monthly', year: 'Annually', custom: 'Period' };
const PAGES = ['cat', 'account', 'search', 'bookmarks', 'manage'];
const state = { tab: 'trans', sub: 'daily', month: monthStart(new Date()) };
// len: a custom period's days after date.
const stats = { period: 'month', date: monthStart(new Date()), type: '1', len: 30 };
let routeKey = null;

function mainHash() {
  if (state.tab === 'trans') return `#/trans/${state.sub}/${ym(state.month)}`;
  if (state.tab === 'stats') {
    return `#/stats/${stats.period}/${iso(stats.date)}/${stats.type}${stats.period === 'custom' ? `/${stats.len}` : ''}`;
  }
  return `#/${state.tab}`;
}
// Replace the current route (no new history entry) and show it.
function replaceRoute(hash, histState = history.state) {
  history.replaceState(histState, '', hash);
  onRoute();
}
const setMain = () => replaceRoute(mainHash());
const go = hash => { location.hash = hash; };
const back = () => history.back();
const isOverlay = hash => /^#\/(add|edit|cat|account|search|bookmarks|manage)\b/.test(hash);

function onRoute() {
  const key = location.hash + JSON.stringify(history.state);
  if (key === routeKey) return;
  const parts = location.hash.replace(/^#\/?/, '').split('/').map(decodeURIComponent);
  const toForm = parts[0] === 'add' || parts[0] === 'edit';
  // The form's pickers open the manage pages over it, keeping what's typed.
  const overForm = parts[0] === 'manage' && F && !F.saved;
  // Back (or any way out) from a form with unsaved changes asks first, and
  // stays on the form if the answer is no.
  if (F && !toForm && !overForm && !F.saved && formDirty() && !confirm('Discard the changes to this transaction?')) {
    history.pushState(null, '', F.hash);
    routeKey = location.hash + JSON.stringify(history.state);
    return;
  }
  routeKey = key;
  if (toForm) {
    if (F && F.hash === location.hash) {
      closePage();
      renderForm();
    } else {
      openForm(parts[0], parts.slice(1));
    }
    return;
  }
  if (overForm) {
    openPage(parts, true);
    return;
  }
  closeFormScreen();
  if (PAGES.includes(parts[0])) {
    openPage(parts);
    return;
  }
  closePage();
  if (TABS.includes(parts[0])) state.tab = parts[0];
  if (state.tab === 'trans') {
    if (SUBS.includes(parts[1])) state.sub = parts[1];
    if (/^\d{4}-\d\d$/.test(parts[2] || '')) state.month = parseIso(parts[2]);
  } else if (state.tab === 'stats') {
    if (parts[1] in PERIODS) stats.period = parts[1];
    if (isIsoDate(parts[2])) stats.date = parseIso(parts[2]);
    if (parts[3] === '0' || parts[3] === '1') stats.type = parts[3];
    if (/^\d+$/.test(parts[4] || '')) stats.len = Number(parts[4]);
  }
  renderMain();
  const day = history.state && history.state.sheet;
  if (day) renderSheet(day); else $('#sheet').hidden = true;
}
window.addEventListener('popstate', onRoute);
window.addEventListener('hashchange', onRoute);

// ---------------------------------------------------------------------------
// Shared pieces
// ---------------------------------------------------------------------------

let renderToken = 0;

function navTitle(title, prev = 'prev', next = 'next') {
  return h`<button type="button" class="nav-btn" data-act="${prev}" aria-label="Previous">${icon('left')}</button>
    <span class="title">${title}</span>
    <button type="button" class="nav-btn" data-act="${next}" aria-label="Next">${icon('right')}</button>`;
}

function summary(cells) {
  return h`<div class="summary" style="grid-template-columns: repeat(${cells.length}, 1fr)">${cells.map(([label, value, cls]) =>
    h`<div><div class="lbl">${label}</div><div class="val ${cls}">${value}</div></div>`)}</div>`;
}
const incExpSummary = (income, expense) => summary([
  ['Income', mainNum(income), 'inc'], ['Expenses', mainNum(expense), 'exp'], ['Total', mainNum(income - expense), '']]);

function subtabs(items, current, act) {
  return h`<div class="subtabs">${items.map(([key, label]) => h`<button type="button"
    class="${key === current ? 'on' : ''}" data-act="${act}" data-key="${key}">${label}</button>`)}</div>`;
}

function dayHead(day, [inc, exp] = [day.income, day.expense], cur = META.main_currency) {
  const d = parseIso(day.date);
  const wd = d.getDay();
  return h`<div class="dayhead"><span class="dnum">${pad2(d.getDate())}</span>
    <span class="badge ${wd === 6 ? 'sat' : wd === 0 ? 'sun' : ''}">${weekdayFmt.format(d)}</span>
    <span class="dmy">${pad2(d.getMonth() + 1)}.${d.getFullYear()}</span>
    <span class="dinc inc">${inc ? money(inc, cur) : ''}</span><span class="dexp exp">${exp ? money(exp, cur) : ''}</span></div>`;
}

const dayBlock = day => h`<section class="day">${dayHead(day)}${day.rows.map(r => txRow(r))}</section>`;

// A transaction row. With a balance (an account's statement), the amount
// is coloured by what it does to that account.
function txRow(r, { balance, first } = {}) {
  const transfer = r.type === '3' || r.type === '4';
  const adjustment = r.type === '7' || r.type === '8';
  const left = transfer ? h`<div class="cat">Transfer</div>`
    : adjustment ? h`<div class="cat">Adjustment</div>`
      : h`<div class="cat">${r.category}</div>${r.subcategory && h`<div class="sub">${r.subcategory}</div>`}`;
  // A receiving leg's account is where the money arrived.
  const where = r.type === '3' ? `${r.account} → ${r.to_account}`
    : r.type === '4' ? `${r.to_account} → ${r.account}` : r.account;
  const desc = r.description && h`<span class="desc">${r.description}</span>`;
  const top = r.note ? h`<span class="note">${r.note}</span>${desc}` : h`${where}${desc}`;
  const bottom = r.note ? h`${r.time}<span class="acct">${where}</span>` : r.time;
  const adds = ['0', '7', '4'].includes(r.type);
  const cls = balance === undefined
    ? (r.type === '0' || r.type === '7' ? 'inc' : r.type === '1' || r.type === '8' ? 'exp' : 'tr')
    : (adds ? 'inc' : 'exp');
  return h`<div class="row" data-uid="${r.uid}"><div class="c1">${left}</div>
    <div class="c2"><div class="top">${top}</div><div class="bot">${bottom}</div></div>
    <div class="amt ${cls}">${money(r.amount, r.currency)}${balance !== undefined
      && h`<div class="bal">(${first ? 'Balance  ' : ''}${num(balance, currency(r.currency).decimals)})</div>`}</div></div>`;
}

// ---------------------------------------------------------------------------
// Main screen
// ---------------------------------------------------------------------------

function renderMain() {
  $('#fab').hidden = state.tab !== 'trans';
  $('#bottomnav').innerHTML = h`${[
    ['trans', 'Trans.'], ['stats', 'Stats'], ['accounts', 'Accounts'], ['more', 'More'],
  ].map(([tab, label]) => h`<button type="button" class="${tab === state.tab ? 'on' : ''}" data-act="tab" data-tab="${tab}">
      ${icon(tab)}<span>${label}</span></button>`)}`;
  ({ trans: renderTrans, stats: renderStats, accounts: renderAccounts, more: renderMore })[state.tab]();
}

// Swap a view's content in, keeping its list's scroll position if it shows
// the same thing as before.
function show(view, content, what) {
  const top = view.dataset.shown === what ? view.querySelector('.scroll')?.scrollTop : 0;
  view.innerHTML = content;
  view.dataset.shown = what;
  if (top) view.querySelector('.scroll').scrollTop = top;
}

async function renderTrans() {
  const token = ++renderToken;
  const view = $('#view');
  const monthly = state.sub === 'monthly';
  $('#topbar').innerHTML = h`${navTitle(monthly ? String(state.month.getFullYear()) : monthFmt.format(state.month))}
    <span class="spacer"></span>
    <button type="button" class="icon-btn" data-act="bookmarks" aria-label="Bookmarks">${icon('star')}</button>
    <button type="button" class="icon-btn" data-act="search" aria-label="Search">${icon('search')}</button>`;
  const tabs = subtabs([['daily', 'Daily'], ['calendar', 'Calendar'], ['monthly', 'Monthly'], ['total', 'Total']],
    state.sub, 'sub');
  try {
    if (state.sub === 'total') {
      const [byDate, data] = await Promise.all([
        loadSums(iso(state.month), iso(monthEnd(state.month))), cached(`/api/app/total?month=${ym(state.month)}`)]);
      if (token !== renderToken) return;
      const [income, expense] = sumRange(byDate, state.month, monthEnd(state.month));
      show(view, h`${tabs}${incExpSummary(income, expense)}<div class="scroll">${totalTab(data)}</div>`,
        `total${ym(state.month)}`);
      return;
    }
    if (monthly) {
      const year = state.month.getFullYear();
      const byDate = await loadSums(iso(addDays(new Date(year, 0, 1), -6)), iso(addDays(new Date(year, 11, 31), 6)));
      if (token !== renderToken) return;
      const [income, expense] = sumRange(byDate, new Date(year, 0, 1), new Date(year, 11, 31));
      show(view, h`${tabs}${incExpSummary(income, expense)}<div class="scroll">${monthList(byDate, year)}</div>`,
        `monthly${year}`);
      return;
    }
    const calendar = state.sub === 'calendar';
    const [from, to] = calendar ? calendarRange(state.month) : [state.month, monthEnd(state.month)];
    const days = await loadDays(iso(from), iso(to));
    if (token !== renderToken) return;
    const [income, expense] = days.filter(d => d.date.startsWith(ym(state.month)))
      .reduce((t, d) => [t[0] + d.income, t[1] + d.expense], [0, 0]);
    show(view, calendar
      ? h`${tabs}${incExpSummary(income, expense)}${calendarGrid(days)}`
      : h`${tabs}${incExpSummary(income, expense)}<div class="scroll">${days.length
        ? days.map(dayBlock) : h`<div class="empty">No transactions this month.</div>`}</div>`,
    state.sub + ym(state.month));
  } catch (e) {
    if (token === renderToken) view.innerHTML = h`${tabs}<div class="scroll"><div class="empty">${e.message}</div></div>`;
  }
}

// Calendar: six weeks from the week the month starts in.
function calendarRange(month) {
  const start = weekStart(month);
  return [start, addDays(start, 41)];
}

function calendarGrid(days) {
  const byDate = Object.fromEntries(days.map(d => [d.date, d]));
  const [start] = calendarRange(state.month);
  const today = iso(new Date());
  const weekdays = [0, 1, 2, 3, 4, 5, 6].map(i => addDays(start, i));
  const nbsp = raw('&nbsp;');
  const cells = [];
  for (let i = 0; i < 42; i++) {
    const d = addDays(start, i);
    const key = iso(d);
    const day = byDate[key] || { income: 0, expense: 0 };
    const label = d.getDate() === 1 ? `${d.getMonth() + 1}.1` : d.getDate();
    cells.push(h`<div class="cell ${d.getMonth() === state.month.getMonth() ? '' : 'out'}" data-date="${key}">
      <div class="n ${d.getDay() === 0 ? 'sun' : ''} ${key === today ? 'today' : ''}">${label}</div>
      <div class="v"><div class="inc">${day.income ? mainNum(day.income) : nbsp}</div>
        <div class="exp">${day.expense ? mainNum(day.expense) : nbsp}</div>
        <div>${day.income && day.expense ? mainNum(day.income - day.expense) : nbsp}</div></div></div>`);
  }
  const cls = d => (d.getDay() === 6 ? 'sat' : d.getDay() === 0 ? 'sun' : '');
  return h`<div class="cal"><div class="wdays">${weekdays.map(d => h`<div class="${cls(d)}">${weekdayFmt.format(d)}</div>`)}</div>
    <div class="grid">${cells}</div></div>`;
}

// Monthly: the year's months, newest first, the selected one split into weeks.
function monthList(byDate, year) {
  const now = new Date();
  const last = year === now.getFullYear() ? now.getMonth() : year > now.getFullYear() ? -1 : 11;
  const thisWeek = iso(weekStart(now));
  const out = [];
  for (let m = last; m >= 0; m--) {
    const first = new Date(year, m, 1);
    const end = monthEnd(first);
    const [income, expense] = sumRange(byDate, first, end);
    const current = year === now.getFullYear() && m === now.getMonth();
    out.push(h`<div class="prow" data-act="month" data-month="${ym(first)}">
      <div class="pname"><b>${monthNameFmt.format(first)}</b>${current && h`<small>${mmdd(first)} ~ ${mmdd(end)}</small>`}</div>
      <div class="pinc inc">${mainMoney(income)}</div>
      <div class="pexp"><div class="exp">${mainMoney(expense)}</div><small>${mainMoney(income - expense)}</small></div></div>`);
    if (m !== state.month.getMonth()) continue;
    for (let w = weekStart(end); w >= weekStart(first); w = addDays(w, -7)) {
      const [wi, we] = sumRange(byDate, w, addDays(w, 6));
      const cur = iso(w) === thisWeek;
      out.push(h`<div class="prow week ${cur ? 'cur' : ''}" data-act="week" data-month="${ym(first)}">
        <div class="pname">${mmdd(w)} ~ ${mmdd(addDays(w, 6))}</div>
        <div class="pinc inc">${mainMoney(wi)}</div>
        <div class="pexp"><div class="exp">${mainMoney(we)}</div><small>${cur ? 'Total ' : ''}${mainMoney(wi - we)}</small></div></div>`);
    }
  }
  return out.length ? out : h`<div class="empty">Nothing yet this year.</div>`;
}

// One day's transactions in a bottom sheet over the calendar.
async function renderSheet(date) {
  const sheet = $('#sheet');
  sheet.hidden = false;
  let days;
  try {
    days = await loadDays(date, date);
  } catch (e) {
    toast(e.message);
    return;
  }
  const day = days[0] || { date, income: 0, expense: 0, rows: [] };
  sheet.innerHTML = h`<div class="panel-wrap">${dayHead(day)}
    <div class="list">${day.rows.length ? day.rows.map(r => txRow(r)) : h`<div class="empty">No transactions.</div>`}</div>
    <button type="button" class="fab" data-act="add-on" data-date="${date}" aria-label="Add">${icon('plus')}</button>
    <div class="foot">
      <button type="button" class="nav" data-act="sheet-move" data-by="-1" aria-label="Previous day">${icon('left')}</button>
      <button type="button" class="nav" data-act="sheet-move" data-by="1" aria-label="Next day">${icon('right')}</button>
      <button type="button" class="close" data-act="sheet-close">Close</button></div></div>`;
}

// Total: the month's budgets, and how its spending compares.
const openBudgets = new Set();

function budgetRow(b, child = false) {
  const ratio = b.amount > 0 ? b.spent / b.amount : 0;
  const over = b.spent > b.amount;
  const open = openBudgets.has(b.uid);
  return h`<div class="budget ${child ? 'child' : ''}" ${b.children.length ? raw(`data-act="budget" data-uid="${piece(b.uid)}"`) : ''}>
      <div class="bl"><div class="bname">${b.name}${b.children.length ? icon(open ? 'down' : 'right') : ''}</div>
        <div class="bamt">${mainMoney(b.amount)}</div></div>
      <div class="br"><div class="bar"><div class="fill ${over ? 'over' : ''}" style="width:${Math.min(100, ratio * 100).toFixed(1)}%"></div>
        <span>${Math.floor(ratio * 100)}%</span></div>
        <div class="bfoot"><span class="exp">${mainMoney(b.spent)}</span>
          <span>${over ? 'Over ' : 'Left '}${mainMoney(Math.abs(b.amount - b.spent))}</span></div></div></div>
    ${open && b.children.map(c => budgetRow(c, true))}`;
}

function totalTab(data) {
  const first = state.month;
  return h`<div class="tblock"><div class="thead">${icon('budget')}<span>Budget</span></div>
      ${data.budgets.length ? data.budgets.map(b => budgetRow(b)) : h`<div class="tnote">No budgets are set in Money Manager.</div>`}</div>
    <div class="tblock"><div class="thead">${icon('accounts')}<span>Accounts</span>
      <span class="trange">${fmtShort(first)}  ~  ${fmtShort(monthEnd(first))}</span></div>
      <div class="tbox">
        <div><span>Compared Expenses <i>(Last month)</i></span>
          <span>${data.previous ? `${Math.round((100 * data.expenses) / data.previous)}%` : '-'}</span></div>
        <div><span>Expenses <i>(Cash)</i></span><span>${mainMoney(data.cash)}</span></div>
      </div></div>`;
}

// --- Stats -------------------------------------------------------------------

function statsRange(period = stats.period, date = stats.date) {
  if (period === 'custom') return [date, addDays(date, stats.len)];
  if (period === 'year') return [new Date(date.getFullYear(), 0, 1), new Date(date.getFullYear(), 11, 31)];
  if (period === 'week') return [weekStart(date), addDays(weekStart(date), 6)];
  return [monthStart(date), monthEnd(date)];
}
function statsTitle(period = stats.period, date = stats.date) {
  if (period === 'custom') return `${fmtShort(date)} ~ ${fmtShort(addDays(date, stats.len))}`;
  if (period === 'year') return String(date.getFullYear());
  if (period === 'week') return `${mmdd(weekStart(date))} ~ ${mmdd(addDays(weekStart(date), 6))}`;
  return monthFmt.format(date);
}
function movePeriod(date, period, by) {
  if (period === 'custom') return addDays(date, by * (stats.len + 1));
  if (period === 'year') return new Date(date.getFullYear() + by, 0, 1);
  if (period === 'week') return addDays(weekStart(date), 7 * by);
  return new Date(date.getFullYear(), date.getMonth() + by, 1);
}
const loadStats = () => {
  const [from, to] = statsRange();
  return cached(`/api/app/stats?${query({ from: iso(from), to: iso(to) })}`);
};

const PALETTE = ['#e85d75', '#f29e4c', '#f1c453', '#83c167', '#4cb5ae', '#5a9bd8', '#8d7ad6', '#d672b5', '#c49a6c',
  '#9fb94d', '#61c3e2', '#a3a9b5'];
const colour = i => PALETTE[i % PALETTE.length];
const pct = (part, whole) => (whole ? (100 * part) / whole : 0);
// A share for a list: a small one shows as <1 rather than 0.
const share = (part, whole) => {
  const p = pct(part, whole);
  return p > 0 && p < 0.5 ? '<1' : String(Math.round(p));
};

let periodMenu = false;

async function renderStats() {
  const token = ++renderToken;
  $('#topbar').innerHTML = h`${navTitle(statsTitle())}<span class="spacer"></span>
    <button type="button" class="period" data-act="period-menu">${PERIODS[stats.period]}${icon('down')}</button>
    ${periodMenu && h`<div class="menu">${Object.entries(PERIODS).map(([k, label]) =>
      h`<button type="button" data-act="period" data-key="${k}">${label}</button>`)}</div>`}`;
  const view = $('#view');
  let data;
  try {
    data = await loadStats();
  } catch (e) {
    view.innerHTML = h`<div class="scroll"><div class="empty">${e.message}</div></div>`;
    return;
  }
  if (token !== renderToken) return;
  const side = stats.type === '0' ? data.income : data.expense;
  const tabs = h`<div class="subtabs two">
    <button type="button" class="${stats.type === '0' ? 'on' : ''}" data-act="stats-type" data-key="0">Income&nbsp; ${mainMoney(data.income.total)}</button>
    <button type="button" class="${stats.type === '1' ? 'on' : ''}" data-act="stats-type" data-key="1">Expenses&nbsp; ${mainMoney(data.expense.total)}</button></div>`;
  const cats = side.categories.filter(c => c.amount > 0);
  show(view, h`${tabs}<div class="scroll">${cats.length
    ? h`<div class="pie">${pie(cats, side.total)}</div><div class="slist">${cats.map((c, i) => h`<div class="srow"
        data-act="cat" data-uid="${c.uid}"><span class="pct" style="background:${colour(i)}">${share(c.amount, side.total)}%</span>
        <span class="name">${c.name || '(no category)'}</span><span class="amt">${mainMoney(c.amount)}</span></div>`)}</div>`
    : h`<div class="empty">No data available.</div>`}</div>`, `stats${statsTitle()}${stats.type}`);
}

// A pie from 12 o'clock clockwise, each slice's name and share in a column
// beside it, pushed apart so they don't overlap, with a line to the slice.
function pie(cats, total) {
  const W = 412;
  const H = 300;
  const cx = W / 2;
  const cy = H / 2;
  const r = 90;
  const gap = 31;
  let a = -Math.PI / 2;
  const slices = [];
  const labels = [];
  const at = (ang, rad) => [cx + rad * Math.cos(ang), cy + rad * Math.sin(ang)];
  cats.forEach((c, i) => {
    const frac = c.amount / total;
    const b = a + frac * 2 * Math.PI;
    if (frac > 0.9999) {
      slices.push(`<circle cx="${cx}" cy="${cy}" r="${r}" fill="${colour(i)}"/>`);
    } else {
      const [x0, y0] = at(a, r);
      const [x1, y1] = at(b, r);
      slices.push(`<path d="M${cx},${cy}L${x0.toFixed(2)},${y0.toFixed(2)}A${r},${r} 0 ${b - a > Math.PI ? 1 : 0} 1 `
        + `${x1.toFixed(2)},${y1.toFixed(2)}Z" fill="${colour(i)}" stroke="#1c1f26" stroke-width="1"/>`);
    }
    const mid = (a + b) / 2;
    if (frac >= 0.009 && labels.length < 14) {
      const [ex, ey] = at(mid, r);
      const [kx, ky] = at(mid, r + 10);
      labels.push({ name: c.name || '(no category)', share: (100 * frac).toFixed(1), colour: colour(i),
        right: Math.cos(mid) >= 0, ex, ey, kx, ky, y: ky });
    }
    a = b;
  });
  for (const right of [true, false]) {
    const side = labels.filter(l => l.right === right).sort((l, m) => l.y - m.y);
    for (let i = 1; i < side.length; i++) side[i].y = Math.max(side[i].y, side[i - 1].y + gap);
    const over = side.length ? side[side.length - 1].y - (H - 20) : 0;
    if (over > 0) side.forEach(l => { l.y -= over; });
    for (let i = side.length - 2; i >= 0; i--) side[i].y = Math.min(side[i].y, side[i + 1].y - gap);
  }
  const text = labels.map(l => {
    const x = l.right ? cx + r + 18 : cx - r - 18;
    const anchor = l.right ? 'start' : 'end';
    const end = x + (l.right ? -5 : 5);
    return `<polyline points="${l.ex.toFixed(1)},${l.ey.toFixed(1)} ${l.kx.toFixed(1)},${l.ky.toFixed(1)} ${end.toFixed(1)},${l.y.toFixed(1)}"
      fill="none" stroke="${l.colour}" stroke-width="1"/>`
      + `<text x="${x}" y="${(l.y - 2).toFixed(1)}" text-anchor="${anchor}" class="pl-name">${piece(l.name)}</text>`
      + `<text x="${x}" y="${(l.y + 13).toFixed(1)}" text-anchor="${anchor}" class="pl-pct">${l.share} %</text>`;
  }).join('');
  return raw(`<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Share by category">${slices.join('')}${text}</svg>`);
}

// --- Accounts ----------------------------------------------------------------

// The last twelve months' month-end totals, from a starting total and its
// changes by day ({date: change}).
function monthEnds(opening, changes) {
  const now = new Date();
  const months = [];
  for (let i = 11; i >= 0; i--) months.push(new Date(now.getFullYear(), now.getMonth() - i, 1));
  let total = opening;
  const dates = Object.keys(changes).sort();
  let k = 0;
  const values = months.map(m => {
    const end = iso(monthEnd(m));
    while (k < dates.length && dates[k] <= end) total += changes[dates[k++]];
    return total;
  });
  return [values, months.map(m => monthNameFmt.format(m))];
}
const twelveMonths = () => {
  const now = new Date();
  return [iso(new Date(now.getFullYear(), now.getMonth() - 11, 1)), iso(monthEnd(now))];
};
let accountsChart = false;

async function renderAccounts() {
  const token = ++renderToken;
  $('#topbar').innerHTML = h`<span class="title static">Accounts</span><span class="spacer"></span>
    <button type="button" class="icon-btn ${accountsChart ? 'on' : ''}" data-act="accounts-chart" aria-label="Chart">${icon('chart')}</button>
    <button type="button" class="icon-btn" data-act="go" data-hash="#/manage/accounts" aria-label="Edit accounts">${icon('pencil')}</button>`;
  const view = $('#view');
  let data;
  let chart = '';
  try {
    data = await cached('/api/app/accounts');
    if (accountsChart) {
      const [from, to] = twelveMonths();
      const nw = await cached(`/api/app/networth?${query({ from, to })}`);
      const [values, labels] = monthEnds(nw.opening, Object.fromEntries(nw.days.map(d => [d.date, d.change])));
      chart = h`<div class="chart">${lineChart(values, labels)}</div>`;
    }
  } catch (e) {
    view.innerHTML = h`<div class="scroll"><div class="empty">${e.message}</div></div>`;
    return;
  }
  if (token !== renderToken) return;
  const signed = v => (v < 0 ? 'exp' : 'inc');
  show(view, h`${summary([['Assets', mainNum(data.assets), 'inc'], ['Liabilities', mainNum(data.liabilities), 'exp'],
    ['Total', mainNum(data.assets - data.liabilities), '']])}
    <div class="scroll">${chart}${data.groups.map(g => h`<div class="ghead"><span>${g.name}</span>
      <span class="${signed(g.total)}">${mainMoney(Math.abs(g.total))}</span></div>
      ${g.accounts.map(a => h`<div class="arow" data-act="account" data-uid="${a.uid}"><span class="name">${a.name}</span>
        <span class="${signed(a.balance)}">${money(Math.abs(a.balance), a.currency)}</span></div>`)}`)}</div>`, 'accounts');
}

function renderMore() {
  $('#topbar').innerHTML = h`<span class="title static">More</span><span class="spacer"></span>
    <span class="version">${CFG.version}</span>`;
  $('#view').innerHTML = h`<div class="more"><div class="grid">
    <a href="#/manage/accounts">${icon('accounts')}<span>Accounts</span></a>
    <a href="#/manage/categories/1">${icon('list')}<span>Categories</span></a>
    <a href="/editor">${icon('editor')}<span>Editor</span></a>
    <a href="/settings">${icon('settings')}<span>Settings</span></a>
    ${CFG.sync_pending && h`<a href="/sync">${icon('sync')}<span>Review sync</span></a>`}
    ${CFG.admin && h`<a href="/users">${icon('users')}<span>Users</span></a>`}
    <button type="button" data-act="logout">${icon('logout')}<span>Log out</span></button>
    </div><p class="note">Signed in as ${CFG.user}</p></div>`;
  $('#view').dataset.shown = 'more';
}

// ---------------------------------------------------------------------------
// Pages over the main screen: a category's stats, an account's statement
// ---------------------------------------------------------------------------

let page = null;  // { kind, ... } of the open page

function openPage(parts, overForm = false) {
  const el = $('#page');
  el.hidden = false;
  el.classList.toggle('top', overForm);
  if (parts[0] === 'manage') {
    page = { kind: 'manage', what: parts[1] === 'categories' ? 'categories' : 'accounts', tree: parts[2] === '0' ? '0' : '1' };
    renderManagePage();
  } else if (parts[0] === 'bookmarks') {
    page = { kind: 'bookmarks' };
    renderBookmarksPage();
  } else if (parts[0] === 'search') {
    page = { kind: 'search' };
    renderSearchPage();
  } else if (parts[0] === 'cat') {
    const same = page && page.kind === 'cat' && page.root === parts[2] && page.type === parts[1];
    page = { kind: 'cat', type: parts[1], root: parts[2], child: same ? page.child : null };
    renderCategoryPage();
  } else {
    const view = ['daily', 'monthly', 'annually'].includes(parts[2]) ? parts[2] : 'daily';
    const chart = !!(page && page.kind === 'account' && page.uid === parts[1] && page.chart);
    page = { kind: 'account', uid: parts[1], view, chart,
      date: isIsoDate(parts[3]) ? parseIso(parts[3]) : monthStart(new Date()) };
    renderAccountPage();
  }
}
function closePage() {
  page = null;
  $('#page').hidden = true;
  $('#page').innerHTML = '';
  $('#page').dataset.shown = '';
}
function pageHash() {
  if (page.kind === 'cat') return `#/cat/${page.type}/${encodeURIComponent(page.root)}`;
  if (page.kind === 'search' || page.kind === 'bookmarks') return `#/${page.kind}`;
  if (page.kind === 'manage') return `#/manage/${page.what}${page.what === 'categories' ? `/${page.tree}` : ''}`;
  return `#/account/${encodeURIComponent(page.uid)}/${page.view}/${iso(page.date)}`;
}
const backButton = () => h`<button type="button" class="nav-btn" data-act="back" aria-label="Back">${icon('back')}</button>`;

// Managing accounts and categories: rename (tap the name), move, hide,
// delete and add, saved as soon as done.
async function manage(path, method, body) {
  try {
    await api(path, { method, body });
    await reloadData();
  } catch (e) {
    toast(e.message);
    return;
  }
  renderManagePage();
}

function renderManagePage() {
  const el = $('#page');
  const disabled = raw('disabled');
  const moves = (act, uid, i, n) => h`<button type="button" data-act="${act}" data-uid="${uid}" data-by="-1" aria-label="Up"
      ${i === 0 ? disabled : ''}>${icon('up')}</button><button type="button" data-act="${act}" data-uid="${uid}" data-by="1"
      aria-label="Down" ${i === n - 1 ? disabled : ''}>${icon('dn')}</button>`;
  if (page.what === 'accounts') {
    const groups = META.groups.map(g => [g, META.accounts.filter(a => a.group_uid === g.uid && a.status !== '1')]);
    el.innerHTML = h`<header class="topbar">${backButton()}<span class="title">Accounts</span><span class="spacer"></span>
        <button type="button" class="icon-btn" data-act="m-add-account" aria-label="Add an account">${icon('plus')}</button></header>
      <div class="scroll">${groups.map(([g, accounts]) => h`<div class="ghead"><span>${g.name}</span></div>
        ${accounts.map((a, i) => h`<div class="mrow ${a.status === '3' ? 'hidden' : ''}">
          <span class="mname" data-act="m-rename-account" data-uid="${a.uid}">${a.name}${a.status === '3' && h`<span class="mtag">hidden</span>`}</span>
          ${moves('m-move-account', a.uid, i, accounts.length)}
          <button type="button" data-act="m-hide-account" data-uid="${a.uid}" aria-label="${a.status === '3' ? 'Show' : 'Hide'}">${icon(a.status === '3' ? 'eyeoff' : 'eye')}</button>
          <button type="button" data-act="m-delete-account" data-uid="${a.uid}" aria-label="Delete">${icon('trash')}</button></div>`)}`)}</div>`;
    return;
  }
  const roots = page.tree === '0' ? META.categories.income : META.categories.expense;
  el.innerHTML = h`<header class="topbar">${backButton()}<span class="title">Categories</span><span class="spacer"></span></header>
    ${subtabs([['1', 'Expense'], ['0', 'Income']], page.tree, 'm-tree')}
    <div class="scroll">${roots.map((r, i) => h`<div class="mrow">
        <span class="mname" data-act="m-rename-category" data-uid="${r.uid}">${r.name}</span>
        <button type="button" data-act="m-add-category" data-parent="${r.uid}" aria-label="Add a subcategory">${icon('plus')}</button>
        ${moves('m-move-category', r.uid, i, roots.length)}
        <button type="button" data-act="m-delete-category" data-uid="${r.uid}" aria-label="Delete">${icon('trash')}</button></div>
      ${r.children.map((c, j) => h`<div class="mrow child">
        <span class="mname" data-act="m-rename-category" data-uid="${c.uid}">${c.name}</span>
        ${moves('m-move-category', c.uid, j, r.children.length)}
        <button type="button" data-act="m-delete-category" data-uid="${c.uid}" aria-label="Delete">${icon('trash')}</button></div>`)}`)}
      <button type="button" class="madd" data-act="m-add-category" data-parent="">${icon('plus')}Add a category</button></div>`;
}

function categoryNamed(tree, uid) {
  for (const r of tree === '0' ? META.categories.income : META.categories.expense) {
    if (r.uid === uid) return { name: r.name };
    const c = r.children.find(x => x.uid === uid);
    if (c) return { name: c.name, root: r };
  }
  return { name: '' };
}

// Bookmarks: transactions saved to enter again.
async function renderBookmarksPage() {
  const el = $('#page');
  el.innerHTML = h`<header class="topbar">${backButton()}<span class="title">Bookmarks</span></header><div class="scroll"></div>`;
  let d;
  try {
    d = await cached('/api/app/bookmarks');
  } catch (e) {
    toast(e.message);
    return;
  }
  if (!page || page.kind !== 'bookmarks') return;
  el.querySelector('.scroll').innerHTML = d.bookmarks.length ? h`${d.bookmarks.map(b => {
    const cat = b.type === '3' ? { name: 'Transfer' } : categoryNamed(b.type === '0' ? '0' : '1', b.category);
    const name = uid => (accountBy(uid) || {}).name || '';
    const where = b.type === '3' ? `${name(b.account)} → ${name(b.to_account)}` : name(b.account);
    const cls = b.type === '0' ? 'inc' : b.type === '1' ? 'exp' : 'tr';
    return h`<div class="row brow" data-act="use-bookmark" data-bm="${b.uid}">
      <div class="c1">${cat.root ? h`<div class="cat">${cat.root.name}</div><div class="sub">${cat.name}</div>` : h`<div class="cat">${cat.name}</div>`}</div>
      <div class="c2"><div class="top">${b.note ? h`<span class="note">${b.note}</span>` : where}</div>${b.note && h`<div class="bot">${where}</div>`}</div>
      <div class="amt ${cls}">${b.amount !== null ? money(b.amount, b.currency) : ''}</div>
      <button type="button" class="del" data-act="delete-bookmark" data-bm="${b.uid}" aria-label="Delete">${icon('trash')}</button></div>`;
  })}` : h`<div class="empty">No bookmarks yet. Save one with the star on the add screen.</div>`;
}

async function saveBookmark() {
  const t = F.type;
  const body = { type: t, account: F.account, to_account: t === '3' ? F.to_account : '',
    category: t === '0' || t === '1' ? F.category : '', note: F.note, description: F.description };
  if (F.amount !== '' && Number.isFinite(evalAmount(F.amount))) {
    body.amount = evalAmount(F.amount);
    body.currency = entryCurrency();
  }
  try {
    await api('/api/app/bookmarks', { method: 'POST', body });
  } catch (e) {
    toast(e.message);
    return;
  }
  cache.delete('/api/app/bookmarks');
  toast('Bookmarked.');
}

async function modifyBalance() {
  let d;
  try {
    d = await api(`/api/app/accounts/${encodeURIComponent(page.uid)}?${query({ from: '1970-01-01', to: '2999-12-31', sums: 1 })}`);
  } catch (e) {
    toast(e.message);
    return;
  }
  const cur = d.currency;
  const decimals = currency(cur).decimals;
  const v = await dialog('Modify balance', [{ name: 'balance', label: 'Balance', type: 'number', value: d.closing.toFixed(decimals) }],
    'Save', `${d.name} is at ${money(d.closing, cur)}. The difference is saved as a balance adjustment.`);
  if (!v) return;
  const target = parseNumber(v.balance);
  if (!Number.isFinite(target)) {
    toast('That is not a number.');
    return;
  }
  const diff = Number((target - d.closing).toFixed(decimals));
  if (!diff) {
    toast('The balance is already that.');
    return;
  }
  try {
    await api('/api/app/transactions', { method: 'POST', body: {
      type: diff > 0 ? '7' : '8', account: page.uid, amount: Math.abs(diff), date: iso(new Date()), time: nowTime() } });
  } catch (e) {
    toast(e.message);
    return;
  }
  cache.clear();
  toast('Balance adjusted.');
  renderAccountPage();
}

// Search: the note and description, with filters, as in the app.
const search = { q: '', filters: false, from: '', to: '', account: '', category: '', min: '', max: '' };
let searchToken = 0;
let searchTimer;

function renderSearchPage() {
  const el = $('#page');
  const option = (value, label, selected) => h`<option value="${value}" ${selected ? raw('selected') : ''}>${label}</option>`;
  const cats = (type, label) => h`<optgroup label="${label}">${META.categories[type === '0' ? 'income' : 'expense'].map(r =>
    [option(`${type}:${r.uid}`, r.name, search.category === `${type}:${r.uid}`),
      r.children.map(c => option(`${type}:${c.uid}`, `${r.name}/${c.name}`, search.category === `${type}:${c.uid}`))])}</optgroup>`;
  el.innerHTML = h`<header class="topbar"><button type="button" class="nav-btn" data-act="back" aria-label="Back">${icon('back')}</button>
      <span class="title">Search</span><span class="spacer"></span>
      <button type="button" class="icon-btn ${search.filters ? 'on' : ''}" data-act="search-filters" aria-label="Filters">${icon('filter')}</button></header>
    <div class="sbox"><span class="sicon">${icon('search')}</span>
      <input type="search" data-s="q" value="${search.q}" enterkeyhint="search" autocomplete="off" aria-label="Search">
      <button type="button" class="sclear" data-act="search-clear" aria-label="Clear" ${search.q ? '' : raw('hidden')}>${icon('close')}</button></div>
    ${search.filters && h`<div class="fields sfilters">
      <div class="field"><div class="lbl">Period</div><div class="val">
        <input type="date" data-s="from" value="${search.from}" aria-label="From"> ~ <input type="date" data-s="to" value="${search.to}" aria-label="To"></div></div>
      <div class="field"><div class="lbl">Account</div><div class="val"><select data-s="account" aria-label="Account">
        ${option('', 'All', !search.account)}${META.accounts.map(a => option(a.uid, a.name, search.account === a.uid))}</select></div></div>
      <div class="field"><div class="lbl">Category</div><div class="val"><select data-s="category" aria-label="Category">
        ${option('', 'All', !search.category)}${cats('1', 'Expense')}${cats('0', 'Income')}</select></div></div>
      <div class="field"><div class="lbl">Amount</div><div class="val">
        <input inputmode="decimal" data-s="min" value="${search.min}" placeholder="Min." aria-label="Min."> ~
        <input inputmode="decimal" data-s="max" value="${search.max}" placeholder="Max." aria-label="Max."></div></div></div>`}
    <div class="search-results" id="search-results"></div>`;
  runSearch();
  if (!search.q && !search.filters) el.querySelector('[data-s="q"]').focus();
}

async function runSearch() {
  const box = $('#search-results');
  if (!box) return;
  const params = {};
  if (search.q) params.q = search.q;
  if (search.from) params.from = search.from;
  if (search.to) params.to = search.to;
  if (search.account) params.account = search.account;
  if (search.category) [params.type, params.category] = search.category.split(':');
  if (search.min) params.amount_min = search.min;
  if (search.max) params.amount_max = search.max;
  const token = ++searchToken;
  if (!Object.keys(params).length) {
    box.innerHTML = '';
    return;
  }
  let d;
  try {
    d = await cached(`/api/app/search?${query(params)}`);
  } catch (e) {
    if (token === searchToken) box.innerHTML = h`<div class="empty">${e.message}</div>`;
    return;
  }
  if (token !== searchToken) return;
  const shown = box.querySelector('.scroll');
  const top = shown && box.dataset.shown === JSON.stringify(params) ? shown.scrollTop : 0;
  box.innerHTML = h`${summary([['Income', mainMoney(d.income), 'inc'], ['Expenses', mainMoney(d.expense), 'exp'],
    ['Transfer', mainMoney(d.transfer), '']])}
    <div class="scroll">${d.rows.length ? d.rows.map(searchRow) : h`<div class="empty">No data available.</div>`}
      ${d.count > d.rows.length && h`<div class="tnote">The newest ${d.rows.length} of ${d.count}.</div>`}</div>`;
  box.dataset.shown = JSON.stringify(params);
  if (top) box.querySelector('.scroll').scrollTop = top;
}

function searchRow(r) {
  const where = r.type === '3' ? `${r.account} → ${r.to_account}` : r.account;
  const cat = r.type === '3' ? 'Transfer' : r.type === '7' || r.type === '8' ? 'Adjustment'
    : r.subcategory ? `${r.category}/${r.subcategory}` : r.category;
  const desc = r.description && h`<span class="desc">${r.description}</span>`;
  const cls = r.type === '0' || r.type === '7' ? 'inc' : r.type === '1' || r.type === '8' ? 'exp' : 'tr';
  return h`<div class="row wide" data-uid="${r.uid}"><div class="c1"><div class="cat">${r.date}</div><div class="sub">${cat}</div></div>
    <div class="c2"><div class="top">${r.note ? h`<span class="note">${r.note}</span>${desc}` : h`${where}${desc}`}</div>
      ${r.note && h`<div class="bot">${where}</div>`}</div>
    <div class="amt ${cls}">${money(r.amount, r.currency)}</div></div>`;
}

async function renderCategoryPage() {
  const token = ++renderToken;
  const el = $('#page');
  const [from, to] = statsRange();
  let data;
  let days;
  let trend;
  // Eight periods up to this one, for the trend line.
  const periods = [];
  for (let i = 7; i >= 0; i--) periods.push(statsRange(stats.period, movePeriod(stats.date, stats.period, -i)));
  const filter = page.child && page.child === page.root ? { category_only: page.root }
    : { category: page.child || page.root };
  try {
    [data, days, trend] = await Promise.all([
      loadStats(),
      loadDays(iso(from), iso(to), { ...filter, type: page.type }),
      loadSums(iso(periods[0][0]), iso(to), { ...filter, type: page.type }),
    ]);
  } catch (e) {
    toast(e.message);
    return;
  }
  if (token !== renderToken || !page || page.kind !== 'cat') return;
  const root = (page.type === '0' ? data.income : data.expense).categories.find(c => c.uid === page.root)
    || { name: '', amount: 0, children: [] };
  const values = periods.map(([a, b]) => sumRange(trend, a, b)[page.type === '0' ? 0 : 1]);
  const labels = periods.map(([a]) => (stats.period === 'year' ? String(a.getFullYear())
    : stats.period === 'week' || stats.period === 'custom' ? mmdd(a) : monthNameFmt.format(a)));
  const shown = page.child ? (root.children.find(c => c.uid === page.child) || { amount: 0 }).amount : root.amount;
  show(el, h`<header class="topbar"><button type="button" class="nav-btn" data-act="back" aria-label="Back">${icon('back')}</button>
      <span class="title">${root.name}</span><span class="spacer"></span>${navTitle(statsTitle(), 'page-prev', 'page-next')}</header>
    <div class="scroll">
      <div class="bigtotal"><div class="lbl">Total</div><div class="val">${mainMoney(shown)}</div></div>
      <div class="sublist">
        <div class="subrow ${page.child ? '' : 'sel'}" data-act="child" data-uid=""><span>All</span><span>100%</span><span>${mainMoney(root.amount)}</span></div>
        ${root.children.map(c => h`<div class="subrow ${c.uid === page.child ? 'sel' : ''}" data-act="child" data-uid="${c.uid}">
          <span>${c.name}</span><span>${share(c.amount, root.amount)}%</span><span>${mainMoney(c.amount)}</span></div>`)}
      </div>
      <div class="chart">${lineChart(values, labels)}</div>
      ${days.map(dayBlock)}</div>`, `cat${page.root}${statsTitle()}${page.child}`);
}

// A line through the values, on a scale of round steps.
function lineChart(values, labels) {
  const W = 412;
  const H = 170;
  const right = 16;
  const top = 12;
  const bottom = 34;
  const lo0 = Math.min(...values);
  const hi0 = Math.max(...values);
  const span = hi0 - lo0 || Math.abs(hi0) || 1;
  const mag = 10 ** Math.floor(Math.log10(span / 2));
  const step = [1, 2, 2.5, 5, 10].map(m => m * mag).find(s => span / s <= 3) || 10 * mag;
  const lo = Math.floor(lo0 / step) * step;
  const hi = Math.max(Math.ceil(hi0 / step) * step, lo + step);
  // Room for the longest axis label.
  const left = 14 + 6.5 * Math.max(num(lo, 0).length, num(hi, 0).length);
  const x = i => left + 18 + (i * (W - left - right - 36)) / (values.length - 1);
  const y = v => top + ((hi - v) * (H - top - bottom)) / (hi - lo);
  let grid = '';
  for (let v = lo; v <= hi + step / 2; v += step) {
    grid += `<line x1="${left}" x2="${W}" y1="${y(v).toFixed(1)}" y2="${y(v).toFixed(1)}" class="gl"/>`
      + `<text x="${left - 8}" y="${(y(v) + 4).toFixed(1)}" text-anchor="end" class="ax">${num(v, 0)}</text>`;
  }
  const pts = values.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(' ');
  return raw(`<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Trend">${grid}`
    + `<line x1="${left}" x2="${left}" y1="${top - 6}" y2="${H - bottom}" class="gl"/>`
    + `<polyline points="${pts}" fill="none" class="ln"/>`
    + values.map((v, i) => `<circle cx="${x(i).toFixed(1)}" cy="${y(v).toFixed(1)}" r="4.5" class="pt"/>`).join('')
    + labels.map((l, i) => `<text x="${x(i).toFixed(1)}" y="${H - 10}" text-anchor="middle" class="ax">${piece(l)}</text>`).join('')
    + '</svg>');
}

const ACCOUNT_VIEWS = [['daily', 'Daily'], ['monthly', 'Monthly'], ['annually', 'Annually']];

function accountRange() {
  if (page.view === 'daily') return [monthStart(page.date), monthEnd(page.date)];
  if (page.view === 'monthly') return [new Date(page.date.getFullYear(), 0, 1), new Date(page.date.getFullYear(), 11, 31)];
  return [new Date(1970, 0, 1), new Date(new Date().getFullYear(), 11, 31)];
}
const fmtShort = d => shortDateFmt.format(d);

async function renderAccountPage() {
  const token = ++renderToken;
  const el = $('#page');
  const [from, to] = accountRange();
  let data;
  try {
    data = await cached(`/api/app/accounts/${encodeURIComponent(page.uid)}?${query({
      from: iso(from), to: iso(to), ...(page.view === 'daily' ? {} : { sums: 1 }) })}`);
  } catch (e) {
    toast(e.message);
    return;
  }
  if (token !== renderToken || !page || page.kind !== 'account') return;
  let chart = '';
  if (page.chart) {
    try {
      const [from, to] = twelveMonths();
      const s = await cached(`/api/app/accounts/${encodeURIComponent(page.uid)}?${query({ from, to, sums: 1 })}`);
      const [values, labels] = monthEnds(s.opening, Object.fromEntries(s.days.map(d => [d.date, d.deposit - d.withdrawal])));
      chart = h`<div class="chart">${lineChart(values, labels)}</div>`;
    } catch (e) {
      toast(e.message);
    }
    if (token !== renderToken || !page || page.kind !== 'account') return;
  }
  const cur = data.currency;
  const m = v => money(v, cur);
  const deposit = data.days.reduce((s, d) => s + d.deposit, 0);
  const withdrawal = data.days.reduce((s, d) => s + d.withdrawal, 0);
  const title = page.view === 'daily' ? monthFmt.format(page.date) : page.view === 'monthly' ? String(page.date.getFullYear()) : '';
  let list;
  if (page.view === 'daily') {
    let first = true;
    list = data.days.length ? data.days.map(d => h`<section class="day">${dayHead(d, [d.deposit, d.withdrawal], cur)}${
      d.rows.map(r => {
        const out = txRow(r, { balance: r.balance, first });
        first = false;
        return out;
      })}</section>`) : h`<div class="empty">No data available.</div>`;
  } else {
    // Months of the year, or years since the first row, newest first, each
    // with the balance at its end.
    const byDate = Object.fromEntries(data.days.map(d => [d.date, d]));
    const buckets = [];
    if (page.view === 'monthly') {
      for (let i = 0; i < 12; i++) buckets.push([new Date(from.getFullYear(), i, 1), monthEnd(new Date(from.getFullYear(), i, 1))]);
    } else {
      const firstYear = data.days.length ? Number(data.days[data.days.length - 1].date.slice(0, 4)) : to.getFullYear();
      for (let y = firstYear; y <= to.getFullYear(); y++) buckets.push([new Date(y, 0, 1), new Date(y, 11, 31)]);
    }
    let balance = data.opening;
    const rows = buckets.map(([a, b]) => {
      let dep = 0;
      let wd = 0;
      for (const [date, d] of Object.entries(byDate)) {
        if (date >= iso(a) && date <= iso(b)) {
          dep += d.deposit;
          wd += d.withdrawal;
        }
      }
      balance += dep - wd;
      const now = new Date();
      const current = page.view === 'monthly' ? ym(a) === ym(now) : a.getFullYear() === now.getFullYear();
      return h`<div class="prow ${current ? 'cur' : ''}" data-act="account-open" data-date="${iso(a)}">
        <div class="pname"><b>${page.view === 'monthly' ? monthNameFmt.format(a) : a.getFullYear()}</b>
          <small>${page.view === 'monthly' ? `${mmdd(a)} ~ ${mmdd(b)}` : ''}</small></div>
        <div class="pinc"><div class="inc">${m(dep)}</div><small>${m(dep - wd)}</small></div>
        <div class="pexp"><div class="exp">${m(wd)}</div><small>(${m(balance)})</small></div></div>`;
    });
    list = rows.reverse();
  }
  show(el, h`<header class="topbar"><button type="button" class="nav-btn" data-act="back" aria-label="Back">${icon('back')}</button>
      <span class="title">${data.name}</span><span class="spacer"></span>${title && navTitle(title, 'page-prev', 'page-next')}</header>
    ${subtabs(ACCOUNT_VIEWS, page.view, 'account-view')}
    <div class="statement"><div><div class="lbl">Statement</div>
      <div class="val">${page.view === 'annually' ? 'All' : `${fmtShort(from)}  ~  ${fmtShort(to)}`}</div></div>
      <span class="spacer"></span>
      <button type="button" class="icon-btn ${page.chart ? 'on' : ''}" data-act="account-chart" aria-label="Chart">${icon('chart')}</button>
      <button type="button" class="icon-btn" data-act="account-balance" aria-label="Modify balance">${icon('pencil')}</button></div>
    ${summary([['Deposit', num(deposit, currency(cur).decimals), 'inc'], ['Withdrawal', num(withdrawal, currency(cur).decimals), 'exp'],
    ['Total', num(deposit - withdrawal, currency(cur).decimals), ''], ['Balance', num(data.closing, currency(cur).decimals), 'inc']])}
    <div class="scroll">${chart}${list}</div>
    <button type="button" class="fab" data-act="add-account" aria-label="Add">${icon('plus')}</button>`,
  `account${page.uid}${page.view}${iso(page.date)}`);
}

// ---------------------------------------------------------------------------
// Add / edit form
// ---------------------------------------------------------------------------

const TYPE_NAMES = { 0: 'Income', 1: 'Expense', 3: 'Transfer', 4: 'Transfer', 7: 'Adjustment', 8: 'Adjustment' };
let F = null;  // the open form's state
let pendingCopy = null;

function nowTime() {
  const d = new Date();
  return `${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
}

// Whether the open form holds anything that leaving it would lose.
function formDirty() {
  if (F.loading) return false;
  if (F.mode === 'edit') return Object.keys(changes()).length > 0;
  return !F.blank || !!(F.category || F.amount || F.note || F.description || F.to_account || F.fee);
}

async function openForm(kind, args) {
  // cur: the currency the amount is typed in, null for the account's own;
  // fee, fee_category: a transfer's fee, shown once feeOn.
  const base = { type: '1', date: iso(new Date()), time: nowTime(), account: '', to_account: '', category: '',
    amount: '', cur: null, note: '', description: '', parent: null, fresh: false, feeOn: false, fee: '',
    fee_category: '' };
  loadNotes();
  if (kind === 'add') {
    F = { ...base, mode: 'add', panel: 'account', ...(pendingCopy || {}) };
    F.blank = !pendingCopy;
    if (isIsoDate(args[0])) F.date = args[0];
    if (args[1] && accountBy(args[1])) {
      F.account = args[1];
      F.panel = 'category';
    }
    if (pendingCopy) {
      const t = F.type;
      F.panel = !F.account ? 'account' : t === '3' && !F.to_account ? 'to_account'
        : (t === '0' || t === '1') && !F.category ? 'category' : F.amount === '' ? 'amount' : null;
    }
    pendingCopy = null;
  } else {
    F = { ...base, mode: 'edit', uid: args[0], panel: null, loading: true };
  }
  F.hash = location.hash;
  $('#form').hidden = false;
  renderForm();
  if (kind === 'edit') {
    const uid = args[0];
    try {
      const { transaction: t } = await api(`/api/app/transactions/${encodeURIComponent(uid)}`);
      if (!F || F.uid !== uid) return;
      // Shown as entered when that was in another currency (e.g. a row
      // from before the account switched to the euro).
      const other = t.entered_currency && t.entered_currency !== t.currency && t.entered_amount !== null
        && META.currencies[t.entered_currency];
      const amount = other ? t.entered_amount.toFixed(currency(t.entered_currency).decimals)
        : t.amount === null ? '' : t.amount.toFixed(currency(t.currency).decimals);
      Object.assign(F, { type: t.type, date: t.date, time: t.time, account: t.account || '', to_account: t.to_account || '',
        category: t.category, amount, cur: other ? t.entered_currency : null, note: t.note, description: t.description,
        loading: false });
      // A transfer's receiving leg edits as the transfer it belongs to.
      if (t.type === '4') Object.assign(F, { type: '3', account: t.to_account || '', to_account: t.account || '' });
      F.orig = { ...F };
      renderForm();
    } catch (e) {
      toast(e.message);
      back();
    }
  }
}

function closeFormScreen() {
  F = null;
  $('#form').hidden = true;
  $('#form').innerHTML = '';
}

// The category tree a picker shows: a fee's is always the expense tree.
const tree = (panel = F.panel) => (panel !== 'fee_category' && F.type === '0' ? META.categories.income
  : META.categories.expense);
const categoryField = () => (F.panel === 'fee_category' ? 'fee_category' : 'category');
function categoryLabel(uid, panel = 'category') {
  for (const root of tree(panel)) {
    if (root.uid === uid) return root.name;
    const child = root.children.find(c => c.uid === uid);
    if (child) return `${root.name}/${child.name}`;
  }
  return '';
}
const formCurrency = () => (accountBy(F.account) || {}).currency || META.main_currency;
const entryCurrency = () => F.cur || formCurrency();

// The amount fields to send: in the account's currency, and as typed when
// that was in another one.
function amountFields() {
  const typed = evalAmount(F.amount);
  const acct = formCurrency();
  const cur = entryCurrency();
  if (cur === acct) return { amount: typed };
  const value = (typed * currency(cur).rate) / currency(acct).rate;
  return { amount: Number(value.toFixed(currency(acct).decimals)), entered_amount: typed, entered_currency: cur };
}

let NOTES = [];
function loadNotes() {
  cached('/api/app/suggestions').then(d => { NOTES = d.notes; }).catch(() => {});
}
function suggestNotes(text) {
  const box = $('#form .suggest');
  if (!box) return;
  const t = text.trim().toLowerCase();
  const hits = t ? NOTES.filter(n => n !== text && n.toLowerCase().includes(t)).slice(0, 5) : [];
  box.innerHTML = h`${hits.map(n => h`<button type="button" data-act="suggest" data-note="${n}">${n}</button>`)}`;
  box.hidden = !hits.length;
}

function evalAmount(text) {
  const terms = String(text).match(/[+-]?[^+-]+/g);
  if (!terms) return NaN;
  return terms.reduce((sum, t) => sum + parseFloat(t), 0);
}

function field(label, value, { panel, readonly, extra } = {}) {
  return h`<div class="field ${F.panel === panel && panel ? 'on' : ''}">
    <div class="lbl">${label}</div>
    <div class="val ${readonly ? 'ro' : ''}" ${panel && !readonly ? raw(`data-act="panel" data-panel="${panel}"`) : ''}>
      <span>${value}</span>${extra}</div></div>`;
}

function renderForm() {
  const el = $('#form');
  const t = F.type;
  const transfer = t === '3';
  const adjustment = t === '7' || t === '8';
  el.className = `screen form-t${transfer ? 3 : t === '0' ? 0 : 1}`;
  const head = h`<header class="topbar"><button type="button" class="nav-btn" data-act="back" aria-label="Back">${icon('back')}</button>
    <span class="title">${F.loading ? '' : TYPE_NAMES[t]}</span><span class="spacer"></span>
    ${!F.loading && F.mode === 'add' && !(t === '7' || t === '8')
      && h`<button type="button" class="icon-btn" data-act="bookmark" aria-label="Bookmark">${icon('star')}</button>`}</header>`;
  if (F.loading) {
    el.innerHTML = head;
    return;
  }
  // Edits can switch income and expense, not to or from a transfer.
  const locked = x => F.mode === 'edit' && (F.orig.type === '3') !== (x === '3');
  const d = parseIso(F.date);
  const amountText = F.panel === 'amount' ? `${currency(entryCurrency()).symbol} ${F.amount}`
    : F.amount === '' ? '' : money(evalAmount(F.amount), entryCurrency());
  const accountName = uid => (accountBy(uid) || {}).name || '';
  const lockedAccounts = F.mode === 'edit' && transfer;
  el.innerHTML = h`${head}<div class="body">
    ${!adjustment && h`<div class="types">${['0', '1', '3'].map(x => h`<button type="button"
      class="t${x} ${x === t ? 'on' : ''}" data-act="type" data-type="${x}" ${locked(x) ? raw('disabled') : ''}>${TYPE_NAMES[x]}</button>`)}</div>`}
    <div class="fields">
      <div class="field"><div class="lbl">Date</div><div class="val">
        <span class="pick">${shortDateFmt.format(d)} (${weekdayFmt.format(d)})<input type="date" data-f="date" value="${F.date}" required></span>
        <span class="pick">${F.time}<input type="time" data-f="time" value="${F.time}" required></span></div></div>
      ${transfer
        ? [field('From', accountName(F.account), { panel: 'account', readonly: lockedAccounts }),
          field('To', accountName(F.to_account), { panel: 'to_account', readonly: lockedAccounts })]
        : field('Account', accountName(F.account), { panel: 'account' })}
      ${!transfer && !adjustment && field('Category', categoryLabel(F.category), { panel: 'category' })}
      ${field('Amount', amountText, { panel: 'amount', extra: transfer && F.mode === 'add'
        && h`<button type="button" class="feebtn ${F.feeOn ? 'on' : ''}" data-act="fee-toggle">Fees</button>` })}
      ${transfer && F.feeOn && [
        field('Fee', F.panel === 'fee' ? `${currency(formCurrency()).symbol} ${F.fee}`
          : F.fee === '' ? '' : money(evalAmount(F.fee), formCurrency()), { panel: 'fee' }),
        field('Fee type', categoryLabel(F.fee_category, 'fee_category'), { panel: 'fee_category' })]}
      <div class="field"><div class="lbl">Note</div><div class="val">
        <input class="plain" data-f="note" value="${F.note}" autocomplete="off" enterkeyhint="done"></div></div>
      <div class="suggest" hidden></div>
    </div>
    <div class="desc-block"><textarea data-f="description" rows="1" placeholder="Description">${F.description}</textarea></div>
    ${F.mode === 'add'
      ? h`<div class="actions"><button type="button" class="save" data-act="save">Save</button>
          <button type="button" class="cont" data-act="continue">Continue</button></div>`
      : h`<div class="actions"><button type="button" class="save" data-act="save">Save</button></div>
          <div class="actions"><button type="button" class="half" data-act="delete">${icon('trash')}Delete</button>
          <button type="button" class="half" data-act="copy">${icon('copy')}Copy</button>
          ${!(t === '7' || t === '8') && h`<button type="button" class="half" data-act="bookmark">${icon('star')}Bookmark</button>`}</div>`}
  </div>${formPanel()}`;
  autosize(el.querySelector('textarea'));
}

function autosize(textarea) {
  textarea.style.height = 'auto';
  textarea.style.height = `${textarea.scrollHeight}px`;
}

function formPanel() {
  if (!F.panel) return '';
  const close = h`<button type="button" data-act="panel-close" aria-label="Close">${icon('close')}</button>`;
  const edit = what => h`<button type="button" class="edit" data-act="manage" data-what="${what}" aria-label="Edit">${icon('pencil')}</button>`;
  if (F.panel === 'account' || F.panel === 'to_account') {
    return h`<div class="panel"><div class="head">Accounts<span class="spacer"></span>${edit('accounts')}${close}</div>
      <div class="opts acct-grid">${META.accounts.filter(a => !a.hidden).map(a => h`<button type="button"
        data-act="pick-account" data-uid="${a.uid}">${a.name}</button>`)}</div></div>`;
  }
  if (F.panel === 'category' || F.panel === 'fee_category') {
    const roots = tree();
    const parent = roots.find(r => r.uid === F.parent);
    return h`<div class="panel"><div class="head">${F.panel === 'fee_category' ? 'Fee type' : 'Category'}<span class="spacer"></span>
      ${edit('categories')}${close}</div>
      <div class="cat-cols"><div>${roots.map(r => h`<button type="button" class="${r.uid === F.parent ? 'sel' : ''}"
        data-act="pick-root" data-uid="${r.uid}"><span>${r.name}</span>${r.children.length ? icon('right') : ''}</button>`)}</div>
      <div>${parent ? parent.children.map(c => h`<button type="button" data-act="pick-category" data-uid="${c.uid}">
        <span>${c.name}</span></button>`) : ''}</div></div></div>`;
  }
  const keys = ['1', '2', '3', 'back', '4', '5', '6', '-', '7', '8', '9', '+', '', '0', '.', 'done'];
  const curs = Object.entries(META.currencies).sort((a, b) => (a[1].order ?? 1e9) - (b[1].order ?? 1e9));
  return h`<div class="panel"><div class="head">${F.panel === 'fee' ? 'Fee' : 'Amount'}<span class="spacer"></span>${close}</div>
    ${curs.length > 1 && F.panel === 'amount' && h`<div class="chips">${curs.map(([uid, c]) => h`<button type="button"
      class="${uid === entryCurrency() ? 'on' : ''}" data-act="currency" data-uid="${uid}">${c.symbol}</button>`)}</div>`}
    <div class="pad">${keys.map(k => (k === '' ? h`<button type="button" class="blank" tabindex="-1"></button>`
      : k === 'back' ? h`<button type="button" data-act="key" data-key="back" aria-label="Delete">${icon('backspace')}</button>`
        : k === 'done' ? h`<button type="button" class="done" data-act="key" data-key="done">Done</button>`
          : h`<button type="button" data-act="key" data-key="${k}">${k}</button>`))}</div></div>`;
}

const PADS = { amount: 'amount', fee: 'fee' };

function setPanel(panel) {
  if (PADS[F.panel] && panel !== F.panel) finishAmount(F.panel);
  F.panel = panel;
  if (panel === 'category' || panel === 'fee_category') {
    const current = F[categoryField()];
    F.parent = (tree().find(r => r.uid === current || r.children.some(c => c.uid === current)) || {}).uid || null;
  }
  if (PADS[panel]) F.fresh = F[panel] !== '';
  document.activeElement?.blur?.();
  renderForm();
}

// After the account, the category, then the amount, as in the app.
function nextPanel(from) {
  const t = F.type;
  if (from === 'account' && t === '3') return F.to_account ? 'amount' : 'to_account';
  if (from === 'account' || from === 'to_account') return (t === '0' || t === '1') && !F.category ? 'category' : 'amount';
  return 'amount';
}

// A typed amount (or fee) as a plain number in its currency's decimals.
function finishAmount(key = 'amount') {
  if (F[key] === '') return;
  const v = evalAmount(F[key]);
  F[key] = Number.isFinite(v) ? v.toFixed(currency(key === 'fee' ? formCurrency() : entryCurrency()).decimals) : '';
}

function pressKey(k) {
  const key = PADS[F.panel] || 'amount';
  if (k === 'done') {
    finishAmount(key);
    F.panel = key === 'fee' && !F.fee_category ? 'fee_category' : null;
    if (F.panel) setPanel(F.panel); else renderForm();
    if (!F.panel) $('#form input[data-f="note"]')?.focus();
    return;
  }
  if (F.fresh) {
    F.fresh = false;
    if (k !== 'back' && k !== '+' && k !== '-') F[key] = '';
  }
  let a = F[key];
  if (k === 'back') a = a.slice(0, -1);
  else if (k === '+' || k === '-') a = a === '' ? a : /[+-]$/.test(a) ? a.slice(0, -1) + k : a + k;
  else if (k === '.') a += /(^|[+-])[^+-]*\.[^+-]*$/.test(a) ? '' : /(^|[+-])$/.test(a) ? '0.' : '.';
  else a += k;
  F[key] = a;
  renderForm();
}

function changes() {
  const t = F.type;
  const data = { type: t, date: F.date, time: F.time, account: F.account, note: F.note, description: F.description };
  if (t === '0' || t === '1') data.category = F.category;
  if (t === '3') data.to_account = F.to_account;
  if (F.mode === 'add') {
    const fee = t === '3' && F.feeOn && evalAmount(F.fee) > 0
      ? { fee: evalAmount(F.fee), fee_category: F.fee_category } : {};
    return { ...data, ...amountFields(), ...fee };
  }
  const o = F.orig;
  const out = {};
  for (const k of ['type', 'date', 'time', 'note', 'description']) if (data[k] !== o[k]) out[k] = data[k];
  if (t !== '3' && data.account !== o.account) out.account = data.account;
  if ('category' in data && (data.category !== o.category || out.type)) out.category = data.category;
  if (Math.abs(evalAmount(F.amount) - evalAmount(o.amount)) > 1e-9 || F.cur !== o.cur) {
    Object.assign(out, amountFields());
    // Back to the account's own currency: what was entered goes with it.
    if (!F.cur && o.cur) Object.assign(out, { entered_amount: out.amount, entered_currency: formCurrency() });
  }
  return out;
}

async function save(again) {
  if (PADS[F.panel]) finishAmount(F.panel);
  const t = F.type;
  const fee = t === '3' && F.feeOn && evalAmount(F.fee) > 0;
  const missing = !F.account ? (t === '3' ? 'From' : 'Account')
    : t === '3' && !F.to_account ? 'To'
      : (t === '0' || t === '1') && !F.category ? 'Category'
        : !(evalAmount(F.amount) > 0) ? 'Amount'
          : fee && !F.fee_category ? 'Fee type' : null;
  if (missing) {
    toast(`Fill in ${missing}.`);
    setPanel({ From: 'account', Account: 'account', To: 'to_account', Category: 'category', Amount: 'amount',
      'Fee type': 'fee_category' }[missing]);
    return;
  }
  if (t === '3' && F.account === F.to_account) {
    toast('Pick two different accounts.');
    return;
  }
  const data = changes();
  try {
    if (F.mode === 'add') {
      await api('/api/app/transactions', { method: 'POST', body: data });
    } else if (Object.keys(data).length) {
      await api(`/api/app/transactions/${encodeURIComponent(F.uid)}`, { method: 'PATCH', body: data });
    }
  } catch (e) {
    toast(e.message);
    return;
  }
  cache.clear();
  if (again) {
    F.blank = true;
    toast('Saved.');
    Object.assign(F, { category: '', amount: '', note: '', description: '', fee: '', fee_category: '', feeOn: false });
    setPanel(t === '0' || t === '1' ? 'category' : 'amount');
  } else {
    F.saved = true;
    back();
  }
}

async function remove() {
  if (!confirm('Delete this transaction?')) return;
  try {
    await api(`/api/app/transactions/${encodeURIComponent(F.uid)}`, { method: 'DELETE' });
  } catch (e) {
    toast(e.message);
    return;
  }
  cache.clear();
  F.saved = true;
  back();
}

function copy() {
  const { type, account, to_account, category, amount, cur, note, description } = F;
  pendingCopy = { type, account, to_account, category, amount, cur, note, description };
  routeKey = null;
  replaceRoute('#/add', null);
}

// ---------------------------------------------------------------------------
// Events
// ---------------------------------------------------------------------------

const ACTIONS = {
  tab: el => {
    state.tab = el.dataset.tab;
    periodMenu = false;
    setMain();
  },
  sub: el => {
    state.sub = el.dataset.key;
    setMain();
  },
  prev: () => (state.tab === 'stats' ? moveStats(-1) : moveMonth(-1)),
  next: () => (state.tab === 'stats' ? moveStats(1) : moveMonth(1)),
  add: () => go('#/add'),
  search: () => go('#/search'),
  'search-filters': () => {
    search.filters = !search.filters;
    renderSearchPage();
  },
  'search-clear': () => {
    search.q = '';
    renderSearchPage();
    $('#page [data-s="q"]').focus();
  },
  budget: el => {
    if (openBudgets.has(el.dataset.uid)) openBudgets.delete(el.dataset.uid); else openBudgets.add(el.dataset.uid);
    renderTrans();
  },
  suggest: el => {
    F.note = el.dataset.note;
    const input = $('#form input[data-f="note"]');
    input.value = F.note;
    input.focus();
    suggestNotes('');
  },
  currency: el => {
    F.cur = el.dataset.uid === formCurrency() ? null : el.dataset.uid;
    renderForm();
  },
  'add-on': el => go(`#/add/${el.dataset.date}`),
  'add-account': () => go(`#/add/${iso(new Date())}/${encodeURIComponent(page.uid)}`),
  'sheet-move': el => {
    const date = iso(addDays(parseIso(history.state.sheet), Number(el.dataset.by)));
    replaceRoute(location.hash, { sheet: date });
  },
  'sheet-close': back,
  back,
  month: el => {
    state.month = parseIso(el.dataset.month);
    setMain();
  },
  week: el => {
    state.month = parseIso(el.dataset.month);
    state.sub = 'daily';
    setMain();
  },
  'period-menu': () => {
    periodMenu = !periodMenu;
    renderStats();
  },
  period: async el => {
    periodMenu = false;
    if (el.dataset.key === 'custom') {
      const [from, to] = statsRange();
      renderStats();
      const v = await dialog('Period', [{ name: 'from', label: 'From', type: 'date', value: iso(from) },
        { name: 'to', label: 'To', type: 'date', value: iso(to) }], 'Show');
      if (!v || !isIsoDate(v.from) || !isIsoDate(v.to) || v.to < v.from) return;
      stats.date = parseIso(v.from);
      stats.len = Math.round((parseIso(v.to) - stats.date) / 864e5);
    } else {
      stats.date = statsRange(el.dataset.key, stats.date)[0];
    }
    stats.period = el.dataset.key;
    setMain();
  },
  'stats-type': el => {
    stats.type = el.dataset.key;
    setMain();
  },
  cat: el => go(`#/cat/${stats.type}/${encodeURIComponent(el.dataset.uid)}`),
  child: el => {
    page.child = el.dataset.uid || null;
    renderCategoryPage();
  },
  account: el => go(`#/account/${encodeURIComponent(el.dataset.uid)}/daily/${iso(monthStart(new Date()))}`),
  'account-view': el => {
    page.view = el.dataset.key;
    replaceRoute(pageHash());
  },
  'account-open': el => {
    page.date = parseIso(el.dataset.date);
    page.view = page.view === 'annually' ? 'monthly' : 'daily';
    replaceRoute(pageHash());
  },
  'page-prev': () => movePage(-1),
  'page-next': () => movePage(1),
  logout: () => $('#logout-form').submit(),
  type: el => {
    const x = el.dataset.type;
    if (x === F.type) return;
    const tree0 = F.type === '0';
    F.type = x;
    if ((x === '0') !== tree0 || x === '3') F.category = '';
    if (x !== '3') Object.assign(F, { to_account: '', feeOn: false, fee: '', fee_category: '' });
    setPanel(!F.account ? 'account' : nextPanel('account'));
  },
  panel: el => setPanel(el.dataset.panel),
  'panel-close': () => setPanel(null),
  'pick-account': el => {
    F[F.panel] = el.dataset.uid;
    if (F.cur === formCurrency()) F.cur = null;
    setPanel(nextPanel(F.panel));
  },
  'pick-root': el => {
    const root = tree().find(r => r.uid === el.dataset.uid);
    if (root.children.length) {
      F.parent = root.uid;
      renderForm();
    } else {
      pickCategory(root.uid);
    }
  },
  'pick-category': el => pickCategory(el.dataset.uid),
  'fee-toggle': () => {
    F.feeOn = !F.feeOn;
    setPanel(F.feeOn ? 'fee' : null);
  },
  bookmark: () => saveBookmark(),
  bookmarks: () => go('#/bookmarks'),
  go: el => go(el.dataset.hash),
  manage: el => go(el.dataset.what === 'categories'
    ? `#/manage/categories/${F.panel !== 'fee_category' && F.type === '0' ? '0' : '1'}` : '#/manage/accounts'),
  'use-bookmark': async el => {
    const b = (await cached('/api/app/bookmarks')).bookmarks.find(x => x.uid === el.dataset.bm);
    if (!b) return;
    const own = (accountBy(b.account) || {}).currency;
    pendingCopy = { type: b.type, account: b.account, to_account: b.to_account, category: b.category, note: b.note,
      description: b.description, cur: b.currency && b.currency !== own && META.currencies[b.currency] ? b.currency : null,
      amount: b.amount === null ? '' : b.amount.toFixed(currency(b.currency).decimals) };
    routeKey = null;
    replaceRoute('#/add', null);
  },
  'delete-bookmark': async el => {
    if (!confirm('Delete this bookmark?')) return;
    try {
      await api(`/api/app/bookmarks/${encodeURIComponent(el.dataset.bm)}`, { method: 'DELETE' });
    } catch (e) {
      toast(e.message);
      return;
    }
    cache.delete('/api/app/bookmarks');
    renderBookmarksPage();
  },
  'accounts-chart': () => {
    accountsChart = !accountsChart;
    renderAccounts();
  },
  'account-chart': () => {
    page.chart = !page.chart;
    renderAccountPage();
  },
  'account-balance': () => modifyBalance(),
  'm-tree': el => {
    page.tree = el.dataset.key;
    replaceRoute(pageHash());
  },
  'm-add-account': async () => {
    const v = await dialog('New account', [{ name: 'name', label: 'Name' },
      { name: 'group', label: 'Group', type: 'select', value: (META.groups[0] || {}).uid,
        options: META.groups.map(g => [g.uid, g.name]) },
      { name: 'currency', label: 'Currency', type: 'select', value: META.main_currency, options: currencyOptions() }], 'Add');
    if (v) manage('/api/app/accounts', 'POST', v);
  },
  'm-rename-account': async el => {
    const a = accountBy(el.dataset.uid);
    const v = await dialog('Rename account', [{ name: 'name', label: 'Name', value: a.name }]);
    if (v && v.name !== a.name) manage(`/api/app/accounts/${encodeURIComponent(a.uid)}`, 'PATCH', { name: v.name });
  },
  'm-move-account': el => manage(`/api/app/accounts/${encodeURIComponent(el.dataset.uid)}`, 'PATCH',
    { move: Number(el.dataset.by) }),
  'm-hide-account': el => manage(`/api/app/accounts/${encodeURIComponent(el.dataset.uid)}`, 'PATCH',
    { status: accountBy(el.dataset.uid).status === '3' ? '0' : '3' }),
  'm-delete-account': el => {
    const a = accountBy(el.dataset.uid);
    if (confirm(`Delete ${a.name}? Its transactions stay, as when you delete an account in Money Manager.`)) {
      manage(`/api/app/accounts/${encodeURIComponent(a.uid)}`, 'PATCH', { status: '1' });
    }
  },
  'm-add-category': async el => {
    const parent = el.dataset.parent;
    const title = parent ? `New subcategory of ${categoryNamed(page.tree, parent).name}`
      : `New ${page.tree === '0' ? 'income' : 'expense'} category`;
    const v = await dialog(title, [{ name: 'name', label: 'Name' }], 'Add');
    if (v) manage(`/api/app/categories/${page.tree}`, 'POST', { name: v.name, parent });
  },
  'm-rename-category': async el => {
    const { name } = categoryNamed(page.tree, el.dataset.uid);
    const v = await dialog('Rename category', [{ name: 'name', label: 'Name', value: name }]);
    if (v && v.name !== name) {
      manage(`/api/app/categories/${page.tree}/${encodeURIComponent(el.dataset.uid)}`, 'PATCH', { name: v.name });
    }
  },
  'm-move-category': el => manage(`/api/app/categories/${page.tree}/${encodeURIComponent(el.dataset.uid)}`, 'PATCH',
    { move: Number(el.dataset.by) }),
  'm-delete-category': el => {
    if (confirm(`Delete ${categoryNamed(page.tree, el.dataset.uid).name}?`)) {
      manage(`/api/app/categories/${page.tree}/${encodeURIComponent(el.dataset.uid)}`, 'DELETE');
    }
  },
  key: el => pressKey(el.dataset.key),
  save: () => save(false),
  continue: () => save(true),
  delete: () => remove(),
  copy: () => copy(),
};

function pickCategory(uid) {
  F[categoryField()] = uid;
  setPanel(F.panel === 'fee_category' ? null : 'amount');
}

function moveMonth(by) {
  const m = state.month;
  state.month = state.sub === 'monthly' ? new Date(m.getFullYear() + by, m.getMonth(), 1)
    : new Date(m.getFullYear(), m.getMonth() + by, 1);
  setMain();
}
function moveStats(by) {
  stats.date = movePeriod(stats.date, stats.period, by);
  setMain();
}
function movePage(by) {
  if (page.kind === 'cat') {
    stats.date = movePeriod(stats.date, stats.period, by);
    renderCategoryPage();
    return;
  }
  page.date = page.view === 'daily' ? new Date(page.date.getFullYear(), page.date.getMonth() + by, 1)
    : new Date(page.date.getFullYear() + by, 0, 1);
  replaceRoute(pageHash());
}

document.addEventListener('click', e => {
  // Desktop browsers only open a date or time picker from its own button.
  if (e.target.matches('.pick input')) {
    try { e.target.showPicker(); } catch (err) { /* already open, or unsupported */ }
    return;
  }
  if (periodMenu && !e.target.closest('.menu, [data-act="period-menu"]')) {
    periodMenu = false;
    renderStats();
  }
  const act = e.target.closest('[data-act]');
  if (act && !act.disabled) {
    ACTIONS[act.dataset.act]?.(act, e);
    return;
  }
  const row = e.target.closest('.row[data-uid]');
  if (row) {
    go(`#/edit/${encodeURIComponent(row.dataset.uid)}`);
    return;
  }
  const cell = e.target.closest('.cell[data-date]');
  if (cell) {
    history.pushState({ sheet: cell.dataset.date }, '', location.hash);
    onRoute();
    return;
  }
  if (e.target.id === 'sheet') back();
});

document.addEventListener('input', e => {
  const sf = e.target.dataset && e.target.dataset.s;
  if (sf && page && page.kind === 'search') {
    search[sf] = e.target.value.trim();
    if (sf === 'q') $('#page .sclear').hidden = !search.q;
    clearTimeout(searchTimer);
    searchTimer = setTimeout(runSearch, 250);
    return;
  }
  const f = e.target.dataset && e.target.dataset.f;
  if (!F || !f) return;
  if (f === 'date' || f === 'time') {
    if (e.target.value) F[f] = e.target.value;
    renderForm();
    return;
  }
  F[f] = e.target.value;
  if (f === 'description') autosize(e.target);
  if (f === 'note') suggestNotes(F.note);
});

document.addEventListener('focusin', e => {
  if (F && F.panel && e.target.dataset && (e.target.dataset.f === 'note' || e.target.dataset.f === 'description')) {
    if (F.panel === 'amount') finishAmount();
    F.panel = null;
    const f = e.target.dataset.f;
    renderForm();
    $(`#form [data-f="${f}"]`).focus();
  }
});

// Swipe sideways to change month (or period) on the main screen.
(function swipe() {
  let x0 = null;
  let y0 = 0;
  let t0 = 0;
  const view = $('#view');
  view.addEventListener('touchstart', e => {
    const t = e.touches[0];
    [x0, y0, t0] = [t.clientX, t.clientY, Date.now()];
  }, { passive: true });
  view.addEventListener('touchend', e => {
    if (x0 === null || (state.tab !== 'trans' && state.tab !== 'stats')) return;
    const t = e.changedTouches[0];
    const dx = t.clientX - x0;
    const dy = t.clientY - y0;
    x0 = null;
    if (Math.abs(dx) > 70 && Math.abs(dx) > 2 * Math.abs(dy) && Date.now() - t0 < 700) {
      ACTIONS[dx < 0 ? 'next' : 'prev']();
    }
  }, { passive: true });
})();

// Coming back to the app after the database changed (a sync, the editor,
// the phone) shows the new data.
let dbMtime = null;
document.addEventListener('visibilitychange', async () => {
  if (document.visibilityState !== 'visible') return;
  try {
    const { mtime } = await api('/api/db-status');
    if (dbMtime !== null && mtime !== dbMtime && !F) {
      await reloadData();
      routeKey = null;
      onRoute();
    }
    dbMtime = mtime;
  } catch (e) { /* offline: keep what's shown */ }
});

(async function init() {
  $('#fab').innerHTML = icon('plus');
  $('#fab').dataset.act = 'add';
  try {
    META = await api('/api/app/meta');
    WEEK_START = META.week_start;
    dbMtime = (await api('/api/db-status')).mtime;
  } catch (e) {
    $('#view').innerHTML = h`<div class="empty">${e.message}</div>`;
    return;
  }
  // A link straight to a page or the form gets the main screen under it,
  // so Back leads there instead of out of the app.
  const target = location.hash;
  if (isOverlay(target)) {
    history.replaceState(null, '', mainHash());
    history.pushState(null, '', target);
  } else if (!target || target === '#') {
    history.replaceState(null, '', mainHash());
  }
  onRoute();
  if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js').catch(() => {});
})();
