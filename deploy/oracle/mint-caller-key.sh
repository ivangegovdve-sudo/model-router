#!/usr/bin/env bash
# Run ON Oracle as opc:  sudo -v && bash mint-caller-key.sh <name> <cap-usd>
# Creates a spend-capped caller key in the running router's store and puts its value in
# GCP SM as modelrouter-key-<name>. The value moves CLI -> root-only temp file -> pipe ->
# gcloud; it is never printed. If the vault write fails, the key is revoked at once.
set -euo pipefail
NAME="$1"; CAP="$2"; PROJECT="forest-family-cloud"; SECRET="modelrouter-key-$NAME"
[[ "$NAME" =~ ^[a-z0-9-]{2,40}$ ]] || { echo "name: lowercase letters, digits, dashes"; exit 2; }
# Inside the router's own state dir (owned by modelrouter, mode 700 by StateDirectory), so the
# CLI -- running as modelrouter -- can create it; the CLI refuses to overwrite an existing file.
TMP="/var/lib/modelrouter/.mint-$(date +%s)-$RANDOM"
OUT=$(sudo -u modelrouter env MODELROUTER_CONFIG=/opt/modelrouter/config.toml MODELROUTER_STATE=/var/lib/modelrouter \
      /opt/modelrouter/venv/bin/modelrouter keys create --name "$NAME" --cap-usd "$CAP" \
      --store "file:$TMP" --note "minted by mint-caller-key.sh")
echo "$OUT"
KID=$(echo "$OUT" | sed -n 's/^created caller key \([0-9a-f]*\) .*/\1/p')
if sudo cat "$TMP" | gcloud secrets create "$SECRET" --replication-policy=automatic --project="$PROJECT" --data-file=- >/dev/null 2>&1; then
  echo "value stored in GCP SM $SECRET (project $PROJECT)"
else
  sudo -u modelrouter env MODELROUTER_CONFIG=/opt/modelrouter/config.toml MODELROUTER_STATE=/var/lib/modelrouter \
      /opt/modelrouter/venv/bin/modelrouter keys revoke "$KID"
  echo "ABORT: could not write $SECRET -- key $KID revoked"; RC=3
fi
sudo shred -u "$TMP" 2>/dev/null || sudo rm -f "$TMP"
exit "${RC:-0}"
