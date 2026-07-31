"""Native and OpenAI-v1 model-provider discovery."""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

from .environment import (
    ConfigurationError,
    clean,
    floating,
    openclaw_command,
    secret_ref,
)


MAX_DISCOVERY_RESPONSE_BYTES = 8 * 1024 * 1024


def _clean_openai_v1(value: str | None) -> str:
    """Match the established SOT parser for quoted env-file values."""

    return (value or "").strip().strip('"').strip("'")


@dataclass(frozen=True)
class OpenAIV1Provider:
    """A custom OpenAI-compatible provider represented without its secret."""

    index: int
    provider_id: str
    configured_name: str
    base_url: str
    key_env: str
    models: tuple[str, ...]
    streaming: bool

    def openclaw_config(
        self,
        *,
        context_window: int = 128_000,
        max_tokens: int = 8_192,
    ) -> dict[str, Any]:
        """Return this provider's SecretRef-backed OpenClaw config."""

        return {
            "baseUrl": self.base_url,
            "api": "openai-completions",
            "apiKey": secret_ref(self.key_env),
            "request": {"allowPrivateNetwork": True},
            "models": [
                {
                    "id": model,
                    "name": model,
                    "reasoning": True,
                    "input": ["text"],
                    "contextWindow": context_window,
                    "maxTokens": max_tokens,
                }
                for model in self.models
            ],
        }


def _json_stdout(result: Any) -> Any:
    if getattr(result, "returncode", 1) != 0:
        return {}
    output = getattr(result, "stdout", "")
    if not isinstance(output, str):
        return {}
    output = output.strip()
    if not output:
        return {}
    try:
        return json.loads(output)
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        for index, character in enumerate(output):
            if character not in "[{":
                continue
            try:
                payload, _ = decoder.raw_decode(output[index:])
            except json.JSONDecodeError:
                continue
            return payload
    return {}


def _run_openclaw_json(
    arguments: Sequence[str],
    *,
    environ: Mapping[str, str],
    runner: Callable[..., Any],
    timeout: float,
) -> Any:
    try:
        result = runner(
            [*openclaw_command(environ), *arguments],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=dict(environ),
        )
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return {}
    return _json_stdout(result)


def _catalog_rows(payload: Any) -> list[Mapping[str, Any]]:
    if isinstance(payload, Mapping):
        rows = payload.get("models", [])
    else:
        rows = payload
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, Mapping)]


def _model_key(row: Mapping[str, Any], provider: str) -> str:
    for field in ("key", "ref", "model"):
        value = row.get(field)
        if isinstance(value, str) and "/" in value:
            return value.strip()
    identifier = row.get("id")
    if isinstance(identifier, str) and identifier.strip():
        identifier = identifier.strip()
        return identifier if "/" in identifier else f"{provider}/{identifier}"
    return ""


_ENV_SOURCE_LABEL = re.compile(
    r"^(?:shell\s+)?env:\s*([A-Z][A-Z0-9_]*)$",
    re.IGNORECASE,
)


def _env_source_ids(value: Any) -> set[str]:
    """Extract exact environment IDs from old and Beta 5 status shapes."""

    if isinstance(value, str):
        match = _ENV_SOURCE_LABEL.fullmatch(value.strip())
        return {match.group(1).upper()} if match else set()
    if isinstance(value, Mapping):
        found: set[str] = set()
        source = clean(
            value.get("source") if isinstance(value.get("source"), str) else ""
        )
        if source.lower() == "env":
            for field in ("id", "name", "envVar", "envVarName"):
                candidate = value.get(field)
                if isinstance(candidate, str) and re.fullmatch(
                    r"[A-Z][A-Z0-9_]*",
                    candidate.strip(),
                    re.IGNORECASE,
                ):
                    found.add(candidate.strip().upper())
        for field in ("source", "env", "effective", "resolved", "credential"):
            if field in value:
                found.update(_env_source_ids(value[field]))
        return found
    if isinstance(value, (list, tuple)):
        found: set[str] = set()
        for item in value:
            found.update(_env_source_ids(item))
        return found
    return set()


