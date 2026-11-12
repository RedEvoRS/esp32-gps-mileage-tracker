const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../mileage-viewer/static/endpoint-places.js'), 'utf8');
const tick = () => new Promise(resolve => setImmediate(resolve));
function setup({auto = false, cached = [], endpoints = ['start', 'end']} = {}) {
  const destination = {value: 'Unsaved edit', focus() { this.focused = true; }};
  const useEnd = {disabled: !cached.includes('end'), addEventListener(_, fn) { this.click = fn; }};
  const both = {disabled: true, addEventListener(_, fn) { this.click = fn; }};
  const labels = {start: {textContent: 'Cached start'}, end: {textContent: 'Cached end'}};
  const forms = endpoints.map(endpoint => {
    const button = {disabled: true, attributes: {}, setAttribute(k,v) { this.attributes[k]=v; }, removeAttribute(k) { delete this.attributes[k]; }};
    return {action: '/trip/2026-10/sample/lookup-place', dataset: {placeCached: String(cached.includes(endpoint))},
      endpoint, addEventListener(_, fn) { this.submit = fn; }, querySelector() { return button; },
      closest() { return {dataset: {endpoint}, querySelector() { return labels[endpoint]; }}; }};
  });
  const calls = [];
  vm.runInNewContext(source, {document: {
    currentScript: {dataset: auto ? {autoLookup: 'true'} : {}},
    getElementById(id) { return {'use-end-place': useEnd, 'lookup-both-places': endpoints.length === 2 ? both : null, destination}[id]; },
    querySelector() { return labels.end; }, querySelectorAll() { return forms; }},
    FormData: class {constructor(form) { this.form = form; }},
    fetch(url, options) { return new Promise(resolve => calls.push({url, options, resolve})); }});
  async function respond(index, data, ok = true) {
    calls[index].resolve({ok, async json() { if (data === null) throw Error('not JSON'); return data; }});
    await tick();
  }
  return {forms, labels, both, calls, destination, useEnd, respond,
    submit(index) { return forms[index].submit({preventDefault() {}}); }};
}
async function run() {
  const s = setup();
  assert.equal(s.calls.length, 0);
  assert.ok(s.forms.every(f => !f.querySelector().disabled));
  s.submit(1); s.submit(0); s.submit(0); s.submit(1);
  assert.equal(s.calls.length, 1);
  assert.match(s.labels.start.textContent, /Waiting/);
  assert.equal(s.calls[0].options.method, 'POST');
  assert.equal(s.calls[0].options.credentials, 'same-origin');
  assert.equal(s.calls[0].options.body.form, s.forms[1]);
  const hostile = '<img src=x onerror=alert(1)>';
  await s.respond(0, {label: hostile});
  assert.equal(s.labels.end.textContent, hostile);
  assert.equal(s.destination.value, 'Unsaved edit');
  s.useEnd.click(); assert.equal(s.destination.value, hostile);
  assert.equal(s.calls.length, 2);
  assert.equal(s.calls[1].options.body.form, s.forms[0]);
  await s.respond(1, {label: ''});
  assert.equal(s.labels.start.textContent, 'No mapped address found.');
  s.both.click(); assert.equal(s.calls.length, 2, 'both skips fetched and no-match results');
  s.submit(1); await s.respond(2, {error: 'Provider unavailable.'}, false);
  assert.equal(s.labels.end.textContent, 'Provider unavailable.');
  s.destination.value = 'Another edit'; s.useEnd.click(); assert.equal(s.destination.value, hostile);
  s.submit(1); await s.respond(3, {label: ''}); assert.equal(s.useEnd.disabled, true);
  s.submit(1); await s.respond(4, null); assert.match(s.labels.end.textContent, /Check your session/);
  assert.ok(s.forms.every(f => !('aria-disabled' in f.querySelector().attributes)));
  for (const auto of [false, true]) {
    const t = setup({auto});
    if (!auto) t.both.click();
    t.both.click(); t.submit(0); // no duplicate for running/queued endpoints
    assert.equal(t.calls.length, 1);
    assert.equal(t.calls[0].options.body.form.endpoint, 'start');
    await t.respond(0, {label: 'Example start'});
    assert.equal(t.calls.length, 2);
    assert.equal(t.calls[1].options.body.form.endpoint, 'end');
    await t.respond(1, {label: 'Example end'});
    assert.equal(t.useEnd.disabled, false);
    t.both.click(); assert.equal(t.calls.length, 2);
    const c = setup({auto, cached: ['start', 'end']});
    if (!auto) c.both.click();
    assert.equal(c.calls.length, 0, 'cached endpoints send nothing');
    const partial = setup({auto, cached: ['start']});
    if (!auto) partial.both.click();
    assert.equal(partial.calls.length, 1);
    assert.equal(partial.calls[0].options.body.form.endpoint, 'end');
    await partial.respond(0, {label: ''});
    const single = setup({auto, endpoints: ['end']});
    if (!auto) single.submit(0);
    assert.equal(single.calls.length, 1);
    await single.respond(0, {label: 'Example'});
  }
  console.log('Endpoint lookup controller: 1 suite passed (queue order/deduplication, both, auto, cache, inert text, edits, failures).');
}
run().catch(error => {console.error(error); process.exitCode = 1;});
