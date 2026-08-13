const fs = require('fs');
const path = require('path');
const { chromium } = require('playwright');

const baseUrl = process.argv[2] || 'http://127.0.0.1:58731/';
const outputDir = path.resolve(process.argv[3] || 'test-artifacts/v022-webui');
const executablePath = process.argv[4] || 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe';
fs.mkdirSync(outputDir, { recursive: true });

function assert(condition, message) {
  if (!condition) {
    throw new Error(message);
  }
}

function jsonResponse(body) {
  return {
    status: 200,
    contentType: 'application/json; charset=utf-8',
    body: JSON.stringify(body),
  };
}

async function installDeterministicUpdateRoutes(context) {
  const checkedAt = 1786290000;
  await context.route('**/api/updates/status*', (route) => route.fulfill(jsonResponse({
    current_version: 'v0.2.2',
    current_tag: 'v0.2.2',
    latest_version: 'v0.2.3-rc.1',
    latest_tag: 'v0.2.3-rc.1',
    minimum_compatible_tag: 'v0.2.2',
    update_available: true,
    checked_at: checkedAt,
    stale: false,
    error: '',
    refresh_limited: false,
    active_transaction: null,
  })));
  await context.route('**/api/updates/releases*', (route) => route.fulfill(jsonResponse({
    checked_at: checkedAt,
    stale: false,
    error: '',
    refresh_limited: false,
    releases: [
      {
        tag_name: 'v0.2.3-rc.1',
        version: 'v0.2.3-rc.1',
        name: 'UI validation preview',
        published_at: '2026-08-09T00:00:00Z',
        prerelease: true,
        notes: 'Deterministic prerelease candidate for the isolated browser validation.',
        action: 'update',
        asset: {
          name: 'RocketCatShell-v0.2.3-rc.1.zip',
          size: 1048576,
          digest: `sha256:${'a'.repeat(64)}`,
        },
      },
      {
        tag_name: 'v0.2.2',
        version: 'v0.2.2',
        name: 'Current version',
        published_at: '2026-08-08T00:00:00Z',
        prerelease: false,
        notes: 'Deterministic same-version reinstall candidate.',
        action: 'reinstall',
        asset: {
          name: 'RocketCatShell-v0.2.2.zip',
          size: 1048576,
          digest: `sha256:${'b'.repeat(64)}`,
        },
      },
    ],
  })));
}

async function openSettings(context, errors) {
  const page = await context.newPage();
  page.on('pageerror', (error) => errors.push(`pageerror: ${error.message}`));
  page.on('response', (response) => {
    if (response.status() >= 400 && !response.url().endsWith('/favicon.ico')) {
      errors.push(`response ${response.status()}: ${response.url()}`);
    }
  });
  page.on('console', (message) => {
    if (message.type() === 'error' && !message.text().startsWith('Failed to load resource:')) {
      errors.push(`console: ${message.text()}`);
    }
  });
  await page.goto(baseUrl, { waitUntil: 'domcontentloaded' });
  if (await page.locator('#authPasswordInput').count()) {
    await page.locator('#authPasswordInput').fill('123456');
    await page.locator('#authSubmitButton').click();
    await page.locator('[data-page="settings"]').waitFor({ state: 'visible' });
  }
  if (page.viewportSize().width <= 1120) {
    await page.locator('#mobileMenuButton').click();
    await page.waitForFunction(() => (
      state.ui.mobileNavigationOpen === true
      && document.body.classList.contains('mobile-navigation-open')
      && !document.body.classList.contains('navigation-gesturing')
    ));
  }
  await page.locator('[data-page="settings"]').click();
  await page.waitForFunction(() => document.querySelector('#updateCurrentVersion')?.textContent === 'v0.2.2');
  await page.waitForFunction(() => document.querySelector('#updateLatestVersion')?.textContent === 'v0.2.3-rc.1');
  return page;
}

