# openclaw-ephemeral-latest

Testing lane of the environment-driven Python runtime for OpenClaw. It rebuilds
the complete `openclaw.json` from the current process environment without
reading or merging an older JSON file.

The repository is centered on `openclaw-ephemeral.py`, the
`openclaw_ephemeral` package, and their tests. The Beta 5 container packaging is
secondary and isolated below `container/`.

## Python runtime

```text
openclaw-ephemeral.py configure
openclaw-ephemeral.py run
openclaw-ephemeral.py restart
```

This lane uses the Beta 5 canonical paths:

- `OPENCLAW_STATE_DIR=/root/.openclaw`
- `OPENCLAW_CONFIG_PATH=/root/.openclaw/openclaw.json`
- `OPENCLAW_WORKSPACE_DIR=/root/.openclaw/workspace`

The generated configuration uses `agents.entries.main` and
`agents.defaults.modelPolicy.allow`. Legacy Control UI authentication bypasses
are not emitted. `OPENCLAW_GATEWAY_TOKEN` is mandatory for `run` and `restart`;
a blank or missing token fails before gateway startup.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

## Optional testing image

The image definition and its helpers live in `container/`. The repository root
remains the build context so the Containerfile can copy the unchanged Python
package and launcher.

The testing image is published separately from Stable:

```text
ghcr.io/safrano9999/openclaw-ephemeral-testing:latest
```

Its immutable upstream pin is:

- Image: `docker.io/openclaw/openclaw:2026.7.2-beta.5-slim`
- Digest: `sha256:86e0a480a37d879311c9723ad2487cca9eb6c1925fa4732dec3f505b4728eee9`
- Source commit: `ee929dbb857c717a60f3b2b502db5a6dd31b5c11`

`pinned.md` records the human-readable pin. The build also requires the exact
release tag, asset name, and SHA-256 produced by
`openclaw-deterministic-latest`; the Containerfile intentionally has no
fallback. NOTE is installed as a separate pinned release.

Example:

```bash
docker run --rm \
  -e OPENCLAW_GATEWAY_TOKEN='replace-with-a-secret' \
  -p 18789:18789 \
  ghcr.io/safrano9999/openclaw-ephemeral-testing:latest
```
