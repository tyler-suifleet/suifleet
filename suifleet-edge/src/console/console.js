// SuiFleet local support console UI (SF-079). Renders with textContent only;
// the CSRF token lives in memory, the session in an HttpOnly cookie.
'use strict';
(() => {
  let csrf = null;
  let grants = [];
  let selected = null;
  const $ = (id) => document.getElementById(id);
  const say = (text) => { $('status').textContent = text; };
  const show = (value) => { $('output').textContent = typeof value === 'string' ? value : JSON.stringify(value, null, 2); };

  async function call(path, body, headers = {}) {
    const response = await fetch(path, {
      method: 'POST', credentials: 'same-origin', cache: 'no-store',
      headers: { 'Content-Type': 'application/json', ...(csrf ? { 'X-SuiFleet-CSRF': csrf } : {}), ...headers },
      body: JSON.stringify(body),
    });
    const result = await response.json().catch(() => ({ status: 'invalid-response' }));
    if (result.status === 'unauthenticated' && csrf) signedOut('Session ended; sign in again.');
    return result;
  }

  function signedOut(message) {
    csrf = null; grants = []; selected = null;
    $('console').hidden = true; $('login').hidden = false; $('component').hidden = true;
    $('who').textContent = ''; show(''); say(message);
  }

  function table(rows, columns, onSelect) {
    const element = document.createElement('table');
    const head = element.insertRow();
    for (const column of columns) { const th = document.createElement('th'); th.textContent = column; head.appendChild(th); }
    for (const row of rows) {
      const tr = element.insertRow();
      for (const column of columns) {
        const cell = tr.insertCell();
        const value = row[column];
        cell.textContent = value === null || value === undefined ? '' : String(value);
        if (column === columns[0] && onSelect) { cell.className = 'link'; cell.addEventListener('click', () => onSelect(row)); }
      }
    }
    const output = $('output'); output.textContent = ''; output.appendChild(element);
  }

  function select(name) {
    selected = name;
    $('component').hidden = false;
    $('component-name').textContent = name;
    for (const button of document.querySelectorAll('[data-component-op]')) button.disabled = !grants.includes(button.dataset.componentOp);
  }

  async function run(operation, component) {
    say(`${operation}…`);
    const result = await call('/api/v1/request', { version: 1, operation, ...(component ? { component } : {}) });
    say(`${operation}: ${result.status}`);
    if (result.status !== 'ok') { show(''); return; }
    const data = result.data;
    if (operation === 'inventory') table(data.components, ['component', 'version', 'health', 'restarts', 'deployment', 'last_exit'], (row) => select(row.component));
    else if (operation === 'deployments') table(data.deployments, ['deployment', 'status', 'reported', 'indeterminate', 'error']);
    else if (operation === 'audit') table(data.entries, ['ts_ms', 'user', 'operation', 'component', 'status', 'peer']);
    else if (operation === 'logs') show(data.lines.join('\n'));
    else show(data);
  }

  document.addEventListener('DOMContentLoaded', () => {
    $('login').addEventListener('submit', async (event) => {
      event.preventDefault();
      const token = $('token').value.trim();
      $('token').value = '';
      const result = await call('/api/v1/session', {}, { Authorization: `Bearer ${token}` });
      if (result.status !== 'ok') { say(`Sign-in failed: ${result.status}`); return; }
      csrf = result.data.csrf; grants = result.data.grants;
      $('who').textContent = `${result.data.user} (${grants.join(', ')})`;
      $('login').hidden = true; $('console').hidden = false; say('Signed in.');
      for (const button of document.querySelectorAll('[data-op]')) button.disabled = !grants.includes(button.dataset.op);
    });
    for (const button of document.querySelectorAll('[data-op]')) button.addEventListener('click', () => run(button.dataset.op));
    for (const button of document.querySelectorAll('[data-component-op]')) button.addEventListener('click', () => selected && run(button.dataset.componentOp, selected));
    $('logout').addEventListener('click', async () => { await call('/api/v1/logout', {}); signedOut('Signed out.'); });
  });
})();
