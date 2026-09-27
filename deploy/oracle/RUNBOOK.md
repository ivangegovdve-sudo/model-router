# modelrouter on Oracle — first external caller

Goal: one person outside the fleet makes real, spend-capped calls to
`https://chloe.blumenkraft.cloud/modelrouter/v1`.

Everything below that changes production, the vault or the public internet is marked
**[IVAN]**. Nothing marked so has been run.

## Verified before writing this (2026-09-27, read-only)
- Oracle `forest-a1-v3` (aarch64): `python3.11` 3.11.13, `gcloud` with ADC, `nginx -t` OK,
  38 GB free, ports 8687–8689 free.
- Oracle's ADC reads all six provider keys, and every catalogue answers from Oracle:
  openrouter 458 models, akashml 6, venice 123, nous 417, sail 12, io.net 37.
- Caller keys, end to end on the PC against real providers: a $0.05-capped key answered
  ("Red, Blue, Yellow", charged $0.0000010574), an over-cap call was refused 402 before any
  provider was called, `/v1/usage` matched, operator routes returned 403. Test key revoked.

## Status (2026-09-27)
Steps 1 and 2 are DONE, with Ivan's go: `modelrouter-operator-token` v1 in SM; service
`active` on Oracle at 127.0.0.1:8687 from `main` @ 5b0952c, 66 measured models seeded; no
token -> 401; a real call answered on-host ($0.00000079); not reachable from the internet.
Steps 3 (public) and 4 (buyer key) await Ivan's per-step go.

## 1. Operator token **[IVAN — vault write]**
The router refuses to start off loopback without one. Mint a random value into SM:
```
python -c "import secrets,sys; sys.stdout.write(secrets.token_urlsafe(32))" | gcloud secrets create modelrouter-operator-token --replication-policy=automatic --project=forest-family-cloud --data-file=-
```

## 2. Deploy **[IVAN — prod]**
From the PC, repo root. Dry run first (prints the plan, changes nothing), then for real,
seeding the measured facts (floors, decode speeds, concurrency caps, billed rates):
```
bash deploy/oracle/deploy.sh
MODELROUTER_STATE_DB=N:/work/modelrouter-state/modelrouter.sqlite3 bash deploy/oracle/deploy.sh --yes
```
Ends with `/health` from 127.0.0.1:8687 on the host and `active`. Not public yet.
Register the port first: `D:\projects\PORT_REGISTRY.md`, Oracle 8687 (entry prepared).

## 3. Expose **[IVAN — public]**
Paste `deploy/oracle/nginx-location.conf` into the `chloe.blumenkraft.cloud` server block, then:
```
sudo cp /etc/nginx/conf.d/chloe-blumenkraft.conf /etc/nginx/conf.d/chloe-blumenkraft.conf.bak-$(date +%Y%m%d)-modelrouter
sudo nginx -t && sudo systemctl reload nginx
curl -s https://chloe.blumenkraft.cloud/modelrouter/health
```
The app refuses caller keys on every `/router/*` route (403) and requires the operator token there.

## 4. Mint the buyer's key **[IVAN — vault write, spends money up to the cap]**
On Oracle, as opc (`deploy/oracle/mint-caller-key.sh` copied over by step 2's kit, or paste it):
```
bash mint-caller-key.sh friend-1 5
```
Creates a caller key capped at $5, stores its value in SM `modelrouter-key-friend-1`, prints
only the key id. Deliver the value out of band (e.g. read it into a password manager share):
```
gcloud secrets versions access latest --secret=modelrouter-key-friend-1 --project=forest-family-cloud
```
Send the buyer `docs/BUYER.md`.

## Operate
```
sudo -u modelrouter env MODELROUTER_CONFIG=/opt/modelrouter/config.toml MODELROUTER_STATE=/var/lib/modelrouter /opt/modelrouter/venv/bin/modelrouter keys list
... keys usage <id>      ... keys set-cap <id> 10      ... keys revoke <id>
```
Revocation takes effect on the next request. Rotating a provider key: update it in SM, re-run
`deploy.sh --yes` (the env file is delivery, not the vault).

## Roll back
`sudo systemctl disable --now modelrouter` and remove the nginx location (restore the `.bak`).
Caller keys stop working immediately; nothing else on the host depends on the router.
