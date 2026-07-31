#!/usr/bin/env bash
set -euo pipefail

: "${NOTE_RELEASE_TAG:?NOTE_RELEASE_TAG is required}"
: "${NOTE_RELEASE_SHA256:?NOTE_RELEASE_SHA256 is required}"

asset=note-latest.zip
release_url="https://github.com/safrano9999/NOTE/releases/download/${NOTE_RELEASE_TAG}"
temporary="$(mktemp -d)"
trap 'rm -rf "$temporary"' EXIT

curl -fsSL --retry 3 "${release_url}/${asset}" -o "${temporary}/${asset}"
printf '%s  %s\n' "$NOTE_RELEASE_SHA256" "$asset" > "${temporary}/${asset}.sha256"
(cd "$temporary" && sha256sum -c "${asset}.sha256")

openclaw plugins install \
    --force \
    --dangerously-force-unsafe-install \
    "${temporary}/${asset}"

[ -f "${OPENCLAW_CONFIG_DIR}/extensions/note/openclaw.plugin.json" ] || {
    printf 'NOTE plugin was not installed below %s\n' "$OPENCLAW_CONFIG_DIR" >&2
    exit 1
}

note_root="${OPENCLAW_CONFIG_DIR}/extensions/note"
note_python="$("${note_root}/scripts/setup-python.sh")"
"$note_python" -c 'import dotenv, sqlalchemy, psycopg, pymysql'
rm -rf "${note_root}/.uv"

printf 'Installed NOTE release %s\n' "$NOTE_RELEASE_TAG"
