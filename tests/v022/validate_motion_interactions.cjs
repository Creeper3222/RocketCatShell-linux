const fs = require('fs');
const path = require('path');
const { chromium } = require('playwright');

const baseUrl = process.argv[2] || 'http://127.0.0.1:58731/';
const outputDir = path.resolve(process.argv[3] || 'data/temp/motion-validation-screenshots');
const executablePath = process.argv[4] || 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe';

fs.mkdirSync(outputDir, { recursive: true });

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

function checkpoint(message) {
  process.stderr.write(`[motion-validation] ${message}\n`);
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
  await page.waitForFunction(() => typeof setInputModality === 'function' && typeof state === 'object');
  await page.waitForFunction(() => document.querySelector('#networkPage')?.getAttribute('aria-busy') !== 'true');
}

async function waitForDrawer(page, open) {
  await page.waitForFunction((expected) => {
    const sidebar = document.querySelector('#appSidebar');
    if (!sidebar || state.ui.mobileNavigationOpen !== expected) return false;
    const matrix = new DOMMatrixReadOnly(getComputedStyle(sidebar).transform);
    const target = expected ? 0 : -sidebar.getBoundingClientRect().width;
    return Math.abs(matrix.m41 - target) < 1 && !document.body.classList.contains('navigation-gesturing');
  }, open);
}

async function openMobilePage(page, pageName) {
  if (!await page.locator('body').evaluate((body) => body.classList.contains('mobile-navigation-open'))) {
    await page.locator('#mobileMenuButton').click();
    await waitForDrawer(page, true);
  }
  await page.locator(`[data-page="${pageName}"]`).click();
  await page.waitForFunction(
    (name) => window.location.hash === `#${name}` && !document.querySelector(`#${name}Page`)?.classList.contains('hidden'),
    pageName,
  );
  await waitForDrawer(page, false);
}

async function dragMouse(page, start, points, { release = true } = {}) {
  await page.mouse.move(start.x, start.y);
  await page.mouse.down();
  for (const point of points) {
    await page.mouse.move(point.x, point.y, { steps: point.steps || 1 });
    if (point.wait) await page.waitForTimeout(point.wait);
  }
  if (release) await page.mouse.up();
}

