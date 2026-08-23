#!/usr/bin/env bash
set -Eeuo pipefail

SOURCE_ROOT="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
TARGET_ROOT="${TARGET_ROOT:-/opt/miso/app}"
UNIT_SOURCE="${SOURCE_ROOT}/ops/systemd/miso.service"
CONVERSATION_ENV_SOURCE="${SOURCE_ROOT}/ops/systemd/miso-conversation.env"

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

[[ "${EUID}" -eq 0 ]] || fail "run as root"
for command in aplay arecord getent install python3 rsync systemctl usermod; do
  command -v "${command}" >/dev/null || fail "missing command: ${command}"
done
[[ -d "${SOURCE_ROOT}/src/miso" ]] || fail "Miso source not found under ${SOURCE_ROOT}"
[[ -f "${UNIT_SOURCE}" ]] || fail "systemd unit not found: ${UNIT_SOURCE}"
[[ -f "${CONVERSATION_ENV_SOURCE}" ]] || fail \
  "conversation environment not found: ${CONVERSATION_ENV_SOURCE}"
getent passwd miso >/dev/null || fail "miso service user is not configured"
getent group audio >/dev/null || fail "audio group is not configured"
usermod --append --groups audio miso

install -d -o root -g root -m 0755 "${TARGET_ROOT}" "${TARGET_ROOT}/src"
rsync -a --delete --exclude '__pycache__/' \
  "${SOURCE_ROOT}/src/" "${TARGET_ROOT}/src/"
install -o root -g root -m 0644 "${SOURCE_ROOT}/pyproject.toml" \
  "${TARGET_ROOT}/pyproject.toml"
install -o root -g root -m 0644 "${SOURCE_ROOT}/README.md" \
  "${TARGET_ROOT}/README.md"
install -o root -g root -m 0644 "${UNIT_SOURCE}" \
  /etc/systemd/system/miso.service
install -d -o root -g root -m 0755 /etc/miso
install -o root -g root -m 0644 "${CONVERSATION_ENV_SOURCE}" \
  /etc/miso/miso-conversation.env

PYTHONPATH="${TARGET_ROOT}/src" PYTHONDONTWRITEBYTECODE=1 \
  python3 -m compileall -q "${TARGET_ROOT}/src"
systemctl daemon-reload
systemctl enable miso.service
systemctl restart miso.service

printf 'Installed and started Miso from %s\n' "${SOURCE_ROOT}"
