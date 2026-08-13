const fs = require('fs');
const path = require('path');
const { chromium } = require('playwright');

const baseUrl = process.argv[2] || 'http://127.0.0.1:58732/';
const outputDir = path.resolve(process.argv[3] || 'data/temp/card-order-validation-screenshots');
const executablePath = process.argv[4] || 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe';

fs.mkdirSync(outputDir, { recursive: true });

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

function collectErrors(page, errors) {
  page.on('pageerror', (error) => errors.push(`pageerror: ${error.message}`));
  page.on('console', (message) => {
    if (message.type() === 'error' && !message.text().startsWith('Failed to load resource:')) {
      errors.push(`console: ${message.text()}`);
    }
  });
}

async function login(page) {
  await page.goto(baseUrl, { waitUntil: 'domcontentloaded' });
  if (await page.locator('#authPasswordInput').count()) {
    await page.locator('#authPasswordInput').fill('123456');
    await page.locator('#authLoginForm').evaluate((form) => form.requestSubmit());
  }
  await page.locator('[data-page="network"]').waitFor();
  await page.waitForFunction(() => document.querySelectorAll('#botGrid > [data-card-order-id]').length >= 6);
}

async function apiOrder(page) {
  return page.evaluate(async () => {
    const response = await fetch('/api/settings/card-order');
    return response.json();
  });
}

async function gridOrder(page, selector) {
  return page.locator(`${selector} > [data-card-order-id]`).evaluateAll(
    (cards) => cards.map((card) => card.dataset.cardOrderId),
  );
}

async function cardDragSurfacePoint(card) {
  return card.evaluate((element) => {
    const surface = element.querySelector('[data-card-order-drag-surface]');
    if (!surface) throw new Error('card drag surface is missing');
    const rect = element.getBoundingClientRect();
    for (let y = rect.top + 3; y < rect.bottom - 3; y += 6) {
      for (let x = rect.left + 3; x < rect.right - 3; x += 6) {
        const hit = document.elementFromPoint(x, y);
        if (hit === surface || hit === element) return { x, y };
      }
    }
    throw new Error(`no blank drag surface found for ${element.dataset.cardOrderId}`);
  });
}

async function dragCard(page, gridSelector, fromIndex, toIndex, { release = true } = {}) {
  const cards = page.locator(`${gridSelector} > [data-card-order-id]`);
  const source = cards.nth(fromIndex);
  const target = cards.nth(toIndex);
  const sourcePoint = await cardDragSurfacePoint(source);
  const targetBox = await target.boundingBox();
  assert(sourcePoint && targetBox, `unable to measure ${gridSelector} drag targets`);
  await page.mouse.move(sourcePoint.x, sourcePoint.y);
  await page.mouse.down();
  await page.mouse.move(
    targetBox.x + targetBox.width - 8,
    targetBox.y + targetBox.height / 2,
    { steps: 12 },
  );
  if (release) await page.mouse.up();
}

async function selectCardText(page, gridSelector) {
  const target = page.locator(`${gridSelector} > [data-card-order-id]`).first().locator('code').first();
  const textRect = await target.evaluate((element) => {
    const textNode = Array.from(element.childNodes).find(
      (node) => node.nodeType === Node.TEXT_NODE && String(node.nodeValue || '').trim(),
    );
    if (!textNode) throw new Error('selectable card text node is missing');
    const range = document.createRange();
    range.selectNodeContents(textNode);
    const rect = range.getClientRects()[0];
    if (!rect) throw new Error('selectable card text has no client rect');
    return { left: rect.left, right: rect.right, top: rect.top, bottom: rect.bottom };
  });
  const startX = textRect.left + Math.min(4, Math.max(1, (textRect.right - textRect.left) / 6));
  const endX = Math.min(textRect.right - 1, startX + Math.max(12, (textRect.right - textRect.left) / 2));
  const y = (textRect.top + textRect.bottom) / 2;
  await page.evaluate(() => window.getSelection()?.removeAllRanges());
  await page.mouse.move(startX, y);
  await page.mouse.down();
  await page.mouse.move(endX, y, { steps: 8 });
  await page.mouse.up();
  return page.evaluate(() => window.getSelection()?.toString().trim() || '');
}

