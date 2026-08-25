#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd -P)"
MSGFMT="${MSGFMT:-msgfmt}"

command -v "${MSGFMT}" >/dev/null 2>&1 || {
  printf 'error: msgfmt is required to compile prompt catalogs\n' >&2
  exit 1
}

for locale in en zh; do
  source="${REPO_ROOT}/zero_agent/assets/locale/${locale}/LC_MESSAGES/zero_agent.po"
  output="${REPO_ROOT}/zero_agent/assets/locale/${locale}/LC_MESSAGES/zero_agent.mo"
  "${MSGFMT}" --check -o "${output}" "${source}"
done
