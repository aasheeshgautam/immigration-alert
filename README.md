# immigration-alert

Watches the Philippine Bureau of Immigration [visa application status page](https://immigration.gov.ph/resources/visa-application-status/)
and messages me on Telegram when application number **2026229963** appears on a
published agenda PDF.

## Schedule

Runs twice a day — **11:00 AM** and **6:00 PM** Philippine time (`0 3 * * *` and
`0 10 * * *` UTC). GitHub sometimes starts scheduled jobs a little late when its
queue is busy; that's normal and doesn't affect the result.

## What it does on each run

1. Opens the **"VISA APPLICATION STATUS (Agenda Verification &lt;year&gt;)"**
   accordion for the current year and sorts its PDFs by the date in the
   filename. Any new list for any month (July, August, … December) is picked up
   automatically — nothing is pinned to a specific date. At New Year it targets
   the new year's panel, falling back to the newest panel present.
2. Downloads the **3 newest** PDFs and extracts every 10-digit application
   number. Scanning more than one means a failed run can't cause a missed list —
   the site times out fairly often.
3. Reacts:

| Situation | What happens |
| --- | --- |
| **2026229963 is on the list** | 🎉 Alert — and it repeats **every run** until I reply `stop` in Telegram |
| **Any number from 2026229900 to 2026229963 appears** | 👀 "They're getting close" alert, once per number |
| **Nothing new** | Silent, except the Saturday check-in |
| **Every Saturday** | 📅 Weekly check-in so I know it's still alive and running |
| **The run fails** | ⚠️ Error message, instead of dying silently in CI |

## Stopping it

When the number is found, the alert deliberately repeats twice a day so it can't
be missed. To stop it, **reply `stop` in the Telegram chat**. The next run picks
that up, confirms, and goes permanently quiet. Nothing stops automatically —
manual confirmation is required by design.

## Setup

Two repo secrets:

| Secret | Value |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | From [@BotFather](https://t.me/BotFather) |
| `TELEGRAM_CHAT_ID` | My **numeric** chat id |

`TELEGRAM_CHAT_ID` must be the numeric id — a `@username` only works for
channels, never a private chat. The bot also can't message anyone who hasn't
opened it and pressed **Start** at least once. Those two mistakes are the usual
reason a bot looks configured but never delivers.

## Checking that it works

Actions tab → *Immigration Status Checker* → **Run workflow**, tick
**Send a Telegram test message**. It prints the bot username, the configured chat
id, and every chat the bot can currently see, then sends a test message.

## State

`state.json` is committed back by the workflow. It remembers which near-window
numbers were already announced, whether the number was found, whether `stop` was
received, and which week the last check-in went out — so nothing repeats itself.

## Configuration

Set as `env:` in `.github/workflows/check.yml`:

| Variable | Default | Meaning |
| --- | --- | --- |
| `TARGET_NUMBER` | `2026229963` | The number to watch for |
| `NEAR_FROM` | `2026229900` | Bottom of the "getting close" window |
| `SCAN_COUNT` | `3` | How many recent PDFs to scan per run |
