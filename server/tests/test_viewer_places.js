// Run the real browser controller with a minimal DOM and fake HTTP transport.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

async function run() {
  const destination = { value: 'Unsaved manual edit', focus() { this.focused = true; } };
  const useEnd = { disabled: true, addEventListener(type, callback) { this.click = callback; } };
  const labels = { start: { textContent: 'Not looked up yet.' }, end: { textContent: 'Not looked up yet.' } };
  const forms = ['start', 'end'].map(endpoint => {
    const button = {
      disabled: false, attributes: {},
      setAttribute(name, value) { this.attributes[name] = value; },
      removeAttribute(name) { delete this.attributes[name]; },
    };
    return {
      action: '/trip/2026-10/sample/lookup-place',
      addEventListener(type, callback) { this.submit = callback; },
      closest() { return { dataset: { endpoint }, querySelector() { return labels[endpoint]; } }; },
      querySelector() { return button; },
    };
  });
  const calls = [];
  let respond;
  const document = {
    getElementById(id) { return id === 'destination' ? destination : useEnd; },
    querySelector() { return labels.end; },
    querySelectorAll() { return forms; },
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../mileage-viewer/static/endpoint-places.js'), 'utf8'), {
    document,
    FormData: class { constructor(form) { this.form = form; } },
    fetch(url, options) {
      calls.push({ url, options });
      return new Promise(resolve => { respond = resolve; });
    },
  });
  assert.equal(calls.length, 0, 'page load must never perform geocoding');
  let prevented = 0;
  const submit = form => form.submit({ preventDefault() { prevented++; } });
  const pending = submit(forms[1]);
  assert.equal(prevented, 1);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].options.method, 'POST');
  assert.equal(calls[0].options.credentials, 'same-origin');
  assert.equal(calls[0].options.body.form, forms[1], 'CSRF form is sent unchanged');
  assert.ok(forms.every(form => !form.querySelector().disabled), 'focused buttons remain enabled');
  assert.ok(forms.every(form => form.querySelector().attributes['aria-disabled'] === 'true'));
  await submit(forms[0]);
  assert.equal(calls.length, 1, 'double clicks and other endpoint cannot overlap');
  const hostile = '<img src=x onerror=alert(1)>';
  respond({ ok: true, async json() { return { label: hostile }; } });
  await pending;
  assert.equal(labels.end.textContent, hostile, 'provider markup must be inert text');
  assert.equal(destination.value, 'Unsaved manual edit', 'lookup cannot overwrite edits');
  assert.equal(useEnd.disabled, false);
  assert.ok(forms.every(form => !form.querySelector().disabled));
  assert.ok(forms.every(form => !('aria-disabled' in form.querySelector().attributes)));
  useEnd.click();
  assert.equal(destination.value, hostile);
  assert.equal(destination.focused, true);
  assert.equal(calls.length, 1, 'applying suggestion must not save automatically');

  const failed = submit(forms[1]);
  respond({ ok: false, async json() { return { error: 'Provider unavailable.' }; } });
  await failed;
  assert.equal(labels.end.textContent, 'Provider unavailable.');
  destination.value = 'Another unsaved edit';
  useEnd.click();
  assert.equal(destination.value, hostile, 'previous successful suggestion stays usable');

  const noMatch = submit(forms[1]);
  respond({ ok: true, async json() { return { label: '' }; } });
  await noMatch;
  assert.equal(labels.end.textContent, 'No mapped address found.');
  assert.equal(useEnd.disabled, true);
  destination.value = 'Preserve';
  useEnd.click();
  assert.equal(destination.value, 'Preserve');

  const expired = submit(forms[1]);
  respond({ ok: false, async json() { throw new Error('not JSON'); } });
  await expired;
  assert.match(labels.end.textContent, /Check your session/);
  assert.ok(forms.every(form => !form.querySelector().disabled));
  console.log('Endpoint lookup controller: privacy, editing, failures and duplicate actions passed.');
}
run().catch(error => { console.error(error); process.exitCode = 1; });
