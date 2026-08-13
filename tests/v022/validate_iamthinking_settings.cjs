const fs = require('fs');
const path = require('path');
const { chromium } = require('playwright');

const baseUrl = process.argv[2] || 'http://127.0.0.1:58731/';
const outputDir = path.resolve(process.argv[3] || 'data/temp/iamthinking-settings-screenshots');
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
  page.on('response', (response) => {
    if (response.status() >= 400 && !response.url().endsWith('/favicon.ico')) {
      errors.push(`response ${response.status()}: ${response.url()}`);
    }
  });
}

async function login(page) {
  await page.goto(baseUrl, { waitUntil: 'domcontentloaded' });
  if (await page.locator('#authPasswordInput').count()) {
    await page.locator('#authPasswordInput').fill('123456');
    await page.locator('#authLoginForm').evaluate((form) => form.requestSubmit());
  }
  await page.locator('[data-page="plugins"]').waitFor({ state: 'attached' });
  await page.waitForFunction(() => document.querySelector('#networkPage')?.getAttribute('aria-busy') !== 'true');
}

async function openSettings(page, mobile = false) {
  if (mobile) {
    await page.locator('#mobileMenuButton').click();
    await page.waitForFunction(() => state.ui.mobileNavigationOpen === true);
  }
  await page.locator('[data-page="plugins"]').click();
  await page.waitForFunction(() => window.location.hash === '#plugins');
  const card = page.locator('.plugin-card').filter({ hasText: 'I Am Thinking 适配器' });
  await card.waitFor({ state: 'visible' });
  await card.locator('[data-plugin-role="settings"]').click();
  await page.locator('#pluginModal').waitFor({ state: 'visible' });
  await page.waitForTimeout(220);
}

async function assertNoOverflow(page, label) {
  const result = await page.evaluate(() => ({
    viewport: window.innerWidth,
    document: document.documentElement.scrollWidth,
    panelClient: document.querySelector('#pluginModal .plugin-modal-panel').clientWidth,
    panelScroll: document.querySelector('#pluginModal .plugin-modal-panel').scrollWidth,
  }));
  assert(result.document <= result.viewport, `${label} document overflow: ${JSON.stringify(result)}`);
  assert(result.panelScroll <= result.panelClient + 1, `${label} panel overflow: ${JSON.stringify(result)}`);
}

async function validateDesktop(browser, errors) {
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await context.newPage();
  watchBrowser(page, errors);
  await login(page);
  await openSettings(page);

  const cards = page.locator('.plugin-state-mapping-card');
  assert(await cards.count() === 4, 'expected four state mapping cards');
  assert(await page.locator('#pluginSettingsForm .plugin-json-field').count() === 0, 'adapter still renders JSON textareas');
  const layout = await cards.evaluateAll((items) => items.map((item) => ({
    width: item.getBoundingClientRect().width,
    listFields: item.querySelectorAll('[data-plugin-integer-list="true"]').length,
    shortcodes: item.querySelectorAll('.plugin-shortcode-field input').length,
  })));
  assert(layout.every((item) => item.width > 400), `desktop cards are too narrow: ${JSON.stringify(layout)}`);
  assert(layout.every((item) => item.listFields === 1 && item.shortcodes === 1), 'state pair is not grouped in one card');
  const editTarget = await cards.first().locator('[data-plugin-list-edit]').evaluate((button) => button.getBoundingClientRect().height);
  assert(editTarget >= 44, `list edit target is too small: ${editTarget}`);
  await assertNoOverflow(page, 'desktop settings');
  await page.screenshot({ path: path.join(outputDir, '01-settings-1440x900.png'), animations: 'disabled' });

  await cards.first().locator('[data-plugin-list-edit]').click();
  await page.locator('#pluginListEditorModal').waitFor({ state: 'visible' });
  assert(await page.locator('#pluginListEditorItems [role="listitem"]').count() === 1, 'default thinking ID was not rendered');
  await page.locator('#pluginListEditorInput').fill('999');
  await page.locator('#pluginListEditorAddButton').click();
  assert(await page.locator('#pluginListEditorItems [role="listitem"]').count() === 2, 'new ID was not added');
  await page.screenshot({ path: path.join(outputDir, '02-list-editor-1440x900.png'), animations: 'disabled' });
  const removeTarget = await page.locator('[data-plugin-list-remove="66"]').evaluate((button) => button.getBoundingClientRect().height);
  assert(removeTarget >= 44, `list remove target is too small: ${removeTarget}`);
  await page.locator('[data-plugin-list-remove="66"]').click();
  assert(await page.locator('#pluginListEditorItems [role="listitem"]').count() === 1, 'ID was not removed');
  await page.locator('#pluginListEditorConfirmButton').click();
  await page.locator('#pluginListEditorModal').waitFor({ state: 'hidden' });
  assert(await cards.first().locator('.plugin-id-chip').filter({ hasText: '999' }).count() === 1, 'confirmed ID did not update the state card');
  const payload = await page.evaluate(() => collectPluginSettingsPayload());
  assert(JSON.stringify(payload.thinking_emoji_ids) === '[999]', `list payload is incorrect: ${JSON.stringify(payload.thinking_emoji_ids)}`);
  assert(payload.llm_thinking_reaction === ':heart:', `shortcode payload is incorrect: ${payload.llm_thinking_reaction}`);

  await cards.nth(1).locator('[data-plugin-list-edit]').click();
  await page.locator('#pluginListEditorInput').fill('999');
  await page.locator('#pluginListEditorAddButton').click();
  const conflict = await page.locator('#pluginListEditorStatus').textContent();
  assert(conflict.includes('不能跨状态重复'), `cross-state conflict was not rejected: ${conflict}`);
  await page.locator('#pluginListEditorCancelButton').click();
  await page.locator('#pluginCancelButton').click();
  await page.locator('#confirmModalSubmitButton').click();
  await context.close();
}

async function validateMobile(browser, errors) {
  const context = await browser.newContext({ viewport: { width: 390, height: 844 } });
  const page = await context.newPage();
  watchBrowser(page, errors);
  await login(page);
  await openSettings(page, true);
  await assertNoOverflow(page, 'mobile settings');
  const widths = await page.locator('.plugin-state-mapping-card').evaluateAll((items) => items.map((item) => item.getBoundingClientRect().width));
  assert(widths.every((width) => width <= 350), `mobile cards exceed the sheet: ${widths.join(',')}`);
  await page.screenshot({ path: path.join(outputDir, '03-settings-390x844.png'), animations: 'disabled' });
  await page.locator('.plugin-state-mapping-card').first().locator('[data-plugin-list-edit]').click();
  await page.locator('#pluginListEditorModal').waitFor({ state: 'visible' });
  await assertNoOverflow(page, 'mobile list editor');
  await page.screenshot({ path: path.join(outputDir, '04-list-editor-390x844.png'), animations: 'disabled' });
  await context.close();
}

(async () => {
  const browser = await chromium.launch({ headless: true, executablePath });
  const errors = [];
  try {
    await validateDesktop(browser, errors);
    await validateMobile(browser, errors);
    assert(errors.length === 0, `browser errors: ${errors.join(' | ')}`);
    process.stdout.write(JSON.stringify({ outputDir, screenshots: 4, errors }, null, 2));
  } finally {
    await browser.close();
  }
})().catch((error) => {
  process.stderr.write(`${error.stack || error}\n`);
  process.exitCode = 1;
});
