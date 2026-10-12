#!/usr/bin/env bash
# Run ssc-core for development, serving the built web UI.
#
#   scripts/run-dev.sh                  # loopback only, http://127.0.0.1:7777
#   scripts/run-dev.sh --lan            # bind this host's LAN address, reachable from a phone
#   scripts/run-dev.sh --lan --port 8080
#
# Prints the pairing URL on startup — the UI refuses to talk to the API without
# it, so open THAT url, not the bare host:port. The token is also what the QR
# code in the desktop shell will encode (DESIGN.md section 1).
#
# ssc-core refuses to bind anything that is not loopback or RFC1918 (DESIGN
# section 9). That refusal is deliberate; do not work around it.
set -euo pipefail

cd "$(dirname "$0")/.."
export PATH="$HOME/.local/bin:$PATH"

HOST=127.0.0.1
PORT=7777
LAN=0

while [ $# -gt 0 ]; do
  case "$1" in
    --lan)  LAN=1; shift ;;
    --port) PORT="$2"; shift 2 ;;
    --host) HOST="$2"; LAN=0; shift 2 ;;
    *) echo "run-dev: unknown option $1" >&2; exit 2 ;;
  esac
done

LAN_IP=""
if [ "$LAN" = 1 ]; then
  # The address the kernel would use to reach the LAN. Not gethostbyname(),
  # which returns 127.0.1.1 on many distros and would advertise loopback.
  LAN_IP=$(python3 -c "
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.connect(('192.0.2.1', 9))   # RFC 5737, no packet is sent
print(s.getsockname()[0])
s.close()")
  # Bind all interfaces rather than only $LAN_IP. Not cosmetic: /api/auth/pairing
  # answers only a loopback or already-paired caller, so binding a single LAN
  # address leaves nothing on loopback, the pairing token becomes unobtainable,
  # and the UI can never be paired at all. This is a workaround for a bug in
  # ssc-core, not the intended end state.
  HOST=0.0.0.0
  echo "run-dev: NOTE binding 0.0.0.0 (all interfaces), not just $LAN_IP."
  echo "run-dev:      Fine on a home LAN. Not on a machine with a public IP."
fi

if [ ! -d web/dist ]; then
  echo "run-dev: web/dist missing — building the UI first"
  npm --prefix web install --no-audit --no-fund
  npm --prefix web run build
fi

export SSC_STATIC_DIR="$PWD/web/dist"

# Met.no requires a genuinely identifying User-Agent with a contact — sending a
# generic one is a terms violation, not a missing nicety, so ssc-core refuses to
# run the provider without it rather than degrading quietly. Passed through from
# the environment and deliberately NOT baked into this file: a real contact
# address does not belong in a checked-in script.
if [ -z "${SSC_WEATHER_CONTACT:-}" ]; then
  echo "run-dev: NOTE SSC_WEATHER_CONTACT is unset — weather will report unavailable."
  echo "run-dev:      Set it to an email or URL Met.no can reach you at, e.g."
  echo "run-dev:      export SSC_WEATHER_CONTACT='you@example.com'"
fi

echo "run-dev: starting ssc-core on http://$HOST:$PORT"
echo "run-dev: static  $SSC_STATIC_DIR"
[ "$LAN" = 1 ] && echo "run-dev: reachable from other devices on this network"

# Print the pairing URL once the API answers. The endpoint is loopback-or-paired
# only, so this runs locally alongside the server rather than from elsewhere.
(
  for _ in $(seq 1 60); do
    if curl -sf --max-time 1 "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1; then
      url=$(curl -sf --max-time 2 "http://127.0.0.1:$PORT/api/auth/pairing" \
            | python3 -c "import sys,json; print(json.load(sys.stdin)['pairing_url'])" 2>/dev/null || true)
      if [ -n "${url:-}" ]; then
        # The endpoint reports the configured origin; swap in the bind address
        # so the printed link works from another device.
        ADDR="${LAN_IP:-127.0.0.1}"
        printf '\n  OPEN THIS:  %s\n\n' "${url/http:\/\/localhost:$PORT/http://$ADDR:$PORT}"
      fi
      break
    fi
    sleep 0.5
  done
) &

exec uv run python -m ssc.main --host "$HOST" --port "$PORT"
