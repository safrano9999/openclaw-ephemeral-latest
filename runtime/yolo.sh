#!/usr/bin/env bash
set -euo pipefail

# Keep the exact trusted-container policy already used by safrano9999-openclaw.
# The preset writes the host-side approval file; the config keys are then
# normalized to the OpenClaw 2026.7.1 tools.exec.mode form.
openclaw config unset tools.exec.mode >/dev/null 2>&1 || true
openclaw exec-policy preset yolo
openclaw config unset tools.exec.security
openclaw config unset tools.exec.ask
openclaw config set agents.defaults.sandbox.mode '"off"' --strict-json
openclaw config set tools.profile '"full"' --strict-json
openclaw config set tools.fs.workspaceOnly false --strict-json
openclaw config set tools.exec.host '"gateway"' --strict-json
openclaw config set tools.exec.mode '"full"' --strict-json
openclaw config set tools.exec.applyPatch.workspaceOnly false --strict-json
