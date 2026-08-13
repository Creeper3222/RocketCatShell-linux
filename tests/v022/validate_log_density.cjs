const fs = require('fs');
const path = require('path');
const { chromium } = require('playwright');

const baseUrl = process.argv[2] || 'http://127.0.0.1:58731/';
const outputDir = path.resolve(process.argv[3] || 'data/temp/log-density-screenshots');
const executablePath = process.argv[4] || 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe';

fs.mkdirSync(outputDir, { recursive: true });

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

function watchBrowser(page, errors) {
  page.on('pageerror', (error) => errors.push(`pageerror: ${error.message}`));
  page.on('console', (message) => {
    if (message.type() === 'error' && !message.text().startsWith('Failed to load resource:')) {
      errors.push(`console: ${message.text()}`);
    }
  });
}

async function waitForMobileDrawerClosed(page) {
  await page.waitForFunction(() => {
    const sidebar = document.querySelector('#appSidebar');
    if (!sidebar || state.ui.mobileNavigationOpen) return false;
    const matrix = new DOMMatrixReadOnly(getComputedStyle(sidebar).transform);
    return Math.abs(matrix.m41 + sidebar.getBoundingClientRect().width) < 1
      && !document.body.classList.contains('navigation-gesturing');
  });
}

async function login(page) {
  await page.goto(baseUrl, { waitUntil: 'domcontentloaded' });
  if (await page.locator('#authPasswordInput').count()) {
    await page.locator('#authPasswordInput').fill('123456');
    await page.locator('#authLoginForm').evaluate((form) => form.requestSubmit());
  }
  await page.locator('[data-page="logs"]').waitFor({ state: 'attached' });
  await page.waitForFunction(() => document.querySelector('#networkPage')?.getAttribute('aria-busy') !== 'true');
}

async function openLogs(page, mobile = false) {
  if (mobile) {
    await page.locator('#mobileMenuButton').click();
    await page.waitForFunction(() => state.ui.mobileNavigationOpen === true);
  }
  await page.locator('[data-page="logs"]').click();
  await page.waitForFunction(() => window.location.hash === '#logs');
  if (mobile) {
    await waitForMobileDrawerClosed(page);
  }
  await page.evaluate(() => stopLogPolling());
}

async function installFixtureLogs(page, count) {
  await page.evaluate((itemCount) => {
    const levels = ['INFO', 'DEBUG', 'WARN', 'ERROR'];
    state.logs.items = Array.from({ length: itemCount }, (_, index) => {
      const id = index + 1;
      const isPerf = id % 6 === 0;
      return {
        id,
        level: levels[index % levels.length],
        is_perf: isPerf,
        line: isPerf
          ? `[2026-08-10 22:40:${String(id).padStart(2, '0')}.000] [PERF] inbound translate duration=${id}.4ms queue=0`
          : `[2026-08-10 22:40:${String(id).padStart(2, '0')}.000] [RocketCatShell] 日志密度验证记录 ${id} · runtime event processed successfully`,
      };
    });
    state.logs.lastId = itemCount;
    state.logs.showPerf = false;
    state.logs.autoScroll = true;
    elements.logConsole.replaceChildren();
    renderLogs();
  }, count);
}

async function assertNoHorizontalOverflow(page, label) {
  const metrics = await page.evaluate(() => ({
    viewport: window.innerWidth,
    document: document.documentElement.scrollWidth,
    consoleClient: document.querySelector('#logConsole').clientWidth,
    consoleScroll: document.querySelector('#logConsole').scrollWidth,
  }));
  assert(metrics.document <= metrics.viewport, `${label} document overflow: ${JSON.stringify(metrics)}`);
  assert(metrics.consoleScroll <= metrics.consoleClient + 1, `${label} log overflow: ${JSON.stringify(metrics)}`);
}

async function validateDesktop(browser, errors) {
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await context.newPage();
  watchBrowser(page, errors);
  await login(page);
  await openLogs(page);
  await installFixtureLogs(page, 36);

  const perfButton = page.locator('[data-log-perf]');
  assert(await perfButton.getAttribute('aria-pressed') === 'false', 'Perf filter is not disabled by default');
  assert(await page.locator('.log-entry').count() === 30, 'default view did not exclude Perf logs');
  const rowMetrics = await page.locator('.log-entry').first().evaluate((row) => {
    const style = getComputedStyle(row);
    return {
      height: row.getBoundingClientRect().height,
      paddingTop: style.paddingTop,
      paddingBottom: style.paddingBottom,
      lineHeight: style.lineHeight,
      borderBottomWidth: style.borderBottomWidth,
      borderBottomStyle: style.borderBottomStyle,
    };
  });
  assert(rowMetrics.height <= 25, `log row is not compact: ${JSON.stringify(rowMetrics)}`);
  assert(rowMetrics.paddingTop === '3px' && rowMetrics.paddingBottom === '3px', `unexpected row padding: ${JSON.stringify(rowMetrics)}`);
  assert(rowMetrics.borderBottomWidth === '0px' || rowMetrics.borderBottomStyle === 'none', `row separator remains: ${JSON.stringify(rowMetrics)}`);
  await assertNoHorizontalOverflow(page, 'desktop logs');
  await page.screenshot({ path: path.join(outputDir, '01-logs-default-1440x900.png'), animations: 'disabled' });

  await perfButton.click();
  assert(await perfButton.getAttribute('aria-pressed') === 'true', 'Perf filter did not turn on');
  assert(await page.locator('.log-entry').count() === 36, 'Perf logs did not return after enabling the filter');
  await page.screenshot({ path: path.join(outputDir, '02-logs-perf-enabled-1440x900.png'), animations: 'disabled' });
  await context.close();
}

async function validateMobile(browser, errors) {
  const context = await browser.newContext({ viewport: { width: 390, height: 844 } });
  const page = await context.newPage();
  watchBrowser(page, errors);
  await login(page);
  await openLogs(page, true);
  await installFixtureLogs(page, 18);
  assert(await page.locator('[data-log-perf]').getAttribute('aria-pressed') === 'false', 'mobile Perf filter is not disabled by default');
  assert(await page.locator('.log-entry').count() === 15, 'mobile default view did not exclude Perf logs');
  await assertNoHorizontalOverflow(page, 'mobile logs');
  await page.screenshot({ path: path.join(outputDir, '03-logs-default-390x844.png'), animations: 'disabled' });
  await context.close();
}

(async () => {
  const browser = await chromium.launch({ headless: true, executablePath });
  const errors = [];
  try {
    await validateDesktop(browser, errors);
    await validateMobile(browser, errors);
    assert(errors.length === 0, `browser errors: ${errors.join(' | ')}`);
    process.stdout.write(JSON.stringify({ outputDir, screenshots: 3, errors }, null, 2));
  } finally {
    await browser.close();
  }
})().catch((error) => {
  process.stderr.write(`${error.stack || error}\n`);
  process.exitCode = 1;
});
