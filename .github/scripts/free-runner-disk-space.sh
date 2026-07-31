#!/usr/bin/env bash
# Source of truth: SCRIPTS/githubactions. Generated copies are overwritten.
set -euo pipefail

echo "::group::Runner disk space before cleanup"
df -h /
docker system df || true
echo "::endgroup::"

# GitHub-hosted runners contain SDKs and tool caches that these container
# workflows do not use. Removing them leaves BuildKit enough room for the
# large Fedora image layers.
unused_paths=(
    /opt/ghc
    /opt/hostedtoolcache
    /usr/local/.ghcup
    /usr/local/lib/android
    /usr/local/share/boost
    /usr/local/share/powershell
    /usr/share/dotnet
    /usr/share/swift
)

sudo rm -rf -- "${unused_paths[@]}"
sudo apt-get clean
docker system prune --all --force --volumes || true

echo "::group::Runner disk space after cleanup"
df -h /
docker system df || true
echo "::endgroup::"
