#!/usr/bin/env bash
# Source of truth: SCRIPTS/githubactions. Generated copies are overwritten.
set -euo pipefail

# Published images can be pulled again for smoke tests. Drop the much larger
# intermediate BuildKit state before continuing with the next layer or test.
docker buildx prune --all --force || true
docker system prune --all --force --volumes || true

df -h /
docker system df || true
