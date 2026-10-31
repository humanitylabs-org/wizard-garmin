# Garmin (Wizard connector)

A Wizard OS connector app for one Garmin Connect account. It holds the owner's Garmin personal API key, keeps a private synced copy of daily summaries, sleep, HRV, Body Battery and activities, and offers that copy read-only to other apps on the same box through the `health-data` spot, contract [`wizard.health-data/1`](docs/health-data-contract.md). The [Health](https://github.com/oscarhenrycollins/wizard-health) app is the screen for the data; this app is only the connection.

**What the owner needs:** a free Garmin Connect account and a personal API key created on Garmin Connect Web (Garmin's self-serve [Connect Personal API](https://developer.garmin.com/gc-developer-program/personal/personal-faq/): individual users, their own data, no approval, no fee). Nothing is shared between boxes.

## Setup (all on the app's own screen)

1. A Garmin Connect account. Body Battery, sleep score and HRV status need a Garmin watch that measures them.
2. Open the Personal API Key page on Garmin Connect Web (https://connect.garmin.com/app/settings/personalApiToken) and sign in.
3. Create Key, accept Garmin's terms, pick an expiry (30, 90 or 180 days, or one year), Create. Garmin shows the key once.
4. Paste the key and pick the same expiry. The app tests it by trading it for an access token at Garmin's real token endpoint (`200` = valid, `401` = wrong, deleted or expired) and starts syncing.

Tokens last 24 h and Garmin issues no refresh tokens: the app trades the key again when needed. When the key runs out (Garmin emails a week before; the app warns 7 days before the expiry the owner picked) the owner creates a new key and uses Replace key.

## Data and sync

- Backfill after the key is accepted, then every 20 minutes with a 4-day overlap: daily summaries (one call covers a year), sleep and HRV in 31-day windows, Body Battery at wake one call per day (last 90 days on backfill), and the activity search, paged.
- 429 answers are retried with backoff; a long pause stops syncing for 15 minutes.
- Mappings (readiness = Body Battery at wake, load = intensity minutes with Garmin-goal targets) are in `docs/health-data-contract.md` under "Provider mappings".

## Verified vs not

Built from Garmin's official Personal API Swagger and Garmin's own Python client, tested against a fake API built from those schemas and example values. The token endpoint was called for real with fake keys (401). Not yet run against a real Garmin account: the exact `sleepScores` key (`overall`) and the units of `sleepNeed.actual` (minutes) are not spelled out in the Swagger and are read defensively.

## Interfaces

- Screen: `/wizard-garmin/` (status, Sync now, Replace key, Disconnect, guided setup).
- Contract (box-only: refused through the private-access proxy): `GET /api/status`, `GET /api/days?from&to&limit&page&changed_since`.
- Setup API used by the screen: `GET /api/setup`, `POST /api/key`, `/api/key/test`, `/api/key/forget`, `/api/sync`, `/api/disconnect`.
- MCP (`POST /mcp`, box-only): `health_data_status`, `health_data_sync`, `health_data_days`.
