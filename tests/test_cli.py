from __future__ import annotations

import io
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from openclaw_ephemeral.cli import TRUSTED_POLICY_SCRIPT, main
from openclaw_ephemeral.configuration import ConfigurationResult
from openclaw_ephemeral.environment import ConfigurationError


class ExecCalled(RuntimeError):
    pass


def result(path: Path) -> ConfigurationResult:
    return ConfigurationResult(
        path=path,
        primary_model="dummy/dummy",
        native_model_count=2,
        openai_v1_provider_count=1,
        openai_v1_model_count=3,
        telegram_configured=False,
        note_full_mode=False,
        warnings=(),
    )


def make_hook(
    root: Path,
    phase: str,
    name: str,
    *,
    mode: int = 0o755,
) -> Path:
    directory = root / f"{phase}.d"
    directory.mkdir(parents=True, exist_ok=True)
    hook = directory / name
    hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    hook.chmod(mode)
    return hook


class CliLifecycleTests(unittest.TestCase):
    def test_configure_mode_applies_policy_without_hooks_or_gateway(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            hook_root = Path(raw) / "runtime.d"
            for phase in ("pre-config", "post-config", "pre-gateway"):
                make_hook(hook_root, phase, "10-must-not-run.sh")
            make_hook(hook_root, "pre-config", "unsafe hook")
            calls: list[Any] = []

            def runner(*args: Any, **kwargs: Any) -> None:
                calls.append((args, kwargs))

            with (
                patch(
                    "openclaw_ephemeral.cli.RUNTIME_HOOK_ROOT",
                    hook_root,
                ),
                patch(
                    "openclaw_ephemeral.cli.configure",
                    return_value=result(Path(raw) / "openclaw.json"),
                ),
            ):
                exit_code = main(
                    ["configure"],
                    environ={"HOME": raw},
                    runner=runner,
                    execvpe=lambda *_args: self.fail("must not exec"),
                    stdout=io.StringIO(),
                    stderr=io.StringIO(),
                )
            self.assertEqual(exit_code, 0)
            self.assertEqual(
                calls,
                [
                    (
                        ([TRUSTED_POLICY_SCRIPT],),
                        {"check": True, "env": {"HOME": raw}},
                    )
                ],
            )

    def test_run_executes_each_hook_phase_in_lexical_order(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            hook_root = Path(raw) / "runtime.d"
            make_hook(hook_root, "pre-config", "20-second.sh")
            make_hook(hook_root, "pre-config", "10-first.sh")
            make_hook(
                hook_root,
                "pre-config",
                "15-disabled.sh",
                mode=0o644,
            )
            make_hook(hook_root, "post-config", "10-after-policy.sh")
            make_hook(hook_root, "pre-gateway", "10-before-exec.sh")
            order: list[str] = []
            inherited = {
                "HOME": raw,
                "HOOK_MARKER": "visible",
                "OPENCLAW_GATEWAY_TOKEN": "gateway-secret",
            }

            def fake_configure(
                *_args: Any,
                **_kwargs: Any,
            ) -> ConfigurationResult:
                order.append("configure")
                return result(Path(raw) / "openclaw.json")

            def fake_runner(arguments: list[str], **kwargs: Any) -> None:
                if arguments == [TRUSTED_POLICY_SCRIPT]:
                    order.append("policy")
                    self.assertEqual(
                        kwargs,
                        {"check": True, "env": inherited},
                    )
                    return
                hook = Path(arguments[0])
                order.append(f"{hook.parent.name}/{hook.name}")
                self.assertEqual(arguments, [str(hook)])
                self.assertEqual(
                    kwargs,
                    {
                        "check": True,
                        "env": inherited,
                        "shell": False,
                    },
                )

            def fake_exec(*_args: Any) -> None:
                order.append("exec")
                raise ExecCalled()

            with (
                patch(
                    "openclaw_ephemeral.cli.RUNTIME_HOOK_ROOT",
                    hook_root,
                ),
                patch(
                    "openclaw_ephemeral.cli.configure",
                    side_effect=fake_configure,
                ),
            ):
                with self.assertRaises(ExecCalled):
                    main(
                        ["run"],
                        environ=inherited,
                        runner=fake_runner,
                        execvpe=fake_exec,
                        stdout=io.StringIO(),
                        stderr=io.StringIO(),
                    )

            self.assertEqual(
                order,
                [
                    "pre-config.d/10-first.sh",
                    "pre-config.d/20-second.sh",
                    "configure",
                    "policy",
                    "post-config.d/10-after-policy.sh",
                    "pre-gateway.d/10-before-exec.sh",
                    "exec",
                ],
            )

    def test_run_configures_before_execing_gateway(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            order: list[str] = []
            captured: dict[str, Any] = {}

            def fake_configure(*_args: Any, **_kwargs: Any) -> ConfigurationResult:
                order.append("configure")
                return result(Path(raw) / "openclaw.json")

            def fake_exec(executable: str, arguments: list[str], environ: dict[str, str]) -> None:
                order.append("exec")
                captured.update(
                    executable=executable,
                    arguments=arguments,
                    environ=environ,
                )
                raise ExecCalled()

            def fake_runner(arguments: list[str], **_kwargs: Any) -> None:
                self.assertEqual(arguments, [TRUSTED_POLICY_SCRIPT])
                order.append("policy")

            with patch(
                "openclaw_ephemeral.cli.configure",
                side_effect=fake_configure,
            ):
                with self.assertRaises(ExecCalled):
                    main(
                        ["run"],
                        environ={
                            "HOME": raw,
                            "OPENCLAW_BIN": "node /app/openclaw.mjs",
                            "OPENCLAW_GATEWAY_TOKEN": "gateway-secret",
                            "OPENCLAW_GATEWAY_PORT": "19000",
                        },
                        runner=fake_runner,
                        execvpe=fake_exec,
                        stdout=io.StringIO(),
                        stderr=io.StringIO(),
                    )

            self.assertEqual(order, ["configure", "policy", "exec"])
            self.assertEqual(captured["executable"], "node")
            self.assertEqual(
                captured["arguments"],
                [
                    "node",
                    "/app/openclaw.mjs",
                    "gateway",
                    "run",
                    "--bind",
                    "lan",
                    "--port",
                    "19000",
                    "--auth",
                    "token",
                ],
            )

    def test_gateway_token_keeps_existing_auth_argument(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            captured: dict[str, Any] = {}

            def fake_exec(
                executable: str,
                arguments: list[str],
                environ: dict[str, str],
            ) -> None:
                captured.update(
                    executable=executable,
                    arguments=arguments,
                    environ=environ,
                )
                raise ExecCalled()

            with patch(
                "openclaw_ephemeral.cli.configure",
                return_value=result(Path(raw) / "openclaw.json"),
            ):
                with self.assertRaises(ExecCalled):
                    main(
                        ["run"],
                        environ={
                            "HOME": raw,
                            "OPENCLAW_GATEWAY_TOKEN": "gateway-secret",
                        },
                        runner=lambda *_args, **_kwargs: None,
                        execvpe=fake_exec,
                        stdout=io.StringIO(),
                        stderr=io.StringIO(),
                    )

            self.assertEqual(
                captured["arguments"][-2:],
                ["--auth", "token"],
            )
            self.assertNotIn("gateway-secret", captured["arguments"])

    def test_run_passes_reverse_alias_to_gateway_without_putting_it_on_argv(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            captured: dict[str, Any] = {}

            def fake_exec(executable: str, arguments: list[str], environ: dict[str, str]) -> None:
                captured.update(arguments=arguments, environ=environ)
                raise ExecCalled(executable)

            with patch(
                "openclaw_ephemeral.cli.configure",
                return_value=result(Path(raw) / "openclaw.json"),
            ):
                with self.assertRaises(ExecCalled):
                    main(
                        ["run"],
                        environ={
                            "HOME": raw,
                            "OPENAI_V1_KEY": "custom-secret",
                            "OPENCLAW_GATEWAY_TOKEN": "gateway-secret",
                            "OPENAI_V1_API_KEY_ALIAS": "GEMINI_API_KEY",
                        },
                        runner=lambda *_args, **_kwargs: None,
                        execvpe=fake_exec,
                        stdout=io.StringIO(),
                        stderr=io.StringIO(),
                    )

            self.assertEqual(
                captured["environ"]["GEMINI_API_KEY"],
                "custom-secret",
            )
            self.assertNotIn("custom-secret", captured["arguments"])

    def test_failed_hook_stops_later_hooks_and_gateway(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            hook_root = Path(raw) / "runtime.d"
            failed = make_hook(hook_root, "post-config", "10-fail.sh")
            make_hook(hook_root, "post-config", "20-too-late.sh")
            make_hook(hook_root, "pre-gateway", "10-too-late.sh")
            order: list[str] = []
            error_output = io.StringIO()

            def fake_configure(
                *_args: Any,
                **_kwargs: Any,
            ) -> ConfigurationResult:
                order.append("configure")
                return result(Path(raw) / "openclaw.json")

            def fake_runner(arguments: list[str], **_kwargs: Any) -> None:
                if arguments == [TRUSTED_POLICY_SCRIPT]:
                    order.append("policy")
                    return
                order.append(Path(arguments[0]).name)
                if arguments == [str(failed)]:
                    raise subprocess.CalledProcessError(17, arguments)

            with (
                patch(
                    "openclaw_ephemeral.cli.RUNTIME_HOOK_ROOT",
                    hook_root,
                ),
                patch(
                    "openclaw_ephemeral.cli.configure",
                    side_effect=fake_configure,
                ),
            ):
                exit_code = main(
                    ["run"],
                    environ={
                        "HOME": raw,
                        "OPENCLAW_GATEWAY_TOKEN": "gateway-secret",
                    },
                    runner=fake_runner,
                    execvpe=lambda *_args: self.fail("must not exec"),
                    stdout=io.StringIO(),
                    stderr=error_output,
                )

            self.assertEqual(exit_code, 2)
            self.assertEqual(order, ["configure", "policy", "10-fail.sh"])
            self.assertIn("status 17", error_output.getvalue())

    def test_invalid_hook_entries_fail_before_configuration(self) -> None:
        for invalid_kind in (
            "unsafe-name",
            "symlink",
            "non-regular",
            "symlink-directory",
        ):
            with self.subTest(invalid_kind=invalid_kind):
                with tempfile.TemporaryDirectory() as raw:
                    hook_root = Path(raw) / "runtime.d"
                    hook_root.mkdir()
                    phase = hook_root / "pre-config.d"
                    if invalid_kind == "symlink-directory":
                        target = hook_root / "target"
                        target.mkdir()
                        phase.symlink_to(target, target_is_directory=True)
                    else:
                        phase.mkdir()
                        if invalid_kind == "unsafe-name":
                            make_hook(
                                hook_root,
                                "pre-config",
                                "unsafe hook",
                            )
                        elif invalid_kind == "symlink":
                            target = hook_root / "target"
                            target.write_text(
                                "#!/bin/sh\nexit 0\n",
                                encoding="utf-8",
                            )
                            target.chmod(0o755)
                            (phase / "10-link.sh").symlink_to(target)
                        else:
                            (phase / "10-directory").mkdir()

                    calls: list[Any] = []
                    error_output = io.StringIO()
                    with (
                        patch(
                            "openclaw_ephemeral.cli.RUNTIME_HOOK_ROOT",
                            hook_root,
                        ),
                        patch(
                            "openclaw_ephemeral.cli.configure",
                            return_value=result(
                                Path(raw) / "openclaw.json"
                            ),
                        ) as configure_mock,
                    ):
                        exit_code = main(
                            ["run"],
                            environ={"HOME": raw},
                            runner=lambda *args, **kwargs: calls.append(
                                (args, kwargs)
                            ),
                            execvpe=lambda *_args: self.fail("must not exec"),
                            stdout=io.StringIO(),
                            stderr=error_output,
                        )

                    self.assertEqual(exit_code, 2)
                    configure_mock.assert_not_called()
                    self.assertEqual(calls, [])
                    self.assertIn(
                        "Runtime hook error",
                        error_output.getvalue(),
                    )

    def test_restart_occurs_only_after_successful_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            order: list[str] = []
            captured: dict[str, Any] = {}

            def fake_configure(*_args: Any, **_kwargs: Any) -> ConfigurationResult:
                order.append("configure")
                return result(Path(raw) / "openclaw.json")

            def runner(arguments: list[str], **kwargs: Any) -> None:
                if arguments == [TRUSTED_POLICY_SCRIPT]:
                    order.append("policy")
                    return
                order.append("restart")
                captured.update(arguments=arguments, kwargs=kwargs)

            with patch(
                "openclaw_ephemeral.cli.configure",
                side_effect=fake_configure,
            ):
                exit_code = main(
                    ["restart"],
                    environ={
                        "HOME": raw,
                        "OPENCLAW_GATEWAY_TOKEN": "gateway-secret",
                    },
                    runner=runner,
                    stdout=io.StringIO(),
                    stderr=io.StringIO(),
                )

            self.assertEqual(exit_code, 0)
            self.assertEqual(order, ["configure", "policy", "restart"])
            self.assertEqual(
                captured["arguments"],
                ["openclaw", "gateway", "restart"],
            )
            self.assertTrue(captured["kwargs"]["check"])

    def test_failed_configuration_never_restarts_gateway(self) -> None:
        calls: list[Any] = []
        error_output = io.StringIO()
        with patch(
            "openclaw_ephemeral.cli.configure",
            side_effect=ConfigurationError("bad input"),
        ):
            exit_code = main(
                ["restart"],
                environ={},
                runner=lambda *args, **kwargs: calls.append((args, kwargs)),
                stdout=io.StringIO(),
                stderr=error_output,
            )
        self.assertEqual(exit_code, 2)
        self.assertEqual(calls, [])
        self.assertIn("Configuration error", error_output.getvalue())

    def test_report_contains_no_environment_secret(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            output = io.StringIO()
            with patch(
                "openclaw_ephemeral.cli.configure",
                return_value=result(Path(raw) / "openclaw.json"),
            ):
                main(
                    ["configure"],
                    environ={
                        "HOME": raw,
                        "OPENCLAW_GATEWAY_TOKEN": "gateway-secret",
                    },
                    runner=lambda *_args, **_kwargs: None,
                    stdout=output,
                    stderr=io.StringIO(),
                )
            self.assertNotIn("gateway-secret", output.getvalue())


if __name__ == "__main__":
    unittest.main()
