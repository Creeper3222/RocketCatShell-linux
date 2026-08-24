from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class OneBotTransportUiContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app_source = (ROOT / "rocketcat_shell" / "shell" / "static" / "app.js").read_text(
            encoding="utf-8"
        )
        cls.html_source = (ROOT / "rocketcat_shell" / "shell" / "static" / "index.html").read_text(
            encoding="utf-8"
        )
        cls.css_source = (ROOT / "rocketcat_shell" / "shell" / "static" / "styles.css").read_text(
            encoding="utf-8"
        )

    def test_dynamic_catalog_drives_menu_filters_fields_and_cards(self) -> None:
        self.assertIn("/api/onebot/transports", self.app_source)
        self.assertIn("for (const spec of getTransportCatalogItems())", self.app_source)
        self.assertIn("for (const field of Array.isArray(spec.fields)", self.app_source)
        self.assertIn("Array.isArray(spec.card_fields)", self.app_source)
        self.assertIn("transportFilterBar", self.html_source)
        self.assertIn("createTransportMenu", self.html_source)
        self.assertIn("botTransportFields", self.html_source)

    def test_create_button_uses_concise_label(self) -> None:
        self.assertIn(">新建</button>", self.html_source)
        self.assertNotIn(">新建 Bot</button>", self.html_source)

    def test_form_uses_tagged_union_and_type_is_not_editable(self) -> None:
        self.assertIn("payload.onebot_transport = collectTransportFormData()", self.app_source)
        self.assertNotIn('name="onebot_transport_type"', self.html_source)
        self.assertIn("botTransportTypeBadge", self.html_source)

    def test_mobile_filters_wrap_without_document_overflow(self) -> None:
        self.assertIn(".transport-filter-bar", self.css_source)
        self.assertIn("flex-wrap: wrap", self.css_source)
        self.assertIn("@media (max-width: 720px)", self.css_source)


if __name__ == "__main__":
    unittest.main()
