const fs = require('fs');
const path = require('path');
const { chromium } = require('playwright');

const baseUrl = process.argv[2] || 'http://127.0.0.1:58732/';
const outputDir = path.resolve(process.argv[3] || 'data/temp/v023-hover-feedback');
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

function alphaOf(color) {
  const match = String(color || '').match(/rgba?\([^,]+,[^,]+,[^,]+(?:,\s*([\d.]+))?\)/);
  return match ? Number(match[1] ?? 1) : 0;
}

async function login(page) {
  await page.goto(baseUrl, { waitUntil: 'domcontentloaded' });
  if (await page.locator('#authPasswordInput').count()) {
    await page.locator('#authPasswordInput').fill('123456');
    await page.locator('#authLoginForm').evaluate((form) => form.requestSubmit());
  }
  await page.locator('[data-page="network"]').waitFor();
  await page.waitForFunction(() => document.querySelectorAll('#botGrid > [data-card-order-id]').length >= 6);
  await page.waitForFunction(() => document.body.dataset.inputModality);
}

async function navigate(page, pageName) {
  await page.locator(`[data-page="${pageName}"]`).click();
  await page.waitForFunction(
    (name) => window.location.hash === `#${name}` && !document.querySelector(`#${name}Page`)?.classList.contains('hidden'),
    pageName,
  );
}

async function hoverStyle(locator) {
  return locator.evaluate((element) => {
    const style = getComputedStyle(element);
    const transform = style.transform;
    const matrix = !transform || transform === 'none' ? null : new DOMMatrixReadOnly(transform);
    return {
      x: matrix?.m41 || 0,
      y: matrix?.m42 || 0,
      borderColor: style.borderColor,
      backgroundColor: style.backgroundColor,
      transitionDuration: style.transitionDuration,
    };
  });
}

async function verifyCardHover(page, pageName, selector) {
  await navigate(page, pageName);
  const card = page.locator(selector).first();
  await card.waitFor({ state: 'visible' });
  const before = await hoverStyle(card);
  await card.hover();
  await page.waitForTimeout(230);
  const after = await hoverStyle(card);
  assert(Math.abs(after.y + 2) <= 0.25, `${selector} hover translate mismatch: ${JSON.stringify(after)}`);
  assert(after.borderColor !== before.borderColor, `${selector} hover border did not change`);
  await page.locator(`#${pageName}Page .page-header h1`).hover();
  await page.waitForTimeout(230);
  const settled = await hoverStyle(card);
  assert(Math.abs(settled.y) <= 0.1, `${selector} did not settle after hover: ${JSON.stringify(settled)}`);
  return card;
}

async function validateDesktop(browser, errors) {
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await context.newPage();
  collectErrors(page, errors);
  await login(page);

  const inactiveNav = page.locator('[data-page="plugins"]');
  const inactiveBefore = await hoverStyle(inactiveNav);
  await inactiveNav.hover();
  await page.waitForTimeout(160);
  const inactiveAfter = await hoverStyle(inactiveNav);
  assert(Math.abs(inactiveAfter.x - 2) <= 0.25, `sidebar hover translate mismatch: ${JSON.stringify(inactiveAfter)}`);
  assert(inactiveAfter.backgroundColor !== inactiveBefore.backgroundColor, 'sidebar hover background did not change');

  const activeNav = page.locator('[data-page="network"]');
  await page.locator('#networkPage .page-header h1').hover();
  const activeBefore = await hoverStyle(activeNav);
  await activeNav.hover();
  await page.waitForTimeout(160);
  const activeAfter = await hoverStyle(activeNav);
  assert(Math.abs(activeAfter.x - 2) <= 0.25, `active sidebar hover translate mismatch: ${JSON.stringify(activeAfter)}`);
  assert(alphaOf(activeAfter.backgroundColor) >= alphaOf(activeBefore.backgroundColor), 'active sidebar hover weakened selection');

  await verifyCardHover(page, 'network', '.bot-card');
  await verifyCardHover(page, 'basic', '.basic-info-card');
  await verifyCardHover(page, 'diagnostics', '.diagnostics-card');
  await verifyCardHover(page, 'plugins', '.plugin-card');

  await navigate(page, 'network');
  const dragCard = page.locator('.bot-card').first();
  await dragCard.hover();
  await page.waitForTimeout(230);
  const box = await dragCard.boundingBox();
  assert(box, 'network drag card is not visible');
  const start = { x: box.x + box.width / 2, y: box.y + 22 };
  await page.mouse.move(start.x, start.y);
  await page.mouse.down();
  await page.mouse.move(start.x + 32, start.y + 18, { steps: 4 });
  await page.waitForTimeout(40);
  assert(await dragCard.evaluate((card) => card.classList.contains('is-card-order-dragging')), 'hover card did not enter drag state');
  const dragTransition = await dragCard.evaluate((card) => getComputedStyle(card).transitionDuration);
  assert(dragTransition.split(',').every((value) => value.trim() === '0s'), `drag retained CSS transition: ${dragTransition}`);
  await page.mouse.up();
  await page.waitForTimeout(700);
  assert(await page.locator('.is-card-order-dragging').count() === 0, 'drag state did not settle');
  assert(await dragCard.evaluate((card) => card.style.transform === ''), 'drag left an inline transform');

  await page.screenshot({ path: path.join(outputDir, 'hover-feedback-1440x900.png'), fullPage: true });
  await context.close();
}

