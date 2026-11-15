const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../mileage-viewer/static/trip-annotation.js'), 'utf8');

// Fake radios in one group: checking one unchecks the others, as in a browser.
function setup(values, checkedValue) {
  const inputs = values.map(value => {
    let on = value === checkedValue;
    const input = {value, handlers: {}, addEventListener(type, fn) { this.handlers[type] = fn; },
      uncheck() { on = false; }};
    Object.defineProperty(input, 'checked', {
      get() { return on; },
      set(next) {
        if (next) inputs.forEach(other => { if (other !== input) other.uncheck(); });
        on = Boolean(next);
      },
    });
    return input;
  });
  vm.runInNewContext(source, {document: {
    querySelectorAll(selector) { return selector === '.purpose-choices input' ? inputs : []; }}});
  const by = value => inputs.find(input => input.value === value);
  function checked() {
    const on = inputs.filter(input => input.checked);
    assert.equal(on.length, 1, 'exactly one purpose is checked');
    return on[0].value;
  }
  // A browser checks the clicked radio, runs its click handlers, then fires
  // change only if the radio was not already checked before the click.
  function click(value) {
    const input = by(value);
    const before = input.checked;
    input.checked = true;
    input.handlers.click();
    if (!before) input.handlers.change();
  }
  return {click, checked};
}

const purposes = ['', 'business', 'medical'];
let s = setup(purposes, 'business');
s.click('business');
assert.equal(s.checked(), '', 'second click on the selected purpose selects None');

s = setup(purposes, 'business');
s.click('medical');
assert.equal(s.checked(), 'medical', 'a different purpose is selected');
s.click('medical');
assert.equal(s.checked(), '', 'then clicking it again clears it');

s = setup(purposes, '');
s.click('');
assert.equal(s.checked(), '', 'clicking None leaves None');
s.click('business');
assert.equal(s.checked(), 'business', 'first click on an unselected purpose selects it');

// A closed year renders no purpose inputs: the script must do nothing, without throwing.
setup([], undefined);
console.log('trip annotation script checks passed');
