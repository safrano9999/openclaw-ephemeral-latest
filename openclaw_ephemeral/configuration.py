"""Construct a complete OpenClaw config exclusively from injected state."""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.request import urlopen

from .environment import (
    agent_dir_path,
    boolean,
    clean,
    config_path,
    expand_api_key_aliases,
    integer,
    secret_ref,
    workspace_path,
    without_secret_values,
)
from .filesystem import atomic_write_json
from .providers import (
    OpenAIV1Provider,
    discover_native_models,
    discover_openai_v1_providers,
    select_openai_v1_default,
)


DUMMY_MODEL = "dummy/dummy"
NOTE_MODEL = "dummy/note"


@dataclass(frozen=True)
class ConfigurationResult:
    """Non-sensitive summary of one complete configuration rebuild."""

    path: Path
    primary_model: str
    native_model_count: int
    openai_v1_provider_count: int
    openai_v1_model_count: int
    telegram_configured: bool
    note_full_mode: bool
    warnings: tuple[str, ...]


def _origin(host: str, port: int) -> str:
    host = host.strip() or "127.0.0.1"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"http://{host}:{port}"


def _tailscale_hosts(
    environ: Mapping[str, str],
    *,
    runner: Callable[..., Any],
) -> list[str]:
    ts_hostname = clean(environ.get("TS_HOSTNAME")).rstrip(".")
    if not ts_hostname:
        return []

    hosts: list[str] = []
    try:
        result = runner(
            ["tailscale", "status", "--json"],
            check=True,
            capture_output=True,
            text=True,
            timeout=3,
        )
        payload = json.loads(result.stdout)
    except (
        FileNotFoundError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        json.JSONDecodeError,
    ):
        payload = {}

    self_info = payload.get("Self") if isinstance(payload, dict) else {}
    if isinstance(self_info, dict):
        dns_name = clean(self_info.get("DNSName")).rstrip(".")
        if dns_name:
            hosts.append(dns_name)
        for ip_addr in self_info.get("TailscaleIPs") or []:
            ip_addr = clean(ip_addr)
            if ip_addr:
                hosts.append(ip_addr)
    if "." in ts_hostname:
        hosts.append(ts_hostname)
    return list(dict.fromkeys(hosts))


def _gateway_config(
    environ: Mapping[str, str],
    *,
    tailscale_runner: Callable[..., Any],
) -> dict[str, Any]:
    port = integer(
        environ,
        "OPENCLAW_GATEWAY_PORT",
        default=18_789,
        maximum=65_535,
    )
    publish_port = integer(
        environ,
        "OPENCLAW_GATEWAY_PUBLISH_PORT",
        default=20_789,
        maximum=65_535,
    )
    host = clean(environ.get("FASTAPI_HOST")) or "127.0.0.1"
    origins = [
        _origin(host, port),
        _origin("127.0.0.1", port),
        _origin("localhost", port),
        _origin(host, publish_port),
    ]
    for tailscale_host in _tailscale_hosts(environ, runner=tailscale_runner):
        origins.extend(
            [
                _origin(tailscale_host, port),
                _origin(tailscale_host, publish_port),
            ]
        )
    origins = list(dict.fromkeys(origins))

    gateway: dict[str, Any] = {
        "mode": "local",
        "bind": "lan",
        "port": port,
        "controlUi": {
            "allowedOrigins": origins,
        },
    }
    gateway["auth"] = {
        "mode": "token",
        "token": secret_ref("OPENCLAW_GATEWAY_TOKEN"),
    }
    return gateway


