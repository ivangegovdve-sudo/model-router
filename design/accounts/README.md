# Prepaid accounts / credits: design sample (v1, NOT built)

Status: **awaiting Ivan's approval of the look.** Nothing functional exists yet; do not build
the app from these files until the look is approved.

Preview (live canvas, private to Ivan's account): https://claude.ai/artifact/LD9CVqrHGy32mWeZW8Nv1E

The model: prepaid accounts, not invoices. An account's balance IS its router caller key's hard
spend cap; a top-up raises the cap; every call is charged its actual cost; a call that could
overrun the balance is refused 402 before anything is spent (see `engine/modelrouter/clientkeys.py`,
`/v1/usage`).

| file | screen |
|---|---|
| `Main.dc.html` | Overview dashboard: balance vs topped-up, this month, API key (id only), top-up presets, 14-day spend, recent calls |
| `SignIn.dc.html` | Sign in / create account |
| `TopUp.dc.html` | Top-up payment step (payment form is a stub in v1) |
| `NewKey.dc.html` | New key, shown once (self-serve mint / rotate) |
| `canvas.json` | Canvas layout for the Design artifact |

Look ("Ledger"): dark graphite, one lime accent reserved for money actions, Bricolage Grotesque
display + IBM Plex Sans body + Plex Mono for figures only, rectangular controls.

Sample data: balance, chart and month totals. Recent-call rows use real charges from live router
tests (2026-09-26/27). Placeholders awaiting decisions: product name (wordmark reads
"modelrouter"), fees, expiry and refund terms, legal links, runway estimate.

The `.dc.html` files are Design-canvas components (they load `./support.js` from the canvas
runtime); they are the source of the preview above, not standalone pages.
