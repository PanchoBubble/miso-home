#!/usr/bin/env bash
set -Eeuo pipefail

umask 077

T7_UUID="${T7_UUID:-081E-DA7A}"
T7_ROOT="${T7_ROOT:-/media/pancho/T7}"
MISO_ROOT="${MISO_ROOT:-/var/lib/miso}"
BACKUP_ROOT="${BACKUP_ROOT:-${T7_ROOT}/backups/miso}"
ROOT_HARD_MIN_GIB="${ROOT_HARD_MIN_GIB:-20}"
T7_HARD_MIN_GIB="${T7_HARD_MIN_GIB:-100}"

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

available_gib() {
  local path="$1"
  df -Pk "${path}" | awk 'END {printf "%d", $4 / 1024 / 1024}'
}

[[ "${EUID}" -eq 0 ]] || fail "run as root"
for command in cmp df findmnt install sqlite3 stat; do
  command -v "${command}" >/dev/null || fail "missing command: ${command}"
done

[[ "$(findmnt -n -o FSTYPE -T "${MISO_ROOT}" 2>/dev/null || true)" == "ext4" ]] \
  || fail "Miso state is not on ext4: ${MISO_ROOT}"
ls "${T7_ROOT}/." >/dev/null 2>&1 || fail "cannot access T7 mount at ${T7_ROOT}"
mounted_uuid="$(findmnt -rn -o UUID -T "${T7_ROOT}" 2>/dev/null \
  | awk 'NF { uuid = $1 } END { print uuid }')"
[[ "${mounted_uuid}" == "${T7_UUID}" ]] \
  || fail "T7 UUID ${T7_UUID} is not mounted at ${T7_ROOT}"

for path in "${MISO_ROOT}" "${MISO_ROOT}/db" "${MISO_ROOT}/state" \
  "${MISO_ROOT}/models"; do
  [[ -d "${path}" ]] || fail "missing directory: ${path}"
  [[ "$(stat -c '%U:%G' "${path}")" == "miso:miso" ]] \
    || fail "unexpected owner for ${path}"
done
[[ "$(stat -c '%U:%G' "${BACKUP_ROOT}")" == "pancho:pancho" ]] \
  || fail "unexpected mount-mapped owner for ${BACKUP_ROOT}"
[[ "$(stat -c '%U:%G' /etc/miso)" == "root:miso" ]] \
  || fail "unexpected owner for /etc/miso"

root_free="$(available_gib "${MISO_ROOT}")"
t7_free="$(available_gib "${BACKUP_ROOT}")"
(( root_free >= ROOT_HARD_MIN_GIB )) \
  || fail "root free space ${root_free} GiB is below ${ROOT_HARD_MIN_GIB} GiB"
(( t7_free >= T7_HARD_MIN_GIB )) \
  || fail "T7 free space ${t7_free} GiB is below ${T7_HARD_MIN_GIB} GiB"

stage="$(mktemp -d "${MISO_ROOT}/.storage-layout-test.XXXXXX")"
partial="${BACKUP_ROOT}/.storage-layout-test-$$.sqlite3.partial"
artifact="${BACKUP_ROOT}/.storage-layout-test-$$.sqlite3"
cleanup() {
  rm -rf -- "${stage}"
  rm -f -- "${partial}" "${artifact}"
}
trap cleanup EXIT

live="${stage}/live.sqlite3"
snapshot="${stage}/snapshot.sqlite3"
restored="${stage}/restored.sqlite3"
sqlite3 "${live}" >/dev/null <<'SQL'
PRAGMA journal_mode=WAL;
CREATE TABLE storage_test (id INTEGER PRIMARY KEY, value TEXT NOT NULL);
INSERT INTO storage_test (value) VALUES ('miso-storage-restore-ok');
SQL
sqlite3 "${live}" ".backup '${snapshot}'"
[[ "$(sqlite3 "${snapshot}" 'PRAGMA integrity_check;')" == "ok" ]] \
  || fail "online SQLite snapshot failed integrity check"

install -m 0600 "${snapshot}" "${partial}"
mv -- "${partial}" "${artifact}"
install -m 0600 "${artifact}" "${restored}"
[[ "$(sqlite3 "${restored}" 'PRAGMA integrity_check;')" == "ok" ]] \
  || fail "restored SQLite fixture failed integrity check"
[[ "$(sqlite3 "${restored}" 'SELECT value FROM storage_test WHERE id = 1;')" \
  == "miso-storage-restore-ok" ]] || fail "restored SQLite fixture has wrong content"
cmp -- "${snapshot}" "${restored}"

printf 'Miso storage layout and isolated SQLite restore passed '
printf '(root=%s GiB free, T7=%s GiB free)\n' "${root_free}" "${t7_free}"
