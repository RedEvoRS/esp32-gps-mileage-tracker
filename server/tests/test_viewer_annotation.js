const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../mileage-viewer/static/trip-annotation.js'), 'utf8');

// Fake radios: like real ones, clicking checks the clicked one and unchecks the others.
function setup(values, checkedValue) {
  const inputs = values.map(value => ({value, checked: value === checkedValue, handlers: {},
    addEventListener(type, fn) { this.handlers[type] = fn; }}));
  vm.runInNewContext(source, {document: {
    querySelectorAll(selector) { return selector === '.purpose-choices input' ? inputs : []; }}});
  const by = value => inputs.find(input => input.value === value);
  const checked = () => (inputs.find(input => input.checked) || {}).value;
  function click(value) {
    const input = by(value);
    inputs.forEach(other => { other.checked = other === input; });
    input.handlers.click();
    input.handlers.change();
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
