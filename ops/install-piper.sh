#!/usr/bin/env bash
set -euo pipefail

PIPER_VERSION="1.4.2"
INSTALL_ROOT="/opt/miso/piper"
MODEL_ROOT="/var/lib/miso/models/piper"
ENV_SOURCE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/systemd/miso-tts.env"
VOICE_BASE_URL="https://huggingface.co/rhasspy/piper-voices/resolve/main"
ENGLISH_VOICE="en_GB-cori-medium"
SPANISH_VOICE="es_ES-davefx-medium"
ENGLISH_SHA256="1899f98e5fb8310154f3c2973f4b8a929ba7245e722b3d3a85680b833d95f10d"
ENGLISH_CONFIG_SHA256="e262c16d7f192f69d4edd6b4ef8a5915379e67495fcc402f1ab15eeb33da3d36"
SPANISH_SHA256="6658b03b1a6c316ee4c265a9896abc1393353c2d9e1bca7d66c2c442e222a917"
SPANISH_CONFIG_SHA256="0e0dda87c732f6f38771ff274a6380d9252f327dca77aa2963d5fbdf9ec54842"

fail() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

[[ "$(id -u)" -eq 0 ]] || fail "run this installer as root"
for command in curl getent install python3 sha256sum systemctl; do
  command -v "${command}" >/dev/null || fail "missing command: ${command}"
done
[[ -f "${ENV_SOURCE}" ]] || fail "TTS environment file not found: ${ENV_SOURCE}"
getent passwd miso >/dev/null || fail "the miso user must exist first"
getent group miso >/dev/null || fail "the miso group must exist first"

install_tmp="$(mktemp -d /tmp/miso-piper-install.XXXXXX)"
trap 'rm -rf -- "${install_tmp}"' EXIT

download_voice() {
  local voice="$1"
  local voice_path="$2"
  local model_sha="$3"
  local config_sha="$4"
  local model_file="${install_tmp}/${voice}.onnx"
  local config_file="${model_file}.json"

  curl --fail --location --retry 3 --output "${model_file}" \
    "${VOICE_BASE_URL}/${voice_path}/${voice}.onnx"
  curl --fail --location --retry 3 --output "${config_file}" \
    "${VOICE_BASE_URL}/${voice_path}/${voice}.onnx.json"
  printf '%s  %s\n' "${model_sha}" "${model_file}" | sha256sum --check --status \
    || fail "${voice} model checksum verification failed"
  printf '%s  %s\n' "${config_sha}" "${config_file}" | sha256sum --check --status \
    || fail "${voice} config checksum verification failed"
}

download_voice "${ENGLISH_VOICE}" "en/en_GB/cori/medium" \
  "${ENGLISH_SHA256}" "${ENGLISH_CONFIG_SHA256}"
download_voice "${SPANISH_VOICE}" "es/es_ES/davefx/medium" \
  "${SPANISH_SHA256}" "${SPANISH_CONFIG_SHA256}"

rm -rf -- "${INSTALL_ROOT}"
install -d -o root -g root -m 0755 "${INSTALL_ROOT}"
python3 -m venv "${INSTALL_ROOT}"
"${INSTALL_ROOT}/bin/python" -m pip install --disable-pip-version-check \
  --no-cache-dir "piper-tts==${PIPER_VERSION}"
chown -R root:root "${INSTALL_ROOT}"
ln -sfn "${INSTALL_ROOT}/bin/piper" /usr/local/bin/piper

install -d -o root -g miso -m 0750 "${MODEL_ROOT}"
for voice in "${ENGLISH_VOICE}" "${SPANISH_VOICE}"; do
  install -o root -g miso -m 0640 "${install_tmp}/${voice}.onnx" \
    "${MODEL_ROOT}/${voice}.onnx"
  install -o root -g miso -m 0640 "${install_tmp}/${voice}.onnx.json" \
    "${MODEL_ROOT}/${voice}.onnx.json"
done
install -o root -g root -m 0644 "${ENV_SOURCE}" /etc/miso/miso-tts.env

"${INSTALL_ROOT}/bin/python" -c \
  'from importlib.metadata import version; print("piper-tts", version("piper-tts"))'
if systemctl is-active --quiet miso.service; then
  systemctl restart miso.service
fi
printf 'Installed Piper %s with %s and %s voices\n' \
  "${PIPER_VERSION}" "${ENGLISH_VOICE}" "${SPANISH_VOICE}"
