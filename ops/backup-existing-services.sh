#!/usr/bin/env bash
set -Eeuo pipefail

umask 077

BACKUP_ROOT="${BACKUP_ROOT:-/media/pancho/T7/backups/existing-services}"
KEY_FILE="${KEY_FILE:-/home/pancho/.config/miso-backup/backup.key}"
T7_UUID="${T7_UUID:-081E-DA7A}"
RETENTION_DAYS="${RETENTION_DAYS:-14}"
LOCK_FILE="${LOCK_FILE:-/run/lock/miso-existing-services-backup.lock}"

IMMICH_DB_CONTAINER="${IMMICH_DB_CONTAINER:-immich-postgres}"
NEXTCLOUD_CONTAINER="${NEXTCLOUD_CONTAINER:-nextcloud}"
NEXTCLOUD_DB_CONTAINER="${NEXTCLOUD_DB_CONTAINER:-nextcloud-db}"
VAULTWARDEN_DATA="${VAULTWARDEN_DATA:-/var/www/services/bitwarden/bwdata}"
NEXTCLOUD_HTML="${NEXTCLOUD_HTML:-/var/lib/docker/volumes/services_nextcloud_html/_data}"
SERVICES_ROOT="${SERVICES_ROOT:-/var/www/services}"

log() {
  printf '%s %s\n' "$(date --iso-8601=seconds)" "$*"
}

fail() {
  log "ERROR: $*" >&2
  exit 1
}

[[ "${EUID}" -eq 0 ]] || fail "run as root"

for command in docker findmnt flock openssl rsync sha256sum sqlite3 tar; do
  command -v "${command}" >/dev/null || fail "missing command: ${command}"
done

exec 9>"${LOCK_FILE}"
flock -n 9 || fail "another backup is already running"

mounted_uuid="$(findmnt -n -o UUID -T /media/pancho/T7 2>/dev/null || true)"
[[ "${mounted_uuid}" == "${T7_UUID}" ]] || fail "T7 UUID ${T7_UUID} is not mounted at /media/pancho/T7"
[[ -r "${KEY_FILE}" ]] || fail "encryption key is not readable: ${KEY_FILE}"

for container in "${IMMICH_DB_CONTAINER}" "${NEXTCLOUD_CONTAINER}" \
  "${NEXTCLOUD_DB_CONTAINER}" vaultwarden; do
  [[ "$(docker inspect -f '{{.State.Running}}' "${container}" 2>/dev/null || true)" == "true" ]] \
    || fail "required container is not running: ${container}"
done

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
stage="$(mktemp -d /var/tmp/miso-existing-services-backup.XXXXXX)"
partial="${BACKUP_ROOT}/.existing-services-${timestamp}.tar.zst.enc.partial"
archive="${BACKUP_ROOT}/existing-services-${timestamp}.tar.zst.enc"

cleanup() {
  rm -rf -- "${stage}"
  rm -f -- "${partial}"
}
trap cleanup EXIT

mkdir -p "${BACKUP_ROOT}" "${stage}/databases" "${stage}/vaultwarden" \
  "${stage}/nextcloud" "${stage}/deployment"

log "creating Immich PostgreSQL dump"
docker exec "${IMMICH_DB_CONTAINER}" sh -c \
  'exec pg_dump --clean --if-exists --format=custom --dbname="$POSTGRES_DB" --username="$POSTGRES_USER"' \
  >"${stage}/databases/immich.pgdump"

log "creating Nextcloud MySQL dump"
docker exec "${NEXTCLOUD_DB_CONTAINER}" sh -c \
  'export MYSQL_PWD="$MYSQL_PASSWORD"; exec mysqldump --single-transaction --quick --lock-tables=false --no-tablespaces --routines --triggers --hex-blob --user="$MYSQL_USER" "$MYSQL_DATABASE"' \
  >"${stage}/databases/nextcloud.sql"

log "creating Vaultwarden online SQLite backup"
sqlite3 "${VAULTWARDEN_DATA}/db.sqlite3" \
  ".backup '${stage}/vaultwarden/db.sqlite3'"
[[ "$(sqlite3 "${stage}/vaultwarden/db.sqlite3" 'PRAGMA integrity_check;')" == "ok" ]] \
  || fail "Vaultwarden SQLite integrity check failed"

log "copying Vaultwarden non-database state"
rsync -a --exclude 'db.sqlite3*' --exclude 'icon_cache/' \
  "${VAULTWARDEN_DATA}/" "${stage}/vaultwarden/data/"

log "copying Nextcloud configuration and locally installed extensions"
for directory in config custom_apps themes; do
  if [[ -e "${NEXTCLOUD_HTML}/${directory}" ]]; then
    rsync -a "${NEXTCLOUD_HTML}/${directory}" "${stage}/nextcloud/"
  fi
done

log "copying deployment source"
rsync -a \
  --exclude '.git/' \
  --exclude 'bitwarden/bwdata/' \
  --exclude 'immich/postgres/' \
  --exclude 'dmaga/node_modules/' \
  --exclude 'dmaga/.next/' \
  "${SERVICES_ROOT}/" "${stage}/deployment/var-www-services/"

{
  printf 'created_utc=%s\n' "${timestamp}"
  printf 'hostname=%s\n' "$(hostname)"
  printf 't7_uuid=%s\n' "${T7_UUID}"
  printf 'docker_version=%s\n' "$(docker version --format '{{.Server.Version}}')"
  docker compose ls --all
  docker ps -a --format '{{.Names}}|{{.Image}}|{{.Status}}|{{.Ports}}'
} >"${stage}/manifest.txt"

log "encrypting archive"
tar --zstd -C "${stage}" -cf - . \
  | openssl enc -aes-256-cbc -salt -pbkdf2 -iter 600000 \
    -pass "file:${KEY_FILE}" -out "${partial}"
mv -- "${partial}" "${archive}"

(
  cd "${BACKUP_ROOT}"
  sha256sum "$(basename "${archive}")" >"$(basename "${archive}").sha256"
)

log "removing encrypted backup sets older than ${RETENTION_DAYS} days"
find "${BACKUP_ROOT}" -maxdepth 1 -type f \
  \( -name 'existing-services-*.tar.zst.enc' -o -name 'existing-services-*.tar.zst.enc.sha256' \) \
  -mtime "+${RETENTION_DAYS}" -delete

log "backup complete: ${archive}"
