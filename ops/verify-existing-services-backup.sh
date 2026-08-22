#!/usr/bin/env bash
set -Eeuo pipefail

umask 077

BACKUP_ROOT="${BACKUP_ROOT:-/media/pancho/T7/backups/existing-services}"
KEY_FILE="${KEY_FILE:-/home/pancho/.config/miso-backup/backup.key}"
MODE="quick"
ARCHIVE=""

usage() {
  printf 'Usage: %s [--quick|--full] [archive]\n' "$0"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --quick) MODE="quick" ;;
    --full) MODE="full" ;;
    -h|--help) usage; exit 0 ;;
    *)
      [[ -z "${ARCHIVE}" ]] || { usage >&2; exit 2; }
      ARCHIVE="$1"
      ;;
  esac
  shift
done

if [[ -z "${ARCHIVE}" ]]; then
  ARCHIVE="$(find "${BACKUP_ROOT}" -maxdepth 1 -type f \
    -name 'existing-services-*.tar.zst.enc' -print | sort | tail -n 1)"
fi

[[ -n "${ARCHIVE}" && -f "${ARCHIVE}" ]] || { echo "backup archive not found" >&2; exit 1; }
[[ -r "${KEY_FILE}" ]] || { echo "backup key not readable: ${KEY_FILE}" >&2; exit 1; }

checksum="${ARCHIVE}.sha256"
[[ -f "${checksum}" ]] || { echo "checksum not found: ${checksum}" >&2; exit 1; }
(
  cd "$(dirname "${ARCHIVE}")"
  sha256sum -c "$(basename "${checksum}")"
)

stage="$(mktemp -d /var/tmp/miso-existing-services-verify.XXXXXX)"
pg_container=""
mysql_container=""

cleanup() {
  [[ -z "${pg_container}" ]] || docker rm -f "${pg_container}" >/dev/null 2>&1 || true
  [[ -z "${mysql_container}" ]] || docker rm -f "${mysql_container}" >/dev/null 2>&1 || true
  rm -rf -- "${stage}"
}
trap cleanup EXIT

openssl enc -d -aes-256-cbc -pbkdf2 -iter 600000 \
  -pass "file:${KEY_FILE}" -in "${ARCHIVE}" \
  | tar --zstd -C "${stage}" -xf -

required=(
  databases/immich.pgdump
  databases/nextcloud.sql
  vaultwarden/db.sqlite3
  manifest.txt
)
for path in "${required[@]}"; do
  [[ -s "${stage}/${path}" ]] || { echo "missing backup member: ${path}" >&2; exit 1; }
done

[[ "$(sqlite3 "${stage}/vaultwarden/db.sqlite3" 'PRAGMA integrity_check;')" == "ok" ]] \
  || { echo "Vaultwarden restore copy failed integrity check" >&2; exit 1; }

docker exec -i immich-postgres pg_restore --list \
  <"${stage}/databases/immich.pgdump" >/dev/null
grep -q '^-- Dump completed on ' "${stage}/databases/nextcloud.sql"

if [[ "${MODE}" == "quick" ]]; then
  echo "quick verification passed: ${ARCHIVE}"
  exit 0
fi

suffix="$(date +%s)-$$"
pg_container="miso-restore-check-pg-${suffix}"
mysql_container="miso-restore-check-mysql-${suffix}"
pg_image="$(docker inspect immich-postgres --format '{{.Config.Image}}')"
mysql_image="$(docker inspect nextcloud-db --format '{{.Config.Image}}')"

docker run -d --name "${pg_container}" --network none \
  --tmpfs /var/lib/postgresql/data:rw,nosuid,nodev,size=5g \
  -e POSTGRES_PASSWORD=restore-test -e POSTGRES_USER=postgres \
  -e POSTGRES_DB=immich "${pg_image}" >/dev/null

for _ in $(seq 1 90); do
  docker exec "${pg_container}" pg_isready -U postgres -d immich >/dev/null 2>&1 && break
  sleep 1
done
docker exec "${pg_container}" pg_isready -U postgres -d immich >/dev/null
docker exec -i "${pg_container}" pg_restore --exit-on-error --no-owner \
  --no-privileges --dbname=immich --username=postgres \
  <"${stage}/databases/immich.pgdump"
pg_tables="$(docker exec "${pg_container}" psql -U postgres -d immich -Atc \
  "select count(*) from pg_catalog.pg_tables where schemaname = 'public';")"
[[ "${pg_tables}" =~ ^[1-9][0-9]*$ ]] || { echo "Immich restore produced no tables" >&2; exit 1; }

docker run -d --name "${mysql_container}" --network none \
  --tmpfs /var/lib/mysql:rw,nosuid,nodev,size=2g \
  -e MYSQL_ROOT_PASSWORD=restore-test -e MYSQL_DATABASE=nextcloud \
  "${mysql_image}" >/dev/null

for _ in $(seq 1 120); do
  docker exec -e MYSQL_PWD=restore-test "${mysql_container}" \
    mysqladmin ping -h127.0.0.1 -uroot --silent >/dev/null 2>&1 && break
  sleep 1
done
docker exec -e MYSQL_PWD=restore-test "${mysql_container}" \
  mysqladmin ping -h127.0.0.1 -uroot --silent >/dev/null
docker exec -e MYSQL_PWD=restore-test -i "${mysql_container}" mysql -uroot nextcloud \
  <"${stage}/databases/nextcloud.sql"
mysql_tables="$(docker exec -e MYSQL_PWD=restore-test "${mysql_container}" mysql -N -uroot \
  -e "select count(*) from information_schema.tables where table_schema = 'nextcloud';")"
[[ "${mysql_tables}" =~ ^[1-9][0-9]*$ ]] || { echo "Nextcloud restore produced no tables" >&2; exit 1; }

echo "full restore verification passed: ${ARCHIVE}"