async function assertNoHorizontalOverflow(page, label) {
  const dimensions = await page.evaluate(() => ({
    innerWidth: window.innerWidth,
    scrollWidth: document.documentElement.scrollWidth,
  }));
  assert(
    dimensions.scrollWidth <= dimensions.innerWidth,
    `${label} has horizontal overflow: ${JSON.stringify(dimensions)}`,
  );
}

(async () => {
  const browser = await chromium.launch({ headless: true, executablePath });
  const errors = [];
  try {
    const desktop = await browser.newContext({ viewport: { width: 1440, height: 900 } });
    await installDeterministicUpdateRoutes(desktop);
    const page = await openSettings(desktop, errors);
    await assertNoHorizontalOverflow(page, 'desktop settings');
    await page.screenshot({ path: path.join(outputDir, '01-settings-1440x900.png') });

    await page.locator('#updateSelectButton').click();
    await page.locator('[data-update-tag="v0.2.3-rc.1"]').waitFor({ state: 'visible' });
    await page.screenshot({ path: path.join(outputDir, '02-release-modal-1440x900.png') });
    await page.locator('[data-update-tag="v0.2.3-rc.1"]').click();
    await page.locator('#updateConfirmModal').waitFor({ state: 'visible' });
    await page.screenshot({ path: path.join(outputDir, '03-confirm-1440x900.png') });
    await page.keyboard.press('Escape');

    await page.evaluate(() => {
      showUpdateRestartOverlay('0123456789abcdef01234567');
      updateRestartStage({ stage: 'backing_up', status: 'running' });
    });
    await page.screenshot({ path: path.join(outputDir, '04-preparing-overlay-1440x900.png') });
    await page.evaluate(() => document.querySelector('#updateRestartOverlay').classList.add('hidden'));

    await page.evaluate(() => {
      showUpdateRestartOverlay('0123456789abcdef01234567');
      const spinner = document.querySelector('#updateRestartSpinner');
      spinner.className = 'update-restart-spinner error';
      document.querySelector('#updateRestartTitle').textContent = '已自动回滚';
      document.querySelector('#updateRestartMessage').textContent = '目标版本启动失败，已自动回滚到 v0.2.2。';
      const progress = document.querySelector('#updateRestartProgress');
      progress.style.width = '100%';
      progress.style.animation = 'none';
    });
    await page.screenshot({ path: path.join(outputDir, '05-rolled-back-1440x900.png') });
    await page.evaluate(() => document.querySelector('#updateRestartOverlay').classList.add('hidden'));

    await page.evaluate(() => {
      showUpdateRestartOverlay('0123456789abcdef01234567');
      finishUpdatePolling({
        status: 'recovery_required',
        error: '原版本恢复健康检查失败，请查看本机日志并人工恢复。',
      });
    });
    await page.screenshot({ path: path.join(outputDir, '06-recovery-required-1440x900.png') });
    await desktop.close();

    const mobile = await browser.newContext({ viewport: { width: 390, height: 844 } });
    await installDeterministicUpdateRoutes(mobile);
    const mobilePage = await openSettings(mobile, errors);
    await assertNoHorizontalOverflow(mobilePage, 'mobile settings');
    await mobilePage.locator('#versionManagementTitle').scrollIntoViewIfNeeded();
    await mobilePage.screenshot({ path: path.join(outputDir, '07-settings-390x844.png') });
    await mobilePage.locator('#updateSelectButton').click();
    await mobilePage.locator('[data-update-tag="v0.2.3-rc.1"]').waitFor({ state: 'visible' });
    await assertNoHorizontalOverflow(mobilePage, 'mobile release modal');
    await mobilePage.screenshot({ path: path.join(outputDir, '08-release-modal-390x844.png') });
    await mobile.close();

    assert(errors.length === 0, `browser errors: ${errors.join(' | ')}`);
    process.stdout.write(JSON.stringify({ outputDir, screenshots: 8, errors }, null, 2));
  } finally {
    await browser.close();
  }
})().catch((error) => {
  process.stderr.write(`${error.stack || error}\n`);
  process.exitCode = 1;
});
