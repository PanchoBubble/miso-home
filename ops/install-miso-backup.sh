#!/usr/bin/env bash
set -Eeuo pipefail

SOURCE_ROOT="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
BACKUP_TOOL="${SOURCE_ROOT}/ops/miso_backup.py"
KEY_FILE="${MISO_BACKUP_KEY_FILE:-/home/pancho/.config/miso-backup/backup.key}"

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

[[ "${EUID}" -eq 0 ]] || fail "run as root"
for command in install openssl python3 systemctl; do
  command -v "${command}" >/dev/null || fail "missing command: ${command}"
done
[[ -f "${BACKUP_TOOL}" ]] || fail "backup tool not found: ${BACKUP_TOOL}"
[[ -s "${KEY_FILE}" ]] || fail "backup key is missing or empty: ${KEY_FILE}"

install -o root -g root -m 0755 "${BACKUP_TOOL}" /usr/local/sbin/miso-backup
for unit in \
  miso-database-backup.service \
  miso-database-backup.timer \
  miso-database-restore-check.service \
  miso-database-restore-check.timer; do
  install -o root -g root -m 0644 "${SOURCE_ROOT}/ops/systemd/${unit}" \
    "/etc/systemd/system/${unit}"
done

systemctl daemon-reload
systemctl enable --now miso-database-backup.timer
systemctl enable --now miso-database-restore-check.timer

printf 'Installed Miso database backup automation; timers are enabled\n'
