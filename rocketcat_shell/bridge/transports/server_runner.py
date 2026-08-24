from __future__ import annotations

from aiohttp import web


class AiohttpServerRunner:
    def __init__(self, app: web.Application, *, host: str, port: int):
        self.app = app
        self.host = str(host)
        self.port = int(port)
        self.runner: web.AppRunner | None = None
        self.site: web.TCPSite | None = None
        self.listening = False

    async def start(self) -> None:
        if self.listening:
            return
        self.runner = web.AppRunner(self.app, access_log=None)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, self.host, self.port)
        try:
            await self.site.start()
        except Exception:
            await self.runner.cleanup()
            self.runner = None
            self.site = None
            raise
        self.listening = True

    async def stop(self) -> None:
        self.listening = False
        if self.runner is not None:
            await self.runner.cleanup()
        self.runner = None
        self.site = None
