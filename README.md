# openclaw-ephemeral-testing

Separate testing lane for OpenClaw Beta 5. It does not replace or retag the
stable `openclaw-ephemeral` image.

## Immutable upstream pin

- Image: `openclaw/openclaw:2026.7.2-beta.5-slim`
- Digest: `sha256:86e0a480a37d879311c9723ad2487cca9eb6c1925fa4732dec3f505b4728eee9`
- Source commit: `ee929dbb857c717a60f3b2b502db5a6dd31b5c11`

`pinned.md` is the human-readable pin record for this lane.

## Patch artifact contract

The compatibility artifact is installed as a release tarball over `/app`.
A build must provide all three values; the Containerfile intentionally has no
fallback:

- `OPENCLAW_DETERMINISTIC_RELEASE_TAG`
- `OPENCLAW_DETERMINISTIC_ASSET`
- `OPENCLAW_DETERMINISTIC_SHA256`

The installer downloads from `safrano9999/openclaw-deterministic`, verifies
SHA-256, extracts the tarball, and checks the resulting OpenClaw version.

## Runtime contract

Beta 5 canonical paths are used:

- `OPENCLAW_STATE_DIR=/root/.openclaw`
- `OPENCLAW_CONFIG_PATH=/root/.openclaw/openclaw.json`
- `OPENCLAW_WORKSPACE_DIR=/root/.openclaw/workspace`

The gateway binds to LAN. `OPENCLAW_GATEWAY_TOKEN` is mandatory for `run`
and `restart`; a blank or missing token fails before gateway startup.

Generated configuration uses `agents.entries.main` and
`agents.defaults.modelPolicy.allow`. Legacy Control UI authentication
bypasses are not emitted.

Target image:

```text
docker.io/safrano9999/openclaw-ephemeral-testing:latest
```

Example:

```bash
docker run --rm \
  -e OPENCLAW_GATEWAY_TOKEN='replace-with-a-secret' \
  -p 18789:18789 \
  docker.io/safrano9999/openclaw-ephemeral-testing:latest
```

## Tests

The GitHub workflow runs the repository suite before build and push:

```bash
python3 -m unittest discover -s tests -v
```
