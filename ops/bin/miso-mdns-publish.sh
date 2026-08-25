#!/usr/bin/env bash
set -Eeuo pipefail

ALIAS="${1:-miso.local}"
REQUESTED_INTERFACE="${2:-auto}"

pids=()

select_interface() {
  if [[ "${REQUESTED_INTERFACE}" != "auto" ]]; then
    printf '%s\n' "${REQUESTED_INTERFACE}"
    return
  fi

  # Follow the usable LAN default route instead of assuming Ethernet. This
  # keeps the alias available when the appliance moves between eth0 and wlan0.
  ip -4 route show default \
    | awk '/ dev / { for (i = 1; i <= NF; i++) if ($i == "dev") { print $(i + 1); exit } }'
}

INTERFACE="$(select_interface)"
[[ -n "${INTERFACE}" ]] || {
  printf 'ERROR: no interface with a default IPv4 route\n' >&2
  exit 1
}

first_address() {
  ip -"$1" -o addr show dev "${INTERFACE}" scope global \
    | awk '{print $4}' | cut -d/ -f1 | head -n1
}

publish() {
  /usr/bin/avahi-publish-address --no-fail --no-reverse "${ALIAS}" "$1" &
  pids+=("$!")
}

terminate() {
  [[ "${#pids[@]}" -gt 0 ]] && kill "${pids[@]}" 2>/dev/null
  return 0
}

ipv4="$(first_address 4)"
[[ -n "${ipv4}" ]] || {
  printf 'ERROR: no global IPv4 address on %s\n' "${INTERFACE}" >&2
  exit 1
}
publish "${ipv4}"

# mDNS has no negative answer, so a missing AAAA record makes resolvers wait out
# their full IPv6 timeout (~5s) on every lookup of the alias.
ipv6="$(first_address 6)"
[[ -n "${ipv6}" ]] && publish "${ipv6}"

trap terminate EXIT INT TERM

# Any publisher exiting means the alias is no longer fully advertised.
wait -n "${pids[@]}" || true
exit 1
