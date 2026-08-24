const fs = require('fs');
const path = require('path');
const { chromium } = require('playwright');

const baseUrl = process.argv[2] || 'http://127.0.0.1:58731/';
const outputDir = path.resolve(process.argv[3] || 'data/temp/ui-validation-screenshots');
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

async function installDeterministicRoutes(context) {
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
        notes: '用于验证预发布标签、长说明文本和升级操作的本地模拟候选，不对应真实 Release。',
        action: 'update',
        asset: { name: 'RocketCatShell-v0.2.3-rc.1.zip', size: 1048576, digest: `sha256:${'a'.repeat(64)}` },
      },
      {
        tag_name: 'v0.2.2',
        version: 'v0.2.2',
        name: 'Current version',
        published_at: '2026-08-08T00:00:00Z',
        prerelease: false,
        notes: '当前版本同版本重装入口。',
        action: 'reinstall',
        asset: { name: 'RocketCatShell-v0.2.2.zip', size: 1048576, digest: `sha256:${'b'.repeat(64)}` },
      },
    ],
  })));
  await context.route('**/api/bots/ui_validation_bot/user-mappings*', async (route) => {
    if (route.request().method() !== 'GET') {
      await route.continue();
      return;
    }
    await route.fulfill(jsonResponse({
      bot_name: '用于验证长名称与响应式布局的 Rocket.Chat Bot',
      ready: true,
      algorithm: 'sha256-linear-v1',
      total: 2,
      offset: 0,
      limit: 50,
      items: [
        {
          user_id: 'very-long-rocketchat-user-identity-for-responsive-validation',
          username: 'rocketcat-ui-user',
          nickname: '移动端长昵称验证用户',
          onebot_id: 12345678901,
          primary_onebot_id: 12345678901,
          probe_offset: 0,
          revision: 1,
          manual_override: false,
          synthetic: false,
          is_bot: false,
          conflict_role: 'incumbent',
        },
        {
          user_id: 'displaced-user',
          username: 'conflict-user',
          nickname: '冲突偏移用户',
          onebot_id: 12345678902,
          primary_onebot_id: 12345678901,
          probe_offset: 1,
          revision: 2,
          manual_override: true,
          synthetic: false,
          is_bot: false,
          conflict_role: 'displaced',
        },
      ],
    }));
  });
}

function collectBrowserDiagnostics(page, errors) {
  page.on('pageerror', (error) => errors.push(`pageerror: ${error.message}`));
  page.on('console', (message) => {
    if (message.type() === 'error' && !message.text().startsWith('Failed to load resource:')) {
      errors.push(`console: ${message.text()}`);
    }
  });
  page.on('response', (response) => {
    if (response.status() >= 400 && !response.url().endsWith('/favicon.ico')) {
      errors.push(`response ${response.status()}: ${response.url()}`);
    }
  });
}

async function login(page, screenshotName = '') {
  await page.goto(baseUrl, { waitUntil: 'domcontentloaded' });
  const faviconHref = await page.locator('link[rel="icon"]').getAttribute('href');
  assert(faviconHref?.startsWith('/static/logo.png'), `unexpected favicon: ${faviconHref}`);
  if (await page.locator('#authPasswordInput').count()) {
    await page.waitForFunction(() => document.querySelector('.auth-brand-avatar img')?.naturalWidth > 0);
    const loginBrandSize = await page.locator('.auth-brand-avatar').evaluate((element) => {
      const rect = element.getBoundingClientRect();
      return { width: rect.width, height: rect.height };
    });
    const expectedLoginBrandSize = page.viewportSize().width <= 720 ? 52 : 56;
    assert(
      loginBrandSize.width === expectedLoginBrandSize && loginBrandSize.height === expectedLoginBrandSize,
      `unexpected login brand size: ${JSON.stringify(loginBrandSize)}`,
    );
  }
  if (screenshotName && await page.locator('#authPasswordInput').count()) {
    await page.screenshot({ path: path.join(outputDir, screenshotName) });
  }
  if (await page.locator('#authPasswordInput').count()) {
    await page.locator('#authPasswordInput').fill('123456');
    await page.locator('#authLoginForm').evaluate((form) => form.requestSubmit());
  }
  await page.locator('[data-page="network"]').waitFor({ state: 'attached' });
  await page.waitForFunction(() => document.querySelector('#networkPage')?.getAttribute('aria-busy') !== 'true');
  await page.waitForFunction(() => (
    document.querySelector('#bridgeStatus')?.textContent !== '-'
    && /\d+\s*个\s*Bot/.test(document.querySelector('#botListSummary')?.textContent || '')
  ));
}