def discover_native_models(
    environ: Mapping[str, str],
    *,
    runner: Callable[..., Any] = subprocess.run,
    timeout: float | None = None,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Use OpenClaw itself to map injected *_API_KEY variables to models.

    The command receives a new temporary HOME, state directory, and empty config.
    It therefore cannot read or migrate the destination config or any prior
    OpenClaw state.
    """

    command_timeout = timeout or floating(
        environ,
        "OPENCLAW_DISCOVERY_TIMEOUT",
        default=15.0,
    )
    configured_keys = {
        name
        for name, value in environ.items()
        if name.endswith("_API_KEY") and clean(value)
    }
    if not configured_keys:
        return (), ()

    warnings: list[str] = []
    models: set[str] = set()
    with tempfile.TemporaryDirectory(prefix="openclaw-ephemeral-discovery-") as raw_dir:
        root = Path(raw_dir)
        state = root / "state"
        home = root / "home"
        state.mkdir(mode=0o700)
        home.mkdir(mode=0o700)
        scratch_config = state / "openclaw.json"
        scratch_config.write_text("{}\n", encoding="utf-8")

        isolated = dict(environ)
        isolated.update(
            {
                "HOME": str(home),
                "OPENCLAW_HOME": str(home),
                "OPENCLAW_STATE_DIR": str(state),
                "OPENCLAW_CONFIG_PATH": str(scratch_config),
            }
        )
        isolated.pop("OPENCLAW_AGENT_DIR", None)

        status = _run_openclaw_json(
            ("models", "status", "--json"),
            environ=isolated,
            runner=runner,
            timeout=command_timeout,
        )
        auth = status.get("auth", {}) if isinstance(status, Mapping) else {}
        provider_rows = auth.get("providers", []) if isinstance(auth, Mapping) else []
        providers: set[str] = set()
        if isinstance(provider_rows, list):
            for row in provider_rows:
                if not isinstance(row, Mapping):
                    continue
                provider = clean(
                    row.get("provider") if isinstance(row.get("provider"), str) else ""
                )
                if provider and configured_keys.intersection(_env_source_ids(row)):
                    providers.add(provider)

        # OpenClaw 2026.7.1 does not consistently advertise this provider in
        # status output, although its model catalog recognizes the env key.
        if clean(environ.get("SAKANA_API_KEY")):
            providers.add("sakana")

        if configured_keys and not providers:
            warnings.append(
                "OpenClaw did not recognize any injected *_API_KEY provider"
            )

        for provider in sorted(providers):
            catalog = _run_openclaw_json(
                ("models", "list", "--all", "--provider", provider, "--json"),
                environ=isolated,
                runner=runner,
                timeout=command_timeout,
            )
            for row in _catalog_rows(catalog):
                if row.get("available") is not True or row.get("missing") is not False:
                    continue
                key = _model_key(row, provider)
                if key:
                    models.add(key)

    return tuple(sorted(models)), tuple(warnings)


def _openai_v1_indexes(environ: Mapping[str, str]) -> list[int]:
    indexes = {1}
    pattern = re.compile(
        r"^OPENAI_V1_(?:PROVIDER|URL|PORT|KEY|API_KEY_ALIAS|STREAM)_(\d+)$"
    )
    for name in environ:
        match = pattern.fullmatch(name)
        if match:
            indexes.add(int(match.group(1)))
    return sorted(indexes)


def _group_name(
    environ: Mapping[str, str],
    field: str,
    index: int,
) -> str:
    if index == 1:
        return f"OPENAI_V1_{field}"
    candidates = (
        f"OPENAI_V1_{field}_{index}",
        f"OPENAI_V1_{field}_{index:02d}",
    )
    for candidate in candidates:
        if clean(environ.get(candidate)):
            return candidate
    for candidate in candidates:
        if candidate in environ:
            return candidate
    return candidates[0]


def _group_value(
    environ: Mapping[str, str],
    field: str,
    index: int,
) -> str:
    return _clean_openai_v1(environ.get(_group_name(environ, field, index)))


def _streaming_value(environ: Mapping[str, str], index: int) -> bool:
    name = _group_name(environ, "STREAM", index)
    raw = _clean_openai_v1(environ.get(name)).lower()
    if not raw:
        return False
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"{name} must be a boolean value")


def normalize_openai_v1_url(raw_url: str, raw_port: str = "") -> str:
    """Normalize one injected endpoint to an OpenAI-v1 API base URL."""

    value = clean(raw_url).rstrip("/")
    port = clean(raw_port)
    if not value:
        return ""
    if "://" not in value:
        value = f"http://{value}"
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ConfigurationError("OPENAI_V1_URL must be an HTTP(S) endpoint")
    if parsed.username is not None or parsed.password is not None:
        raise ConfigurationError("OPENAI_V1_URL must not contain credentials")

    try:
        parsed_port = parsed.port
    except ValueError as exc:
        raise ConfigurationError("OPENAI_V1_URL contains an invalid port") from exc
    if port:
        try:
            requested_port = int(port, 10)
        except ValueError as exc:
            raise ConfigurationError("OPENAI_V1_PORT must be an integer") from exc
        if not 1 <= requested_port <= 65_535:
            raise ConfigurationError("OPENAI_V1_PORT must be between 1 and 65535")
    else:
        requested_port = None

    host = parsed.hostname
    if ":" in host:
        host = f"[{host}]"
    effective_port = parsed_port if parsed_port is not None else requested_port
    netloc = host if effective_port is None else f"{host}:{effective_port}"
    path = parsed.path.rstrip("/")
    if not path:
        path = "/v1"
    return urlunsplit((parsed.scheme, netloc, path, "", ""))


def _provider_id(raw: str, index: int, used: set[str]) -> str:
    fallback = "openai_v1" if index == 1 else f"openai_v1_{index}"
    candidate = re.sub(r"[^a-z0-9._-]+", "_", raw.lower()).strip("._-")
    candidate = candidate or fallback
    if candidate in used:
        candidate = f"{candidate}_{index}"
    counter = 2
    unique = candidate
    while unique in used:
        unique = f"{candidate}_{counter}"
        counter += 1
    used.add(unique)
    return unique


def _models_endpoint(base_url: str) -> str:
    return f"{base_url.rstrip('/')}/models"


def _read_models_response(response: Any) -> tuple[str, ...]:
    payload = response.read(MAX_DISCOVERY_RESPONSE_BYTES + 1)
    if len(payload) > MAX_DISCOVERY_RESPONSE_BYTES:
        raise ValueError("model discovery response is too large")
    decoded = json.loads(payload.decode("utf-8"))
    rows = decoded.get("data", []) if isinstance(decoded, Mapping) else []
    if not isinstance(rows, list):
        return ()
    model_ids = {
        clean(row.get("id"))
        for row in rows
        if isinstance(row, Mapping) and isinstance(row.get("id"), str)
    }
    return tuple(sorted(model_id for model_id in model_ids if model_id))


def _discover_openai_v1_models(
    base_url: str,
    *,
    key: str,
    opener: Callable[..., Any],
    timeout: float,
) -> tuple[str, ...]:
    request = Request(
        _models_endpoint(base_url),
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {key}",
        },
        method="GET",
    )
    response = opener(request, timeout=timeout)
    close = getattr(response, "close", None)
    try:
        return _read_models_response(response)
    finally:
        if callable(close):
            close()


def discover_openai_v1_providers(
    environ: Mapping[str, str],
    *,
    opener: Callable[..., Any] = urlopen,
    timeout: float | None = None,
) -> tuple[tuple[OpenAIV1Provider, ...], tuple[str, ...]]:
    """Discover every repeated OPENAI_V1 group through its /v1/models API."""

    request_timeout = timeout or floating(
        environ,
        "OPENCLAW_DISCOVERY_TIMEOUT",
        default=5.0,
    )
    providers: list[OpenAIV1Provider] = []
    warnings: list[str] = []
    used_ids: set[str] = set()

    for index in _openai_v1_indexes(environ):
        raw_url = _group_value(environ, "URL", index)
        if not raw_url:
            continue
        key_env = _group_name(environ, "KEY", index)
        key = _clean_openai_v1(environ.get(key_env))
        if not key:
            raise ConfigurationError(f"{key_env} must not be empty")
        configured_name = _group_value(environ, "PROVIDER", index)
        provider_id = _provider_id(configured_name, index, used_ids)
        base_url = normalize_openai_v1_url(
            raw_url,
            _group_value(environ, "PORT", index),
        )
        try:
            models = _discover_openai_v1_models(
                base_url,
                key=key,
                opener=opener,
                timeout=request_timeout,
            )
        except Exception:
            models = ()
            warnings.append(
                f"OpenAI-v1 model discovery failed for provider {provider_id}"
            )
        providers.append(
            OpenAIV1Provider(
                index=index,
                provider_id=provider_id,
                configured_name=configured_name,
                base_url=base_url,
                key_env=key_env,
                models=models,
                streaming=_streaming_value(environ, index),
            )
        )

    return tuple(providers), tuple(warnings)


def select_openai_v1_default(
    providers: Sequence[OpenAIV1Provider],
    configured_model: str,
) -> tuple[str, tuple[OpenAIV1Provider, ...]] | None:
    """Resolve the established default-model variable against repeated groups."""

    wanted = clean(configured_model)
    if not wanted or not providers:
        return None

    selected: OpenAIV1Provider | None = None
    selected_model = wanted
    for provider in providers:
        aliases = {provider.provider_id}
        if provider.configured_name:
            aliases.add(provider.configured_name.lower())
        for alias in aliases:
            prefix = f"{alias}/"
            if wanted.lower().startswith(prefix):
                selected = provider
                selected_model = wanted[len(prefix) :].strip()
                break
        if selected is not None:
            break

    if selected is None:
        selected = next(
            (provider for provider in providers if wanted in provider.models),
            providers[0],
        )
    if not selected_model:
        raise ConfigurationError(
            "OPENCLAW_OPENAI_V1_DEFAULT_LLM must include a model name"
        )

    if selected_model not in selected.models:
        replacement = OpenAIV1Provider(
            index=selected.index,
            provider_id=selected.provider_id,
            configured_name=selected.configured_name,
            base_url=selected.base_url,
            key_env=selected.key_env,
            models=(selected_model, *selected.models),
            streaming=selected.streaming,
        )
        providers = tuple(
            replacement if provider == selected else provider
            for provider in providers
        )
        selected = replacement
    return f"{selected.provider_id}/{selected_model}", tuple(providers)
