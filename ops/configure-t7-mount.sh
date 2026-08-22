#!/usr/bin/env bash
set -Eeuo pipefail

T7_UUID="${T7_UUID:-081E-DA7A}"
MOUNT_POINT="${MOUNT_POINT:-/media/pancho/T7}"
FSTAB="${FSTAB:-/etc/fstab}"
BEGIN_MARKER="# BEGIN MISO MANAGED T7 MOUNT"
END_MARKER="# END MISO MANAGED T7 MOUNT"

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

[[ "${EUID}" -eq 0 ]] || fail "run as root"
[[ -f "${FSTAB}" ]] || fail "fstab not found: ${FSTAB}"
mounted_uuid="$(findmnt -rn -o UUID -T "${MOUNT_POINT}" 2>/dev/null \
  | awk 'NF { uuid = $1 } END { print uuid }')"
[[ "${mounted_uuid}" == "${T7_UUID}" ]] \
  || fail "expected T7 UUID ${T7_UUID} is not currently mounted at ${MOUNT_POINT}"

tmp="$(mktemp /var/tmp/miso-fstab.XXXXXX)"
cleanup() {
  rm -f -- "${tmp}"
}
trap cleanup EXIT

awk -v begin="${BEGIN_MARKER}" -v end="${END_MARKER}" '
  $0 == begin { managed = 1; next }
  $0 == end { managed = 0; next }
  !managed { print }
' "${FSTAB}" >"${tmp}"

{
  printf '\n%s\n' "${BEGIN_MARKER}"
  printf 'UUID=%s %s exfat rw,nosuid,nodev,nofail,x-systemd.automount,x-systemd.device-bound,x-systemd.device-timeout=15s,x-systemd.mount-timeout=30s,uid=1000,gid=1000,fmask=0022,dmask=0022 0 0\n' \
    "${T7_UUID}" "${MOUNT_POINT}"
  printf '%s\n' "${END_MARKER}"
} >>"${tmp}"

findmnt --verify --tab-file "${tmp}" >/dev/null

backup="${FSTAB}.pre-miso-$(date -u +%Y%m%dT%H%M%SZ)"
install -m 0644 "${FSTAB}" "${backup}"
install -m 0644 "${tmp}" "${FSTAB}"

printf 'Installed boot-safe T7 mount entry; previous fstab: %s\n' "${backup}"