async function assertNoHorizontalOverflow(page, label) {
  const dimensions = await page.evaluate(() => ({
    innerWidth: window.innerWidth,
    bodyWidth: document.body.scrollWidth,
    documentWidth: document.documentElement.scrollWidth,
  }));
  assert(
    dimensions.bodyWidth <= dimensions.innerWidth && dimensions.documentWidth <= dimensions.innerWidth,
    `${label} has horizontal overflow: ${JSON.stringify(dimensions)}`,
  );
}

async function waitForMobileDrawer(page, open) {
  await page.waitForFunction((expected) => {
    const sidebar = document.querySelector('#appSidebar');
    if (!sidebar || state.ui.mobileNavigationOpen !== expected) return false;
    const matrix = new DOMMatrixReadOnly(getComputedStyle(sidebar).transform);
    const target = expected ? 0 : -sidebar.getBoundingClientRect().width;
    return Math.abs(matrix.m41 - target) < 1 && !document.body.classList.contains('navigation-gesturing');
  }, open);
}

async function assertContrast(page, foregroundSelector, backgroundSelector, minimum, label) {
  const ratio = await page.evaluate(({ foregroundSelector: foreground, backgroundSelector: background }) => {
    const parse = (value) => (value.match(/[\d.]+/g) || []).slice(0, 3).map(Number);
    const luminance = (value) => {
      const channels = parse(value).map((channel) => {
        const normalized = channel / 255;
        return normalized <= 0.04045
          ? normalized / 12.92
          : ((normalized + 0.055) / 1.055) ** 2.4;
      });
      return (0.2126 * channels[0]) + (0.7152 * channels[1]) + (0.0722 * channels[2]);
    };
    const foregroundColor = getComputedStyle(document.querySelector(foreground)).color;
    const backgroundColor = getComputedStyle(document.querySelector(background)).backgroundColor;
    const first = luminance(foregroundColor);
    const second = luminance(backgroundColor);
    return (Math.max(first, second) + 0.05) / (Math.min(first, second) + 0.05);
  }, { foregroundSelector, backgroundSelector });
  assert(ratio >= minimum, `${label} contrast ${ratio.toFixed(2)} is below ${minimum}`);
}

async function navigate(page, pageName, mobile = false) {
  if (mobile) {
    await page.locator('#mobileMenuButton').click();
    await waitForMobileDrawer(page, true);
  }
  await page.locator(`[data-page="${pageName}"]`).click();
  await page.waitForFunction(
    (name) => window.location.hash === `#${name}` && !document.querySelector(`#${name}Page`)?.classList.contains('hidden'),
    pageName,
  );
  await page.waitForFunction(
    (name) => document.querySelector(`#${name}Page`)?.getAttribute('aria-busy') !== 'true',
    pageName,
  );
  if (mobile) {
    await waitForMobileDrawer(page, false);
  }
  await assertNoHorizontalOverflow(page, `${page.viewportSize().width}px ${pageName}`);
}

async function screenshot(page, name) {
  await page.screenshot({ path: path.join(outputDir, name), animations: 'disabled' });
}

