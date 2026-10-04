/* Shared helpers for the dashboard browser tests (CommonJS, Playwright).
 *
 * launch()       headless Chromium from /opt/pw-browsers (falls back to the full binary)
 * startServer()  runs tests/e2e/serve_demo.py and resolves with { url, child }
 * check()        throws "check failed: <message>" when the condition is false
 */
'use strict';

const fs = require('fs');
const path = require('path');
const { spawn } = require('child_process');

if (!process.env.PLAYWRIGHT_BROWSERS_PATH && fs.existsSync('/opt/pw-browsers')) {
  process.env.PLAYWRIGHT_BROWSERS_PATH = '/opt/pw-browsers';
}
const PW_MODULE = process.env.PLAYWRIGHT_MODULE || '/opt/node-tools/node_modules/playwright';
const { chromium } = require(PW_MODULE);

const ROOT = path.resolve(__dirname, '..', '..');

function findChrome() {
  if (process.env.CHROME_PATH && fs.existsSync(process.env.CHROME_PATH)) return process.env.CHROME_PATH;
  const base = '/opt/pw-browsers';
  if (!fs.existsSync(base)) return null;
  for (const dir of fs.readdirSync(base).filter((d) => /^chromium-\d+$/.test(d)).sort().reverse()) {
    const bin = path.join(base, dir, 'chrome-linux', 'chrome');
    if (fs.existsSync(bin)) return bin;
  }
  return null;
}

async function launch() {
  const headless = !process.env.E2E_HEADED;
  if (process.env.CHROME_PATH) return chromium.launch({ headless, executablePath: findChrome() });
  try {
    return await chromium.launch({ headless });
  } catch (err) {
    const bin = findChrome();
    if (!bin) throw err;
    return chromium.launch({ headless, executablePath: bin });
  }
}

function startServer(interval) {
  return new Promise((resolve, reject) => {
    const python = process.env.PYTHON || 'python3';
    const child = spawn(python, [path.join(__dirname, 'serve_demo.py'), '--interval', String(interval || 2)], {
      cwd: ROOT,
      stdio: ['pipe', 'pipe', 'inherit'],
    });
    let buf = '';
    const timer = setTimeout(() => reject(new Error('serve_demo.py did not print a URL within 60 s')), 60000);
    child.on('error', (err) => { clearTimeout(timer); reject(err); });
    child.on('exit', (code) => { clearTimeout(timer); reject(new Error('serve_demo.py exited early with code ' + code)); });
    child.stdout.on('data', (chunk) => {
      buf += chunk.toString();
      const nl = buf.indexOf('\n');
      if (nl !== -1) {
        clearTimeout(timer);
        child.removeAllListeners('exit');
        resolve({ url: buf.slice(0, nl).trim(), child });
      }
    });
  });
}

function stopServer(child) {
  if (!child) return;
  child.stdin.end();
  setTimeout(() => child.kill('SIGTERM'), 3000).unref();
}

function check(cond, message) {
  if (!cond) throw new Error('check failed: ' + message);
}

module.exports = { chromium, launch, startServer, stopServer, check, ROOT };
