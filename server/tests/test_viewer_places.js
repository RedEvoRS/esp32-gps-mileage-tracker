const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../mileage-viewer/static/endpoint-places.js'), 'utf8');
const tick = () => new Promise(resolve => setImmediate(resolve));
function setup({auto = false, cached = [], endpoints = ['start', 'end'], storage = new Map(), throws = []} = {}) {
  const autoSwitch = {hidden: true, attributes: {}, textContent: '',
    setAttribute(k, v) { this.attributes[k] = v; }, addEventListener(_, fn) { this.click = fn; }};
  const autoStatus = {textContent: ''};
  const localStorage = {
    getItem(k) { if (throws.includes('get')) throw Error('blocked'); return storage.get(k); },
    setItem(k, v) { if (throws.includes('set')) throw Error('blocked'); storage.set(k, v); },
    removeItem(k) { if (throws.includes('remove')) throw Error('blocked'); storage.delete(k); },
  };
  const destination = {value: 'Unsaved edit', focus() { this.focused = true; }};
  const useEnd = {disabled: !cached.includes('end'), addEventListener(_, fn) { this.click = fn; }};
  const suggestion = {textContent: ''};
  const both = {disabled: true, addEventListener(_, fn) { this.click = fn; }};
  const labels = {start: {textContent: 'Cached start'}, end: {textContent: 'Cached end'}};
  const forms = endpoints.map(endpoint => {
    const button = {disabled: true, attributes: {}, setAttribute(k,v) { this.attributes[k]=v; }, removeAttribute(k) { delete this.attributes[k]; }};
    return {action: '/trip/2026-10/sample/lookup-place', dataset: {placeCached: String(cached.includes(endpoint))},
      endpoint, addEventListener(_, fn) { this.submit = fn; }, querySelector() { return button; },
      closest() { return {dataset: {endpoint}, querySelector() { return labels[endpoint]; }}; }};
  });
  const calls = [];
  vm.runInNewContext(source, {localStorage, document: {
    currentScript: {dataset: auto ? {autoLookup: 'true'} : {}},
    getElementById(id) { return {'end-suggestion': suggestion, 'auto-lookup': auto ? autoSwitch : null, 'use-end-place': useEnd, 'lookup-both-places': endpoints.length === 2 ? both : null, destination}[id]; },
    querySelector(selector) { return selector === '[data-auto-lookup-status]' ? autoStatus : labels.end; }, querySelectorAll() { return forms; }},
    FormData: class {constructor(form) { this.form = form; }},
    fetch(url, options) { return new Promise(resolve => calls.push({url, options, resolve})); }});
  async function respond(index, data, ok = true) {
    calls[index].resolve({ok, async json() { if (data === null) throw Error('not JSON'); return data; }});
    await tick();
  }
  return {suggestion, autoSwitch, autoStatus, storage, forms, labels, both, calls, destination, useEnd, respond,
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
  assert.equal(s.suggestion.textContent, 'End: ' + hostile);
  assert.equal(s.destination.value, 'Unsaved edit');
  s.useEnd.click(); assert.equal(s.destination.value, hostile);
  assert.equal(s.calls.length, 2);
  assert.equal(s.calls[1].options.body.form, s.forms[0]);
  await s.respond(1, {label: ''});
  assert.equal(s.labels.start.textContent, 'No mapped address found.');
  s.both.click(); assert.equal(s.calls.length, 2, 'both skips fetched and no-match results');
  s.submit(1); await s.respond(2, {error: 'Provider unavailable.'}, false);
  assert.equal(s.labels.end.textContent, 'Provider unavailable.');
  assert.equal(s.suggestion.textContent, 'End: Provider unavailable.');
  assert.equal(s.useEnd.disabled, true, 'failed lookup disables stale suggestion');
  s.submit(1); await s.respond(3, {label: ''}); assert.equal(s.useEnd.disabled, true);
  assert.equal(s.suggestion.textContent, 'End: no mapped address found');
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
  const key = 'viewer-auto-lookup';
  const onText = 'Uncached endpoints are looked up when a trip opens.';
  const offText = 'Automatic lookup is off in this browser. Nothing is sent until you click.';
  function state(t, on) {
    assert.equal(t.autoSwitch.hidden, false);
    assert.equal(t.autoSwitch.textContent, on ? 'Auto lookup: On' : 'Auto lookup: Off');
    assert.equal(t.autoSwitch.attributes['aria-pressed'], String(on));
    assert.equal(t.autoStatus.textContent, on ? onText : offText);
  }
  const retry = setup({auto: true});
  assert.equal(retry.both.hidden, true);
  assert.ok(retry.forms.every(form => form.querySelector().hidden));
  await retry.respond(0, {error: 'Provider unavailable.'}, false);
  assert.equal(retry.forms[0].querySelector().hidden, false);
  assert.equal(retry.forms[0].querySelector().textContent, 'Try again');
  assert.equal(retry.suggestion.textContent, 'End: looking up\u2026');
  await retry.respond(1, {label: '100 Example Ave'});
  assert.equal(retry.suggestion.textContent, 'End: 100 Example Ave');
  assert.equal(retry.forms[1].querySelector().hidden, true);
  retry.submit(0);
  assert.equal(retry.forms[0].querySelector().hidden, true);
  await retry.respond(2, {label: ''});
  retry.autoSwitch.click();
  assert.equal(retry.both.hidden, false);
  assert.ok(retry.forms.every(form => !form.querySelector().hidden));
  const defaultOn = setup({auto: true}); state(defaultOn, true);
  assert.equal(defaultOn.calls.length, 1);
  defaultOn.autoSwitch.click(); state(defaultOn, false);
  assert.equal(defaultOn.storage.get(key), 'off');
  await defaultOn.respond(0, {label: 'Example'});
  assert.equal(defaultOn.calls.length, 2, 'queued lookup may finish after Off');
  await defaultOn.respond(1, {label: ''});
  const next = setup({auto: true, storage: defaultOn.storage}); state(next, false);
  assert.equal(next.calls.length, 0, 'Off persists on the next trip page');
  next.submit(1); assert.equal(next.calls.length, 1, 'single manual lookup works while Off');
  await next.respond(0, {label: 'Example'});
  next.both.click(); assert.equal(next.calls.length, 2, 'both works while Off');
  await next.respond(1, {label: ''});
  const off = setup({auto: true, cached: ['start'], storage: new Map([[key, 'off']])});
  state(off, false); assert.equal(off.calls.length, 0);
  off.autoSwitch.click(); state(off, true);
  assert.equal(off.storage.has(key), false, 'On removes preference');
  assert.equal(off.calls.length, 1);
  assert.equal(off.calls[0].options.body.form.endpoint, 'end');
  await off.respond(0, {label: ''});
  off.autoSwitch.click(); state(off, false);
  off.autoSwitch.click(); state(off, true);
  assert.equal(off.calls.length, 1, 'completed no-match is not looked up again');
  for (const throws of [['get'], ['set'], ['remove'], ['get', 'set', 'remove']]) {
    const blocked = setup({auto: true, throws}); state(blocked, true);
    blocked.autoSwitch.click(); state(blocked, false);
    blocked.autoSwitch.click(); state(blocked, true);
    assert.equal(blocked.calls.length, 1, 'storage errors preserve queue deduplication');
    await blocked.respond(0, {label: ''});
    await blocked.respond(1, {label: ''});
  }
  const denied = setup({storage: new Map([[key, 'off']])});
  assert.equal(denied.autoSwitch.hidden, true);
  assert.equal(denied.calls.length, 0);
  console.log('Endpoint lookup controller: 1 suite passed (queue order/deduplication, both, auto preference/toggles/storage errors, cache, inert text, edits, failures).');
}
run().catch(error => {console.error(error); process.exitCode = 1;});