def _main_agent_config(
    environ: Mapping[str, str],
    destination: Path,
    primary_model: str,
    model_allowlist: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    workspace = workspace_path(environ, destination)
    agent_dir = agent_dir_path(environ, destination)
    workspace.mkdir(parents=True, exist_ok=True)
    agent_dir.mkdir(parents=True, exist_ok=True)
    defaults = {
        "workspace": str(workspace),
        "model": {"primary": primary_model},
        "models": model_allowlist,
        "modelPolicy": {"allow": list(model_allowlist)},
        "sandbox": {"mode": "off"},
    }
    main = {
        "name": "main",
        "default": True,
        "workspace": str(workspace),
        "agentDir": str(agent_dir),
        "heartbeat": {
            "every": "360m",
            "target": "last",
            "directPolicy": "allow",
        },
        "tools": {"allow": ["*"], "deny": []},
    }
    return {"defaults": defaults, "entries": {"main": main}}


def _telegram_config(environ: Mapping[str, str]) -> dict[str, Any]:
    if not clean(environ.get("OPENCLAW_TELEGRAMTOKEN")):
        return {}
    telegram = {
        "enabled": True,
        "dmPolicy": "open",
        "allowFrom": ["*"],
        "groupPolicy": "open",
        "groupAllowFrom": ["*"],
        "groups": {"*": {"requireMention": False}},
        "capabilities": {"inlineButtons": "dm"},
        "commands": {"native": True, "nativeSkills": False},
        "streaming": {"mode": "off"},
        "execApprovals": {
            "enabled": False,
            "approvers": [],
            "agentFilter": ["main"],
            "target": "dm",
        },
        "network": {
            "autoSelectFamily": False,
            "dnsResultOrder": "ipv4first",
        },
        "accounts": {
            "default": {
                "name": "main",
                "enabled": True,
                "dmPolicy": "open",
                "allowFrom": ["*"],
                "botToken": secret_ref("OPENCLAW_TELEGRAMTOKEN"),
                "groupPolicy": "open",
                "groupAllowFrom": ["*"],
                "streaming": {"mode": "partial"},
            }
        },
        "defaultAccount": "default",
    }
    config: dict[str, Any] = {"channels": {"telegram": telegram}}
    owner = clean(environ.get("OPENCLAW_TELEGRAM_CHAT_ID"))
    if owner:
        owner_sender = owner if owner.startswith("telegram:") else f"telegram:{owner}"
        config["commands"] = {"ownerAllowFrom": [owner_sender]}
    return config


def _extension_load_paths(destination: Path) -> list[str]:
    """Return installed OpenClaw extensions in deterministic load order."""

    extensions_dir = destination.parent / "extensions"
    try:
        candidates = sorted(extensions_dir.iterdir(), key=lambda path: path.name)
    except FileNotFoundError:
        return []
    return [
        str(path)
        for path in candidates
        if path.is_dir() and (path / "openclaw.plugin.json").is_file()
    ]


def _plugins_config(note_full_mode: bool, destination: Path) -> dict[str, Any]:
    # The deterministic OpenClaw patch supplies dummy/dummy. The separately
    # installed NOTE extension supplies dummy/note and its direct-capture hook.
    note: dict[str, Any] = {"enabled": True}
    if note_full_mode:
        note["hooks"] = {"allowConversationAccess": True}
    plugins: dict[str, Any] = {
        "entries": {
            "codex": {"enabled": True},
            "note": note,
        }
    }
    load_paths = _extension_load_paths(destination)
    if load_paths:
        plugins["load"] = {"paths": load_paths}
    return plugins


def _trusted_container_tools() -> dict[str, Any]:
    """Return the established trusted-container policy used by existing images."""

    return {
        "profile": "full",
        "fs": {"workspaceOnly": False},
        "exec": {
            "host": "gateway",
            "mode": "full",
            "applyPatch": {"workspaceOnly": False},
        },
    }


def _models_config(
    providers: Sequence[OpenAIV1Provider],
) -> dict[str, Any]:
    if not providers:
        return {}
    return {
        "mode": "merge",
        "providers": {
            provider.provider_id: provider.openclaw_config()
            for provider in providers
        },
    }


def _model_allowlist(
    native_models: Sequence[str],
    providers: Sequence[OpenAIV1Provider],
    explicit_model: str = "",
) -> dict[str, dict[str, Any]]:
    models: dict[str, dict[str, Any]] = {
        DUMMY_MODEL: {},
        NOTE_MODEL: {},
    }
    for model in sorted(set(native_models)):
        models[model] = {}
    for provider in providers:
        for model in provider.models:
            models[f"{provider.provider_id}/{model}"] = {}
    if explicit_model:
        models[explicit_model] = {}
    return models


def _select_explicit_custom_model(
    providers: Sequence[OpenAIV1Provider],
    explicit_model: str,
) -> tuple[str, tuple[OpenAIV1Provider, ...]] | None:
    """Resolve an explicit model only when it belongs to a custom provider."""

    wanted = clean(explicit_model)
    if not wanted:
        return None
    for provider in providers:
        aliases = {provider.provider_id}
        if provider.configured_name:
            aliases.add(provider.configured_name.lower())
        if any(wanted.lower().startswith(f"{alias}/") for alias in aliases):
            return select_openai_v1_default(providers, wanted)
    if "/" not in wanted and any(wanted in provider.models for provider in providers):
        return select_openai_v1_default(providers, wanted)
    return None


def build_config(
    environ: Mapping[str, str],
    *,
    destination: Path,
    native_models: Sequence[str] = (),
    openai_v1_providers: Sequence[OpenAIV1Provider] = (),
    tailscale_runner: Callable[..., Any] = subprocess.run,
) -> tuple[dict[str, Any], str, bool]:
    """Build a complete config without opening the destination file."""

    requested_note_full_mode = boolean(
        environ,
        "OPENCLAW_NOTE_FULL_MODE",
        default=boolean(environ, "NOTE_FULL_MODE", default=True),
    )
    explicit_model = clean(environ.get("OPENCLAW_MODEL"))
    note_full_mode = requested_note_full_mode or explicit_model == NOTE_MODEL
    providers = tuple(openai_v1_providers)
    configured_default = clean(environ.get("OPENCLAW_OPENAI_V1_DEFAULT_LLM"))
    selected = select_openai_v1_default(providers, configured_default)
    custom_primary = ""
    if selected is not None:
        custom_primary, providers = selected
    explicit_custom = _select_explicit_custom_model(providers, explicit_model)
    if explicit_custom is not None:
        _, providers = explicit_custom
    primary_model = explicit_model or custom_primary or (
        NOTE_MODEL if requested_note_full_mode else DUMMY_MODEL
    )
    allowlist = _model_allowlist(
        native_models,
        providers,
        explicit_model=explicit_model,
    )

    config: dict[str, Any] = {
        "meta": {"migrations": {"modelPolicyAllowlist": True}},
        "gateway": _gateway_config(environ, tailscale_runner=tailscale_runner),
        "agents": _main_agent_config(
            environ,
            destination,
            primary_model,
            allowlist,
        ),
        "plugins": _plugins_config(note_full_mode, destination),
        "tools": _trusted_container_tools(),
    }
    custom_models = _models_config(providers)
    if custom_models:
        config["models"] = custom_models
    config.update(_telegram_config(environ))
    return config, primary_model, note_full_mode


def configure(
    environ: Mapping[str, str] | None = None,
    *,
    runner: Callable[..., Any] = subprocess.run,
    opener: Callable[..., Any] = urlopen,
) -> ConfigurationResult:
    """Discover providers, rebuild the config from scratch, and atomically write it."""

    injected = expand_api_key_aliases(os.environ if environ is None else environ)
    destination = config_path(injected)
    native_models, native_warnings = discover_native_models(
        injected,
        runner=runner,
    )
    auth_db = destination.parent / "agents/main/agent/openclaw-agent.sqlite"
    if auth_db.is_file() and auth_db.stat().st_size:
        native_models = tuple(sorted({*native_models, "openai/*"}))
    providers, openai_warnings = discover_openai_v1_providers(
        injected,
        opener=opener,
    )
    config, primary_model, note_full_mode = build_config(
        injected,
        destination=destination,
        native_models=native_models,
        openai_v1_providers=providers,
        tailscale_runner=runner,
    )

    secret_values = {
        clean(value)
        for name, value in injected.items()
        if clean(value)
        and (
            name.endswith("_API_KEY")
            or name.startswith("OPENAI_V1_KEY")
            or name in {"OPENCLAW_GATEWAY_TOKEN", "OPENCLAW_TELEGRAMTOKEN"}
        )
    }
    if not without_secret_values(config, secret_values):
        raise RuntimeError("Refusing to persist a resolved secret value")
    atomic_write_json(destination, config)

    return ConfigurationResult(
        path=destination,
        primary_model=primary_model,
        native_model_count=len(native_models),
        openai_v1_provider_count=len(providers),
        openai_v1_model_count=sum(len(provider.models) for provider in providers),
        telegram_configured=bool(clean(injected.get("OPENCLAW_TELEGRAMTOKEN"))),
        note_full_mode=note_full_mode,
        warnings=(*native_warnings, *openai_warnings),
    )
