#!/usr/bin/env bash
set -Eeuo pipefail

# Parakeet TDT 0.6B v3, int8 ONNX, served by sherpa-onnx. Chosen by benchmark:
# 0.44 s median against 0.90 s for a tuned whisper-server and 3.0 s for the
# whisper CLI, at 3.66% mean word error rate on the bilingual fixtures.
# See docs/miso-transcription-benchmark.md.
SHERPA_VERSION="1.13.7"
MODEL_NAME="sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8"
MODEL_SHA256="5793d0fd397c5778d2cf2126994d58e9d56b1be7c04d13c7a15bb1b4eafb16bf"
MODEL_URL="https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/${MODEL_NAME}.tar.bz2"
INSTALL_ROOT="/opt/miso/parakeet"
MODEL_ROOT="/var/lib/miso/models/parakeet"
OPS_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_SOURCE="${OPS_ROOT}/systemd/miso-parakeet.service"
ENV_SOURCE="${OPS_ROOT}/systemd/miso-stt.env"
SERVER_URL="http://127.0.0.1:8911/"

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

[[ "${EUID}" -eq 0 ]] || fail "run as root"
for command in curl getent install python3 sha256sum systemctl tar; do
  command -v "${command}" >/dev/null || fail "missing command: ${command}"
done
[[ -f "${UNIT_SOURCE}" ]] || fail "unit not found: ${UNIT_SOURCE}"
[[ -f "${ENV_SOURCE}" ]] || fail "STT environment not found: ${ENV_SOURCE}"
getent passwd miso >/dev/null || fail "miso service user is not configured"

BUILD_ROOT="$(mktemp -d /tmp/miso-parakeet-install.XXXXXX)"
cleanup() { rm -rf -- "${BUILD_ROOT}"; }
trap cleanup EXIT

curl --fail --location --retry 3 --output "${BUILD_ROOT}/model.tar.bz2" "${MODEL_URL}"
printf '%s  %s\n' "${MODEL_SHA256}" "${BUILD_ROOT}/model.tar.bz2" \
  | sha256sum --check --status \
  || fail "downloaded Parakeet model failed SHA-256 verification"
tar -xf "${BUILD_ROOT}/model.tar.bz2" -C "${BUILD_ROOT}"

# sherpa-onnx and its ONNX runtime stay in their own virtualenv, the way Piper
# and openWakeWord do, so the Miso runtime keeps its empty dependency list.
python3 -m venv "${INSTALL_ROOT}"
"${INSTALL_ROOT}/bin/python" -m pip install --disable-pip-version-check --quiet \
  --upgrade pip
"${INSTALL_ROOT}/bin/python" -m pip install --disable-pip-version-check --quiet \
  "sherpa-onnx==${SHERPA_VERSION}" numpy

install -d -o root -g miso -m 0750 "${MODEL_ROOT}"
for asset in encoder.int8.onnx decoder.int8.onnx joiner.int8.onnx tokens.txt; do
  install -o root -g miso -m 0640 \
    "${BUILD_ROOT}/${MODEL_NAME}/${asset}" "${MODEL_ROOT}/${asset}"
done

install -o root -g root -m 0644 "${UNIT_SOURCE}" \
  /etc/systemd/system/miso-parakeet.service
install -o root -g root -m 0644 "${ENV_SOURCE}" /etc/miso/miso-stt.env

systemctl daemon-reload
systemctl enable --now miso-parakeet.service

for _ in $(seq 1 60); do
  if curl --silent --fail --max-time 1 --output /dev/null "${SERVER_URL}" 2>/dev/null; then
    break
  fi
  sleep 1
done
systemctl is-active --quiet miso-parakeet.service \
  || fail "miso-parakeet.service did not stay active"
curl --silent --fail --max-time 5 --output /dev/null "${SERVER_URL}" \
  || fail "parakeet worker is not answering on loopback"

if systemctl is-active --quiet miso.service; then
  systemctl restart miso.service
fi
printf 'Installed Parakeet %s with sherpa-onnx %s\n' "${MODEL_NAME}" "${SHERPA_VERSION}"
