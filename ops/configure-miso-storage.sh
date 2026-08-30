#!/usr/bin/env bash
set -Eeuo pipefail

T7_UUID="${T7_UUID:-081E-DA7A}"
T7_ROOT="${T7_ROOT:-/media/pancho/T7}"
MISO_ROOT="${MISO_ROOT:-/var/lib/miso}"
BACKUP_ROOT="${BACKUP_ROOT:-${T7_ROOT}/backups/miso}"

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

[[ "${EUID}" -eq 0 ]] || fail "run as root"
[[ "$(findmnt -n -o FSTYPE -T "${MISO_ROOT%/miso}" 2>/dev/null || true)" == "ext4" ]] \
  || fail "${MISO_ROOT} must resolve to ext4"
ls "${T7_ROOT}/." >/dev/null 2>&1 || fail "cannot access T7 mount at ${T7_ROOT}"
mounted_uuid="$(findmnt -rn -o UUID -T "${T7_ROOT}" 2>/dev/null \
  | awk 'NF { uuid = $1 } END { print uuid }')"
[[ "${mounted_uuid}" == "${T7_UUID}" ]] \
  || fail "T7 UUID ${T7_UUID} is not mounted at ${T7_ROOT}"

getent group miso >/dev/null || groupadd --system miso
if ! getent passwd miso >/dev/null; then
  useradd --system --gid miso --home-dir "${MISO_ROOT}" \
    --shell /usr/sbin/nologin --no-create-home miso
fi

install -d -o miso -g miso -m 0750 \
  "${MISO_ROOT}" "${MISO_ROOT}/db" "${MISO_ROOT}/state" \
  "${MISO_ROOT}/models" "${MISO_ROOT}/tools.d"
install -d -o root -g miso -m 0750 /etc/miso
install -d "${BACKUP_ROOT}"

if getent passwd ollama >/dev/null; then
  usermod -a -G miso ollama
  install -d -o ollama -g ollama -m 0750 "${MISO_ROOT}/models/ollama"
fi

printf 'Configured Miso ext4 state at %s and T7 backups at %s\n' \
  "${MISO_ROOT}" "${BACKUP_ROOT}"
