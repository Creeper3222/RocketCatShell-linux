from __future__ import annotations

import re
import unittest
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
STATIC_ROOT = ROOT / "rocketcat_shell/shell/static"


class IdCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: list[str] = []

    def handle_starttag(self, _tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.ids.extend(value for key, value in attrs if key == "id" and value)


class UpdateWebUiAssetTests(unittest.TestCase):
    def test_every_javascript_element_reference_exists_once(self) -> None:
        parser = IdCollector()
        parser.feed((STATIC_ROOT / "index.html").read_text(encoding="utf-8"))
        self.assertEqual(len(parser.ids), len(set(parser.ids)))
        javascript = (STATIC_ROOT / "app.js").read_text(encoding="utf-8")
        references = set(re.findall(r"getElementById\('([^']+)'\)", javascript))
        self.assertEqual(references - set(parser.ids), set())

    def test_version_management_contract_is_present_and_linux_container_scoped(self) -> None:
        html = (STATIC_ROOT / "index.html").read_text(encoding="utf-8")
        javascript = (STATIC_ROOT / "app.js").read_text(encoding="utf-8")
        for identifier in (
            "versionManagementTitle",
            "updateReleaseModal",
            "updateConfirmModal",
            "updateRestartOverlay",
        ):
            self.assertIn(f'id="{identifier}"', html)
        for endpoint in (
            "/api/updates/status",
            "/api/updates/releases",
            "/api/updates/transactions/",
            "/api/updates/switch",
            "/api/health",
        ):
            self.assertIn(endpoint, javascript)
        self.assertIn("v0.2.1 及更早版本", html)
        self.assertIn("用户插件", html)
        self.assertIn("删除或重建容器", html)
        self.assertNotIn("RocketCatShell Windows 版本", html)

    def test_responsive_styles_have_balanced_blocks(self) -> None:
        css = (STATIC_ROOT / "styles.css").read_text(encoding="utf-8")
        self.assertEqual(css.count("{"), css.count("}"))
        self.assertIn("@media (max-width: 1120px)", css)
        self.assertIn("@media (max-width: 720px)", css)
        self.assertIn(".version-management-card", css)
        self.assertIn(".update-restart-overlay", css)

    def test_entrypoint_recovers_before_builtin_seed_and_runtime_start(self) -> None:
        entrypoint = (ROOT / "docker/entrypoint.sh").read_text(encoding="utf-8")
        recovery = entrypoint.index('recover "$APP_DIR"')
        seed = entrypoint.index("refresh_from_image")
        runtime = entrypoint.index('run "$APP_DIR"')
        self.assertLess(recovery, seed)
        self.assertLess(seed, runtime)
        self.assertIn("refusing to start", entrypoint)
        self.assertNotIn("docker.sock", entrypoint)

    def test_linux_runtime_has_no_windows_pty_dependency(self) -> None:
        requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        webui = (ROOT / "rocketcat_shell/shell/webui.py").read_text(encoding="utf-8")
        self.assertNotIn("pywinpty", requirements)
        self.assertIn("import pty", webui)
        self.assertIn('"backend": "linux-pty"', webui)


if __name__ == "__main__":
    unittest.main()
