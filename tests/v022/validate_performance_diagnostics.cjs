const fs = require('fs');
const path = require('path');
const { chromium } = require('playwright');

const baseUrl = process.argv[2] || 'http://127.0.0.1:58731/';
const outputDir = path.resolve(process.argv[3] || 'data/temp/performance-diagnostics-screenshots');
const executablePath = process.argv[4] || 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe';

fs.mkdirSync(outputDir, { recursive: true });

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

async function openDiagnostics(page) {
  await page.goto(baseUrl, { waitUntil: 'domcontentloaded' });
  if (await page.locator('#authPasswordInput').count()) {
    await page.locator('#authPasswordInput').fill('123456');
    await page.locator('#authSubmitButton').click();
    await page.locator('[data-page="diagnostics"]').waitFor({ state: 'visible' });
  }
  if (page.viewportSize().width <= 1120) {
    await page.locator('#mobileMenuButton').click();
    await page.waitForFunction(() => state.ui.mobileNavigationOpen === true);
  }
  await page.locator('[data-page="diagnostics"]').click();
  await page.waitForFunction(() => (
    window.location.hash === '#diagnostics'
    && document.querySelector('#diagnosticsPage')?.getAttribute('aria-busy') !== 'true'
  ));
  if (page.viewportSize().width <= 1120) {
    await page.waitForFunction(() => {
      const sidebar = document.querySelector('#appSidebar');
      if (!sidebar || state.ui.mobileNavigationOpen) return false;
      const matrix = new DOMMatrixReadOnly(getComputedStyle(sidebar).transform);
      return Math.abs(matrix.m41 + sidebar.getBoundingClientRect().width) < 1;
    });
  }
  await page.locator('.performance-backpressure-panel').evaluate((details) => {
    details.open = true;
  });
  await page.locator('#performanceEventLoop').waitFor({ state: 'visible' });
  await page.waitForFunction(() => document.querySelector('#performanceEventLoop')?.textContent !== '-');
}

async function assertNoOverflow(page, label) {
  const dimensions = await page.evaluate(() => ({
    viewport: window.innerWidth,
    document: document.documentElement.scrollWidth,
    body: document.body.scrollWidth,
  }));
  assert(
    dimensions.document <= dimensions.viewport && dimensions.body <= dimensions.viewport,
    `${label} horizontal overflow: ${JSON.stringify(dimensions)}`,
  );
}

(async () => {
  const browser = await chromium.launch({ headless: true, executablePath });
  const errors = [];
  try {
    for (const [name, viewport] of [
      ['desktop', { width: 1440, height: 900 }],
      ['mobile', { width: 390, height: 844 }],
    ]) {
      const context = await browser.newContext({ viewport });
      const page = await context.newPage();
      page.on('pageerror', (error) => errors.push(`${name} pageerror: ${error.message}`));
      page.on('console', (message) => {
        if (message.type() === 'error' && !message.text().startsWith('Failed to load resource:')) {
          errors.push(`${name} console: ${message.text()}`);
        }
      });
      await openDiagnostics(page);
      await assertNoOverflow(page, name);
      assert(await page.locator('.performance-bot-card').count() === 6, `${name} missing Bot performance cards`);
      await page.screenshot({
        path: path.join(outputDir, `performance-diagnostics-${name}.png`),
        animations: 'disabled',
        fullPage: true,
      });
      await context.close();
    }
    assert(errors.length === 0, `browser errors: ${errors.join(' | ')}`);
    process.stdout.write(JSON.stringify({ outputDir, screenshots: 2, errors }, null, 2));
  } finally {
    await browser.close();
  }
})().catch((error) => {
  process.stderr.write(`${error.stack || error}\n`);
  process.exitCode = 1;
});
