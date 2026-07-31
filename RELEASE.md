# openclaw-ephemeral-testing release contract

This repository is the Beta 5 testing lane. Stable tags belong to the separate
`openclaw-ephemeral` repository and must not be produced here.

A releasable build requires:

- the immutable upstream image digest recorded in `pinned.md` and
  `Containerfile`
- source commit `ee929dbb857c717a60f3b2b502db5a6dd31b5c11`
- an immutable compatibility release tag, asset name, and SHA-256 supplied
  through the three required `OPENCLAW_DETERMINISTIC_*` build arguments
- a successful unit-test job before build and push

The published repository is
`docker.io/safrano9999/openclaw-ephemeral-testing`; its rolling tag is
`latest`.
