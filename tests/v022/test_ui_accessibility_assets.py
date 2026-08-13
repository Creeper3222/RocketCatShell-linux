from __future__ import annotations

import json
import re
import unittest
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
STATIC_ROOT = ROOT / "rocketcat_shell" / "shell" / "static"
SOURCE_BRAND_LOGO = ROOT / "assets" / "logo.png"
STATIC_BRAND_LOGO = STATIC_ROOT / "logo.png"
IAMTHINKING_SCHEMA = ROOT / "data" / "plugins" / "rocketcat_plugin_adapt_iamthinking" / "_conf_schema.json"


class UiContractParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.dialogs: list[dict[str, str]] = []
        self.page_buttons: list[str] = []
        self.ids: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key: value or "" for key, value in attrs}
        if values.get("id"):
            self.ids.append(values["id"])
        if tag == "dialog":
            self.dialogs.append(values)
        if values.get("data-page"):
            self.page_buttons.append(values["data-page"])


class UiAccessibilityAssetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = (STATIC_ROOT / "index.html").read_text(encoding="utf-8")
        cls.login = (STATIC_ROOT / "login.html").read_text(encoding="utf-8")
        cls.javascript = (STATIC_ROOT / "app.js").read_text(encoding="utf-8")
        cls.css = (STATIC_ROOT / "styles.css").read_text(encoding="utf-8")
        cls.parser = UiContractParser()
        cls.parser.feed(cls.html)

    def test_navigation_has_all_grouped_hash_pages(self) -> None:
        expected_pages = {
            "network",
            "basic",
            "diagnostics",
            "logs",
            "plugins",
            "files",
            "terminal",
            "settings",
        }
        self.assertEqual(set(self.parser.page_buttons), expected_pages)
        self.assertEqual(len(self.parser.page_buttons), len(expected_pages))
        for label in ("连接与状态", "管理工具", "系统"):
            self.assertIn(label, self.html)
        self.assertIn("window.history[method]", self.javascript)
        self.assertIn("window.addEventListener('popstate'", self.javascript)
        self.assertIn("parseHashRoute", self.javascript)
        self.assertIn("aria-current", self.javascript)

    def test_mobile_drawer_is_independent_and_keyboard_contained(self) -> None:
        for identifier in (
            "mobileMenuButton",
            "navigationScrim",
            "navigationEdgeGesture",
            "sidebarDragHandle",
            "appSidebar",
        ):
            self.assertIn(identifier, self.parser.ids)
        self.assertIn("MOBILE_NAVIGATION_QUERY", self.javascript)
        self.assertIn("elements.sidebar.inert", self.javascript)
        self.assertIn("state.ui.mobileNavigationOpen", self.javascript)
        self.assertIn("event.key === 'Tab'", self.javascript)
        self.assertIn("event.key === 'Escape'", self.javascript)
        self.assertIn("body.mobile-navigation-open", self.css)
        self.assertIn("env(safe-area-inset-top)", self.css)

    def test_all_overlays_use_native_dialogs_and_shared_confirmation(self) -> None:
        self.assertGreaterEqual(len(self.parser.dialogs), 15)
        for dialog in self.parser.dialogs:
            self.assertTrue(dialog.get("id"), dialog)
            self.assertTrue(dialog.get("aria-labelledby") or dialog.get("aria-label"), dialog)
        for identifier in ("botModal", "pluginModal", "fileEditModal", "confirmModal", "updateRestartOverlay"):
            self.assertTrue(any(dialog.get("id") == identifier for dialog in self.parser.dialogs))
        self.assertIn(".showModal()", self.javascript)
        self.assertIn("requestDialogClose", self.javascript)
        self.assertIn("dismissDialogThroughCancelAction", self.javascript)
        self.assertIn("DIALOG_CANCEL_ACTIONS", self.javascript)
        self.assertIn("dialogCloseTimers", self.javascript)
        self.assertIn("data-dirty-guard", self.html)
        self.assertIn("放弃未保存的修改", self.javascript)
        self.assertNotIn("window.confirm", self.javascript)

    def test_keyboard_contracts_cover_tree_tabs_and_segmented_controls(self) -> None:
        self.assertIn('role="tree"', self.html)
        self.assertIn('role="tablist"', self.html)
        for token in (
            "role=\"treeitem\"",
            "role=\"group\"",
            "aria-expanded",
            "aria-selected",
            "aria-level",
            "tabindex=\"${focusable ? '0' : '-1'}\"",
            "event.key === 'ArrowUp'",
            "event.key === 'ArrowDown'",
            "event.key === 'Enter' || event.key === ' '",
            "event.altKey && event.shiftKey",
            "setPointerCapture",
            "role=\"tab\"",
        ):
            self.assertIn(token, self.javascript)
        self.assertIn("url.hash = ''", self.javascript)

    def test_loading_notifications_and_login_are_accessible(self) -> None:
        self.assertIn('class="toast-region"', self.html)
        self.assertIn("getVisibleToasts().length < 3", self.javascript)
        self.assertIn("pendingToasts", self.javascript)
        self.assertIn("remaining: tone === 'error' ? 8000 : 4000", self.javascript)
        self.assertIn("notification.addEventListener('focusin'", self.javascript)
        self.assertIn("notification.addEventListener('mouseenter'", self.javascript)
        self.assertIn("document.addEventListener('visibilitychange'", self.javascript)
        self.assertIn("setAttribute('aria-busy', 'true')", self.javascript)
        self.assertIn('aria-busy="false"', self.login)
        self.assertIn('role="alert"', self.login)
        self.assertIn("errorBox.focus", self.login)

    def test_brand_logo_is_shared_by_shell_login_and_browser_icon(self) -> None:
        self.assertTrue(SOURCE_BRAND_LOGO.is_file())
        self.assertTrue(STATIC_BRAND_LOGO.is_file())
        self.assertEqual(SOURCE_BRAND_LOGO.read_bytes(), STATIC_BRAND_LOGO.read_bytes())
        self.assertTrue(STATIC_BRAND_LOGO.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"))
        for document in (self.html, self.login):
            self.assertIn('rel="icon" type="image/png" href="/static/logo.png', document)
            self.assertIn('rel="apple-touch-icon" href="/static/logo.png', document)
        self.assertIn('class="brand-avatar brand-avatar-sidebar"', self.html)
        self.assertIn('class="brand-avatar brand-avatar-mobile"', self.html)
        self.assertIn('class="brand-avatar auth-brand-avatar"', self.login)
        self.assertIn("--brand-avatar-size: 44px", self.css)
        self.assertIn("--brand-avatar-size: 56px", self.css)
        self.assertIn("--brand-avatar-size: 52px", self.css)
        self.assertIn("--brand-avatar-size: 34px", self.css)
        self.assertNotIn("brand-accent", self.html + self.login)

    def test_log_view_is_dense_and_perf_is_opt_in(self) -> None:
        self.assertIn("showPerf: false", self.javascript)
        self.assertIn(
            'class="log-filter" type="button" aria-pressed="false" data-log-perf="true">Perf</button>',
            self.html,
        )
        log_entry_rule = re.search(r"\.log-entry\s*\{(?P<body>[^}]+)\}", self.css)
        self.assertIsNotNone(log_entry_rule)
        rule_body = log_entry_rule.group("body")
        self.assertIn("padding: 3px 0", rule_body)
        self.assertIn("line-height: 1.45", rule_body)
        self.assertNotIn("border-bottom", rule_body)
        self.assertNotIn("border-bottom: 1px dashed", self.css)

    def test_dashboard_waits_for_bridge_ready_and_declares_light_theme(self) -> None:
        self.assertIn("20000", self.javascript)
        self.assertIn("markPluginDashboardReady", self.javascript)
        self.assertIn("页面已载入，正在等待安全 Bridge 握手", self.javascript)
        self.assertRegex(self.javascript, r"theme:\s*'light'")
        self.assertIn("pluginDashboardRetryButton", self.html)
        self.assertIn('data-dashboard-phase="loading"', self.html)
        self.assertIn("setPluginDashboardPhase('ready'", self.javascript)

    def test_plugin_card_switch_uses_logo_height_alignment_box(self) -> None:
        switch_rules = re.findall(
            r"\.plugin-card \.compact-switch\s*\{(?P<body>[^}]+)\}",
            self.css,
        )
        self.assertTrue(switch_rules)
        rule_body = switch_rules[-1]
        self.assertIn("width: 52px", rule_body)
        self.assertIn("height: 58px", rule_body)
        self.assertIn("min-height: 58px", rule_body)
        self.assertIn("justify-content: center", rule_body)
        self.assertRegex(
            self.css,
            r"\.plugin-card \.compact-switch i\s*\{[^}]*position:\s*relative",
        )

    def test_plugin_card_actions_use_stable_cjk_raster_size(self) -> None:
        action_rule = re.search(
            r"\.plugin-card-actions \.action-chip\s*\{(?P<body>[^}]+)\}",
            self.css,
        )
        self.assertIsNotNone(action_rule)
        rule_body = action_rule.group("body")
        self.assertIn("font-size: 14px", rule_body)
        self.assertIn("font-weight: 700", rule_body)
        self.assertIn("line-height: 20px", rule_body)

    def test_iamthinking_settings_use_grouped_integer_list_editor(self) -> None:
        schema = json.loads(IAMTHINKING_SCHEMA.read_text(encoding="utf-8"))
        expected_pairs = {
            "thinking_emoji_ids": "llm_thinking_reaction",
            "using_tool_emoji_ids": "llm_using_tool_reaction",
            "error_emoji_ids": "llm_error_reaction",
            "done_emoji_ids": "llm_done_reaction",
        }
        for list_key, shortcode_key in expected_pairs.items():
            self.assertEqual(schema[list_key]["ui_component"], "integer_list")
            self.assertEqual(schema[list_key]["ui_pair"], shortcode_key)
            self.assertTrue(schema[list_key]["ui_group_label"])
            self.assertEqual(
                "iamthinking_state_emoji_ids",
                schema[list_key]["unique_across"],
            )

        for identifier in (
            "pluginListEditorModal",
            "pluginListEditorInput",
            "pluginListEditorItems",
            "pluginListEditorConfirmButton",
        ):
            self.assertIn(identifier, self.parser.ids)
        for token in (
            "renderPluginStateMappingCard",
            "openPluginListEditor",
            "addPluginListEditorValue",
            "validatePluginIntegerListMappings",
            "不能跨状态重复",
        ):
            self.assertIn(token, self.javascript)
        self.assertIn(".plugin-state-mapping-grid", self.css)
        self.assertIn(".plugin-list-editor-items", self.css)

    def test_responsive_tables_retain_headers_and_mobile_labels(self) -> None:
        self.assertGreaterEqual(self.html.count('scope="col"'), 10)
        self.assertIn("data-label=\"名称\"", self.javascript)
        self.assertIn("data-label=\"OneBot ID\"", self.javascript)
        self.assertIn("content: attr(data-label)", self.css)
        self.assertIn("@media (max-width: 1120px)", self.css)
        self.assertIn("@media (max-width: 720px)", self.css)
        self.assertIn("dialog.modal .modal-panel.user-mappings-panel", self.css)
        self.assertIn("table-layout: fixed", self.css)
        self.assertRegex(
            self.css,
            r"\.user-mappings-table-shell\s*\{[^}]*overflow-x:\s*hidden",
        )
        self.assertNotRegex(
            self.css,
            r"\.user-mappings-table\s*\{[^}]*min-width:\s*1120px",
        )

    def test_semantic_visual_and_motion_tokens_are_enforced(self) -> None:
        expected_tokens = {
            "--accent": "#eb4f8c",
            "--accent-text": "#b91c5c",
            "--accent-strong": "#c81d63",
            "--accent-end": "#d12a6e",
            "--success": "#1f7a5b",
            "--danger": "#b4234c",
            "--info": "#2563a6",
            "--warning": "#8a5c00",
            "--motion-fast": "120ms",
            "--motion-standard": "180ms",
            "--motion-drawer": "220ms",
        }
        for name, value in expected_tokens.items():
            self.assertRegex(self.css, rf"{re.escape(name)}:\s*{re.escape(value)}")
        self.assertIn(":focus-visible", self.css)
        self.assertIn("@media (prefers-reduced-motion: reduce)", self.css)
        self.assertIn("@media (hover: hover) and (pointer: fine)", self.css)
        hover_gate = self.css.index("@media (hover: hover) and (pointer: fine)")
        self.assertNotIn(":hover", self.css[:hover_gate])
        self.assertNotRegex(self.css, r"transition\s*:\s*all\b")
        self.assertNotRegex(self.css, r"(?<!-)\bease-in(?=\s|;|,)")
        self.assertNotRegex(self.css, r"scale\(0(?:[,)])")
        self.assertNotRegex(self.css, r"(?<!d)100vh")
        self.assertNotRegex(self.css, r"outline\s*:\s*none")

        color_literals = {
            match.group(0).lower()
            for match in re.finditer(
                r"(?:#[0-9a-fA-F]{3,8}|rgba?\([^)]*\)|hsla?\([^)]*\))",
                self.css,
            )
        }
        self.assertLess(len(color_literals), 192)

        radius_values = {
            match.group(1).strip()
            for match in re.finditer(r"border-radius:\s*([^;]+)", self.css)
        }
        self.assertLessEqual(
            radius_values,
            {
                "0",
                "50%",
                "999px",
                "var(--radius-sm)",
                "var(--radius-md)",
                "var(--radius-lg)",
                "var(--radius-lg) var(--radius-lg) 0 0",
            },
        )

    def test_cache_markers_are_synchronized(self) -> None:
        index_marker = re.search(r"styles\.css\?v=([^\"']+)", self.html)
        login_marker = re.search(r"styles\.css\?v=([^\"']+)", self.login)
        script_marker = re.search(r"app\.js\?v=([^\"']+)", self.html)
        self.assertIsNotNone(index_marker)
        self.assertIsNotNone(login_marker)
        self.assertIsNotNone(script_marker)
        self.assertEqual(index_marker.group(1), login_marker.group(1))
        self.assertEqual(index_marker.group(1), script_marker.group(1))

    def test_reconnect_settings_and_diagnostics_are_scoped_by_connection(self) -> None:
        for label in (
            "Rocket.Chat 重连延迟（秒）",
            "Rocket.Chat 最大连续重连次数",
            "仅作用于 Rocket.Chat 连接",
            "OneBot 上游未连接时 Bot 会保持启用",
        ):
            self.assertIn(label, self.html)
        for token in (
            "Rocket.Chat 重连失败",
            "OneBot 上游",
            "onebot_waiting_for_upstream",
            "onebot_retry_delay_seconds",
            "OneBot 丢弃事件",
            "onebot_dropped_event_count",
        ):
            self.assertIn(token, self.javascript)

    def test_message_index_settings_are_visually_nested_in_advanced_fields(self) -> None:
        self.assertIn(
            'class="form-section settings-message-index-section"',
            self.html,
        )
        section_rule = re.search(
            r"\.settings-message-index-section\s*\{(?P<body>[^}]*)\}",
            self.css,
        )
        self.assertIsNotNone(section_rule)
        section_body = section_rule.group("body")
        for token in (
            "margin: var(--space-5) var(--space-2) 0",
            "border: 0",
            "background: var(--surface-muted)",
            "box-shadow: none",
        ):
            self.assertIn(token, section_body)
        self.assertRegex(
            self.css,
            r"\.settings-message-index-section::before\s*\{[^}]*background:\s*var\(--accent-strong\)",
        )
        self.assertRegex(
            self.css,
            r"\.settings-message-index-section h3\s*\{[^}]*font-size:\s*16px",
        )

    def test_card_ordering_has_pointer_keyboard_and_persistence_contracts(self) -> None:
        for identifier in ("cardOrderInstructions", "cardOrderLiveRegion"):
            self.assertIn(identifier, self.parser.ids)
        self.assertEqual(4, self.html.count("data-card-order-grid"))
        for token in (
            "/api/settings/card-order",
            "CARD_ORDER_DRAG_THRESHOLD = 8",
            "CARD_ORDER_AUTO_SCROLL_EDGE = 40",
            "CARD_ORDER_AUTO_SCROLL_MAX = 12",
            "CARD_ORDER_FLIP_DURATION = 180",
            "cubic-bezier(0.77, 0, 0.175, 1)",
            "setPointerCapture(event.pointerId)",
            "lostpointercapture",
            "mergeVisibleCardOrder",
            "cancelCardOrderPointerDrag",
            "findVerticalKeyboardTarget",
            "event.key === 'Home'",
            "event.key === 'End'",
            "event.key === 'Escape'",
            "springCardOrderToOrigin",
            "velocityX",
            "velocityY",
            "CARD_ORDER_POINTER_BLOCK_SELECTOR",
            "isPointOverCardText",
            "isCardOrderPointerBlocked",
            "buildCardOrderDragSurface",
            "configureCardOrderCard",
            "event.target !== card",
        ):
            self.assertIn(token, self.javascript)
        for token in (
            ".card-order-drag-surface",
            "[data-card-order-id]:focus-visible",
            "pointer-events: none",
            "pointer-events: auto",
            "user-select: text",
            "touch-action: none",
            ".is-card-order-dragging",
            ".is-card-order-keyboard-selected",
            "will-change: transform",
        ):
            self.assertIn(token, self.css)
        self.assertNotIn("data-card-order-handle", self.javascript)
        self.assertNotIn(".card-reorder-handle", self.css)

    def test_direct_manipulation_motion_contracts(self) -> None:
        for token in (
            "body.dataset.inputModality",
            "inputModality === 'keyboard' ? 'instant' : 'standard'",
            "dampingRatio: 0.8, response: 0.3",
            "dampingRatio: 1.0, response: 0.4",
            "(2 * Math.PI) / response",
            "Math.min(1 / 30",
            "MOTION_SETTLE_POSITION = 0.5",
            "MOTION_SETTLE_VELOCITY = 5",
            "GESTURE_VELOCITY_WINDOW_MS = 100",
            "MOTION_DECELERATION_RATE = 0.99",
            "constant = 0.55",
            "Math.abs(deltaX) <= Math.abs(deltaY) * 1.2",
            "projectedPosition",
            "width * 0.35",
            "Math.abs(velocity) >= 110",
            "data-terminal-drag-handle",
            "scrollDelta = 12",
            "finishTerminalPointerDrag(event, true)",
            "restoreTerminalOrder(drag.originalOrder",
        ):
            self.assertIn(token, self.javascript)
        for token in (
            '@media (prefers-reduced-transparency: reduce)',
            '@media (prefers-contrast: more)',
            'body[data-input-modality="pointer"]',
            'dialog.modal[data-dialog-depth="1"]',
            'touch-action: pan-y',
            'touch-action: pan-x',
        ):
            self.assertIn(token, self.css)


if __name__ == "__main__":
    unittest.main()
