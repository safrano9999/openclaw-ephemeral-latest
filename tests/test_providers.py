from __future__ import annotations

import json
import subprocess
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openclaw_ephemeral.environment import ConfigurationError
from openclaw_ephemeral.providers import (
    OpenAIV1Provider,
    discover_native_models,
    discover_openai_v1_providers,
    normalize_openai_v1_url,
    select_openai_v1_default,
)


@dataclass
class Completed:
    returncode: int = 0
    stdout: str = "{}"
    stderr: str = ""


class FakeResponse:
    def __init__(self, payload: Any) -> None:
        self.payload = json.dumps(payload).encode("utf-8")
        self.closed = False

    def read(self, _size: int = -1) -> bytes:
        return self.payload

    def close(self) -> None:
        self.closed = True


class NativeProviderDiscoveryTests(unittest.TestCase):
    def test_cli_maps_only_recognized_injected_api_keys_to_available_models(self) -> None:
        calls: list[tuple[list[str], dict[str, str]]] = []

        def runner(command: list[str], **kwargs: Any) -> Completed:
            environment = kwargs["env"]
            calls.append((command, environment))
            self.assertNotEqual(environment["HOME"], "/sensitive/home")
            self.assertNotIn("OPENCLAW_CONFIG", environment)
            self.assertEqual(environment["OPENCLAW_HOME"], environment["HOME"])
            self.assertTrue(
                environment["OPENCLAW_CONFIG_PATH"].endswith("/state/openclaw.json")
            )
            self.assertTrue(Path(environment["OPENCLAW_CONFIG_PATH"]).is_file())
            if command[-3:] == ["models", "status", "--json"]:
                return Completed(
                    stdout=json.dumps(
                        {
                            "auth": {
                                "providers": [
                                    {
                                        "provider": "anthropic",
                                        "env": {
                                            "source": "shell env: ANTHROPIC_API_KEY"
                                        },
                                    },
                                    {
                                        "provider": "claude-cli",
                                        "env": {
                                            "source": "env: ANTHROPIC_API_KEY"
                                        },
                                    },
                                    {
                                        "provider": "unrelated",
                                        "env": {"source": "env: OTHER_API_KEY"},
                                    },
                                ]
                            }
                        }
                    )
                )
            provider = command[command.index("--provider") + 1]
            rows = {
                "anthropic": [
                    {
                        "key": "anthropic/claude-a",
                        "available": True,
                        "missing": False,
                    },
                    {
                        "key": "anthropic/unavailable",
                        "available": False,
                        "missing": True,
                    },
                ],
                "claude-cli": [
                    {
                        "id": "claude-b",
                        "available": True,
                        "missing": False,
                    }
                ],
            }
            return Completed(stdout=json.dumps({"models": rows[provider]}))

        models, warnings = discover_native_models(
            {
                "HOME": "/sensitive/home",
                "ANTHROPIC_API_KEY": "native-secret",
                "UNSET_API_KEY": "",
                "OPENCLAW_BIN": "node /app/openclaw.mjs",
            },
            runner=runner,
        )

        self.assertEqual(
            models,
            ("anthropic/claude-a", "claude-cli/claude-b"),
        )
        self.assertEqual(warnings, ())
        self.assertEqual(calls[0][0][:2], ["node", "/app/openclaw.mjs"])
        self.assertEqual(len(calls), 3)

    def test_cli_output_parser_tolerates_a_banner(self) -> None:
        def runner(command: list[str], **_kwargs: Any) -> Completed:
            if "status" in command:
                payload = {
                    "auth": {
                        "providers": [
                            {
                                "provider": "gemini",
                                "env": {"source": "env: GEMINI_API_KEY"},
                            }
                        ]
                    }
                }
            else:
                payload = {
                    "models": [
                        {
                            "ref": "gemini/gemini-test",
                            "available": True,
                            "missing": False,
                        }
                    ]
                }
            return Completed(stdout="OpenClaw banner\n" + json.dumps(payload))

        models, warnings = discover_native_models(
            {"GEMINI_API_KEY": "native-secret"},
            runner=runner,
        )
        self.assertEqual(models, ("gemini/gemini-test",))
        self.assertEqual(warnings, ())

    def test_unknown_key_reports_only_a_generic_warning(self) -> None:
        def runner(_command: list[str], **_kwargs: Any) -> Completed:
            return Completed(stdout=json.dumps({"auth": {"providers": []}}))

        models, warnings = discover_native_models(
            {"MYSTERY_API_KEY": "must-never-appear"},
            runner=runner,
        )
        self.assertEqual(models, ())
        self.assertEqual(len(warnings), 1)
        self.assertNotIn("must-never-appear", warnings[0])

    def test_cli_failure_is_non_fatal(self) -> None:
        def runner(_command: list[str], **_kwargs: Any) -> Completed:
            raise subprocess.TimeoutExpired("openclaw", 1)

        models, warnings = discover_native_models(
            {"ANTHROPIC_API_KEY": "native-secret"},
            runner=runner,
        )
        self.assertEqual(models, ())
        self.assertTrue(warnings)

    def test_no_keys_means_no_cli_process(self) -> None:
        def runner(_command: list[str], **_kwargs: Any) -> Completed:
            self.fail("runner must not be called without an injected API key")

        self.assertEqual(discover_native_models({}, runner=runner), ((), ()))


