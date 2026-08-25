#!/usr/bin/env bash
set -Eeuo pipefail

SOURCE_ROOT="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
TARGET_ROOT="${TARGET_ROOT:-/opt/miso/app}"
UNIT_SOURCE="${SOURCE_ROOT}/ops/systemd/miso.service"
MDNS_UNIT_SOURCE="${SOURCE_ROOT}/ops/systemd/miso-mdns.service"
MDNS_PUBLISH_SOURCE="${SOURCE_ROOT}/ops/bin/miso-mdns-publish.sh"
TUNNEL_UNIT_SOURCE="${SOURCE_ROOT}/ops/systemd/miso-cloudflared.service"
TUNNEL_BOOTSTRAP_SOURCE="${SOURCE_ROOT}/ops/cloudflared/miso-bootstrap.yml"
CONVERSATION_ENV_SOURCE="${SOURCE_ROOT}/ops/systemd/miso-conversation.env"
CALENDAR_ENV_SOURCE="${SOURCE_ROOT}/ops/systemd/miso-calendar.env"
DISPLAY_UNIT_SOURCE="${SOURCE_ROOT}/ops/systemd/miso-display.service"
DISPLAY_ENV_SOURCE="${SOURCE_ROOT}/ops/systemd/miso-display.env"
DISPLAY_TMPFILES_SOURCE="${SOURCE_ROOT}/ops/systemd/miso-display.tmpfiles"
DISPLAY_HELPER_SOURCE="${SOURCE_ROOT}/ops/miso-display-idle.py"

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

[[ "${EUID}" -eq 0 ]] || fail "run as root"
for command in awk aplay arecord avahi-publish-address cloudflared getent install ip python3 rsync systemctl systemd-tmpfiles usermod; do
  command -v "${command}" >/dev/null || fail "missing command: ${command}"
done
[[ -d "${SOURCE_ROOT}/src/miso" ]] || fail "Miso source not found under ${SOURCE_ROOT}"
[[ -f "${UNIT_SOURCE}" ]] || fail "systemd unit not found: ${UNIT_SOURCE}"
[[ -f "${MDNS_UNIT_SOURCE}" ]] || fail "systemd unit not found: ${MDNS_UNIT_SOURCE}"
[[ -f "${MDNS_PUBLISH_SOURCE}" ]] || fail "helper not found: ${MDNS_PUBLISH_SOURCE}"
[[ -f "${TUNNEL_UNIT_SOURCE}" ]] || fail "systemd unit not found: ${TUNNEL_UNIT_SOURCE}"
[[ -f "${TUNNEL_BOOTSTRAP_SOURCE}" ]] || fail "tunnel bootstrap not found: ${TUNNEL_BOOTSTRAP_SOURCE}"
[[ -f "${CONVERSATION_ENV_SOURCE}" ]] || fail \
  "conversation environment not found: ${CONVERSATION_ENV_SOURCE}"
[[ -f "${CALENDAR_ENV_SOURCE}" ]] || fail \
  "calendar environment not found: ${CALENDAR_ENV_SOURCE}"
[[ -f "${DISPLAY_UNIT_SOURCE}" ]] || fail "display unit not found: ${DISPLAY_UNIT_SOURCE}"
[[ -f "${DISPLAY_ENV_SOURCE}" ]] || fail "display environment not found: ${DISPLAY_ENV_SOURCE}"
[[ -f "${DISPLAY_TMPFILES_SOURCE}" ]] || fail "display tmpfiles config not found: ${DISPLAY_TMPFILES_SOURCE}"
[[ -f "${DISPLAY_HELPER_SOURCE}" ]] || fail "display helper not found: ${DISPLAY_HELPER_SOURCE}"
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
install -o root -g root -m 0644 "${MDNS_UNIT_SOURCE}" \
  /etc/systemd/system/miso-mdns.service
install -o root -g root -m 0644 "${TUNNEL_UNIT_SOURCE}" \
  /etc/systemd/system/miso-cloudflared.service
install -o root -g root -m 0644 "${DISPLAY_UNIT_SOURCE}" \
  /etc/systemd/system/miso-display.service
install -d -o root -g root -m 0755 /etc/cloudflared
install -o root -g root -m 0644 "${TUNNEL_BOOTSTRAP_SOURCE}" \
  /etc/cloudflared/miso-bootstrap.yml
install -o root -g root -m 0755 "${MDNS_PUBLISH_SOURCE}" \
  /usr/local/bin/miso-mdns-publish
install -d -o root -g root -m 0755 /etc/miso
install -o root -g root -m 0644 "${CONVERSATION_ENV_SOURCE}" \
  /etc/miso/miso-conversation.env
if [[ ! -e /etc/miso/miso-calendar.env ]]; then
  install -o root -g root -m 0644 "${CALENDAR_ENV_SOURCE}" \
    /etc/miso/miso-calendar.env
fi
if [[ ! -e /etc/miso/miso-display.env ]]; then
  install -o root -g root -m 0644 "${DISPLAY_ENV_SOURCE}" \
    /etc/miso/miso-display.env
fi
install -d -o root -g root -m 0755 /usr/local/lib/miso
install -o root -g root -m 0755 "${DISPLAY_HELPER_SOURCE}" \
  /usr/local/lib/miso/miso-display-idle.py
install -o root -g root -m 0644 "${DISPLAY_TMPFILES_SOURCE}" \
  /etc/tmpfiles.d/miso-display.conf
systemd-tmpfiles --create /etc/tmpfiles.d/miso-display.conf

PYTHONPATH="${TARGET_ROOT}/src" PYTHONDONTWRITEBYTECODE=1 \
  python3 -m compileall -q "${TARGET_ROOT}/src"
systemctl daemon-reload
systemctl enable miso.service
systemctl enable miso-mdns.service
systemctl restart miso.service
systemctl restart miso-mdns.service
display_connected=false
for display_status in /sys/class/drm/card*-DSI-*/status; do
  if [[ -f "${display_status}" ]] && [[ "$(<"${display_status}")" == "connected" ]]; then
    display_connected=true
    break
  fi
done
if [[ "${display_connected}" == true ]] \
  && getent passwd pancho >/dev/null \
  && command -v swayidle >/dev/null \
  && command -v wlopm >/dev/null; then
  systemctl enable miso-display.service
  systemctl restart miso-display.service
else
  systemctl disable --now miso-display.service 2>/dev/null || true
  printf 'No supported local DSI display session; display idle service remains disabled\n'
fi
if [[ -s /etc/cloudflared/miso.token ]]; then
  systemctl enable miso-cloudflared.service
  systemctl restart miso-cloudflared.service
else
  printf 'Miso tunnel token is absent; connector remains disabled\n'
fi

printf 'Installed and started Miso from %s\n' "${SOURCE_ROOT}"
