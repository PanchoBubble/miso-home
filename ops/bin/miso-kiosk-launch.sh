#!/usr/bin/env bash
set -Eeuo pipefail

MISO_URL="${MISO_KIOSK_URL:-http://miso.local/companion}"
HEALTH_URL="${MISO_KIOSK_HEALTH_URL:-http://miso.local/healthz}"
WAIT_SECONDS="${MISO_KIOSK_WAIT_SECONDS:-120}"

case "${WAIT_SECONDS}" in
  ''|*[!0-9]*)
    printf 'ERROR: MISO_KIOSK_WAIT_SECONDS must be a non-negative integer\n' >&2
    exit 2
    ;;
esac

browser=""
for candidate in chromium-browser chromium; do
  if command -v "${candidate}" >/dev/null 2>&1; then
    browser="$(command -v "${candidate}")"
    break
  fi
done
[[ -n "${browser}" ]] || {
  printf 'ERROR: Chromium is not installed\n' >&2
  exit 1
}

deadline=$((SECONDS + WAIT_SECONDS))
until curl --fail --silent --show-error --connect-timeout 2 --max-time 10 \
  --output /dev/null \
  "${HEALTH_URL}"; do
  if (( SECONDS >= deadline )); then
    printf 'WARNING: Miso did not become ready at %s within %s seconds\n' \
      "${HEALTH_URL}" "${WAIT_SECONDS}" >&2
    break
  fi
  sleep 1
done

exec "${browser}" --kiosk --app="${MISO_URL}"