async function validateDesktop(browser, errors) {
  checkpoint('desktop start');
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  let failTerminalOrder = false;
  await context.route('**/api/terminal/order', async (route) => {
    if (failTerminalOrder) {
      failTerminalOrder = false;
      await route.fulfill({
        status: 500,
        contentType: 'application/json',
        body: JSON.stringify({ detail: 'motion validation forced order failure' }),
      });
      return;
    }
    await route.continue();
  });
  const page = await context.newPage();
  collectErrors(page, errors);
  await login(page);

  const modality = await page.evaluate(() => {
    setInputModality('keyboard');
    const keyboard = resolveMotion('auto');
    setInputModality('pointer');
    const pointer = resolveMotion('auto');
    return { keyboard, pointer, attribute: document.body.dataset.inputModality };
  });
  assert(modality.keyboard === 'instant' && modality.pointer === 'standard', `modality mismatch: ${JSON.stringify(modality)}`);

  await page.locator('#createButton').click();
  await page.locator('#createTransportMenu [role="menuitem"]').first().click();
  await page.locator('#botForm input[name="name"]').fill('stack depth validation');
  await page.keyboard.press('Escape');
  await page.locator('#confirmModal').waitFor({ state: 'visible' });
  const depths = await page.evaluate(() => ({
    parent: document.querySelector('#botModal').dataset.dialogDepth,
    top: document.querySelector('#confirmModal').dataset.dialogDepth,
    parentBackdrop: getComputedStyle(document.querySelector('#botModal'), '::backdrop').backgroundColor,
    parentTransform: getComputedStyle(document.querySelector('#botModal .modal-panel')).transform,
    topTransform: getComputedStyle(document.querySelector('#confirmModal .modal-panel')).transform,
  }));
  assert(depths.parent === '1' && depths.top === '0', `dialog depth mismatch: ${JSON.stringify(depths)}`);
  assert(depths.parentTransform === 'none' && depths.topTransform === 'none', `keyboard dialog used transform motion: ${JSON.stringify(depths)}`);
  await page.screenshot({ path: path.join(outputDir, '01-dialog-depth-1440x900.png') });
  await page.locator('#confirmModalSubmitButton').click();
  await page.locator('#botModal').waitFor({ state: 'hidden' });

  await page.locator('[data-page="terminal"]').click();
  await page.waitForFunction(() => window.location.hash === '#terminal');
  while (await page.locator('#terminalTabs [data-terminal-id]').count() < 3) {
    await page.locator('#terminalCreateButton').click();
  }
  const order = async () => page.locator('#terminalTabs [data-terminal-id]').evaluateAll(
    (tabs) => tabs.map((tab) => tab.dataset.terminalId),
  );
  const original = await order();
  const firstHandle = page.locator(`[data-terminal-id="${original[0]}"] [data-terminal-drag-handle]`);
  const lastTab = page.locator(`[data-terminal-id="${original.at(-1)}"]`);
  await firstHandle.scrollIntoViewIfNeeded();
  await lastTab.scrollIntoViewIfNeeded();
  const start = await firstHandle.boundingBox();
  const target = await lastTab.boundingBox();
  assert(start && target, 'terminal drag endpoints are not visible');
  await dragMouse(page, { x: start.x + start.width / 2, y: start.y + start.height / 2 }, [
    { x: target.x + target.width - 8, y: start.y + start.height / 2, steps: 12 },
  ]);
  await page.waitForTimeout(800);
  const reordered = await order();
  assert(reordered.join('|') !== original.join('|'), 'pointer terminal reorder did not update live order');
  assert(await page.locator('.terminal-tab.dragging').count() === 0, 'terminal drag style did not settle');
  await page.screenshot({ path: path.join(outputDir, '02-terminal-reordered-1440x900.png') });

  const beforeCancel = await order();
  const cancelHandle = page.locator(`[data-terminal-id="${beforeCancel[0]}"] [data-terminal-drag-handle]`);
  const cancelTarget = page.locator(`[data-terminal-id="${beforeCancel.at(-1)}"]`);
  await cancelHandle.scrollIntoViewIfNeeded();
  await cancelTarget.scrollIntoViewIfNeeded();
  const cancelStart = await cancelHandle.boundingBox();
  const cancelEnd = await cancelTarget.boundingBox();
  assert(cancelStart && cancelEnd, 'terminal cancel endpoints are not visible');
  await dragMouse(page, { x: cancelStart.x + 20, y: cancelStart.y + 22 }, [
    { x: cancelEnd.x + cancelEnd.width - 8, y: cancelStart.y + 22, steps: 8 },
  ], { release: false });
  await page.evaluate(() => {
    const drag = state.terminal.pointerDrag;
    elements.terminalTabs.dispatchEvent(new PointerEvent('pointercancel', {
      bubbles: true,
      pointerId: drag.pointerId,
    }));
  });
  await page.mouse.up();
  await page.waitForTimeout(700);
  assert((await order()).join('|') === beforeCancel.join('|'), 'pointercancel committed terminal order');

  const beforeFailure = await order();
  const failureHandle = page.locator(`[data-terminal-id="${beforeFailure[0]}"] [data-terminal-drag-handle]`);
  const failureTarget = page.locator(`[data-terminal-id="${beforeFailure.at(-1)}"]`);
  const failureStart = await failureHandle.boundingBox();
  const failureEnd = await failureTarget.boundingBox();
  failTerminalOrder = true;
  await dragMouse(page, { x: failureStart.x + 20, y: failureStart.y + 22 }, [
    { x: failureEnd.x + failureEnd.width - 8, y: failureStart.y + 22, steps: 8 },
  ]);
  await page.locator('.toast-notification.error').waitFor({ state: 'visible' });
  await page.waitForTimeout(250);
  assert((await order()).join('|') === beforeFailure.join('|'), 'failed terminal save did not restore original order');

  await page.evaluate(() => {
    showUpdateRestartOverlay('motion-transaction');
    updateRestartStage({ stage: 'backing_up', status: 'running' });
  });
  await page.waitForTimeout(180);
  const firstToken = await page.evaluate(() => state.updates.overlayTransitionToken);
  await page.evaluate(() => updateRestartStage({ stage: 'backing_up', status: 'running' }));
  const repeatedToken = await page.evaluate(() => state.updates.overlayTransitionToken);
  assert(firstToken === repeatedToken, 'duplicate update polling restarted the stage animation');
  await page.evaluate(() => setUpdateRestartVisual('completed-preview', {
    title: '版本切换完成', message: '状态图形验证', tone: 'complete', motion: 'instant',
  }));
  await page.waitForTimeout(300);
  const glyph = await page.locator('#updateRestartSpinner').evaluate((spinner) => ({
    content: getComputedStyle(spinner, '::after').content,
    opacity: getComputedStyle(spinner, '::after').opacity,
  }));
  assert(glyph.content.includes('✓') && glyph.opacity === '1', `completion glyph missing: ${JSON.stringify(glyph)}`);
  await page.screenshot({ path: path.join(outputDir, '03-update-complete-morph-1440x900.png') });
  await page.evaluate(() => {
    const dialog = document.querySelector('#updateRestartOverlay');
    dialog.dataset.blocking = 'false';
    closeDialog(dialog, { restoreFocus: false, motion: 'instant' });
  });

  await context.close();
  checkpoint('desktop complete');
}

