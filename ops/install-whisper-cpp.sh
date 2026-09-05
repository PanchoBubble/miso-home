#!/usr/bin/env bash
set -Eeuo pipefail

WHISPER_VERSION="v1.9.1"
WHISPER_MODEL="tiny"
MODEL_SHA256="be07e048e1e599ad46341c8d2a135645097a538221678b7acdd1b1919c6e1b21"
MODEL_URL="https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-${WHISPER_MODEL}.bin"
INSTALL_ROOT="/usr/local/libexec/miso-whisper"
MODEL_ROOT="/var/lib/miso/models/whisper"
OPS_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_SOURCE="${OPS_ROOT}/systemd/miso-stt.env"
UNIT_SOURCE="${OPS_ROOT}/systemd/miso-whisper.service"
# whisper-server binds loopback only. Nothing outside the Pi may reach it, and
# cloudflared publishes 127.0.0.1:80, so the port must stay off that host.
SERVER_HOST="127.0.0.1"
SERVER_PORT="8910"

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

[[ "${EUID}" -eq 0 ]] || fail "run as root"
for command in cmake curl g++ getent git install sha256sum systemctl; do
  command -v "${command}" >/dev/null || fail "missing command: ${command}"
done
[[ -f "${ENV_SOURCE}" ]] || fail "STT environment file not found: ${ENV_SOURCE}"
[[ -f "${UNIT_SOURCE}" ]] || fail "whisper unit not found: ${UNIT_SOURCE}"
getent passwd miso >/dev/null || fail "miso service user is not configured"
getent group miso >/dev/null || fail "miso service group is not configured"

BUILD_ROOT="$(mktemp -d /tmp/miso-whisper-install.XXXXXX)"
cleanup() {
  rm -rf -- "${BUILD_ROOT}"
}
trap cleanup EXIT

git clone --branch "${WHISPER_VERSION}" --depth 1 \
  https://github.com/ggml-org/whisper.cpp.git "${BUILD_ROOT}/source"
cmake -S "${BUILD_ROOT}/source" -B "${BUILD_ROOT}/build" \
  -DCMAKE_BUILD_TYPE=Release \
  -DBUILD_SHARED_LIBS=OFF \
  -DGGML_NATIVE=ON \
  -DWHISPER_BUILD_TESTS=OFF \
  -DWHISPER_BUILD_SERVER=ON \
  -DWHISPER_SDL2=OFF
# The server target is what keeps the model resident. Reloading ggml-tiny for
# every utterance was most of the measured transcription latency.
cmake --build "${BUILD_ROOT}/build" --config Release \
  --target whisper-cli whisper-server -j4

curl --fail --location --retry 3 --output "${BUILD_ROOT}/ggml-${WHISPER_MODEL}.bin" \
  "${MODEL_URL}"
printf '%s  %s\n' "${MODEL_SHA256}" "${BUILD_ROOT}/ggml-${WHISPER_MODEL}.bin" \
  | sha256sum --check --status \
  || fail "downloaded whisper model failed SHA-256 verification"

install -d -o root -g root -m 0755 "${INSTALL_ROOT}"
install -o root -g root -m 0755 "${BUILD_ROOT}/build/bin/whisper-cli" \
  "${INSTALL_ROOT}/whisper-cli"
ln -sfn "${INSTALL_ROOT}/whisper-cli" /usr/local/bin/whisper-cli
install -o root -g root -m 0755 "${BUILD_ROOT}/build/bin/whisper-server" \
  "${INSTALL_ROOT}/whisper-server"
ln -sfn "${INSTALL_ROOT}/whisper-server" /usr/local/bin/whisper-server
install -d -o root -g miso -m 0750 "${MODEL_ROOT}"
install -o root -g miso -m 0640 "${BUILD_ROOT}/ggml-${WHISPER_MODEL}.bin" \
  "${MODEL_ROOT}/ggml-${WHISPER_MODEL}.bin"
install -o root -g root -m 0644 "${ENV_SOURCE}" /etc/miso/miso-stt.env
install -o root -g root -m 0644 "${UNIT_SOURCE}" \
  /etc/systemd/system/miso-whisper.service

/usr/local/bin/whisper-cli --version
systemctl daemon-reload
systemctl enable --now miso-whisper.service

for _ in $(seq 1 30); do
  if curl --silent --fail --max-time 1 --output /dev/null \
    "http://${SERVER_HOST}:${SERVER_PORT}/" 2>/dev/null; then
    break
  fi
  sleep 1
done
systemctl is-active --quiet miso-whisper.service \
  || fail "miso-whisper.service did not stay active"

if systemctl is-active --quiet miso.service; then
  systemctl restart miso.service
fi
printf 'Installed whisper.cpp %s CLI and resident server with %s model\n' \
  "${WHISPER_VERSION}" "${WHISPER_MODEL}"