async function waitForCardOrderIdle(page) {
  await page.waitForFunction(() => (
    !document.querySelector('.is-card-order-dragging')
    && !document.querySelector('.is-card-order-keyboard-selected')
    && !document.querySelector('[data-card-order-grid][aria-busy="true"]')
  ));
}

async function openPage(page, name) {
  await page.locator(`[data-page="${name}"]`).click();
  await page.waitForFunction((pageName) => window.location.hash === `#${pageName}`, name);
  await page.waitForFunction((pageName) => {
    const pageElement = document.querySelector(`#${pageName}Page`);
    return pageElement && !pageElement.classList.contains('hidden') && pageElement.getAttribute('aria-busy') !== 'true';
  }, name);
}

async function validateDesktop(browser, errors) {
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  let failNextSave = false;
  let putCount = 0;
  await context.route('**/api/settings/card-order', async (route) => {
    if (route.request().method() === 'PUT') {
      putCount += 1;
      if (failNextSave) {
        failNextSave = false;
        await route.fulfill({
          status: 409,
          contentType: 'application/json',
          body: JSON.stringify({ detail: 'validation entity set changed' }),
        });
        return;
      }
    }
    await route.continue();
  });
  const page = await context.newPage();
  collectErrors(page, errors);
  await login(page);

  const initial = await apiOrder(page);
  assert((await gridOrder(page, '#botGrid')).join('|') === initial.bots.join('|'), 'network order did not follow preference');
  assert(await page.locator('[data-card-order-handle], .card-reorder-handle').count() === 0, 'explicit card drag handle is still rendered');
  const beforeTextSelection = await gridOrder(page, '#botGrid');
  const putsBeforeTextSelection = putCount;
  const selectedText = await selectCardText(page, '#botGrid');
  assert(selectedText.length > 0, 'card text could not be selected');
  assert((await gridOrder(page, '#botGrid')).join('|') === beforeTextSelection.join('|'), 'selecting card text reordered cards');
  assert(putCount === putsBeforeTextSelection, 'selecting card text sent a save request');
  await page.evaluate(() => window.getSelection()?.removeAllRanges());

  for (const selector of ['#botGrid input[data-role="toggle"]', '#botGrid button[data-role="edit"]']) {
    await page.locator(selector).first().evaluate((control) => {
      const rect = control.getBoundingClientRect();
      const init = {
        bubbles: true,
        cancelable: true,
        pointerId: 91,
        pointerType: 'mouse',
        isPrimary: true,
        button: 0,
        buttons: 1,
        clientX: rect.left + rect.width / 2,
        clientY: rect.top + rect.height / 2,
      };
      control.dispatchEvent(new PointerEvent('pointerdown', init));
      window.dispatchEvent(new PointerEvent('pointermove', {
        ...init,
        clientX: init.clientX + 30,
        clientY: init.clientY + 30,
      }));
      window.dispatchEvent(new PointerEvent('pointerup', { ...init, buttons: 0 }));
    });
  }
  assert(await page.locator('.is-card-order-dragging').count() === 0, 'card control started a drag');
  assert((await gridOrder(page, '#botGrid')).join('|') === beforeTextSelection.join('|'), 'card control gesture reordered cards');
  assert(putCount === putsBeforeTextSelection, 'card control gesture sent a save request');

  await dragCard(page, '#botGrid', 0, 5, { release: false });
  await page.locator('#botGrid .is-card-order-dragging').waitFor({ state: 'visible' });
  assert(
    await page.evaluate(() => !(window.getSelection()?.toString() || '')),
    'dragging from blank card surface selected text',
  );
  await page.screenshot({ path: path.join(outputDir, '01-network-dragging-1440x900.png') });
  await page.mouse.up();
  await waitForCardOrderIdle(page);
  const afterPointer = await apiOrder(page);
  assert(afterPointer.bots.join('|') !== initial.bots.join('|'), 'pointer drop did not save bot order');
  assert((await gridOrder(page, '#botGrid')).join('|') === afterPointer.bots.join('|'), 'network DOM and server order diverged');
  assert(await page.locator('#botGrid .is-card-order-dragging').count() === 0, 'dragging class did not settle');

  const beforeCancel = await gridOrder(page, '#botGrid');
  const putsBeforeCancel = putCount;
  await dragCard(page, '#botGrid', 0, 5, { release: false });
  await page.locator('#botGrid .is-card-order-dragging').waitFor({ state: 'visible' });
  await page.evaluate(() => {
    window.dispatchEvent(new PointerEvent('pointercancel', {
      bubbles: true,
      pointerId: 1,
      pointerType: 'mouse',
      isPrimary: true,
    }));
  });
  await page.mouse.up();
  await waitForCardOrderIdle(page);
  assert((await gridOrder(page, '#botGrid')).join('|') === beforeCancel.join('|'), 'pointercancel did not restore DOM order');
  assert(putCount === putsBeforeCancel, 'pointercancel sent a save request');

  await openPage(page, 'basic');
  await page.waitForFunction(() => document.querySelectorAll('#basicInfoGrid > [data-card-order-id]').length === 4);
  const beforeKeyboard = (await apiOrder(page)).bots;
  const visibleBefore = await gridOrder(page, '#basicInfoGrid');
  const firstVisibleId = visibleBefore[0];
  const firstCard = page.locator('#basicInfoGrid > [data-card-order-id]').first();
  await firstCard.focus();
  await page.keyboard.press('Space');
  await page.keyboard.press('End');
  await page.screenshot({ path: path.join(outputDir, '03-basic-keyboard-selected-1440x900.png') });
  await page.keyboard.press('Space');
  await waitForCardOrderIdle(page);
  const afterKeyboard = (await apiOrder(page)).bots;
  assert(afterKeyboard.indexOf(firstVisibleId) > beforeKeyboard.indexOf(firstVisibleId), 'keyboard End did not move the visible bot');
  assert(await page.evaluate((id) => (
    document.activeElement?.dataset.cardOrderId === id
    && document.activeElement.parentElement?.id === 'basicInfoGrid'
  ), firstVisibleId), 'keyboard save did not restore focus to the active grid');
  const visibleSet = new Set(visibleBefore);
  beforeKeyboard.forEach((id, index) => {
    if (!visibleSet.has(id)) {
      assert(afterKeyboard[index] === id, `hidden bot ${id} moved out of its global slot`);
    }
  });

  await openPage(page, 'diagnostics');
  await page.waitForFunction(() => document.querySelectorAll('#diagnosticsGrid > [data-card-order-id]').length === 6);
  assert((await gridOrder(page, '#diagnosticsGrid')).join('|') === afterKeyboard.join('|'), 'diagnostics did not share bot order');

  await openPage(page, 'plugins');
  await page.waitForFunction(() => document.querySelectorAll('#pluginGrid > [data-card-order-id]').length >= 2);
  const botOrderBeforePlugin = (await apiOrder(page)).bots;
  const pluginBeforeFailure = await gridOrder(page, '#pluginGrid');
  failNextSave = true;
  await dragCard(page, '#pluginGrid', 0, 1);
  await page.locator('.toast-notification.error').waitFor({ state: 'visible' });
  await waitForCardOrderIdle(page);
  assert((await gridOrder(page, '#pluginGrid')).join('|') === pluginBeforeFailure.join('|'), 'failed plugin save did not restore server order');
  assert(await page.evaluate((id) => (
    document.activeElement?.dataset.cardOrderId === id
    && document.activeElement.parentElement?.id === 'pluginGrid'
  ), pluginBeforeFailure[0]), 'failed plugin save did not restore focus to the active grid');
  assert((await apiOrder(page)).bots.join('|') === botOrderBeforePlugin.join('|'), 'plugin reorder changed bot order');
  await page.screenshot({ path: path.join(outputDir, '04-plugin-save-failure-1440x900.png') });

  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  assert(overflow <= 1, `desktop document overflowed by ${overflow}px`);
  await context.close();
}

