from __future__ import annotations

import unittest

from tools.migrate_user_identity import parse_args


class MigrationArgumentValidationTests(unittest.TestCase):
    def test_migration_has_no_default_real_user_id(self) -> None:
        args = parse_args(["--bot-id", "compat-test-bot"])
        self.assertEqual("", args.bot_user_id)
        self.assertEqual("", args.anchor_user_id)
        self.assertFalse(args.inject_synthetic)

    def test_synthetic_identity_requires_explicit_anchor_before_migration(self) -> None:
        with self.assertRaises(SystemExit) as raised:
            parse_args(
                [
                    "--bot-id",
                    "compat-test-bot",
                    "--inject-synthetic",
                ]
            )
        self.assertEqual(2, raised.exception.code)

    def test_synthetic_identity_accepts_explicit_fictitious_anchor(self) -> None:
        args = parse_args(
            [
                "--bot-id",
                "compat-test-bot",
                "--inject-synthetic",
                "--anchor-user-id",
                "synthetic:release-validation-anchor",
            ]
        )
        self.assertEqual("synthetic:release-validation-anchor", args.anchor_user_id)


if __name__ == "__main__":
    unittest.main()