async function validateDesktop(browser, errors) {
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  await installDeterministicRoutes(context);
  const page = await context.newPage();
  collectBrowserDiagnostics(page, errors);
  await login(page, '01-login-1440x900.png');

  const tokens = await page.evaluate(() => {
    const style = getComputedStyle(document.documentElement);
    return {
      accent: style.getPropertyValue('--accent').trim(),
      accentText: style.getPropertyValue('--accent-text').trim(),
      fast: style.getPropertyValue('--motion-fast').trim(),
      standard: style.getPropertyValue('--motion-standard').trim(),
      drawer: style.getPropertyValue('--motion-drawer').trim(),
    };
  });
  assert(JSON.stringify(tokens) === JSON.stringify({
    accent: '#eb4f8c', accentText: '#b91c5c', fast: '120ms', standard: '180ms', drawer: '220ms',
  }), `semantic token mismatch: ${JSON.stringify(tokens)}`);

  const groups = await page.locator('.nav-group-label').allTextContents();
  assert(groups.join('|') === '连接与状态|管理工具|系统', `unexpected nav groups: ${groups.join('|')}`);
  const sidebarBrand = await page.locator('.brand-avatar-sidebar').evaluate((element) => {
    const rect = element.getBoundingClientRect();
    const image = element.querySelector('img');
    return { width: rect.width, height: rect.height, loaded: image.naturalWidth > 0 };
  });
  assert(
    sidebarBrand.width === 44 && sidebarBrand.height === 44 && sidebarBrand.loaded,
    `unexpected sidebar brand: ${JSON.stringify(sidebarBrand)}`,
  );
  await assertNoHorizontalOverflow(page, 'desktop network');
  await screenshot(page, '02-network-1440x900.png');

  await page.locator('#createButton').click();
  await page.locator('#createTransportMenu [role="menuitem"]').first().click();
  assert(await page.locator('#botModal').evaluate((dialog) => dialog.open), 'bot dialog did not open natively');
  await page.locator('#botForm input[name="name"]').fill('未保存内容');
  await page.keyboard.press('Escape');
  await page.locator('#confirmModal').waitFor({ state: 'visible' });
  assert(await page.locator('#botModal').evaluate((dialog) => dialog.open), 'dirty bot dialog closed before confirmation');
  await screenshot(page, '03-dirty-confirm-1440x900.png');
  await page.locator('#confirmModalCancelButton').click();
  await page.locator('#confirmModal').waitFor({ state: 'hidden' });
  await page.keyboard.press('Escape');
  await page.locator('#confirmModalSubmitButton').click();
  await page.locator('#botModal').waitFor({ state: 'hidden' });

  const pages = ['basic', 'diagnostics', 'logs', 'plugins', 'files', 'terminal', 'settings'];
  let index = 4;
  for (const pageName of pages) {
    await navigate(page, pageName);
    await screenshot(page, `${String(index).padStart(2, '0')}-${pageName}-1440x900.png`);
    index += 1;
  }

  await navigate(page, 'logs');
  if (await page.locator('.log-entry-line').count() === 0) {
    await page.evaluate(() => {
      state.logs.items = [{
        id: 1,
        timestamp: '2026-08-11 20:00:00.000',
        level: 'INFO',
        is_perf: false,
        message: '[RocketCatShell] deterministic UI contrast fixture',
        line: '[2026-08-11 20:00:00.000] [INFO] [RocketCatShell] deterministic UI contrast fixture',
      }];
      state.logs.lastId = 1;
      renderLogs();
    });
  }
  await assertContrast(page, '.log-entry-line', '#logConsole', 4.5, 'log text');

  await navigate(page, 'diagnostics');
  await page.waitForTimeout(220);
  const diagnosticTransition = await page.locator('.diagnostics-meter-progress').first().evaluate(
    (element) => getComputedStyle(element).transitionDuration,
  );
  assert(diagnosticTransition === '0s', `diagnostic meter kept animating: ${diagnosticTransition}`);

  await navigate(page, 'plugins');
  const fixtureCard = page.locator('.plugin-card').filter({ hasText: 'Dashboard Fixture' });
  await fixtureCard.waitFor({ state: 'visible' });
  const pluginControlAxisDelta = await fixtureCard.evaluate((card) => {
    const logoRect = card.querySelector('.plugin-logo-shell').getBoundingClientRect();
    const switchRect = card.querySelector('.compact-switch i').getBoundingClientRect();
    return Math.abs(
      (logoRect.top + logoRect.height / 2) - (switchRect.top + switchRect.height / 2),
    );
  });
  assert(pluginControlAxisDelta <= 0.5, `plugin logo/switch axis mismatch: ${pluginControlAxisDelta}px`);
  await fixtureCard.locator('[data-plugin-role="dashboard"]').click();
  await page.locator('#pluginDashboardLoading').waitFor({ state: 'hidden' });
  await page.locator('#pluginDashboardFrame').contentFrame().locator('text=Dashboard Bridge 已就绪').waitFor();
  const dashboardContext = await page.locator('#pluginDashboardFrame').contentFrame().locator('#context').textContent();
  assert(dashboardContext.includes('RocketCatShell · light'), `dashboard theme/context mismatch: ${dashboardContext}`);
  await screenshot(page, '11-plugin-dashboard-1440x900.png');
  await page.locator('#pluginDashboardCloseButton').click();

  await navigate(page, 'files');
  const fileTargetSize = await page.locator('#fileUpButton').evaluate((button) => {
    const rect = button.getBoundingClientRect();
    return { width: rect.width, height: rect.height };
  });
  assert(fileTargetSize.width >= 44 && fileTargetSize.height >= 44, `file target too small: ${JSON.stringify(fileTargetSize)}`);
  await page.locator('#fileCreateButton').click();
  await screenshot(page, '12-file-create-dialog-1440x900.png');
  await page.keyboard.press('Escape');
  await page.locator('#fileTableBody [data-file-action="select"]').first().check();
  await page.locator('#fileMoveSelectedButton').click();
  await page.locator('#fileMoveTree [role="treeitem"]').first().focus();
  await page.keyboard.press('End');
  await screenshot(page, '13-file-tree-dialog-1440x900.png');
  await page.locator('#fileMoveCancelButton').click();

  await navigate(page, 'terminal');
  while (await page.locator('#terminalTabs [data-terminal-close]').count()) {
    const before = await page.locator('#terminalTabs [data-terminal-close]').count();
    await page.locator('#terminalTabs [data-terminal-close]').first().click();
    await page.waitForFunction(
      (count) => document.querySelectorAll('#terminalTabs [data-terminal-close]').length < count,
      before,
    );
  }
  await page.locator('#terminalCreateButton').click();
  await page.locator('#terminalCreateButton').click();
  await page.locator('#terminalTabs [role="tab"]').nth(1).waitFor();
  await page.waitForFunction(() => (
    state.terminal.sockets.size === 2
    && Array.from(state.terminal.sockets.values()).every((socket) => socket.readyState === WebSocket.OPEN)
  ));
  assert(await page.locator('.toast-notification.error').count() === 0, 'terminal emitted an error notification');
  const beforeOrder = await page.locator('#terminalTabs [role="tab"]').allTextContents();
  await page.locator('#terminalTabs [role="tab"]').nth(1).focus();
  await page.keyboard.press('Alt+Shift+ArrowLeft');
  const afterOrder = await page.locator('#terminalTabs [role="tab"]').allTextContents();
  assert(beforeOrder.join('|') !== afterOrder.join('|'), 'keyboard terminal reorder did not change order');
  await screenshot(page, '14-terminal-tabs-1440x900.png');

  await navigate(page, 'settings');
  await page.locator('#updateSelectButton').click();
  await page.locator('[data-update-tag="v0.2.3-rc.1"]').waitFor({ state: 'visible' });
  await screenshot(page, '15-update-releases-1440x900.png');
  await page.locator('[data-update-tag="v0.2.3-rc.1"]').click();
  await page.locator('#updateConfirmModal').waitFor({ state: 'visible' });
  await screenshot(page, '16-update-confirm-1440x900.png');
  await page.keyboard.press('Escape');
  await page.locator('#updateReleaseCloseButton').click();

  await page.evaluate(() => {
    showUpdateRestartOverlay('0123456789abcdef01234567');
    updateRestartStage({ stage: 'backing_up', status: 'running' });
  });
  await screenshot(page, '17-update-preparing-1440x900.png');
  await page.evaluate(() => {
    const dialog = document.querySelector('#updateRestartOverlay');
    dialog.dataset.blocking = 'false';
    closeDialog(dialog, { restoreFocus: false });
  });
  await page.locator('#updateRestartOverlay').waitFor({ state: 'hidden' });
  await page.evaluate(() => {
    showUpdateRestartOverlay('0123456789abcdef01234567');
    finishUpdatePolling({
      status: 'recovery_required',
      error: '原版本恢复健康检查失败，请查看本机日志并人工恢复。',
    });
  });
  await screenshot(page, '18-update-recovery-required-1440x900.png');
  const retryFocused = await page.locator('#updateRestartRetryButton').evaluate((button) => document.activeElement === button);
  assert(retryFocused, 'recovery action did not receive focus');
  await page.keyboard.press('Escape');

  await navigate(page, 'basic');
  await navigate(page, 'diagnostics');
  await page.goBack();
  await page.waitForFunction(() => window.location.hash === '#basic' && !document.querySelector('#basicPage').classList.contains('hidden'));

  await context.close();
}

