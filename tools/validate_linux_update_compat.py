"""Validate official Linux updates in synthetic seven-mount Docker installations.

This development tool never cleans images, containers or directories. It leaves
an explicit resource inventory for a separately reviewed safe-cleanup step.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
import secrets
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

import aiohttp
from aiohttp import web

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
from rocketcat_shell.update_manifest import inspect_and_extract_zip, MANAGED_DIRECTORIES, MANAGED_FILES
from tools.stress_v022_full_stack import FakeRocketChat, FakeOneBot

REPOSITORY = "Creeper3222/RocketCatShell-linux"
REGISTRY = "138763327/rocketcatshell-linux"
OLD_RELEASES = {
    "v0.2.2": {"sha256": "7cf0fa547237396e937c4350c504f3d2b2cad23e86ab8136f0d802bdb2294063",
               "image": "sha256:be4920e2c75af8938bfa32efc4877a7ae0f90c8c02a822920d42e4f585d612a9"},
    "v0.2.3": {"sha256": "b8186952e3074964b76cd1122ea8c9aa0bf88b778c66e63811f9771bec5e215a",
               "image": "sha256:439420e246c86c448fcbc4a96bc1742ab88d14d510eba329bb1c7c663f2287e5"},
}
MOUNTS = ("config", "data/bots", "data/temp", "data/user_identity", "data/plugins", "data/plugin_data", "logs")


def command(*args, timeout=1800):
    result = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"command failed: {args[:3]}\n{result.stdout[-2000:]}\n{result.stderr[-2000:]}")
    return result.stdout.strip()


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def official_asset(tag):
    payload = json.loads(command("gh", "api", f"repos/{REPOSITORY}/releases/tags/{tag}"))
    asset = next(item for item in payload["assets"] if item["name"] == f"RocketCatShell-linux-{tag}.zip")
    expected_url = f"https://github.com/{REPOSITORY}/releases/download/{tag}/{asset['name']}"
    if asset["browser_download_url"] != expected_url or not str(asset.get("digest", "")).startswith("sha256:"):
        raise RuntimeError("official asset identity/digest mismatch")
    return {"tag": tag, "name": asset["name"], "url": expected_url, "sha256": asset["digest"][7:],
            "size": asset["size"], "prerelease": payload["prerelease"]}


def download_asset(asset, destination):
    request = urllib.request.Request(asset["url"], headers={"User-Agent": "RocketCatShell-Linux-Update-Validation"})
    with urllib.request.urlopen(request, timeout=120) as response:
        destination.write_bytes(response.read())
    if sha256(destination) != asset["sha256"] or destination.stat().st_size != asset["size"]:
        raise RuntimeError("official ZIP hash/size mismatch")


async def expose_fake(fake, *, onebot=False):
    fake.runner = web.AppRunner(fake.app, access_log=None)
    await fake.runner.setup()
    fake.site = web.TCPSite(fake.runner, "0.0.0.0", 0)
    await fake.site.start()
    port = fake.site._server.sockets[0].getsockname()[1]
    if onebot:
        fake.ws_url = f"ws://host.docker.internal:{port}/ws/"
    else:
        fake.base_url = f"http://host.docker.internal:{port}"


class ObservingOneBot(FakeOneBot):
    def __init__(self):
        super().__init__()
        self.events = []
        self.headers = []

    async def websocket(self, request):
        self.headers.append(dict(request.headers))
        return await super().websocket(request)

    def _observe_message(self, self_id, payload):
        self.events.append(payload)

    async def action(self, action, params):
        await self.wait_clients(1, timeout=45)
        echo = f"linux-validation-{secrets.token_hex(8)}"
        future = asyncio.get_running_loop().create_future()
        self.pending_actions[echo] = (time.perf_counter(), future)
        client = next(iter(self.clients.values()))
        await client.send_json({"action": action, "params": params, "echo": echo})
        try:
            result = await asyncio.wait_for(future, timeout=70)
        finally:
            self.pending_actions.pop(echo, None)
        if result.get("echo") != echo or result.get("status") != "ok" or result.get("retcode") != 0:
            raise RuntimeError(f"OneBot action failed: {action}: {result}")
        return result["data"]


class UpdateHarness:
    def __init__(self, args, source_tag):
        self.args, self.source_tag = args, source_tag
        self.root = args.test_root / f"{source_tag}-{args.run_id}"
        self.mount_root = self.root / "mounts"
        self.name = f"rocketcat-v024-validation-{source_tag.replace('.', '')}-{args.run_id}"
        self.port = free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.password = "synthetic-linux-validation-only"
        self.fake_rc, self.onebot = FakeRocketChat(), ObservingOneBot()
        self.proxy = None
        self.resources = {"containers": [], "images": []}
        self.report = {"source_tag": source_tag, "target_tag": args.target_tag, "root": str(self.root),
                       "started_at": datetime.now().astimezone().isoformat(), "checks": [], "transactions": [], "passed": False}
        self.sentinels = {}
        self.bot_id = ""
        self.pre_message_id = 0
        self.created_root = False

    def docker(self, *args):
        # All mutating container commands require this tool's unique prefix.
        if args[0] in {"stop", "start", "restart", "kill", "exec", "cp", "rename"}:
            if self.name not in args:
                raise RuntimeError("refusing to act outside the isolated container")
        return command("docker", *args)

    async def proxy_connection(self, reader, writer):
        peer_writer = None
        try:
            peer_reader, peer_writer = await asyncio.open_connection("127.0.0.1", self.args.proxy_port)

            async def relay(source, target):
                while chunk := await source.read(65536):
                    target.write(chunk)
                    await target.drain()

            tasks = [asyncio.create_task(relay(reader, peer_writer)), asyncio.create_task(relay(peer_reader, writer))]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        except (OSError, asyncio.CancelledError):
            pass
        finally:
            writer.close()
            if peer_writer is not None:
                peer_writer.close()

    async def prepare(self):
        if self.root.exists():
            raise RuntimeError(f"isolated root already exists: {self.root}")
        self.root.mkdir(parents=True)
        self.created_root = True
        for mount in MOUNTS:
            (self.mount_root / mount).mkdir(parents=True)
        self.source_asset = await asyncio.to_thread(official_asset, self.source_tag)
        self.target_asset = await asyncio.to_thread(official_asset, self.args.target_tag)
        if self.source_asset["prerelease"] or self.source_asset["sha256"] != OLD_RELEASES[self.source_tag]["sha256"]:
            raise RuntimeError("source stable release digest changed")
        self.report["source_asset"], self.report["target_asset"] = self.source_asset, self.target_asset
        for asset in (self.source_asset, self.target_asset):
            archive = self.root / asset["name"]
            await asyncio.to_thread(download_asset, asset, archive)
            root, manifest = inspect_and_extract_zip(archive, self.root / f"reference-{asset['tag']}", expected_tag=asset["tag"])
            if asset is self.target_asset:
                self.target_reference, self.target_manifest = root, manifest
            else:
                self.source_reference, self.source_manifest = root, manifest
        self.image = f"{REGISTRY}@{OLD_RELEASES[self.source_tag]['image']}"
        await asyncio.to_thread(command, "docker", "pull", "--platform", "linux/amd64", self.image)
        image = json.loads(await asyncio.to_thread(command, "docker", "image", "inspect", self.image))[0]
        if image["Architecture"] != "amd64" or image["Config"]["Labels"]["org.opencontainers.image.version"] != self.source_tag:
            raise RuntimeError("official source image version/architecture mismatch")
        self.resources["images"].append({"reference": self.image, "id": image["Id"]})
        await expose_fake(self.fake_rc)
        await expose_fake(self.onebot, onebot=True)
        self.proxy = await asyncio.start_server(self.proxy_connection, "0.0.0.0", 0)
        proxy_port = self.proxy.sockets[0].getsockname()[1]
        self.proxy_url = f"http://host.docker.internal:{proxy_port}"
        self.report["proxy_port"] = proxy_port
        for mount in MOUNTS:
            path = self.mount_root / mount / "validation-preserved.bin"
            path.write_bytes(f"synthetic-{mount}-{self.args.run_id}\0".encode())
            self.sentinels[str(path.relative_to(self.mount_root))] = sha256(path)
        user_plugin = self.mount_root / "data/plugins/rocketcat_plugin_validation_probe"
        user_plugin.mkdir()
        user_files = {
            user_plugin / "metadata.yaml": "name: rocketcat_plugin_validation_probe\nversion: v1.0.0\nauthor: synthetic-validation\ndesc: Synthetic persistence probe\n",
            user_plugin / "main.py": "from rocketcat_shell.plugin_system.base import RocketCatPlugin\nclass Plugin(RocketCatPlugin):\n    pass\n",
            self.mount_root / "data/plugin_data/rocketcat_plugin_validation_probe/state.json": json.dumps({"marker": "synthetic-user-plugin-state", "run_id": self.args.run_id}),
        }
        for path, value in user_files.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(value, encoding="utf-8")
            self.sentinels[str(path.relative_to(self.mount_root))] = sha256(path)
        db = self.mount_root / "data/user_identity/validation-preserved.sqlite3"
        with contextlib.closing(sqlite3.connect(db)) as connection:
            connection.execute("CREATE TABLE validation_state (marker TEXT PRIMARY KEY, payload TEXT NOT NULL)")
            connection.execute("INSERT INTO validation_state VALUES ('upgrade', 'synthetic-identity-preserved')")
            connection.commit()
        self.sentinels[str(db.relative_to(self.mount_root))] = sha256(db)
        self.report["sentinels_before"] = dict(self.sentinels)
        await self.create_container()
        await self.wait_health(self.source_tag)
        self.immutable_before = await self.immutable_hashes()

    async def immutable_hashes(self):
        output = await asyncio.to_thread(self.docker, "exec", self.name, "python", "-c",
            "import hashlib,json; from pathlib import Path; "
            "paths=['/opt/rocketcat/update_helper.py','/usr/local/bin/rocketcat-entrypoint.sh']; "
            "print(json.dumps({p:hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in paths}))")
        return json.loads(output)

    async def create_container(self):
        args = ["run", "-d", "--name", self.name, "--platform", "linux/amd64", "--restart", "unless-stopped",
                "--label", f"io.rocketcat.validation-run={self.args.run_id}",
                "-p", f"127.0.0.1:{self.port}:5751", "--add-host", "host.docker.internal:host-gateway"]
        for mount in MOUNTS:
            args += ["--mount", f"type=bind,source={(self.mount_root / mount).resolve()},target=/app/{mount}"]
        for key, value in {"ROCKETCAT_WEBUI_PASSWORD": self.password, "ROCKETCAT_AUTO_OPEN_BROWSER": "false",
                           "HTTP_PROXY": self.proxy_url, "HTTPS_PROXY": self.proxy_url,
                           "NO_PROXY": "localhost,127.0.0.1,host.docker.internal", "ALL_PROXY": ""}.items():
            args += ["-e", f"{key}={value}"]
        identifier = await asyncio.to_thread(self.docker, *args, self.image)
        self.resources["containers"].append({"id": identifier, "name": self.name})

    async def wait_health(self, version, transaction_id="", timeout=240):
        deadline = time.monotonic() + timeout
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=3), trust_env=False) as session:
            while time.monotonic() < deadline:
                try:
                    async with session.get(self.base_url + "/api/health") as response:
                        body = await response.json()
                        if response.status == 200 and body.get("version") == version and (not transaction_id or body.get("update_transaction") == transaction_id):
                            self.report["last_health"] = body
                            return body
                except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                    pass
                await asyncio.sleep(0.2)
        raise TimeoutError(f"health did not reach {version}/{transaction_id}")

    async def api(self, method, path, payload=None, expected=200):
        async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True), timeout=aiohttp.ClientTimeout(total=180), trust_env=False) as session:
            async with session.post(self.base_url + "/api/login", json={"password": self.password}) as response:
                if response.status != 200:
                    raise RuntimeError("isolated login failed")
            async with session.request(method, self.base_url + path, json=payload) as response:
                body = await response.json()
                if response.status != expected:
                    raise RuntimeError(f"{method} {path}: {response.status}: {body}")
                return body

    async def bot(self):
        payload = await self.api("GET", "/api/bots")
        return next(item for item in payload["items"] if item["id"] == self.bot_id)

    async def create_bots(self):
        active = {"name": "Linux Compat Active", "enabled": True, "server_url": self.fake_rc.base_url,
                  "username": "compat-active", "password": "synthetic-password", "e2ee_password": ""}
        if self.source_tag == "v0.2.2":
            active.update(onebot_ws_url=self.onebot.ws_url, onebot_access_token="synthetic-token", reconnect_delay=0.2,
                          max_reconnect_attempts=5, skip_own_messages=False, debug=True)
        else:
            active["onebot_transport"] = {"type": "websocket-client", "settings": {"url": self.onebot.ws_url,
                "access_token": "synthetic-token", "message_post_format": "string", "report_self_message": False,
                "reconnect_interval_ms": 1700, "heartbeat_interval_ms": 9000, "debug": True}}
        self.bot_id = (await self.api("POST", "/api/bots", active))["item"]["id"]
        await self.fake_rc.wait_clients(1, timeout=45)
        await self.onebot.wait_clients(1, timeout=45)
        if not any(header.get("Authorization") == "Bearer synthetic-token" for header in self.onebot.headers):
            raise RuntimeError("OneBot token was not preserved at source")
        self.source_bot = await self.bot()
        self.self_id = (await self.onebot.action("get_login_info", {}))["user_id"]
        for index in range(2):
            disabled = dict(active, enabled=False, name=f"Linux Compat Disabled {index}", username=f"disabled-{index}")
            if self.source_tag == "v0.2.2":
                disabled["onebot_access_token"] = "" if index == 0 else "synthetic-other-token"
            await self.api("POST", "/api/bots", disabled)
        registry = json.loads((self.mount_root / "config/bots.json").read_text(encoding="utf-8"))
        if any("forward_messages_to_thread" in item for item in registry["bots"]):
            raise RuntimeError("source unexpectedly contains the new thread field")
        self.report["checks"].append("source missing thread field; three synthetic Bot configurations recorded")
        path = self.mount_root / "data/bots" / self.bot_id / "validation-runtime-preserved.bin"
        path.write_bytes(b"synthetic-bot-runtime-preserved")
        self.sentinels[str(path.relative_to(self.mount_root))] = sha256(path)
        timestamp = int(time.time() * 1000)
        before = len(self.onebot.events)
        await self.fake_rc.inject({"_id": f"linux-before-{timestamp}", "rid": "room-0", "msg": "linux upgrade sentinel",
                                   "ts": {"$date": timestamp}, "_updatedAt": {"$date": timestamp},
                                   "u": {"_id": "synthetic-user", "username": "synthetic-user", "name": "Synthetic User"}})
        deadline = time.monotonic() + 20
        while len(self.onebot.events) == before and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        if len(self.onebot.events) == before:
            raise RuntimeError("source OneBot event was not received")
        self.pre_message_id = self.onebot.events[-1]["message_id"]
        self.report["pre_update_message_id"] = self.pre_message_id

    async def switch(self, tag, expected_status="completed", health_version=None):
        releases = await self.api("GET", "/api/updates/releases?refresh=true")
        if releases.get("stale"):
            raise RuntimeError("official update discovery unavailable")
        candidate = next(item for item in releases["releases"] if item["tag_name"] == tag)
        asset = self.target_asset if tag == self.args.target_tag else self.source_asset
        if candidate["asset"]["digest"] != "sha256:" + asset["sha256"]:
            raise RuntimeError("product updater official digest mismatch")
        transaction = await self.api("POST", "/api/updates/switch", {"tag_name": tag})
        identifier = transaction["transaction_id"]
        target_health = health_version or tag
        if expected_status == "completed":
            await self.wait_health(target_health, identifier)
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            try:
                detail = await self.api("GET", f"/api/updates/transactions/{identifier}")
                detail = detail.get("transaction", detail)
                if detail.get("status") in {"completed", "failed", "rolled_back", "recovery_required"}:
                    if detail["status"] != expected_status:
                        raise RuntimeError(f"transaction failed: {detail}")
                    self.report["transactions"].append({key: detail.get(key) for key in ("transaction_id", "action", "status", "stage", "current_version", "target_version")})
                    await self.wait_health(target_health, identifier)
                    print(f"PASS transaction {identifier}: {detail['action']} {tag} -> {detail['status']}", flush=True)
                    return detail
            except (aiohttp.ClientError, asyncio.TimeoutError):
                pass
            await asyncio.sleep(0.3)
        raise TimeoutError("update transaction did not complete")

    async def assert_managed(self, *, source=False):
        manifest = self.source_manifest if source else self.target_manifest
        # A normal container restart has no handoff environment transaction ID.
        # The completed transaction still retains the official candidate manifest.
        transaction_id = self.report["transactions"][-1]["transaction_id"]
        actual = await asyncio.to_thread(self.docker, "exec", self.name, "python", "-c", '''import hashlib,json
import sys
from pathlib import Path
r=Path('/app'); m=json.loads(sys.argv[1]); paths=[]
t=json.loads((r/'data/update/transactions'/sys.argv[2]/'transaction.json').read_text())
manifest_path=Path(t['candidate_root'])/'update-manifest.json'
for d in m['managed_directories']:
 paths.extend(p for p in (r/d).rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix not in {'.pyc','.pyo'})
paths.extend(r/p for p in m['managed_files'])
print(json.dumps({'files':{p.relative_to(r).as_posix():{'size':p.stat().st_size,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()} for p in paths},'manifest_tag':t.get('target_tag',m['version']),'manifest_sha256':hashlib.sha256(manifest_path.read_bytes()).hexdigest()}))''', json.dumps(manifest), transaction_id)
        actual = json.loads(actual)
        reference = self.source_reference if actual["manifest_tag"] == self.source_tag else self.target_reference
        expected = {entry["path"]: {"size": entry["size"], "sha256": entry["sha256"]}
                    for entry in manifest["files"] if entry["path"] not in manifest["image_deployment_files"]}
        if actual["files"] != expected:
            raise RuntimeError("installed managed source differs from official reference")
        if actual["manifest_sha256"] != sha256(reference / "update-manifest.json"):
            raise RuntimeError("transaction candidate manifest differs from official reference")
        if await self.immutable_hashes() != self.immutable_before:
            raise RuntimeError("in-container update changed immutable entrypoint/helper")
        self.report["checks"].append(f"exact {manifest['version']} manifest and {len(expected)} managed hashes; image entrypoint/helper unchanged")

    async def assert_target(self):
        await self.assert_managed()
        item = await self.bot()
        if item.get("forward_messages_to_thread") is not False or item["onebot_self_id"] != self.self_id:
            raise RuntimeError("new thread default or existing identity was not preserved")
        transport = item["onebot_transport"]
        if transport["type"] != "websocket-client" or transport["settings"]["url"] != self.onebot.ws_url:
            raise RuntimeError("source transport was not preserved")
        if self.source_tag == "v0.2.3":
            for key in ("message_post_format", "reconnect_interval_ms", "heartbeat_interval_ms"):
                if transport["settings"][key] != self.source_bot["onebot_transport"]["settings"][key]:
                    raise RuntimeError(f"transport setting changed: {key}")
        message = await self.onebot.action("get_msg", {"message_id": self.pre_message_id})
        if message["message_id"] != self.pre_message_id:
            raise RuntimeError("pre-update message mapping disappeared")
        await asyncio.to_thread(self.docker, "exec", self.name, "python", "/app/tools/check_requirements.py", "/app/requirements.txt")
        self.report["checks"].append("Bot, transport, identity, get_msg and dependencies preserved")
        plugins = await self.api("GET", "/api/plugins")
        if not any(item.get("id") == "rocketcat_plugin_validation_probe" for item in plugins["items"]):
            raise RuntimeError("synthetic user plugin was not preserved")
        await self.assert_sentinels()

    async def inject_failure(self, mode):
        if mode == "interrupt":
            # Install the fault before prepare_switch freezes this synthetic
            # helper. It restores the source helper before backup, then replaces
            # one managed file and pauses for an external Docker KILL. Namespace
            # PID 1 cannot be reliably killed by a peer inside that namespace.
            # Recovery therefore sees an exact
            # original backup plus a genuinely partial replacement.
            code = '''from pathlib import Path
r=Path('/app/data/update'); r.mkdir(parents=True,exist_ok=True)
h=Path('/app/tools/update_helper.py'); raw=h.read_text()
saved=r/'validation-helper-original.py'; saved.write_bytes(h.read_bytes())
validate='        _validate_candidate(candidate_root, payload)'
install='        _install(source_root, candidate_root)'
if raw.count(validate)!=1 or raw.count(install)!=1: raise RuntimeError('unexpected synthetic helper source')
restore="        (source_root / 'tools/update_helper.py').write_bytes(Path('/app/data/update/validation-helper-original.py').read_bytes())\\n"+validate
partial="        shutil.copy2(candidate_root / 'rocketcat_shell/__init__.py', source_root / 'rocketcat_shell/__init__.py')\\n"
partial+="        (transaction_root.parent.parent / 'validation-interrupt-injection.json').write_text(json.dumps({'transaction_id':payload['transaction_id'],'stage':'replacing'}))\\n"
partial+="        time.sleep(60)\\n"+install
patched=raw.replace(validate,restore).replace(install,partial)
compile(patched,str(h),'exec'); h.write_text(patched)
'''
            await asyncio.to_thread(self.docker, "exec", self.name, "python", "-c", code)
            return
        code = '''import hashlib,json,os,signal,time
from pathlib import Path
r=Path('/app/data/update/transactions'); r.mkdir(parents=True,exist_ok=True)
known={p.name for p in r.iterdir() if p.is_dir()}
mode=MODE; deadline=time.monotonic()+240
(r.parent/('validation-'+mode+'-watcher-ready.json')).write_text(json.dumps({'status':'watching'}))
while time.monotonic()<deadline:
 for d in r.iterdir():
  if not d.is_dir() or d.name in known: continue
  p=d/'transaction.json'
  try: t=json.loads(p.read_text())
  except (OSError,ValueError): continue
  if t.get('target_version')!=TARGET: continue
  stage=t.get('stage')
  if mode=='health' and stage=='waiting_for_shutdown' and t.get('status')=='prepared':
   v=Path(t['candidate_root'])/'rocketcat_shell/__init__.py'; raw=v.read_text().replace(TARGET,'v9.9.9-validation-health-failure'); v.write_text(raw)
   for e in t['candidate_files']:
    if e['path']=='rocketcat_shell/__init__.py': e['size']=v.stat().st_size; e['sha256']=hashlib.sha256(v.read_bytes()).hexdigest()
   temp=p.with_suffix('.validation.tmp'); temp.write_text(json.dumps(t)); os.replace(temp,p)
   (r.parent/'validation-health-injection.json').write_text(json.dumps({'transaction_id':t['transaction_id'],'stage':stage}))
   raise SystemExit(0)
 time.sleep(0.001)
raise TimeoutError('isolated failure injection did not observe expected transaction stage')
'''.replace("mode=MODE", "mode=" + repr(mode)).replace("TARGET", repr(self.args.target_tag))
        await asyncio.to_thread(self.docker, "exec", "-d", self.name, "python", "-c", code)
        for _ in range(20):
            ready = await asyncio.to_thread(self.docker, "exec", self.name, "python", "-c",
                f"from pathlib import Path; print(Path('/app/data/update/validation-{mode}-watcher-ready.json').exists())")
            if ready == "True":
                return
            await asyncio.sleep(0.1)
        raise RuntimeError("isolated fault watcher did not start")

    async def recovery_scenarios(self):
        for mode in self.args.recovery_modes:
            await self.inject_failure(mode)
            killer = asyncio.create_task(self.kill_partial_replacement()) if mode == "interrupt" else None
            try:
                transaction = await self.switch(self.args.target_tag, expected_status="rolled_back", health_version=self.source_tag)
                if killer is not None:
                    await killer
            finally:
                if killer is not None and not killer.done():
                    killer.cancel()
                    await asyncio.gather(killer, return_exceptions=True)
            injection = await asyncio.to_thread(self.docker, "exec", self.name, "python", "-c",
                f"from pathlib import Path; print(Path('/app/data/update/validation-{mode}-injection.json').read_text())")
            if json.loads(injection)["transaction_id"] != transaction["transaction_id"]:
                raise RuntimeError("fault recovery was not caused by the intended injection")
            await self.assert_managed(source=True)
            await self.assert_sentinels()
            self.report["checks"].append(f"{mode} failure restores source and completes rolled_back with exact hashes")
        await self.switch(self.args.target_tag)
        await self.assert_target()
        old_name = self.name + "-before-recreate"
        await asyncio.to_thread(self.docker, "stop", "-t", "35", self.name)
        await asyncio.to_thread(self.docker, "rename", self.name, old_name)
        self.resources["containers"][-1]["name"] = old_name
        await self.create_container()
        await self.wait_health(self.source_tag)
        await self.assert_sentinels()
        await self.onebot.action("get_msg", {"message_id": self.pre_message_id})
        self.report["checks"].append("isolated recreation restores original image version with seven mounts and message mapping preserved")

    async def kill_partial_replacement(self):
        deadline = time.monotonic() + 240
        while time.monotonic() < deadline:
            marker = await asyncio.to_thread(self.docker, "exec", self.name, "python", "-c",
                "from pathlib import Path; p=Path('/app/data/update/validation-interrupt-injection.json'); print(p.read_text() if p.exists() else '')")
            if marker:
                if json.loads(marker).get("stage") != "replacing":
                    raise RuntimeError("unexpected partial replacement marker")
                await asyncio.to_thread(self.docker, "kill", "--signal", "KILL", self.name)
                # Docker treats an explicit CLI KILL as a manual stop. Start the
                # same isolated container to exercise entrypoint recovery.
                await asyncio.to_thread(self.docker, "start", self.name)
                return
            await asyncio.sleep(0.1)
        raise TimeoutError("partial replacement did not become ready for external KILL")

    async def assert_sentinels(self):
        after = {relative: sha256(self.mount_root / relative) for relative in self.sentinels}
        if after != self.sentinels:
            raise RuntimeError("persistent sentinel changed")
        with contextlib.closing(sqlite3.connect(self.mount_root / "data/user_identity/validation-preserved.sqlite3")) as connection:
            if connection.execute("SELECT payload FROM validation_state WHERE marker='upgrade'").fetchone() != ("synthetic-identity-preserved",):
                raise RuntimeError("identity sentinel row corrupted")
        identity_scope = self.mount_root / "data/bots" / self.bot_id / "identity_scope.json"
        if identity_scope.exists():
            scope = json.loads(identity_scope.read_text(encoding="utf-8"))
            container_db = str(scope["database_path"])
            if not container_db.startswith("/app/data/user_identity/"):
                raise RuntimeError("identity path escaped protected mount")
            # SQLite WAL/shared-memory locks must stay in the Linux filesystem
            # view. Opening the live Linux DB through the Windows bind mount can
            # create/remove incompatible WAL sidecars and break the next restart.
            result = await asyncio.to_thread(self.docker, "exec", self.name, "python", "-c",
                "import sqlite3,sys; "
                "c=sqlite3.connect('file:'+sys.argv[1]+'?mode=ro',uri=True); "
                "print(c.execute('PRAGMA quick_check').fetchone()[0]); c.close()", container_db)
            if result != "ok":
                raise RuntimeError("runtime identity integrity failure")
        self.report["sentinels_after"] = after

    async def run(self):
        try:
            await self.prepare()
            await self.create_bots()
            if self.args.recovery_only:
                await self.recovery_scenarios()
                self.report["passed"] = True
                return
            await self.switch(self.args.target_tag)
            await self.assert_target()
            await asyncio.to_thread(self.docker, "restart", self.name)
            await self.wait_health(self.args.target_tag)
            await self.assert_target()
            self.report["checks"].append("container restart preserves updated writable layer")
            await self.switch(self.source_tag)
            await self.assert_managed(source=True)
            await self.assert_sentinels()
            await self.switch(self.args.target_tag)
            await self.assert_target()
            # Same-version repair intentionally corrupts only an isolated source file.
            await asyncio.to_thread(self.docker, "exec", self.name, "python", "-c", "from pathlib import Path; p=Path('/app/rocketcat_shell/diagnostics.py'); p.write_bytes(p.read_bytes()+b'\\n# isolated repair sentinel\\n')")
            await self.switch(self.args.target_tag)
            await self.assert_target()
            self.report["checks"].append("same-version reinstall restores corrupted managed source")
            self.report["passed"] = True
        except BaseException as exc:
            self.report["error"] = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            if self.resources["containers"]:
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(self.docker, "stop", "-t", "35", self.name)
                with contextlib.suppress(Exception):
                    (self.root / "container.log").write_text(await asyncio.to_thread(self.docker, "logs", self.name), encoding="utf-8")
            await self.fake_rc.stop()
            await self.onebot.stop()
            if self.proxy:
                self.proxy.close()
                await self.proxy.wait_closed()
            self.report["resources"] = self.resources
            self.report["finished_at"] = datetime.now().astimezone().isoformat()
            if self.created_root:
                (self.root / "compatibility-report.json").write_text(json.dumps(self.report, ensure_ascii=False, indent=2), encoding="utf-8")
                (self.root / "compatibility-report.md").write_text(f"# {self.source_tag} → {self.args.target_tag}\n\nResult: {'PASS' if self.report['passed'] else 'FAIL'}\n\n" + "\n".join('- ' + check for check in self.report['checks']) + "\n", encoding="utf-8")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--test-root", type=Path, required=True)
    parser.add_argument("--source-tags", nargs="+", choices=tuple(OLD_RELEASES), default=list(OLD_RELEASES))
    parser.add_argument("--target-tag", default="v0.2.4")
    parser.add_argument("--run-id", default=datetime.now().strftime("%Y%m%d-%H%M%S"))
    parser.add_argument("--proxy-port", type=int, default=3067)
    parser.add_argument("--recovery-only", action="store_true", help="run intentional health-failure, interruption and recreation scenarios in a separate installation")
    parser.add_argument("--recovery-modes", nargs="+", choices=("health", "interrupt"), default=["health", "interrupt"], help="fault modes; default covers both, select one only while debugging")
    args = parser.parse_args(argv)
    args.test_root = args.test_root.resolve()
    if not args.test_root.is_dir() or args.test_root.name in {"RocketCatShell", "RocketCatShell-linux", "data", "config"}:
        parser.error("--test-root must be an existing dedicated validation parent")
    if not args.run_id or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-" for ch in args.run_id):
        parser.error("--run-id must contain only letters, digits and dashes")
    return args


async def main(args):
    for tag in args.source_tags:
        harness = UpdateHarness(args, tag)
        print(f"Starting isolated {tag} -> {args.target_tag}: {harness.name}", flush=True)
        await harness.run()
        print(f"PASS {tag}: {harness.root}", flush=True)


if __name__ == "__main__":
    asyncio.run(main(parse_args()))