async function validateTablet(browser, errors) {
  const context = await browser.newContext({ viewport: { width: 1024, height: 768 } });
  const page = await context.newPage();
  collectErrors(page, errors);
  await login(page);
  const layout = await page.locator('#botGrid > [data-card-order-id]').evaluateAll((cards) => {
    const rects = cards.map((card) => card.getBoundingClientRect());
    const firstTop = rects[0]?.top ?? 0;
    return {
      firstRowCount: rects.filter((rect) => Math.abs(rect.top - firstTop) <= 4).length,
      rowCount: new Set(rects.map((rect) => Math.round(rect.top))).size,
    };
  });
  assert(layout.firstRowCount === 2 && layout.rowCount >= 2, `tablet grid is not two-column: ${JSON.stringify(layout)}`);
  const before = await gridOrder(page, '#botGrid');
  await dragCard(page, '#botGrid', 0, 3);
  await waitForCardOrderIdle(page);
  const after = await gridOrder(page, '#botGrid');
  assert(after.join('|') !== before.join('|'), 'tablet cross-row drag did not reorder cards');
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  assert(overflow <= 1, `tablet document overflowed by ${overflow}px`);
  await page.screenshot({ path: path.join(outputDir, '02-network-two-column-1024x768.png'), fullPage: true });
  await context.close();
}

