from __future__ import annotations

import hashlib
import re
import tempfile
import unittest
from pathlib import Path

from rocketcat_shell import __version__
from rocketcat_shell.update_manifest import MIN_UPDATE_TAG
from rocketcat_shell.updates import UpdateService


ROOT = Path(__file__).resolve().parents[2]
STATIC_ROOT = ROOT / "rocketcat_shell" / "shell" / "static"


class HoverMotionContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.css = (STATIC_ROOT / "styles.css").read_text(encoding="utf-8")
        cls.index = (STATIC_ROOT / "index.html").read_text(encoding="utf-8")
        cls.login = (STATIC_ROOT / "login.html").read_text(encoding="utf-8")
        cls.javascript = (STATIC_ROOT / "app.js").read_text(encoding="utf-8")

    def test_v024_metadata_is_synchronized(self) -> None:
        self.assertEqual("v0.2.4", __version__)
        self.assertEqual("v0.2.2", MIN_UPDATE_TAG)
        self.assertIn('<span id="sidebarVersion">v0.2.4</span>', self.index)
        self.assertIn("# RocketCatShell v0.2.4 runtime dependencies.", (ROOT / "requirements.txt").read_text(encoding="utf-8"))
        self.assertIn("version: v0.2.4", (ROOT / "data/plugins/rocketcat_plugin_built_in_command/metadata.yaml").read_text(encoding="utf-8"))

        # Versioned static assets are immutable-cached; app.js gets its own key
        # so a behavior change cannot leave new HTML paired with stale code.
        style_markers = [
            re.search(r"styles\.css\?v=([^\"']+)", self.index),
            re.search(r"styles\.css\?v=([^\"']+)", self.login),
        ]
        script_marker = re.search(r"app\.js\?v=([^\"']+)", self.index)
        self.assertTrue(all(style_markers))
        self.assertIsNotNone(script_marker)
        self.assertEqual(
            {"20261007linux0241"},
            {match.group(1) for match in style_markers if match},
        )
        script_digest = hashlib.sha256(self.javascript.encode("utf-8")).hexdigest()[:16]
        self.assertEqual(f"sha256-{script_digest}", script_marker.group(1))

    def test_update_actions_follow_v024_current_version(self) -> None:
        with tempfile.TemporaryDirectory(prefix="rocketcat-v024-update-actions-") as temporary_directory:
            root = Path(temporary_directory)
            service = UpdateService(root, root)
            self.assertEqual("rollback", service.action_for_tag("v0.2.2"))
            self.assertEqual("rollback", service.action_for_tag("v0.2.3"))
            self.assertEqual("reinstall", service.action_for_tag("v0.2.4"))
            self.assertEqual("update", service.action_for_tag("v0.2.5"))

    def test_hover_feedback_uses_existing_motion_tokens(self) -> None:
        self.assertIn("window.addEventListener('pointermove', () => setInputModality('pointer')", self.javascript)
        self.assertIn("inputModality === modality", self.javascript)
        self.assertRegex(
            self.css,
            r"\.nav-item\s*\{[^}]*transform var\(--motion-fast\) var\(--ease-out\)[^}]*"
            r"background-color var\(--motion-fast\) var\(--ease-out\)[^}]*"
            r"border-color var\(--motion-fast\) var\(--ease-out\)",
        )
        self.assertRegex(
            self.css,
            r"\.bot-card,\s*\.basic-info-card,\s*\.diagnostics-card,\s*\.plugin-card\s*\{[^}]*"
            r"transform var\(--motion-standard\) var\(--ease-out\)[^}]*"
            r"border-color var\(--motion-fast\) var\(--ease-out\)",
        )
        hover_gate = self.css.index("@media (hover: hover) and (pointer: fine)")
        hover_end = self.css.index("@media (max-width: 1120px)", hover_gate)
        hover_css = self.css[hover_gate:hover_end]
        self.assertIn('body[data-input-modality="pointer"] .nav-item:hover', hover_css)
        self.assertIn("transform: translateX(2px)", hover_css)
        self.assertIn('body[data-input-modality="pointer"]:not(.card-order-drag-active)', hover_css)
        self.assertIn("transform: translateY(-2px)", hover_css)
        self.assertNotIn(".performance-bot-card", hover_css)
        self.assertNotIn(".diagnostics-meter-card", hover_css)
        self.assertNotRegex(self.css, r"transition\s*:\s*all\b")

    def test_drag_keyboard_and_reduced_motion_are_isolated(self) -> None:
        self.assertRegex(
            self.css,
            r"body\.card-order-drag-active \[data-card-order-id\],\s*"
            r"\[data-card-order-id\]\.is-card-order-dragging\s*\{\s*transition:\s*none",
        )
        for token in (
            ".is-card-order-dragging",
            ".is-card-order-keyboard-selected",
            '[aria-disabled="true"]',
        ):
            self.assertIn(token, self.css)

        reduced_gate = self.css.index("@media (prefers-reduced-motion: reduce)", self.css.index("@media (hover: hover)"))
        reduced_css = self.css[reduced_gate:]
        self.assertIn("transition: border-color var(--motion-fast) var(--ease-out) !important", reduced_css)
        self.assertIn("transform: none !important", reduced_css)


if __name__ == "__main__":
    unittest.main()