async function validateTablet(browser, errors) {
  const context = await browser.newContext({ viewport: { width: 1024, height: 768 } });
  await installDeterministicRoutes(context);
  const page = await context.newPage();
  collectBrowserDiagnostics(page, errors);
  await login(page);
  assert(await page.locator('#appSidebar').evaluate((sidebar) => sidebar.inert), 'closed tablet drawer is not inert');
  await page.locator('#mobileMenuButton').click();
  await waitForMobileDrawer(page, true);
  assert(!await page.locator('#appSidebar').evaluate((sidebar) => sidebar.inert), 'open tablet drawer stayed inert');
  await screenshot(page, '19-drawer-1024x768.png');
  await page.keyboard.press('Escape');
  await waitForMobileDrawer(page, false);
  const menuFocused = await page.locator('#mobileMenuButton').evaluate((button) => document.activeElement === button);
  assert(menuFocused, 'drawer did not restore focus to menu button');
  await navigate(page, 'settings', true);
  await screenshot(page, '20-settings-1024x768.png');
  await context.close();
}

async function validateMobile(browser, errors) {
  const context = await browser.newContext({ viewport: { width: 390, height: 844 } });
  await installDeterministicRoutes(context);
  const page = await context.newPage();
  collectBrowserDiagnostics(page, errors);
  await login(page, '21-login-390x844.png');

  const mobileBrand = await page.locator('.brand-avatar-mobile').evaluate((element) => {
    const rect = element.getBoundingClientRect();
    const image = element.querySelector('img');
    return { width: rect.width, height: rect.height, loaded: image.naturalWidth > 0 };
  });
  assert(
    mobileBrand.width === 34 && mobileBrand.height === 34 && mobileBrand.loaded,
    `unexpected mobile brand: ${JSON.stringify(mobileBrand)}`,
  );

  const pages = ['network', 'basic', 'diagnostics', 'logs', 'plugins', 'files', 'terminal', 'settings'];
  let index = 22;
  for (const pageName of pages) {
    if (pageName !== 'network') {
      await navigate(page, pageName, true);
    }
    await assertNoHorizontalOverflow(page, `mobile ${pageName}`);
    await screenshot(page, `${String(index).padStart(2, '0')}-${pageName}-390x844.png`);
    index += 1;
  }

  await navigate(page, 'files', true);
  const fileRows = page.locator('#fileTableBody tr');
  assert(await fileRows.count() > 0, 'mobile file table has no rows');
  const firstFileRowWidth = await fileRows.first().evaluate((row) => row.getBoundingClientRect().width);
  assert(firstFileRowWidth <= 358, `mobile file card too wide: ${firstFileRowWidth}`);

  await navigate(page, 'network', true);
  await page.locator('[data-role="edit"]').first().click();
  await page.locator('#botModal').waitFor({ state: 'visible' });
  await page.evaluate(() => {
    const button = document.querySelector('#openUserMappingsButton');
    button.disabled = false;
    button.dataset.botId = 'ui_validation_bot';
  });
  await page.locator('#openUserMappingsButton').click();
  await page.locator('#userMappingsTableBody tr').first().waitFor();
  await assertNoHorizontalOverflow(page, 'mobile mappings dialog');
  await screenshot(page, '30-user-mappings-390x844.png');
  await page.locator('#userMappingsDoneButton').click();
  await page.locator('#cancelButton').click();

  await context.close();
}

