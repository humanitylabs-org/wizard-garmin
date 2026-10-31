---
wizard: 0.2
id: wizard-garmin
name: Garmin
type: external
category: system
version: 0.1.0
summary: Connect your Garmin. Health and your other apps get read-only Body Battery, sleep, HRV, heart rate and activity.
tags: [data]
license: GPL-3.0-only
icon: icon.png
source: https://github.com/oscarhenrycollins/wizard-garmin
runtime:
  kind: runtipi
  config: runtipi/config.json
  compose: runtipi/docker-compose.yml
api:
  port: 8838
  path: /api
  spec: openapi.json
mcp:
  path: /mcp
  port: 8838
  transport: streamable-http
skill: skill/SKILL.md
view:
  path: /
  port: 8838
  source:
    path: web
    build: python3 app/build_view.py
    dist: web
provides: [health-data, garmin-health-data]
system: true
uses: []
needs: []
health: /healthz
---

# Garmin

Connects one Garmin Connect account to your Wizard. You need a free Garmin Connect account (the one your Garmin watch syncs to) and a personal API key you create yourself on Garmin Connect's website, on its Personal API Key page (Garmin's self-serve Personal API, for your own data). The app walks you through it on its own screen: create the key, accept Garmin's terms, pick how long it lasts, then paste it here once. No developer account, business approval or fee. The key stays inside this app and is never shown again.

After the key is accepted the app downloads up to a year of daily summaries (resting heart rate, intensity minutes, steps, calories, Body Battery, stress), sleep with score and stages, overnight HRV with its status, the last 90 days of Body Battery at wake and your activities, then checks for new data every 20 minutes. It keeps the copy here and offers it, read-only, to other apps on this box through the `health-data` spot (contract `wizard.health-data/1`, see `docs/health-data-contract.md`). Body Battery at wake becomes the recovery score (Garmin's Personal API does not share Training Readiness) and intensity minutes the activity value, with documented formulas.

Garmin keys run out after the time you picked (30 days to one year). The app shows when, and Garmin emails you a week before; then you create a new key and paste it. Disconnect deletes the local copy.
