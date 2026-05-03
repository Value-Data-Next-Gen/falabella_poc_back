import { createRequire } from 'module';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const __dirname0 = path.dirname(fileURLToPath(import.meta.url));
const FRONT_NM = path.resolve(__dirname0, '../../valuedata_frontend/node_modules');
const require = createRequire(path.join(FRONT_NM, 'puppeteer/'));
const puppeteer = require('puppeteer');

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const HTML_DIR = __dirname;
const OUT_DIR = HTML_DIR;
const VIEWPORT = { width: 1280, height: 800, deviceScaleFactor: 2 };

const files = fs.readdirSync(HTML_DIR)
  .filter(f => /^\d+_.*\.html$/.test(f))
  .sort();

console.log('Files to render:', files.length);
const browser = await puppeteer.launch({ headless: 'new' });
for (const f of files) {
  const page = await browser.newPage();
  await page.setViewport(VIEWPORT);
  const url = 'file:///' + path.join(HTML_DIR, f).replace(/\\/g, '/');
  await page.goto(url, { waitUntil: 'networkidle0' });
  await new Promise(r => setTimeout(r, 800));
  const png = f.replace('.html', '.png');
  await page.screenshot({ path: path.join(OUT_DIR, png), fullPage: false });
  console.log('rendered', png);
  await page.close();
}
await browser.close();
console.log('done');