async function validateMobile(browser, errors) {
  const context = await browser.newContext({
    viewport: { width: 390, height: 844 },
    reducedMotion: 'reduce',
  });
  const page = await context.newPage();
  collectErrors(page, errors);
  await login(page);
  assert(await page.locator('[data-card-order-handle], .card-reorder-handle').count() === 0, 'mobile still renders an explicit drag handle');
  const surfaceContract = await page.locator('#botGrid > [data-card-order-id]').first().evaluate((card) => {
    const surface = card.querySelector('[data-card-order-drag-surface]');
    const cardRect = card.getBoundingClientRect();
    const surfaceRect = surface?.getBoundingClientRect();
    return {
      tabIndex: card.tabIndex,
      touchAction: surface ? getComputedStyle(surface).touchAction : '',
      surfaceSizeDelta: surfaceRect
        ? {
            width: Math.abs(surfaceRect.width - cardRect.width),
            height: Math.abs(surfaceRect.height - cardRect.height),
          }
        : null,
    };
  });
  assert(surfaceContract.tabIndex === 0, `mobile card is not keyboard focusable: ${JSON.stringify(surfaceContract)}`);
  assert(surfaceContract.touchAction === 'none', `mobile blank drag surface does not own direct manipulation: ${JSON.stringify(surfaceContract)}`);
  assert(
    surfaceContract.surfaceSizeDelta
      && surfaceContract.surfaceSizeDelta.width <= 3
      && surfaceContract.surfaceSizeDelta.height <= 3,
    `mobile blank drag surface does not cover the card interior: ${JSON.stringify(surfaceContract)}`,
  );
  const before = await gridOrder(page, '#botGrid');
  await dragCard(page, '#botGrid', 0, 2);
  await waitForCardOrderIdle(page);
  const after = await gridOrder(page, '#botGrid');
  assert(after.join('|') !== before.join('|'), 'reduced-motion mobile drag did not reorder cards');
  const presentation = await page.locator('#botGrid > [data-card-order-id]').evaluateAll((cards) => ({
    transforms: cards.map((card) => card.style.transform),
    runningAnimations: cards.flatMap((card) => card.getAnimations()).filter((animation) => animation.playState === 'running').length,
  }));
  assert(presentation.transforms.every((value) => value === ''), `inline transforms remained: ${JSON.stringify(presentation.transforms)}`);
  assert(presentation.runningAnimations === 0, 'reduced-motion left card animations running');
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  assert(overflow <= 1, `mobile document overflowed by ${overflow}px`);
  await page.screenshot({ path: path.join(outputDir, '05-network-reduced-motion-390x844.png'), fullPage: true });
  await context.close();
}

(async () => {
  const errors = [];
  const browser = await chromium.launch({ executablePath, headless: true });
  try {
    await validateDesktop(browser, errors);
    await validateTablet(browser, errors);
    await validateMobile(browser, errors);
    assert(errors.length === 0, `browser errors:\n${errors.join('\n')}`);
    process.stdout.write(`${JSON.stringify({ ok: true, outputDir }, null, 2)}\n`);
  } finally {
    await browser.close();
  }
})().catch((error) => {
  process.stderr.write(`${error.stack || error}\n`);
  process.exitCode = 1;
});