class OpenAIV1DiscoveryTests(unittest.TestCase):
    def test_url_normalization_adds_v1_and_handles_ports(self) -> None:
        self.assertEqual(
            normalize_openai_v1_url("localhost", "4000"),
            "http://localhost:4000/v1",
        )
        self.assertEqual(
            normalize_openai_v1_url("https://example.test:8443/v1", "443"),
            "https://example.test:8443/v1",
        )
        self.assertEqual(
            normalize_openai_v1_url("http://[::1]", "4000"),
            "http://[::1]:4000/v1",
        )

    def test_url_rejects_embedded_credentials(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "credentials"):
            normalize_openai_v1_url("https://user:secret@example.test", "443")

    def test_repeated_groups_call_models_endpoint_and_keep_only_secret_refs(self) -> None:
        requests: list[tuple[str, str, float]] = []
        responses: list[FakeResponse] = []

        def opener(request: Any, *, timeout: float) -> FakeResponse:
            response = FakeResponse(
                {
                    "data": [
                        {"id": "model-z"},
                        {"id": "model-a"},
                        {"id": "model-a"},
                        {"wrong": "ignored"},
                    ]
                }
            )
            responses.append(response)
            requests.append(
                (
                    request.full_url,
                    request.get_header("Authorization"),
                    timeout,
                )
            )
            return response

        providers, warnings = discover_openai_v1_providers(
            {
                "OPENAI_V1_PROVIDER": "LiteLLM Main",
                "OPENAI_V1_URL": "https://one.test",
                "OPENAI_V1_PORT": "443",
                "OPENAI_V1_KEY": "first-secret",
                "OPENAI_V1_STREAM": "true",
                "OPENAI_V1_PROVIDER_2": "LiteLLM Main",
                "OPENAI_V1_URL_2": "http://127.0.0.1",
                "OPENAI_V1_PORT_2": "4000",
                "OPENAI_V1_KEY_2": "second-secret",
            },
            opener=opener,
            timeout=3.5,
        )

        self.assertEqual(warnings, ())
        self.assertEqual(
            [provider.provider_id for provider in providers],
            ["litellm_main", "litellm_main_2"],
        )
        self.assertEqual(providers[0].models, ("model-a", "model-z"))
        self.assertTrue(providers[0].streaming)
        self.assertFalse(providers[1].streaming)
        self.assertEqual(
            [item[0] for item in requests],
            [
                "https://one.test:443/v1/models",
                "http://127.0.0.1:4000/v1/models",
            ],
        )
        self.assertEqual(requests[0][1], "Bearer first-secret")
        self.assertEqual(requests[1][1], "Bearer second-secret")
        self.assertTrue(all(response.closed for response in responses))

        serialized = json.dumps(providers[0].openclaw_config())
        self.assertNotIn("first-secret", serialized)
        self.assertIn('"id": "OPENAI_V1_KEY"', serialized)

    def test_quoted_env_file_values_match_existing_parser(self) -> None:
        captured: list[tuple[str, str | None]] = []

        def opener(request: Any, *, timeout: float) -> FakeResponse:
            del timeout
            captured.append(
                (
                    request.full_url,
                    request.get_header("Authorization"),
                )
            )
            return FakeResponse({"data": []})

        providers, _ = discover_openai_v1_providers(
            {
                "OPENAI_V1_PROVIDER": '"LiteLLM"',
                "OPENAI_V1_URL": "'http://127.0.0.1'",
                "OPENAI_V1_PORT": '"4000"',
                "OPENAI_V1_KEY": "'quoted-secret'",
                "OPENAI_V1_STREAM": '"false"',
            },
            opener=opener,
        )

        self.assertEqual(providers[0].provider_id, "litellm")
        self.assertEqual(providers[0].base_url, "http://127.0.0.1:4000/v1")
        self.assertEqual(
            captured,
            [("http://127.0.0.1:4000/v1/models", "Bearer quoted-secret")],
        )

    def test_discovery_failure_keeps_provider_without_leaking_exception(self) -> None:
        def opener(_request: Any, *, timeout: float) -> FakeResponse:
            del timeout
            raise OSError("server reflected secret-value")

        providers, warnings = discover_openai_v1_providers(
            {
                "OPENAI_V1_URL": "https://one.test",
                "OPENAI_V1_KEY": "secret-value",
            },
            opener=opener,
        )
        self.assertEqual(len(providers), 1)
        self.assertEqual(providers[0].models, ())
        self.assertEqual(len(warnings), 1)
        self.assertNotIn("secret-value", warnings[0])

    def test_configured_group_requires_key(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "OPENAI_V1_KEY"):
            discover_openai_v1_providers(
                {"OPENAI_V1_URL": "https://one.test"},
                opener=lambda *_args, **_kwargs: FakeResponse({"data": []}),
            )

    def test_default_selection_supports_provider_prefix_and_injects_missing_model(self) -> None:
        provider = OpenAIV1Provider(
            index=1,
            provider_id="litellm",
            configured_name="LiteLLM",
            base_url="http://localhost:4000/v1",
            key_env="OPENAI_V1_KEY",
            models=("known",),
            streaming=False,
        )
        selected = select_openai_v1_default((provider,), "litellm/new-model")
        self.assertIsNotNone(selected)
        full_model, providers = selected or ("", ())
        self.assertEqual(full_model, "litellm/new-model")
        self.assertEqual(providers[0].models, ("new-model", "known"))


if __name__ == "__main__":
    unittest.main()
