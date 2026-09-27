#!/usr/bin/env bash
# Deploy modelrouter to Oracle (forest-a1-v3), bound to 127.0.0.1:8687.
#
#   deploy/oracle/deploy.sh            print the plan, change nothing
#   deploy/oracle/deploy.sh --yes      install / upgrade and (re)start the service
#
# It does NOT touch nginx: making the router public is a separate, explicit step
# (see RUNBOOK.md, "Expose"). Secret values are read from GCP SM on Oracle by Oracle's
# own ADC and written straight into /etc/modelrouter.env (root 600); they never appear
# on a command line, in stdout, or on this machine.
set -euo pipefail

HOST="${ORACLE_HOST:-opc@144.24.59.30}"
KEY="${ORACLE_KEY:-$HOME/.ssh/forest-a1}"
PROJECT="forest-family-cloud"
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
STATE_DB="${MODELROUTER_STATE_DB:-}"      # optional: a local store to seed measured facts from
SSH=(ssh -o BatchMode=yes -o ConnectTimeout=20 -i "$KEY" "$HOST")

# env var -> SM secret. Adding a provider = one line here + one in config.toml.
SECRETS=(
  "MR_KEY_OPENROUTER=openrouter-spare"
  "MR_KEY_AKASHML=akashml-2026-09"
  "MR_KEY_VENICE=venice-api-key"
  "MR_KEY_NOUS=nous-fleet"
  "MR_KEY_SAIL=sail-api-key"
  "MR_KEY_IONET=ionet-intelligence-2026-09"
  "MR_OPERATOR_TOKEN=modelrouter-operator-token"
)

echo "plan:"
echo "  1. git archive the COMMITTED engine/ at HEAD ($(git -C "$REPO" rev-parse --short HEAD)) and copy it to $HOST (base64 over ssh)"
echo "  2. user 'modelrouter', /opt/modelrouter/venv (python3.11), install wheel, config, unit"
echo "  3. render /etc/modelrouter.env (root 600) from SM $PROJECT: ${SECRETS[*]%%=*}"
echo "  4. seed measured facts into /var/lib/modelrouter${STATE_DB:+ from $STATE_DB}"
echo "  5. systemctl enable --now modelrouter; check http://127.0.0.1:8687/health on the host"
[[ "${1:-}" == "--yes" ]] || { echo "dry run: nothing changed (re-run with --yes)"; exit 0; }

# 1. source: exactly what is committed, built on the host by its own pip
[[ -z "$(git -C "$REPO" status --porcelain engine)" ]] || { echo "engine/ has uncommitted changes -- commit first"; exit 2; }
SRC="$(mktemp -d)/modelrouter-src.tar.gz"
git -C "$REPO" archive --format=tar.gz -o "$SRC" HEAD engine
push() {  # push <local file> <remote path>
  base64 -w0 "$1" | "${SSH[@]}" "base64 -d > '$2'"
}
push "$SRC" /tmp/modelrouter-src.tar.gz
push "$HERE/config.toml" /tmp/modelrouter-config.toml
push "$HERE/modelrouter.service" /tmp/modelrouter.service

# 2-3. install, render env, start
"${SSH[@]}" bash -s -- "$PROJECT" "${SECRETS[@]}" <<'REMOTE'
set -euo pipefail
PROJECT="$1"; shift 1
id modelrouter >/dev/null 2>&1 || sudo useradd --system --home /opt/modelrouter --shell /sbin/nologin modelrouter
sudo mkdir -p /opt/modelrouter /var/lib/modelrouter
[ -x /opt/modelrouter/venv/bin/python ] || sudo python3.11 -m venv /opt/modelrouter/venv
# pip runs as root and writes build/ and *.egg-info into the source tree, so the tree is
# root-owned afterwards: clean it up with sudo, before and after (a previous run may have
# left one behind).
sudo rm -rf /tmp/modelrouter-src && mkdir -p /tmp/modelrouter-src && tar -xzf /tmp/modelrouter-src.tar.gz -C /tmp/modelrouter-src
sudo /opt/modelrouter/venv/bin/pip install -q --upgrade /tmp/modelrouter-src/engine
sudo rm -rf /tmp/modelrouter-src /tmp/modelrouter-src.tar.gz
sudo mv /tmp/modelrouter-config.toml /opt/modelrouter/config.toml
sudo mv /tmp/modelrouter.service /etc/systemd/system/modelrouter.service
# Render the delivery file. Values go gcloud -> shell variable -> sudo tee (stdout to
# /dev/null); a secret that cannot be read aborts the deploy by NAME, never by value.
TMP=$(mktemp); chmod 600 "$TMP"
for pair in "$@"; do
  var="${pair%%=*}"; name="${pair#*=}"
  if ! val=$(gcloud secrets versions access latest --secret="$name" --project="$PROJECT" 2>/dev/null) || [ -z "$val" ]; then
    rm -f "$TMP"; echo "ABORT: secret $name unreadable -- nothing restarted" >&2; exit 3
  fi
  printf '%s=%s\n' "$var" "$val" >> "$TMP"; unset val
done
sudo install -o root -g root -m 600 "$TMP" /etc/modelrouter.env; rm -f "$TMP"
sudo chown -R modelrouter:modelrouter /opt/modelrouter /var/lib/modelrouter
command -v restorecon >/dev/null && sudo restorecon -R /opt/modelrouter /etc/systemd/system/modelrouter.service /etc/modelrouter.env /var/lib/modelrouter || true
sudo systemctl daemon-reload
sudo systemctl enable --now modelrouter
sudo systemctl restart modelrouter
for i in $(seq 1 30); do curl -sf -m 2 http://127.0.0.1:8687/health && break; sleep 1; done
echo; sudo systemctl is-active modelrouter
REMOTE

# 4. seed measured facts (floors, decode speeds, concurrency caps, billed rates)
if [[ -n "$STATE_DB" ]]; then
  push "$STATE_DB" /tmp/modelrouter-seed.sqlite3
  "${SSH[@]}" 'sudo systemctl stop modelrouter && sudo install -o modelrouter -g modelrouter -m 600 /tmp/modelrouter-seed.sqlite3 /var/lib/modelrouter/modelrouter.sqlite3 && rm -f /tmp/modelrouter-seed.sqlite3 && (command -v restorecon >/dev/null && sudo restorecon /var/lib/modelrouter/modelrouter.sqlite3 || true) && sudo systemctl start modelrouter && sleep 3 && curl -sf http://127.0.0.1:8687/health'
fi
echo "deployed. Not public yet -- see RUNBOOK.md, 'Expose'."
