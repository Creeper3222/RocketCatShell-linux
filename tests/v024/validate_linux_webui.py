from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import sys
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from rocketcat_shell.layout import ProjectLayout
from rocketcat_shell.shell.manager import ShellManager
from rocketcat_shell.shell.webui import ShellWebUI


async def validate(args):
    args.runtime_root.mkdir(parents=True, exist_ok=False)
    args.output.mkdir(parents=True, exist_ok=True)
    original = ProjectLayout.discover()
    fields = {field.name: getattr(original, field.name) for field in dataclasses.fields(original)}
    for key, value in fields.items():
        if isinstance(value, Path) and key != "package_root":
            fields[key] = args.runtime_root / value.relative_to(ROOT)
    layout = ProjectLayout(**fields)
    manager = ShellManager(layout)
    await manager.initialize(start_runtimes=False)
    server = ShellWebUI(manager, host="127.0.0.1", port=0, access_password="123456")
    await server.start()
    server.mark_application_ready()
    manager.updates._cache = {"checked_at": __import__('time').time(), "stale": False, "error": "", "releases": []}
    results = []
    errors = []
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(executable_path=args.browser, headless=True)
            try:
                for width, height in ((1440, 900), (390, 844)):
                    context = await browser.new_context(viewport={"width": width, "height": height})
                    page = await context.new_page()
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    await page.goto(server.url, wait_until="domcontentloaded")
                    await page.locator('#authPasswordInput').fill('123456')
                    await page.locator('#authLoginForm').evaluate('(form) => form.requestSubmit()')
                    await page.wait_for_function("document.querySelector('#networkPage')?.getAttribute('aria-busy') !== 'true' && typeof openModal === 'function'")
                    await page.evaluate("openModal()")
                    setting = page.locator('[name="forward_messages_to_thread"]')
                    assert await setting.is_visible()
                    assert not await setting.is_checked()
                    assert await setting.is_enabled()
                    await setting.check()
                    await page.locator('#botForm [name="name"]').fill(f'UI validation {width}')
                    await page.locator('#botForm [name="server_url"]').fill('http://synthetic.invalid:3000')
                    await page.locator('#botForm [name="username"]').fill('synthetic')
                    await page.locator('#botForm [name="password"]').fill('synthetic-password')
                    await page.locator('#botForm [name="enabled"]').uncheck()
                    await setting.scroll_into_view_if_needed()
                    await page.screenshot(path=str(args.output / f'forward-{width}x{height}.png'), full_page=True)
                    await page.locator('#submitButton').click()
                    await page.wait_for_function("!document.querySelector('#botModal').open")
                    saved = await page.evaluate("async (name) => (await requestJson('/api/bots')).items.find(item => item.name === name)", f'UI validation {width}')
                    assert saved['forward_messages_to_thread'] is True
                    await page.evaluate('(bot) => openModal(bot)', saved)
                    assert await setting.is_checked()
                    await page.locator('#botForm [name="name"]').fill(f'UI edited {width}')
                    await page.locator('#submitButton').click()
                    await page.wait_for_function("!document.querySelector('#botModal').open")
                    edited = await page.evaluate("async (name) => (await requestJson('/api/bots')).items.find(item => item.name === name)", f'UI edited {width}')
                    assert edited['forward_messages_to_thread'] is True
                    for transport in ('http-server', 'http-client', 'http-sse-server', 'websocket-server'):
                        await page.evaluate('(transport) => openModal(null, transport)', transport)
                        assert not await setting.is_visible(), transport
                        await page.evaluate('closeModal({force:true})')
                        await page.wait_for_function("!document.querySelector('dialog[open]')")
                    await page.evaluate("navigateToPage('settings')")
                    assert await page.locator('#settingsShutdownButton').count() == 0
                    await page.wait_for_timeout(800)
                    await page.screenshot(path=str(args.output / f'settings-{width}x{height}.png'), full_page=True)
                    await page.evaluate("navigateToPage('logs')")
                    await page.evaluate("""() => {
                      stopLogPolling(); state.logs.activeLevels = new Set(['DEBUG','INFO','WARN','ERROR']);
                      state.logs.items = ['DEBUG','INFO','WARN','ERROR'].map((level,index) => ({id:90001+index,level,is_perf:false,line:`[${level}] 多行日志正文颜色验证\n第二行异常信息同色`}));
                      state.logs.items.push({id:90005,level:'WARN',is_perf:true,line:'[PERF] 实际 WARN 等级性能日志'});
                      state.logs.showPerf=true; elements.logConsole.replaceChildren(); renderLogs();
                    }""")
                    colors = await page.locator('.log-entry').evaluate_all("""entries => entries.map(entry => ({
                      level:entry.querySelector('.log-entry-level').textContent,
                      label:getComputedStyle(entry.querySelector('.log-entry-level')).color,
                      body:getComputedStyle(entry.querySelector('.log-entry-line')).color
                    }))""")
                    assert len(colors) == 5, colors
                    assert all(entry['label'] == entry['body'] for entry in colors), colors
                    await page.wait_for_timeout(800)
                    await page.screenshot(path=str(args.output / f'logs-{width}x{height}.png'), full_page=True)
                    overflow = await page.evaluate('({viewport:innerWidth,document:document.documentElement.scrollWidth,logClient:elements.logConsole.clientWidth,logScroll:elements.logConsole.scrollWidth})')
                    assert overflow['document'] <= overflow['viewport'], overflow
                    assert overflow['logScroll'] <= overflow['logClient'] + 1, overflow
                    await page.evaluate("state.logs.activeLevels.delete('INFO'); renderLogs()")
                    assert await page.locator('.log-info').count() == 0
                    await page.evaluate('setLogAutoScroll(false)')
                    assert await page.evaluate('state.logs.autoScroll') is False
                    results.append({"viewport": f"{width}x{height}", "colors": colors, "overflow": overflow,
                                    "true_preserved_after_edit": True, "other_transports_hidden": True, "shutdown_absent": True})
                    await context.close()
            finally:
                await browser.close()
        assert not errors, errors
        (args.output / 'webui-report.json').write_text(json.dumps({"passed": True, "results": results, "page_errors": errors}, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({"passed": True, "viewports": len(results), "screenshots": 6}))
    finally:
        await server.stop()
        await manager.shutdown()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--runtime-root', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--browser', default=r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe')
    asyncio.run(validate(parser.parse_args()))