async function validateDesktopMappings(browser, errors) {
  const context = await browser.newContext({ viewport: { width: 1200, height: 900 } });
  await installDeterministicRoutes(context);
  const page = await context.newPage();
  collectBrowserDiagnostics(page, errors);
  await login(page);
  await page.locator('[data-role="edit"]').first().click();
  await page.locator('#botModal').waitFor({ state: 'visible' });
  await page.evaluate(() => {
    const button = document.querySelector('#openUserMappingsButton');
    button.disabled = false;
    button.dataset.botId = 'ui_validation_bot';
  });
  await page.locator('#openUserMappingsButton').click();
  await page.locator('#userMappingsTableBody tr').first().waitFor();
  await page.waitForTimeout(240);
  const layout = await page.evaluate(() => {
    const panel = document.querySelector('#userMappingsModal .user-mappings-panel');
    const shell = document.querySelector('.user-mappings-table-shell');
    const table = document.querySelector('.user-mappings-table');
    const action = document.querySelector('#userMappingsTableBody tr:first-child .identity-delete-button');
    const panelRect = panel.getBoundingClientRect();
    const shellRect = shell.getBoundingClientRect();
    const tableRect = table.getBoundingClientRect();
    const actionRect = action.getBoundingClientRect();
    return {
      panelWidth: panelRect.width,
      shellClientWidth: shell.clientWidth,
      shellScrollWidth: shell.scrollWidth,
      overflowX: getComputedStyle(shell).overflowX,
      tableLeft: tableRect.left,
      tableRight: tableRect.right,
      shellLeft: shellRect.left,
      shellRight: shellRect.right,
      actionRight: actionRect.right,
    };
  });
  assert(layout.panelWidth >= 1130, `mapping dialog did not expand to the viewport: ${JSON.stringify(layout)}`);
  assert(layout.overflowX === 'hidden', `mapping table still exposes horizontal scrolling: ${JSON.stringify(layout)}`);
  assert(layout.shellScrollWidth <= layout.shellClientWidth + 1, `mapping table still overflows horizontally: ${JSON.stringify(layout)}`);
  assert(layout.tableLeft >= layout.shellLeft - 1 && layout.tableRight <= layout.shellRight + 1, `mapping table is clipped: ${JSON.stringify(layout)}`);
  assert(layout.actionRight <= layout.shellRight + 1, `mapping actions are outside the visible row: ${JSON.stringify(layout)}`);
  await assertNoHorizontalOverflow(page, '1200px mappings dialog');
  await screenshot(page, '31-user-mappings-1200x900.png');
  await context.close();
}

