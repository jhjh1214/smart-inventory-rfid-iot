/* Headless check of the Assistant tab.
 *
 * OPTIONAL and separate from the pytest suite - the project has no build step
 * and no Node toolchain, so nothing depends on this. It exists because the
 * dashboard's escaping rule (CLAUDE.md invariant 6) now has to hold against LLM
 * output, and that is worth a repeatable test rather than a one-off inspection.
 *
 * Loads the real dashboard.html and dashboard.js into jsdom with fetch stubbed,
 * then drives the tab: status states, ask/answer, follow-up history, error
 * handling, and an XSS payload returned as a model answer.
 *
 *   npm install jsdom
 *   node tools/ui_check.mjs
 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { JSDOM } from 'jsdom';

// Resolve the repo from this file's own location, so the check runs from
// anywhere and survives the folder being moved between machines.
const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const html = fs.readFileSync(path.join(REPO, 'backend/templates/dashboard.html'), 'utf8');
const js = fs.readFileSync(path.join(REPO, 'backend/static/js/dashboard.js'), 'utf8');

let pass = 0, fail = 0;
const ok = (cond, msg) => { if (cond) { pass++; console.log('  PASS  ' + msg); }
                            else { fail++; console.log('  FAIL  ' + msg); } };

// Strip Jinja tags so jsdom sees plain HTML.
const plain = html.replace(/\{\{[^}]*\}\}/g, '').replace(/\{%[^%]*%\}/g, '');

const dom = new JSDOM(plain, { url: 'http://localhost:5000/', runScripts: 'outside-only' });
const { window } = dom;
global.window = window;
global.document = window.document;

// Stub the network, routed by URL. Only /api/assistant consults NEXT; every
// other call the boot sequence makes gets a benign empty success, so init()
// never hits its redirect-to-login path.
let NEXT = null, REQUESTS = [];
window.fetch = async (url, opts) => {
  REQUESTS.push({ url, opts });
  if (String(url).includes('/api/me')) {
    return { ok: true, status: 200,
             json: async () => ({ username: 'admin', role: 'admin' }) };
  }
  if (String(url).includes('/api/assistant')) {
    const r = NEXT;
    return { ok: r.status >= 200 && r.status < 300, status: r.status,
             json: async () => r.body };
  }
  return { ok: true, status: 200, json: async () => ({}) };
};
window.localStorage.setItem('activeTab', 'overview');

window.eval(js);

const A = (name) => window[name];

console.log('\n1. Module surface');
for (const fn of ['initAssistant', 'fetchAssistantStatus', 'renderAssistantLog',
                  'onAssistantSubmit', 'clearAssistantChat', 'updateAssistantControls']) {
  ok(typeof A(fn) === 'function', `${fn} is defined globally`);
}

console.log('\n2. Status: configured');
NEXT = { status: 200, body: { available: true, provider: 'gemini',
                              tools: ['list_inventory'], reason: '' } };
await A('fetchAssistantStatus')();
const pill = document.getElementById('assistant-provider');
const setup = document.getElementById('assistant-setup');
ok(pill.textContent.includes('gemini') && pill.textContent.includes('ready'),
   'provider pill shows "gemini · ready"');
ok(setup.classList.contains('hidden'), 'setup banner hidden when configured');
ok(document.getElementById('assistant-input').disabled === false, 'input enabled');

console.log('\n3. Status: not configured');
NEXT = { status: 200, body: { available: false, provider: 'gemini',
                              tools: [], reason: 'GEMINI_API_KEY is not set' } };
await A('fetchAssistantStatus')();
ok(!setup.classList.contains('hidden'), 'setup banner shown');
ok(setup.textContent.includes('GEMINI_API_KEY is not set'), 'reason surfaced to the user');
ok(document.getElementById('assistant-input').disabled === true,
   'input disabled when unavailable');

console.log('\n4. Ask/answer round trip');
NEXT = { status: 200, body: { available: true, provider: 'gemini', tools: [] } };
await A('fetchAssistantStatus')();
await A('initAssistant')();
const input = document.getElementById('assistant-input');
const form = document.getElementById('assistant-form');
input.value = 'which items are low?';
REQUESTS = [];
NEXT = { status: 200, body: { status: 'ok', answer: 'Two items are low: item-003, item-007.',
                              tools_used: ['list_inventory'], model: 'gemini-2.5-flash' } };
await A('onAssistantSubmit')({ preventDefault() {} });
const log = document.getElementById('assistant-log');
ok(log.textContent.includes('which items are low?'), 'question rendered');
ok(log.textContent.includes('item-003'), 'answer rendered');
ok(log.textContent.includes('list_inventory'), 'tool badge rendered');
ok(input.value === '', 'input cleared after send');
const body = JSON.parse(REQUESTS.at(-1).opts.body);
ok(body.question === 'which items are low?', 'question posted');
ok(Array.isArray(body.history) && body.history.length === 0,
   'first turn posts empty history');

console.log('\n5. History accumulates for follow-ups');
input.value = 'and which runs out first?';
NEXT = { status: 200, body: { status: 'ok', answer: 'item-007.', tools_used: [] } };
await A('onAssistantSubmit')({ preventDefault() {} });
const body2 = JSON.parse(REQUESTS.at(-1).opts.body);
ok(body2.history.length === 2, 'prior user+assistant turns sent as history');
ok(body2.history[0].role === 'user' && body2.history[1].role === 'assistant',
   'history roles are correct');
ok(body2.history.every(t => !('tools' in t)), 'tool metadata stripped from history');

console.log('\n6. XSS: model output must never become markup');
const payload = '<img src=x onerror="window.__pwned=1">' +
                '<script>window.__pwned=1<\/script>';
input.value = 'tell me about <script>alert(1)<\/script>';
NEXT = { status: 200, body: { status: 'ok', answer: payload,
                              tools_used: ['<b>evil</b>'] } };
await A('onAssistantSubmit')({ preventDefault() {} });
ok(window.__pwned === undefined, 'no script executed from model output');
ok(log.querySelector('img') === null, 'no <img> element created from answer');
ok(log.querySelector('script') === null, 'no <script> element created from answer');
ok(log.textContent.includes('<img src=x'), 'payload shown literally as text');
ok(log.querySelector('b') === null, 'tool name not rendered as markup');

console.log('\n7. Error handling');
const failedTurns = () => log.querySelectorAll('.assistant-turn').length;
const before = failedTurns();
input.value = 'this one fails';
NEXT = { status: 502, body: { error: 'The assistant could not answer right now.' } };
await A('onAssistantSubmit')({ preventDefault() {} });
ok(failedTurns() === before, 'failed question leaves no dangling turn in the log');
ok(document.getElementById('assistant-send').disabled === false,
   'controls re-enabled after failure');

console.log('\n8. Clear chat');
A('clearAssistantChat')();
ok(log.querySelectorAll('.assistant-turn').length === 0, 'log emptied');
ok(log.textContent.includes('Nothing asked yet'), 'empty state restored');

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