async function validateTablet(browser, errors) {
  const context = await browser.newContext({ viewport: { width: 1024, height: 768 } });
  const page = await context.newPage();
  collectErrors(page, errors);
  await login(page);
  await page.locator('#mobileMenuButton').click();
  await page.waitForFunction(() => document.body.classList.contains('mobile-navigation-open'));
  const nav = page.locator('[data-page="plugins"]');
  await nav.hover();
  await page.waitForTimeout(160);
  const style = await hoverStyle(nav);
  assert(Math.abs(style.x - 2) <= 0.25, `tablet drawer hover mismatch: ${JSON.stringify(style)}`);
  await page.screenshot({ path: path.join(outputDir, 'sidebar-hover-1024x768.png') });
  await context.close();
}

async function validateReducedMotion(browser, errors) {
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, reducedMotion: 'reduce' });
  const page = await context.newPage();
  collectErrors(page, errors);
  await login(page);
  const nav = page.locator('[data-page="plugins"]');
  const navBefore = await hoverStyle(nav);
  await nav.hover();
  await page.waitForTimeout(150);
  const navAfter = await hoverStyle(nav);
  assert(Math.abs(navAfter.x) <= 0.1, `reduced-motion nav moved: ${JSON.stringify(navAfter)}`);
  assert(navAfter.backgroundColor !== navBefore.backgroundColor, 'reduced-motion nav lost color feedback');

  const card = page.locator('.bot-card').first();
  const cardBefore = await hoverStyle(card);
  await card.hover();
  await page.waitForTimeout(150);
  const cardAfter = await hoverStyle(card);
  assert(Math.abs(cardAfter.y) <= 0.1, `reduced-motion card moved: ${JSON.stringify(cardAfter)}`);
  assert(cardAfter.borderColor !== cardBefore.borderColor, 'reduced-motion card lost border feedback');
  await context.close();
}

async function validateTouch(browser, errors) {
  const context = await browser.newContext({ viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true });
  const page = await context.newPage();
  collectErrors(page, errors);
  await login(page);
  const media = await page.evaluate(() => matchMedia('(hover: hover) and (pointer: fine)').matches);
  assert(media === false, 'touch viewport unexpectedly enabled fine-pointer hover');
  const transform = await page.locator('.bot-card').first().evaluate((card) => {
    const value = getComputedStyle(card).transform;
    if (!value || value === 'none') return { x: 0, y: 0 };
    const matrix = new DOMMatrixReadOnly(value);
    return { x: matrix.m41, y: matrix.m42 };
  });
  assert(Math.abs(transform.x) <= 0.1 && Math.abs(transform.y) <= 0.1, `touch card has idle transform: ${JSON.stringify(transform)}`);
  await page.screenshot({ path: path.join(outputDir, 'touch-390x844.png'), fullPage: true });
  await context.close();
}

(async () => {
  const errors = [];
  const browser = await chromium.launch({ headless: true, executablePath });
  try {
    await validateDesktop(browser, errors);
    await validateTablet(browser, errors);
    await validateReducedMotion(browser, errors);
    await validateTouch(browser, errors);
  } finally {
    await browser.close();
  }
  assert(errors.length === 0, errors.join('\n'));
  process.stderr.write('[v023-hover] validation completed\n');
})().catch((error) => {
  process.stderr.write(`${error.stack || error}\n`);
  process.exitCode = 1;
});
