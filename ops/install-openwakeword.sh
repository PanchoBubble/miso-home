#!/usr/bin/env bash
set -Eeuo pipefail

OPENWAKEWORD_VERSION="0.6.0"
INSTALL_ROOT="/opt/miso/openwakeword"
MODEL_ROOT="/var/lib/miso/models/openwakeword"
SCRIPT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_ROOT}/.." && pwd)"
ENV_SOURCE="${SCRIPT_ROOT}/systemd/miso-wake.env"
BUNDLED_MODEL="${PROJECT_ROOT}/models/openwakeword/miso.onnx"
BUNDLED_MODEL_SHA256="f7d67c3d67911e65ff51a10967661b56b1aead161efe3816646a5190aa2ba59f"
MODEL_SOURCE="${1:-${BUNDLED_MODEL}}"
MODEL_SHA256="${2:-}"
MODEL_DATA_SHA256="${3:-}"

if [[ -z "${MODEL_SHA256}" && "${MODEL_SOURCE}" == "${BUNDLED_MODEL}" ]]; then
  MODEL_SHA256="${BUNDLED_MODEL_SHA256}"
fi

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

[[ "${EUID}" -eq 0 ]] || fail "run as root"
[[ -n "${MODEL_SOURCE}" && -f "${MODEL_SOURCE}" ]] \
  || fail "wake model not found: ${MODEL_SOURCE}"
[[ "${MODEL_SHA256}" =~ ^[0-9a-fA-F]{64}$ ]] \
  || fail "usage: $0 [/path/to/miso.onnx MODEL_SHA256 [MODEL_DATA_SHA256]]"
[[ -f "${ENV_SOURCE}" ]] || fail "wake environment file not found: ${ENV_SOURCE}"
for command in getent install python3 sha256sum systemctl; do
  command -v "${command}" >/dev/null || fail "missing command: ${command}"
done
getent passwd miso >/dev/null || fail "miso service user is not configured"
getent group miso >/dev/null || fail "miso service group is not configured"
printf '%s  %s\n' "${MODEL_SHA256}" "${MODEL_SOURCE}" \
  | sha256sum --check --status || fail "wake model checksum verification failed"

if [[ -f "${MODEL_SOURCE}.data" ]]; then
  [[ "${MODEL_DATA_SHA256}" =~ ^[0-9a-fA-F]{64}$ ]] \
    || fail "the ONNX sidecar requires its SHA-256 as the third argument"
  printf '%s  %s\n' "${MODEL_DATA_SHA256}" "${MODEL_SOURCE}.data" \
    | sha256sum --check --status || fail "wake model sidecar checksum failed"
elif [[ -n "${MODEL_DATA_SHA256}" ]]; then
  fail "a sidecar checksum was supplied but ${MODEL_SOURCE}.data is missing"
fi

rm -rf -- "${INSTALL_ROOT}"
install -d -o root -g root -m 0755 "${INSTALL_ROOT}"
python3 -m venv "${INSTALL_ROOT}"
"${INSTALL_ROOT}/bin/python" -m pip install --disable-pip-version-check \
  --no-cache-dir "openwakeword==${OPENWAKEWORD_VERSION}"
"${INSTALL_ROOT}/bin/python" -c \
  'import openwakeword.utils; openwakeword.utils.download_models()'
chown -R root:root "${INSTALL_ROOT}"

install -d -o root -g miso -m 0750 "${MODEL_ROOT}"
install -o root -g miso -m 0640 "${MODEL_SOURCE}" "${MODEL_ROOT}/miso.onnx"
if [[ -f "${MODEL_SOURCE}.data" ]]; then
  install -o root -g miso -m 0640 "${MODEL_SOURCE}.data" \
    "${MODEL_ROOT}/miso.onnx.data"
else
  rm -f -- "${MODEL_ROOT}/miso.onnx.data"
fi
install -o root -g root -m 0644 "${ENV_SOURCE}" /etc/miso/miso-wake.env

"${INSTALL_ROOT}/bin/python" -c \
  'from openwakeword.model import Model; Model(wakeword_models=["/var/lib/miso/models/openwakeword/miso.onnx"], inference_framework="onnx"); print("openWakeWord model loaded")'
if systemctl is-active --quiet miso.service; then
  systemctl restart miso.service
fi
printf 'Installed openWakeWord %s with verified Miso model\n' \
  "${OPENWAKEWORD_VERSION}"