async function validateDrawer(browser, errors) {
  checkpoint('drawer start');
  const context = await browser.newContext({ viewport: { width: 1024, height: 768 } });
  const page = await context.newPage();
  collectErrors(page, errors);
  await login(page);

  await dragMouse(page, { x: 8, y: 190 }, [{ x: 12, y: 300, steps: 8 }]);
  await page.waitForTimeout(100);
  assert(!await page.locator('body').evaluate((body) => body.classList.contains('mobile-navigation-open')), 'vertical edge gesture opened drawer');

  await page.mouse.move(4, 220);
  await page.mouse.down();
  await page.mouse.move(165, 224, { steps: 8 });
  const midOpen = await page.evaluate(() => {
    const sidebar = document.querySelector('#appSidebar');
    const x = new DOMMatrixReadOnly(getComputedStyle(sidebar).transform).m41;
    return { x, width: sidebar.getBoundingClientRect().width, opacity: Number(getComputedStyle(document.querySelector('#navigationScrim')).opacity) };
  });
  assert(midOpen.x < 0 && midOpen.x > -midOpen.width && midOpen.opacity > 0 && midOpen.opacity < 1, `drawer did not track pointer: ${JSON.stringify(midOpen)}`);
  await page.screenshot({ path: path.join(outputDir, '04-drawer-opening-1024x768.png') });
  await page.mouse.move(300, 224, { steps: 8 });
  await page.mouse.up();
  await waitForDrawer(page, true);
  await page.waitForFunction(() => document.activeElement?.matches?.('[aria-current="page"]'));
  assert(await page.locator('[aria-current="page"]').evaluate((item) => document.activeElement === item), 'drawer open did not focus current item');

  const handle = await page.locator('#sidebarDragHandle').boundingBox();
  await page.mouse.move(handle.x + handle.width / 2, 280);
  await page.mouse.down();
  await page.mouse.move(145, 280, { steps: 8 });
  await page.screenshot({ path: path.join(outputDir, '05-drawer-closing-1024x768.png') });
  await page.mouse.move(20, 280, { steps: 8 });
  await page.mouse.up();
  await waitForDrawer(page, false);
  await page.waitForFunction(() => document.activeElement === document.querySelector('#mobileMenuButton'));
  assert(await page.locator('#mobileMenuButton').evaluate((button) => document.activeElement === button), 'drawer close did not restore menu focus');

  await page.locator('#mobileMenuButton').click();
  await waitForDrawer(page, true);
  await page.locator('[data-page="network"]').click();
  await waitForDrawer(page, false);
  await page.locator('#createButton').click();
  await page.locator('#createTransportMenu [role="menuitem"]').first().click();
  await dragMouse(page, { x: 6, y: 220 }, [{ x: 300, y: 220, steps: 8 }]);
  await page.waitForTimeout(100);
  assert(!await page.evaluate(() => state.ui.mobileNavigationOpen), 'open dialog did not block edge drawer gesture');
  await page.locator('#cancelButton').click();
  await page.locator('#botModal').waitFor({ state: 'hidden' });
  await context.close();
  checkpoint('drawer complete');
}

