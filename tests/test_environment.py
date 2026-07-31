from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from openclaw_ephemeral.environment import (
    ConfigurationError,
    boolean,
    config_path,
    expand_api_key_aliases,
    integer,
    openclaw_command,
    secret_ref,
    workspace_path,
)


class EnvironmentTests(unittest.TestCase):
    def test_boolean_accepts_established_spellings(self) -> None:
        for value in ("1", "true", "YES", "on"):
            with self.subTest(value=value):
                self.assertTrue(boolean({"FLAG": value}, "FLAG"))
        for value in ("0", "false", "NO", "off"):
            with self.subTest(value=value):
                self.assertFalse(boolean({"FLAG": value}, "FLAG", default=True))

    def test_boolean_rejects_ambiguous_values(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "FLAG"):
            boolean({"FLAG": "sometimes"}, "FLAG")

    def test_integer_is_bounded(self) -> None:
        self.assertEqual(integer({}, "PORT", default=123), 123)
        self.assertEqual(integer({"PORT": "456"}, "PORT", default=123), 456)
        with self.assertRaisesRegex(ConfigurationError, "between"):
            integer({"PORT": "70000"}, "PORT", default=123, maximum=65_535)
        with self.assertRaisesRegex(ConfigurationError, "integer"):
            integer({"PORT": "nope"}, "PORT", default=123)

    def test_config_path_uses_beta5_canonical_precedence(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            result = config_path(
                {
                    "HOME": str(root),
                    "OPENCLAW_CONFIG": str(root / "ignored-legacy.json"),
                    "OPENCLAW_STATE_DIR": str(root / "state"),
                    "OPENCLAW_CONFIG_PATH": "~/explicit.json",
                }
            )
            self.assertEqual(result, root / "explicit.json")

    def test_config_path_falls_back_to_state_dir_then_openclaw_home(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            self.assertEqual(
                config_path({"OPENCLAW_STATE_DIR": str(root / "state")}),
                root / "state" / "openclaw.json",
            )
            self.assertEqual(
                config_path({"OPENCLAW_HOME": str(root / "home")}),
                root / "home" / ".openclaw" / "openclaw.json",
            )

    def test_default_config_and_workspace_are_derived_from_injected_home(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            destination = config_path({"HOME": str(root)})
            self.assertEqual(destination, root / ".openclaw" / "openclaw.json")
            self.assertEqual(
                workspace_path({"HOME": str(root)}, destination),
                root / ".openclaw" / "workspace",
            )

    def test_reverse_api_key_aliases_remain_in_memory(self) -> None:
        original = {
            "OPENAI_V1_KEY": "first-secret",
            "OPENAI_V1_API_KEY_ALIAS": "GEMINI_API_KEY",
            "OPENAI_V1_KEY_2": "second-secret",
            "OPENAI_V1_API_KEY_ALIAS_2": "XAI_API_KEY",
        }
        expanded = expand_api_key_aliases(original)
        self.assertEqual(expanded["GEMINI_API_KEY"], "first-secret")
        self.assertEqual(expanded["XAI_API_KEY"], "second-secret")
        self.assertNotIn("GEMINI_API_KEY", original)

    def test_explicit_alias_value_wins(self) -> None:
        expanded = expand_api_key_aliases(
            {
                "OPENAI_V1_KEY": "custom-secret",
                "OPENAI_V1_API_KEY_ALIAS": "GEMINI_API_KEY",
                "GEMINI_API_KEY": "native-secret",
            }
        )
        self.assertEqual(expanded["GEMINI_API_KEY"], "native-secret")

    def test_blank_explicit_alias_is_filled(self) -> None:
        expanded = expand_api_key_aliases(
            {
                "OPENAI_V1_KEY": "custom-secret",
                "OPENAI_V1_API_KEY_ALIAS": "GEMINI_API_KEY",
                "GEMINI_API_KEY": "",
            }
        )
        self.assertEqual(expanded["GEMINI_API_KEY"], "custom-secret")

    def test_alias_must_be_a_recognized_api_key_shape(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "uppercase"):
            expand_api_key_aliases(
                {
                    "OPENAI_V1_KEY": "secret",
                    "OPENAI_V1_API_KEY_ALIAS": "arbitrary",
                }
            )

    def test_secret_ref_never_contains_a_value(self) -> None:
        self.assertEqual(
            secret_ref("OPENCLAW_GATEWAY_TOKEN"),
            {
                "source": "env",
                "provider": "default",
                "id": "OPENCLAW_GATEWAY_TOKEN",
            },
        )
        with self.assertRaises(ConfigurationError):
            secret_ref("lowercase")

    def test_openclaw_bin_uses_shell_style_argv_without_a_shell(self) -> None:
        self.assertEqual(
            openclaw_command({"OPENCLAW_BIN": "node '/app/openclaw.mjs'"}),
            ["node", "/app/openclaw.mjs"],
        )
        self.assertEqual(openclaw_command({}), ["openclaw"])


if __name__ == "__main__":
    unittest.main()