async function validateReducedMotion(browser, errors) {
  const context = await browser.newContext({
    viewport: { width: 390, height: 844 },
    reducedMotion: 'reduce',
  });
  await installDeterministicRoutes(context);
  const page = await context.newPage();
  collectBrowserDiagnostics(page, errors);
  await login(page);
  await page.locator('#mobileMenuButton').click();
  const drawerDuration = await page.locator('#appSidebar').evaluate((sidebar) => getComputedStyle(sidebar).transitionDuration);
  assert(drawerDuration === '0s', `reduced-motion drawer still transitions: ${drawerDuration}`);
  await page.keyboard.press('Escape');
  await page.evaluate(() => showUpdateRestartOverlay('0123456789abcdef01234567'));
  const progressAnimation = await page.locator('#updateRestartProgress').evaluate((progress) => getComputedStyle(progress).animationName);
  assert(progressAnimation === 'none', `reduced-motion progress still animates: ${progressAnimation}`);
  await screenshot(page, '32-reduced-motion-390x844.png');
  await context.close();
}

(async () => {
  const browser = await chromium.launch({ headless: true, executablePath });
  const errors = [];
  try {
    await validateDesktop(browser, errors);
    await validateTablet(browser, errors);
    await validateMobile(browser, errors);
    await validateDesktopMappings(browser, errors);
    await validateReducedMotion(browser, errors);
    assert(errors.length === 0, `browser errors: ${errors.join(' | ')}`);
    process.stdout.write(JSON.stringify({ outputDir, screenshots: 32, errors }, null, 2));
  } finally {
    await browser.close();
  }
})().catch((error) => {
  process.stderr.write(`${error.stack || error}\n`);
  process.exitCode = 1;
});