async function validateMobile(browser, errors) {
  checkpoint('mobile start');
  const context = await browser.newContext({ viewport: { width: 390, height: 844 } });
  const page = await context.newPage();
  collectErrors(page, errors);
  await login(page);
  await openMobilePage(page, 'files');
  await page.locator('#fileCreateButton').click();
  await page.waitForTimeout(300);
  const sheetHandle = page.locator('#fileCreateModal > .modal-panel > .dialog-sheet-handle');
  const sheetBox = await sheetHandle.boundingBox();
  assert(sheetBox && sheetBox.height >= 44, `sheet handle is not a 44px target: ${JSON.stringify(sheetBox)}`);
  await page.locator('#fileCreateNameInput').fill('motion-temp');
  await dragMouse(page, { x: sheetBox.x + sheetBox.width / 2, y: sheetBox.y + 22 }, [
    { x: sheetBox.x + sheetBox.width / 2, y: sheetBox.y + 102, steps: 5, wait: 160 },
    { x: sheetBox.x + sheetBox.width / 2, y: sheetBox.y + 92, steps: 1, wait: 180 },
  ]);
  await page.waitForTimeout(700);
  checkpoint('sheet snap-back complete');
  assert(await page.locator('#fileCreateModal').evaluate((dialog) => dialog.open), 'short reversed sheet drag dismissed dialog');
  assert(await page.locator('#fileCreateModal .modal-panel').evaluate((panel) => panel.style.transform === ''), 'sheet did not spring back cleanly');

  const sheetBoxAgain = await sheetHandle.boundingBox();
  await page.mouse.move(sheetBoxAgain.x + sheetBoxAgain.width / 2, sheetBoxAgain.y + 22);
  await page.mouse.down();
  await page.mouse.move(sheetBoxAgain.x + sheetBoxAgain.width / 2, sheetBoxAgain.y + 360, { steps: 5 });
  await page.screenshot({ path: path.join(outputDir, '06-sheet-dragging-390x844.png') });
  await page.mouse.up();
  await page.locator('#fileCreateModal').waitFor({ state: 'hidden' });
  assert(await page.locator('#fileCreateNameInput').inputValue() === '', 'sheet dismissal bypassed dialog cancel cleanup');
  checkpoint('sheet dismiss complete');

  await page.evaluate(() => {
    window.__motionConfirmation = null;
    askForConfirmation({ title: '手势取消验证', message: '下滑应等价于取消。' })
      .then((value) => { window.__motionConfirmation = value; });
  });
  const confirmHandle = page.locator('#confirmModal > .modal-panel > .dialog-sheet-handle');
  const confirmBox = await confirmHandle.boundingBox();
  await dragMouse(page, { x: confirmBox.x + confirmBox.width / 2, y: confirmBox.y + 22 }, [
    { x: confirmBox.x + confirmBox.width / 2, y: confirmBox.y + 330, steps: 5 },
  ]);
  await page.waitForFunction(() => window.__motionConfirmation === false);
  checkpoint('confirmation sheet complete');

  await page.evaluate(() => {
    for (const toast of getVisibleToasts()) dismissToast(toast, { motion: 'instant' });
  });
  await page.waitForTimeout(20);
  await page.evaluate(() => {
    showToast('通知一'); showToast('通知二'); showToast('通知三'); showToast('通知四');
  });
  assert(await page.locator('.toast-notification').count() === 3, 'toast queue exceeded three visible items');
  await page.waitForTimeout(220);
  const messages = await page.locator('.toast-message').allTextContents();
  assert(messages.join('|') === '通知二|通知三|通知四', `toast queue did not exit in order: ${messages.join('|')}`);
  checkpoint('toast queue complete');

  const toast = page.locator('.toast-notification').first();
  const captureBox = await toast.boundingBox();
  await page.mouse.move(captureBox.x + 70, captureBox.y + captureBox.height / 2);
  await page.mouse.down();
  await page.mouse.move(captureBox.x + 130, captureBox.y + captureBox.height / 2, { steps: 3 });
  await toast.evaluate((notification) => {
    const pointerId = toastRecords.get(notification)?.gesture?.pointerId;
    notification.dispatchEvent(new PointerEvent('lostpointercapture', { bubbles: true, pointerId }));
  });
  await page.mouse.up();
  await page.waitForTimeout(700);
  const captureRecovery = await toast.evaluate((notification) => ({
    transform: notification.style.transform,
    dragPaused: toastRecords.get(notification)?.pauseReasons.has('drag'),
  }));
  assert(captureRecovery.transform === '' && captureRecovery.dragPaused === false, `lost toast capture did not recover: ${JSON.stringify(captureRecovery)}`);
  const toastBox = await toast.boundingBox();
  await dragMouse(page, { x: toastBox.x + 80, y: toastBox.y + toastBox.height / 2 }, [
    { x: toastBox.x + 180, y: toastBox.y + toastBox.height / 2, steps: 4, wait: 160 },
    { x: toastBox.x + 170, y: toastBox.y + toastBox.height / 2, steps: 1, wait: 180 },
  ]);
  await page.waitForTimeout(700);
  assert(await toast.count() === 1, 'slow toast swipe should spring back');
  assert(await toast.evaluate((node) => node.style.transform === ''), 'toast retained inline transform after spring back');
  checkpoint('toast snap-back complete');
  const toastBoxAgain = await toast.boundingBox();
  await page.mouse.move(toastBoxAgain.x + 70, toastBoxAgain.y + toastBoxAgain.height / 2);
  await page.mouse.down();
  await page.mouse.move(toastBoxAgain.x + 260, toastBoxAgain.y + toastBoxAgain.height / 2, { steps: 3 });
  await page.screenshot({ path: path.join(outputDir, '07-toast-swiping-390x844.png') });
  await page.mouse.up();
  await toast.waitFor({ state: 'detached' });
  checkpoint('toast dismiss complete');

  await openMobilePage(page, 'network');
  await page.locator('[data-role="edit"]').first().click();
  const dirtySheet = await page.locator('#botModal').evaluate((dialog) => ({
    dismissible: dialog.dataset.sheetDismissible,
    handleDisplay: getComputedStyle(dialog.querySelector('.dialog-sheet-handle')).display,
  }));
  assert(dirtySheet.dismissible === 'false' && dirtySheet.handleDisplay === 'none', `dirty dialog exposed sheet gesture: ${JSON.stringify(dirtySheet)}`);
  await page.locator('#cancelButton').click();
  await context.close();
  checkpoint('mobile complete');
}

