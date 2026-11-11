---
name: wizard-garmin
description: Use when the owner asks about his Garmin connection, Garmin setup, key or sync. Health questions go to Health.
version: 0.1.1
author: Humanity Labs
license: GPL-3.0-only
metadata:
  hermes:
    tags: [garmin, health-data, connector]
    related_skills: [wizard-health]
---
# Garmin (connector)

The box's Garmin connection (`health-data` spot, contract `wizard.health-data/1`). It holds the Garmin personal API key and a synced copy; the **Health** app (`/wizard-health/`) shows the data and answers health questions. Tools: `mcp__wizard__wizard_garmin__<tool>`.

- Questions about Body Battery, sleep, HRV, resting heart rate, intensity minutes, activities or trends: use the Health app's tools, not these.
- "Is my Garmin connected / when did it last sync": `health_data_status`. `connected`, `syncing` or `rate_limited` = data is flowing; `not_configured` = setup not finished; `needs_login` = Garmin stopped accepting the key (it ran out or was deleted): the owner creates a new key. `setup.key_days_left` says when the key runs out.
- Setup or a new key: send the owner the app link `[Garmin](/wizard-garmin/)` and say its screen walks him through it (four steps: Garmin Connect account, Personal API Key page, Create Key, paste it). Never walk him through Garmin's site in chat, and never ask for or accept the key or his Garmin password in chat; the key goes only into the app's own screen.
- `health_data_sync` asks for fresh Garmin data now (the app also syncs every 20 minutes); it returns at once.
- `health_data_days` is the raw contract read. Readiness is Body Battery at wake (Garmin's Personal API has no Training Readiness); load is intensity minutes (moderate + 2 x vigorous) with targets from Garmin's weekly goal. Use it only to check what the connector holds.
- Scripts on the box can read the same contract at `http://127.0.0.1:8838/api/status` and `/api/days`.
