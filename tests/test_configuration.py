from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openclaw_ephemeral.configuration import (
    DUMMY_MODEL,
    NOTE_MODEL,
    build_config,
    configure,
)
from openclaw_ephemeral.providers import OpenAIV1Provider


def provider(
    *,
    index: int = 1,
    provider_id: str = "litellm",
    models: tuple[str, ...] = ("model-a",),
    streaming: bool = False,
) -> OpenAIV1Provider:
    suffix = "" if index == 1 else f"_{index}"
    return OpenAIV1Provider(
        index=index,
        provider_id=provider_id,
        configured_name=provider_id,
        base_url=f"http://provider-{index}.test:4000/v1",
        key_env=f"OPENAI_V1_KEY{suffix}",
        models=models,
        streaming=streaming,
    )


class ConfigBuilderTests(unittest.TestCase):
    def test_minimal_config_has_both_deterministic_routes_and_main_agent(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            destination = root / "state" / "openclaw.json"
            config, primary, full_mode = build_config(
                {
                    "HOME": str(root),
                    "OPENCLAW_AGENT_WORKSPACE": str(root / "workspace"),
                },
                destination=destination,
            )

            self.assertEqual(primary, NOTE_MODEL)
            self.assertTrue(full_mode)
            self.assertEqual(
                config["agents"]["defaults"]["models"],
                {DUMMY_MODEL: {}, NOTE_MODEL: {}},
            )
            self.assertEqual(
                config["agents"]["defaults"]["model"]["primary"],
                NOTE_MODEL,
            )
            self.assertEqual(
                config["agents"]["defaults"]["sandbox"],
                {"mode": "off"},
            )
            self.assertEqual(
                config["agents"]["entries"]["main"]["tools"],
                {"allow": ["*"], "deny": []},
            )
            self.assertEqual(
                config["tools"],
                {
                    "profile": "full",
                    "fs": {"workspaceOnly": False},
                    "exec": {
                        "host": "gateway",
                        "mode": "full",
                        "applyPatch": {"workspaceOnly": False},
                    },
                },
            )
            main = config["agents"]["entries"]["main"]
            self.assertNotIn("id", main)
            self.assertTrue(main["default"])
            self.assertTrue(Path(main["workspace"]).is_dir())
            self.assertTrue(Path(main["agentDir"]).is_dir())

            note = config["plugins"]["entries"]["note"]
            self.assertEqual(
                note,
                {
                    "enabled": True,
                    "hooks": {"allowConversationAccess": True},
                },
            )
            self.assertTrue(config["plugins"]["entries"]["codex"]["enabled"])

    def test_note_full_mode_can_be_disabled_explicitly(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            destination = Path(raw) / "openclaw.json"
            config, primary, full_mode = build_config(
                {
                    "HOME": raw,
                    "OPENCLAW_NOTE_FULL_MODE": "0",
                },
                destination=destination,
            )

            self.assertFalse(full_mode)
            self.assertEqual(primary, DUMMY_MODEL)
            self.assertEqual(
                config["plugins"]["entries"]["note"],
                {"enabled": True},
            )
            self.assertIn(DUMMY_MODEL, config["agents"]["defaults"]["models"])
            self.assertIn(NOTE_MODEL, config["agents"]["defaults"]["models"])

    def test_installed_extensions_are_explicit_plugin_load_paths(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            destination = Path(raw) / "state" / "openclaw.json"
            extensions = destination.parent / "extensions"
            for name in ("note", "kachelmann"):
                plugin = extensions / name
                plugin.mkdir(parents=True)
                (plugin / "openclaw.plugin.json").write_text(
                    '{}\n',
                    encoding="utf-8",
                )
            (extensions / "not-a-plugin").mkdir()

            config, _, _ = build_config(
                {"HOME": raw},
                destination=destination,
            )

            self.assertEqual(
                config["plugins"]["load"]["paths"],
                [
                    str(extensions / "kachelmann"),
                    str(extensions / "note"),
                ],
            )

    def test_native_and_repeated_custom_models_remain_available(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            config, primary, _ = build_config(
                {
                    "HOME": raw,
                    "OPENCLAW_OPENAI_V1_DEFAULT_LLM": "second/model-c",
                },
                destination=Path(raw) / "openclaw.json",
                native_models=("anthropic/claude-a", "gemini/gemini-b"),
                openai_v1_providers=(
                    provider(models=("model-a",), streaming=True),
                    provider(
                        index=2,
                        provider_id="second",
                        models=("model-c",),
                    ),
                ),
            )

            self.assertEqual(primary, "second/model-c")
            allowlist = config["agents"]["defaults"]["models"]
            for model in (
                DUMMY_MODEL,
                NOTE_MODEL,
                "anthropic/claude-a",
                "gemini/gemini-b",
                "litellm/model-a",
                "second/model-c",
            ):
                self.assertIn(model, allowlist)
            self.assertEqual(allowlist["litellm/model-a"], {})
            self.assertEqual(
                set(config["models"]["providers"]),
                {"litellm", "second"},
            )

    def test_openclaw_model_overrides_openai_v1_default_and_is_allowlisted(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            config, primary, full_mode = build_config(
                {
                    "HOME": raw,
                    "OPENCLAW_MODEL": "anthropic/claude-explicit",
                    "OPENCLAW_OPENAI_V1_DEFAULT_LLM": "litellm/model-a",
                },
                destination=Path(raw) / "openclaw.json",
                native_models=("anthropic/claude-discovered",),
                openai_v1_providers=(provider(),),
            )

            self.assertEqual(primary, "anthropic/claude-explicit")
            self.assertTrue(full_mode)
            self.assertIn(
                "anthropic/claude-explicit",
                config["agents"]["defaults"]["models"],
            )
            self.assertIn(
                "litellm/model-a",
                config["agents"]["defaults"]["models"],
            )

    def test_openclaw_model_dummy_note_enables_full_mode_automatically(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            config, primary, full_mode = build_config(
                {
                    "HOME": raw,
                    "OPENCLAW_MODEL": NOTE_MODEL,
                },
                destination=Path(raw) / "openclaw.json",
            )

            self.assertEqual(primary, NOTE_MODEL)
            self.assertTrue(full_mode)
            self.assertEqual(
                config["plugins"]["entries"]["note"]["hooks"],
                {"allowConversationAccess": True},
            )

    def test_openclaw_model_adds_missing_custom_model_to_provider_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            config, primary, _ = build_config(
                {
                    "HOME": raw,
                    "OPENCLAW_MODEL": "litellm/not-discovered",
                },
                destination=Path(raw) / "openclaw.json",
                openai_v1_providers=(provider(),),
            )

            self.assertEqual(primary, "litellm/not-discovered")
            model_ids = [
                item["id"]
                for item in config["models"]["providers"]["litellm"]["models"]
            ]
            self.assertEqual(model_ids, ["not-discovered", "model-a"])

    def test_gateway_telegram_and_origins_use_environment_refs(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            secrets = {
                "gateway": "gateway-secret",
                "telegram": "telegram-secret",
            }
            config, _, _ = build_config(
                {
                    "HOME": raw,
                    "FASTAPI_HOST": "10.0.0.5",
                    "OPENCLAW_GATEWAY_PORT": "19000",
                    "OPENCLAW_GATEWAY_PUBLISH_PORT": "29000",
                    "OPENCLAW_GATEWAY_TOKEN": secrets["gateway"],
                    "OPENCLAW_TELEGRAMTOKEN": secrets["telegram"],
                },
                destination=Path(raw) / "openclaw.json",
            )

            gateway = config["gateway"]
            self.assertEqual(gateway["port"], 19000)
            self.assertEqual(
                gateway["auth"]["token"]["id"],
                "OPENCLAW_GATEWAY_TOKEN",
            )
            self.assertNotIn("allowInsecureAuth", gateway["controlUi"])
            self.assertNotIn("dangerouslyDisableDeviceAuth", gateway["controlUi"])
            telegram = config["channels"]["telegram"]
            self.assertEqual(
                telegram["accounts"]["default"]["botToken"]["id"],
                "OPENCLAW_TELEGRAMTOKEN",
            )
            self.assertEqual(
                telegram["defaultAccount"],
                "default",
            )
            self.assertEqual(telegram["streaming"], {"mode": "off"})
            self.assertEqual(
                telegram["accounts"]["default"]["streaming"],
                {"mode": "partial"},
            )
            self.assertNotIn("bindings", config)
            serialized = json.dumps(config)
            self.assertNotIn(secrets["gateway"], serialized)
            self.assertNotIn(secrets["telegram"], serialized)

    def test_custom_provider_secret_is_only_an_env_reference(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            config, _, _ = build_config(
                {"HOME": raw},
                destination=Path(raw) / "openclaw.json",
                openai_v1_providers=(provider(),),
            )
            key = config["models"]["providers"]["litellm"]["apiKey"]
            self.assertEqual(
                key,
                {
                    "source": "env",
                    "provider": "default",
                    "id": "OPENAI_V1_KEY",
                },
            )


class CompleteConfigureTests(unittest.TestCase):
    def test_existing_openai_oauth_database_enables_openai_models(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            destination = Path(raw) / "state" / "openclaw.json"
            auth_db = (
                destination.parent
                / "agents"
                / "main"
                / "agent"
                / "openclaw-agent.sqlite"
            )
            auth_db.parent.mkdir(parents=True)
            auth_db.write_bytes(b"oauth-state")

            with (
                patch(
                    "openclaw_ephemeral.configuration.discover_native_models",
                    return_value=((), ()),
                ),
                patch(
                    "openclaw_ephemeral.configuration.discover_openai_v1_providers",
                    return_value=((), ()),
                ),
            ):
                configure(
                    {
                        "HOME": raw,
                        "OPENCLAW_CONFIG_PATH": str(destination),
                    }
                )

            written = json.loads(destination.read_text(encoding="utf-8"))
            self.assertIn(
                "openai/*",
                written["agents"]["defaults"]["models"],
            )

    def test_configure_replaces_destination_without_reading_or_merging_it(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            destination = root / "state" / "openclaw.json"
            destination.parent.mkdir()
            destination.write_text(
                '{"discarded": true, "plaintext": "old-secret"}\n',
                encoding="utf-8",
            )
            environ = {
                "HOME": str(root),
                "OPENCLAW_CONFIG_PATH": str(destination),
                "OPENCLAW_GATEWAY_TOKEN": "new-gateway-secret",
                "ANTHROPIC_API_KEY": "native-provider-secret",
            }

            with (
                patch(
                    "openclaw_ephemeral.configuration.discover_native_models",
                    return_value=(("anthropic/claude-a",), ()),
                ) as native,
                patch(
                    "openclaw_ephemeral.configuration.discover_openai_v1_providers",
                    return_value=((), ()),
                ) as custom,
            ):
                result = configure(environ)

            written = json.loads(destination.read_text(encoding="utf-8"))
            self.assertNotIn("discarded", written)
            self.assertNotIn("plaintext", written)
            serialized = destination.read_text(encoding="utf-8")
            self.assertNotIn("old-secret", serialized)
            self.assertNotIn("new-gateway-secret", serialized)
            self.assertNotIn("native-provider-secret", serialized)
            self.assertEqual(result.native_model_count, 1)
            self.assertEqual(
                stat.S_IMODE(destination.stat().st_mode),
                0o600,
            )
            self.assertEqual(list(destination.parent.glob("*.tmp")), [])
            native.assert_called_once()
            custom.assert_called_once()

    def test_configure_reports_discovery_counts_and_generic_warnings(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            destination = Path(raw) / "openclaw.json"
            with (
                patch(
                    "openclaw_ephemeral.configuration.discover_native_models",
                    return_value=(("gemini/model",), ("native warning",)),
                ),
                patch(
                    "openclaw_ephemeral.configuration.discover_openai_v1_providers",
                    return_value=(
                        (provider(models=("one", "two")),),
                        ("custom warning",),
                    ),
                ),
            ):
                result = configure(
                    {
                        "HOME": raw,
                        "OPENCLAW_CONFIG_PATH": str(destination),
                        "OPENAI_V1_KEY": "custom-secret",
                    }
                )

            self.assertEqual(result.native_model_count, 1)
            self.assertEqual(result.openai_v1_provider_count, 1)
            self.assertEqual(result.openai_v1_model_count, 2)
            self.assertEqual(
                result.warnings,
                ("native warning", "custom warning"),
            )
            self.assertNotIn(
                "custom-secret",
                destination.read_text(encoding="utf-8"),
            )


if __name__ == "__main__":
    unittest.main()