async function validateReducedMotion(browser, errors) {
  checkpoint('reduced-motion start');
  const context = await browser.newContext({ viewport: { width: 390, height: 844 }, reducedMotion: 'reduce' });
  const page = await context.newPage();
  collectErrors(page, errors);
  await login(page);
  await dragMouse(page, { x: 5, y: 200 }, [{ x: 310, y: 200, steps: 8 }]);
  assert(!await page.locator('body').evaluate((body) => body.classList.contains('mobile-navigation-open')), 'reduced-motion kept optional drawer swipe');
  await page.evaluate(() => showToast('减弱动效通知'));
  const toastMotion = await page.locator('.toast-notification').evaluate((toast) => ({
    duration: getComputedStyle(toast).transitionDuration,
    transform: getComputedStyle(toast).transform,
  }));
  assert(toastMotion.duration.split(',').every((duration) => duration.trim() === '0.12s'), `reduced toast feedback is not 120ms: ${JSON.stringify(toastMotion)}`);
  assert(toastMotion.transform === 'none', `reduced toast still moves: ${toastMotion.transform}`);
  await page.locator('#createButton').click();
  await page.locator('#createTransportMenu [role="menuitem"]').first().click();
  const dialogMotion = await page.locator('#botModal').evaluate((dialog) => {
    const panel = dialog.querySelector('.modal-panel');
    return {
      motion: dialog.dataset.motion,
      duration: getComputedStyle(panel).transitionDuration,
      transform: getComputedStyle(panel).transform,
    };
  });
  assert(dialogMotion.motion === 'reduced', `reduced dialog did not use fade-only path: ${JSON.stringify(dialogMotion)}`);
  assert(dialogMotion.duration.split(',').every((duration) => duration.trim() === '0.12s'), `reduced dialog feedback is not 120ms: ${JSON.stringify(dialogMotion)}`);
  assert(dialogMotion.transform === 'none', `reduced dialog still moves: ${dialogMotion.transform}`);
  await page.locator('#cancelButton').click();
  await page.locator('#botModal').waitFor({ state: 'hidden' });
  await context.close();
  checkpoint('reduced-motion complete');
}

(async () => {
  const browser = await chromium.launch({ headless: true, executablePath });
  const errors = [];
  try {
    await validateDesktop(browser, errors);
    await validateDrawer(browser, errors);
    await validateMobile(browser, errors);
    await validateReducedMotion(browser, errors);
    assert(errors.length === 0, `browser errors: ${errors.join(' | ')}`);
    process.stdout.write(JSON.stringify({ outputDir, screenshots: 7, errors }, null, 2));
  } finally {
    await browser.close();
  }
})().catch((error) => {
  process.stderr.write(`${error.stack || error}\n`);
  process.exitCode = 1;
});
