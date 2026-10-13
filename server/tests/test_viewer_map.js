// Run with node server/tests/test_viewer_map.js. No third-party test packages.
// Execute the real inline integration and real GPX waypoint parsing branch;
// Leaflet map drawing, the track geometry parser, and the XML DOM are stubs.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const viewer = path.resolve(__dirname, '../mileage-viewer');
const appPath = process.argv[2] || path.join(viewer, 'app.py');
const source = fs.readFileSync(appPath, 'utf8');
const inlineMap = source.match(/<script>\s*(var map = L\.map[\s\S]*?)<\/script>/)[1];
const popups = [];
const geometry = [];
let activeOptions;
let activeLayer;
const trackLayer = { type: 'track' };

const L = {
  FeatureGroup: function(layers) { this.layers = layers; },
  Icon: { extend: () => function() {} },
  Util: { setOptions: (object, options) => { object.options = options; } },
  LatLng: function(lat, lng) { this.lat = Number(lat); this.lng = Number(lng); },
  Marker: function() {
    this.bindPopup = html => { popups.push(html); return this; };
    this.openPopup = () => this;
  },
  map: () => ({}),
  tileLayer: () => ({ addTo: () => {} }),
};
L.FeatureGroup.extend = definition => {
  function GPX(gpx, options) { definition.initialize.call(this, gpx, options); }
  Object.assign(GPX.prototype, definition);
  GPX.prototype.on = function() { return this; };
  GPX.prototype.addTo = function() { return this; };
  GPX.prototype.fire = function() { return this; };
  return GPX;
};
const context = vm.createContext({ L, console });
vm.runInContext(fs.readFileSync(path.join(viewer, 'static/gpx.js'), 'utf8'), context);

// XML parsers decode &lt;img...&gt; to this textContent. The real plugin
// used to concatenate it directly into popup HTML.
const hostileName = '<img src=x onerror="window.pwned=true">';
const hostileDescription = '<svg onload="window.pwned=true"></svg>';
const waypoint = {
  getAttribute: name => name === 'lat' ? '37.0' : '-122.0',
  getElementsByTagName: name => {
    if (name === 'name') return [{ textContent: hostileName }];
    if (name === 'desc') return [{ textContent: hostileDescription }];
    return [];
  },
};
const track = { getElementsByTagNameNS: () => [] };
const xml = {
  getElementsByTagName: name => {
    if (name === 'wpt') return [waypoint];
    if (name === 'trk') return [track];
    if (name === 'name') return [{ textContent: hostileName }];
    if (name === 'desc') return [{ textContent: hostileDescription }];
    return [];
  },
};

// Replace network loading with a fixture; initialize still merges the real
// plugin defaults, and _parse_gpx_data still executes the vendor code.
L.GPX.prototype._parse = function(url, options) {
  activeOptions = options;
  activeLayer = this;
  this._parse_gpx_data(xml, options);
};
L.GPX.prototype._parse_segment = function(line, options, style, tag) {
  geometry.push(tag);
  return [trackLayer];
};
vm.runInContext(inlineMap, context);

assert.equal(popups.length, 0, 'uploaded waypoint markup must never reach popup HTML');
assert.deepEqual(geometry, ['trkpt'], 'the mileage track must still be rendered');
assert.equal(activeLayer.get_name(), hostileName, 'metadata remains inert text');
assert.equal(activeLayer.get_desc(), hostileDescription);

// Control: prove the fixture exercises the vulnerable vendor branch when
// waypoint parsing is enabled, rather than silently passing a vacuous test.
activeLayer._parse_gpx_data(xml, {
  ...activeOptions,
  gpx_options: { ...activeOptions.gpx_options, parseElements: ['waypoint'] },
});
assert.equal(popups.length, 1);
assert.ok(popups[0].includes(hostileName));
assert.ok(popups[0].includes(hostileDescription));
console.log('PASS: active viewer skips hostile waypoint popups and retains tracks');
