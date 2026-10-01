/* Dependency-free preference regression tests: node scripts/test-appearance.cjs */
const { test } = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const source = fs.readFileSync('crates/bss-portal-ui/assets/static/js/appearance.js', 'utf8');
function browser({ saved, fallback = 'system', blocked = false } = {}) {
  const attrs = new Map([['data-appearance-default', fallback]]);
  const listeners = {};
  const picker = { value: 'system', matches: () => true };
  const storage = new Map(saved === undefined ? [] : [['bss.appearance', saved]]);
  const document = {
    documentElement: { getAttribute: k => attrs.get(k), setAttribute: (k,v) => attrs.set(k,v), removeAttribute: k => attrs.delete(k) },
    querySelectorAll: () => [picker],
    addEventListener: (name, fn) => { listeners[name] = fn; }
  };
  const localStorage = {
    getItem: k => { if (blocked) throw Error('blocked'); return storage.get(k); },
    setItem: (k,v) => { if (blocked) throw Error('blocked'); storage.set(k,v); }
  };
  vm.runInNewContext(source, { document, localStorage, window: { addEventListener: (name,fn) => { listeners[name] = fn; } } });
  return { attrs, picker, storage, listeners, choose(value) { picker.value = value; listeners.change({ target: picker }); } };
}
test('saved appearance applies before DOMContentLoaded and persists', () => {
  const b = browser({ saved: 'light' });
  assert.equal(b.attrs.get('data-appearance'), 'light');
  b.choose('dark');
  assert.equal(b.storage.get('bss.appearance'), 'dark');
  assert.equal(browser({ saved: b.storage.get('bss.appearance') }).attrs.get('data-appearance'), 'dark');
});
test('system delegates OS changes to CSS and survives reload', () => {
  const b = browser({ fallback: 'dark', saved: 'system' });
  assert.equal(b.attrs.has('data-appearance'), false);
  b.choose('light'); b.choose('system');
  assert.equal(b.attrs.has('data-appearance'), false);
  assert.equal(b.storage.get('bss.appearance'), 'system');
});
test('blocked storage permits session-only changes', () => {
  const b = browser({ blocked: true, fallback: 'dark' });
  assert.equal(b.attrs.get('data-appearance'), 'dark');
  b.listeners.DOMContentLoaded();
  b.choose('light');
  assert.equal(b.attrs.get('data-appearance'), 'light');
});
test('invalid values fall back to system and same-origin tabs synchronize', () => {
  const b = browser({ saved: 'invalid' });
  assert.equal(b.attrs.has('data-appearance'), false);
  b.storage.set('bss.appearance', 'dark');
  b.listeners.storage({ key: 'bss.appearance' });
  assert.equal(b.attrs.get('data-appearance'), 'dark');
  b.storage.clear(); b.listeners.storage({ key: null });
  assert.equal(b.picker.value, 'system');
});
