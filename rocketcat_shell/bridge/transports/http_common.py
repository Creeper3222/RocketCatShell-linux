from __future__ import annotations

import secrets
from typing import Any

from aiohttp import web

from ..json_codec import json_loads
from .action_dispatcher import OneBotActionDispatcher


def request_token(request: web.Request) -> str:
    query_token = str(request.query.get("access_token") or "")
    if query_token:
        return query_token
    authorization = str(request.headers.get("Authorization") or "")
    scheme, separator, value = authorization.partition(" ")
    if separator and scheme.lower() == "bearer":
        return value.strip()
    return ""


def is_authorized(request: web.Request, expected_token: str) -> bool:
    if not expected_token:
        return True
    return secrets.compare_digest(request_token(request), str(expected_token))


def token_failure() -> web.Response:
    return web.json_response(
        {
            "status": "failed",
            "retcode": 1403,
            "data": None,
            "wording": "token验证失败",
            "echo": None,
        },
        status=403,
    )


async def parse_action_request(request: web.Request) -> tuple[str, dict[str, Any], Any]:
    action = str(request.path or "").strip("/").split("/", 1)[0]
    if request.method.upper() == "GET":
        params: dict[str, Any] = dict(request.query)
    else:
        if request.can_read_body:
            try:
                raw_body = await request.read()
                body = json_loads(raw_body) if raw_body else {}
            except Exception as exc:
                raise web.HTTPBadRequest(text="Invalid JSON") from exc
        else:
            body = {}
        if not isinstance(body, dict):
            raise web.HTTPBadRequest(text="Invalid JSON")
        params = {**body, **dict(request.query)}
    echo = params.pop("echo", None)
    params.pop("access_token", None)
    return action, params, echo


async def handle_http_action(
    request: web.Request,
    dispatcher: OneBotActionDispatcher,
) -> web.Response:
    if request.path in {"", "/"}:
        return web.json_response(
            {
                "status": "ok",
                "retcode": 0,
                "data": {},
                "wording": "",
                "message": "RocketCatShell Is Running",
                "echo": None,
            }
        )
    action, params, echo = await parse_action_request(request)
    if not action:
        raise web.HTTPNotFound()
    response = await dispatcher.execute(action, params, echo)
    return web.json_response(response)


def apply_cors_headers(response: web.StreamResponse) -> None:
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"


def endpoint_label(host: str, port: int, scheme: str = "http") -> str:
    display_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
    return f"{scheme}://{display_host}:{int(port)}"
