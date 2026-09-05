/* Capture every dashboard tab to PNG, for report figures and visual checks.
 *
 * OPTIONAL and separate from the test suite. The project has no build step and
 * no Node toolchain; this only exists because reviewing a UI needs looking at
 * it, and the FYP report needs figures.
 *
 *   npm install puppeteer-core
 *   node tools/screenshot.mjs                 # light theme, all tabs
 *   node tools/screenshot.mjs --dark          # dark theme
 *   node tools/screenshot.mjs --tab assistant # one tab only
 *
 * Writes into screenshots/ (gitignored). The backend must already be running,
 * and Chrome or Edge must be installed - both default paths are probed.
 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import puppeteer from 'puppeteer-core';

const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const OUT = path.join(REPO, 'screenshots');

const BROWSERS = [
  'C:/Program Files/Google/Chrome/Application/chrome.exe',
  'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
];
const BASE = process.env.DASHBOARD_URL || 'http://127.0.0.1:5000';
const USER = process.env.DASHBOARD_USER || 'admin';
const PASS = process.env.DASHBOARD_PASS || 'admin123';

const TABS = ['overview', 'inventory', 'analytics', 'assistant', 'tags',
              'workers', 'manufacturing', 'alerts', 'audit'];

const args = process.argv.slice(2);
const dark = args.includes('--dark');
const only = args.includes('--tab') ? args[args.indexOf('--tab') + 1] : null;

const exe = BROWSERS.find(p => fs.existsSync(p));
if (!exe) {
  console.error('No Chrome or Edge found. Looked in:\n  ' + BROWSERS.join('\n  '));
  process.exit(2);
}
fs.mkdirSync(OUT, { recursive: true });

const browser = await puppeteer.launch({
  executablePath: exe, headless: 'new',
  args: ['--no-sandbox', '--disable-dev-shm-usage'],
});
const page = await browser.newPage();
await page.setViewport({ width: 1440, height: 900, deviceScaleFactor: 2 });

const problems = [];
page.on('console', m => { if (m.type() === 'error') problems.push('console: ' + m.text()); });
page.on('pageerror', e => problems.push('pageerror: ' + e.message));
page.on('response', r => { if (r.status() >= 400) problems.push(`${r.status()} ${r.url()}`); });

await page.goto(`${BASE}/login`, { waitUntil: 'domcontentloaded' });
if (await page.$('#username')) {
  await page.type('#username', USER);
  await page.type('#password', PASS);
  await Promise.all([
    page.waitForNavigation({ waitUntil: 'networkidle2' }).catch(() => {}),
    page.click('button[type=submit]'),
  ]);
  await new Promise(r => setTimeout(r, 1200));
}
if (!page.url().endsWith('/')) {
  await page.goto(`${BASE}/`, { waitUntil: 'networkidle2' });
}
await page.waitForSelector('.nav-item');

if (dark) {
  await page.evaluate(() => document.documentElement.setAttribute('data-theme', 'dark'));
}

const suffix = dark ? '-dark' : '';
for (const tab of (only ? [only] : TABS)) {
  const found = await page.evaluate((t) => {
    const btn = document.querySelector(`.nav-item[data-tab="${t}"]`);
    if (!btn || btn.style.display === 'none') return false;
    btn.click();
    return true;
  }, tab);
  if (!found) {
    console.log(`  skip ${tab} (not visible for this role)`);
    continue;
  }
  await new Promise(r => setTimeout(r, 2200));   // charts and fetches settle
  const file = path.join(OUT, `${tab}${suffix}.png`);
  await page.screenshot({ path: file, fullPage: true });
  console.log(`  wrote ${path.relative(REPO, file)}`);
}

console.log(problems.length ? `\nPage problems:\n  ${problems.join('\n  ')}`
                            : '\nNo console or HTTP errors.');
await browser.close();
