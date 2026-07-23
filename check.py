"""Immigration status watcher.

Checks the Philippine Bureau of Immigration agenda PDFs twice a day and pings
Telegram when application number TARGET_NUMBER shows up — or when the queue gets
close to it.

Once the number is found the alert repeats every run until you reply "stop" in
Telegram. That is deliberate: a single message is easy to miss.
"""

import datetime as dt
import io
import json
import os
import re
import sys
import time

import pdfplumber
import requests
from bs4 import BeautifulSoup

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

TARGET_NUMBER = os.environ.get("TARGET_NUMBER", "2026229963")
# Anything from here up to the target counts as "they're nearly at you".
NEAR_FROM = os.environ.get("NEAR_FROM", "2026229900")

PAGE_URL = "https://immigration.gov.ph/resources/visa-application-status/"
STATE_FILE = "state.json"

# Scan a few of the newest PDFs, not just one: the site times out often enough
# that a single failed run could otherwise make us miss a list entirely.
SCAN_COUNT = int(os.environ.get("SCAN_COUNT", "3"))
TEST_MODE = os.environ.get("TEST_MODE", "").lower() in ("1", "true", "yes")

PH_TZ = dt.timezone(dt.timedelta(hours=8))  # Asia/Manila, no DST
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
MONTHS = {
    m.lower(): i
    for i, m in enumerate(
        ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
         "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"],
        start=1,
    )
}
TG_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

session = requests.Session()
session.headers.update({"User-Agent": UA})


def now_ph():
    return dt.datetime.now(PH_TZ)


def stamp():
    return f"{now_ph():%a %d %b, %I:%M %p} PH time"


# ---------------------------------------------------------------- networking

def fetch(url, timeout=90, attempts=4):
    """GET with backoff — immigration.gov.ph times out regularly."""
    last = None
    for i in range(attempts):
        try:
            r = session.get(url, timeout=timeout)
            r.raise_for_status()
            return r
        except Exception as exc:  # noqa: BLE001 - retry anything transient
            last = exc
            print(f"  attempt {i + 1}/{attempts} failed: {exc}")
            if i < attempts - 1:
                time.sleep(5 * (i + 1))
    raise last


def send_telegram(message):
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        raise RuntimeError("Telegram is not configured (missing token or chat id).")
    r = requests.post(
        f"{TG_API}/sendMessage",
        data={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"},
        timeout=30,
    )
    if not r.ok:
        # Loud failure. A silently-rejected send is exactly how this alert died
        # without anyone noticing for weeks.
        raise RuntimeError(f"Telegram send failed: HTTP {r.status_code} {r.text}")
    print("Telegram message sent.")


def stop_requested(state):
    """Look for a 'stop' reply from you in Telegram."""
    if not TELEGRAM_BOT_TOKEN:
        return False
    params = {"timeout": 0}
    if state.get("tg_offset"):
        params["offset"] = state["tg_offset"]
    try:
        data = requests.get(f"{TG_API}/getUpdates", params=params, timeout=30).json()
    except Exception as exc:  # noqa: BLE001 - never let this break the check
        print(f"  could not read Telegram replies: {exc}")
        return False
    if not data.get("ok"):
        print(f"  getUpdates not ok: {data}")
        return False

    found_stop = False
    offset = state.get("tg_offset") or 0
    for upd in data.get("result", []):
        offset = max(offset, upd["update_id"] + 1)
        msg = upd.get("message") or {}
        chat_id = str((msg.get("chat") or {}).get("id", ""))
        if TELEGRAM_CHAT_ID and chat_id != str(TELEGRAM_CHAT_ID):
            continue
        text = (msg.get("text") or "").strip().lower()
        if text in ("stop", "/stop") or text.startswith("stop"):
            found_stop = True
    state["tg_offset"] = offset
    return found_stop


# -------------------------------------------------------------------- state

def load_state():
    try:
        with open(STATE_FILE) as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state):
    with open(STATE_FILE, "w") as fh:
        json.dump(state, fh, indent=2, sort_keys=True)
        fh.write("\n")


# --------------------------------------------------------------- scraping

def pdf_date(url):
    """Best-effort publication date from the filename, then the upload path."""
    name = url.rsplit("/", 1)[-1]

    m = re.search(r"(20\d{2})\s*([A-Za-z]{3})[a-z]*\s*(\d{1,2})", name)
    if m and m.group(2).lower() in MONTHS:
        try:
            return dt.date(int(m.group(1)), MONTHS[m.group(2).lower()], int(m.group(3)))
        except ValueError:
            pass

    m = re.search(r"([A-Za-z]{3})[a-z]*\s*(\d{1,2})\D+(20\d{2})", name)
    if m and m.group(1).lower() in MONTHS:
        try:
            return dt.date(int(m.group(3)), MONTHS[m.group(1).lower()], int(m.group(2)))
        except ValueError:
            pass

    m = re.search(r"\b(\d{2})\.(\d{2})\.(\d{2})\b", name)  # 03.21.25-AGENDA.pdf
    if m:
        try:
            return dt.date(2000 + int(m.group(3)), int(m.group(1)), int(m.group(2)))
        except ValueError:
            pass

    m = re.search(r"/uploads/(20\d{2})/(\d{2})/", url)  # coarse fallback
    if m:
        return dt.date(int(m.group(1)), int(m.group(2)), 1)

    return None


def get_pdf_urls():
    """Every agenda PDF on the page, newest first."""
    response = fetch(PAGE_URL)
    soup = BeautifulSoup(response.text, "html.parser")

    seen, links = set(), []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not href.lower().endswith(".pdf"):
            continue
        if not href.startswith("http"):
            href = "https://immigration.gov.ph" + href
        if href in seen:
            continue
        seen.add(href)
        links.append(href)

    floor = dt.date(1900, 1, 1)
    ordered = sorted(
        enumerate(links),
        key=lambda pair: (pdf_date(pair[1]) or floor, -pair[0]),
        reverse=True,
    )
    return [url for _, url in ordered]


def numbers_in_pdf(pdf_url):
    response = fetch(pdf_url, timeout=120)
    full_text = ""
    with pdfplumber.open(io.BytesIO(response.content)) as pdf:
        for page in pdf.pages:
            text = page.extract_text()
            if text:
                full_text += text + "\n"
    return sorted(set(re.findall(r"\b202\d{7}\b", full_text)))


# ------------------------------------------------------------------ messages

def found_message(pdf_url, first_time):
    opener = (
        "🎉🎉 <b>ASH, IT'S HERE!</b> 🎉🎉"
        if first_time
        else "🔔 <b>Still reminding you, Ash!</b>"
    )
    tail = (
        "I'll keep nudging you every check until you reply <b>stop</b> here. 🙌"
        if first_time
        else "You haven't told me to stop yet, so here I am again. 😄\n"
        "Reply <b>stop</b> when you've sorted it and I'll go quiet."
    )
    return (
        f"{opener}\n\n"
        f"Your number <b>{TARGET_NUMBER}</b> is on the immigration list! 🥳\n\n"
        f"📄 See it here: {pdf_url}\n"
        f"🏢 Head to the immigration office as soon as you can.\n"
        f"🕒 Spotted {stamp()}\n\n"
        f"{tail}"
    )


def near_message(hits, pdf_url):
    listed = ", ".join(f"<b>{n}</b>" for n in hits)
    return (
        f"👀 <b>Ooh, they're getting close!</b>\n\n"
        f"Numbers right below yours just showed up: {listed}\n"
        f"Yours is <b>{TARGET_NUMBER}</b> — so you could be next. 🤞\n\n"
        f"📄 List: {pdf_url}\n"
        f"🕒 {stamp()}\n\n"
        f"Keep your documents handy — I'm watching closely. 💪"
    )


def weekly_message(highest, gap, pdf_url, near_hits):
    if near_hits:
        mood = (
            f"This week numbers close to yours appeared: "
            f"{', '.join(near_hits)} 👀 You're nearly up!"
        )
    elif gap is None:
        mood = "I couldn't read any numbers off the latest list this week. 🤔"
    elif gap > 5000:
        mood = f"Still a fair way to go — about <b>{gap:,}</b> numbers ahead of you. 😌"
    elif gap > 0:
        mood = f"Getting closer! Only about <b>{gap:,}</b> numbers to go. 🙂"
    else:
        mood = "The list has moved past your range — worth a manual look. 🧐"

    return (
        f"📅 <b>Weekly check-in</b>\n\n"
        f"Hey Ash! Just letting you know I'm alive and still watching for "
        f"<b>{TARGET_NUMBER}</b>. ✅\n\n"
        f"No sign of your number yet.\n"
        f"Latest list reaches <b>{highest}</b>.\n"
        f"{mood}\n\n"
        f"📄 Newest list: {pdf_url}\n"
        f"🕒 {stamp()}\n\n"
        f"Talk again next Saturday! 👋"
    )


# ----------------------------------------------------------------- main flow

def run():
    state = load_state()

    if state.get("stopped"):
        print("Watcher was stopped by your Telegram 'stop' message. Nothing to do.")
        return

    # An explicit "stop" from you always wins, found or not.
    if stop_requested(state):
        state["stopped"] = True
        state["stopped_at"] = now_ph().isoformat()
        save_state(state)
        send_telegram(
            "👍 <b>Got it — stopping here.</b>\n\n"
            "I won't send any more immigration updates. "
            "All the best with the next steps, Ash! 🙏"
        )
        print("Stop acknowledged.")
        return

    print("Checking immigration website...")
    pdf_urls = get_pdf_urls()
    if not pdf_urls:
        raise RuntimeError("No PDF links found on the page — layout may have changed.")

    recent = pdf_urls[:SCAN_COUNT]
    print(f"Found {len(pdf_urls)} PDFs. Scanning newest {len(recent)}:")
    for u in recent:
        print(f"  - {u} ({pdf_date(u)})")

    found_in = None
    all_numbers = set()
    scanned = []

    for url in recent:
        try:
            numbers = numbers_in_pdf(url)
        except Exception as exc:  # noqa: BLE001 - one bad PDF shouldn't kill the run
            print(f"  could not read {url}: {exc}")
            continue
        scanned.append(url)
        print(f"  {url}: {len(numbers)} application numbers")
        all_numbers.update(numbers)
        if TARGET_NUMBER in numbers:
            found_in = url
            break

    if not scanned:
        raise RuntimeError("Could not read any of the recent PDFs.")

    target, near_from = int(TARGET_NUMBER), int(NEAR_FROM)
    highest = max(all_numbers) if all_numbers else None
    gap = target - int(highest) if highest else None
    newest = recent[0]

    state["last_run"] = now_ph().isoformat()
    state["last_scanned"] = scanned
    state["highest_seen"] = highest

    # 1. The number is on the list. Keep saying so until told to stop.
    if found_in:
        first_time = not state.get("found_at")
        if first_time:
            state["found_at"] = now_ph().isoformat()
            state["found_pdf"] = found_in
        send_telegram(found_message(found_in, first_time))
        save_state(state)
        print(f"FOUND in {found_in} (first_time={first_time}).")
        return

    # 2. Anything in the near window, e.g. 2026229900–2026229963.
    near_hits = sorted(n for n in all_numbers if near_from <= int(n) <= target)
    already = set(state.get("near_alerted", []))
    fresh = [n for n in near_hits if n not in already]
    print(f"Not found. Highest: {highest} (gap {gap}). Near-window hits: {near_hits or 'none'}.")

    if fresh:
        send_telegram(near_message(fresh, newest))
        state["near_alerted"] = sorted(already | set(fresh))
        save_state(state)
        return

    # 3. Saturday check-in, so silence never looks like breakage.
    today = now_ph().date()
    week_key = f"{today.isocalendar().year}-W{today.isocalendar().week:02d}"
    if today.weekday() == 5 and state.get("last_weekly") != week_key:
        send_telegram(weekly_message(highest, gap, newest, near_hits))
        state["last_weekly"] = week_key
        print("Weekly check-in sent.")
    else:
        print("Nothing new to report; staying quiet.")

    save_state(state)


def diagnose_telegram():
    """Print who the bot is and which chats it can actually reach.

    A bot can only message a chat that has messaged it first, and chat_id must
    be numeric (a @username only works for channels). Getting this wrong is the
    classic way a bot looks configured but never delivers anything.
    """
    if not TELEGRAM_BOT_TOKEN:
        print("Telegram: no bot token set.")
        return
    try:
        me = requests.get(f"{TG_API}/getMe", timeout=30).json()
    except Exception as exc:  # noqa: BLE001 - diagnostics must not break the run
        print(f"Telegram getMe error: {exc}")
        return
    if not me.get("ok"):
        print(f"Telegram getMe failed: {me}")
        return

    print(f"Telegram bot: @{me['result'].get('username')}")
    print(f"Configured chat_id: {TELEGRAM_CHAT_ID!r}")
    if TELEGRAM_CHAT_ID and not re.fullmatch(r"-?\d+", TELEGRAM_CHAT_ID.strip()):
        print("  WARNING: chat_id is not numeric. For a private chat this must be "
              "your numeric user id, not a @username.")

    try:
        updates = requests.get(f"{TG_API}/getUpdates", timeout=30).json()
        chats = {}
        for upd in updates.get("result", []):
            chat = (upd.get("message") or upd.get("channel_post") or {}).get("chat")
            if chat:
                chats[chat["id"]] = (
                    chat.get("username") or chat.get("title") or chat.get("first_name")
                )
        if chats:
            print("Chats this bot can see:")
            for cid, label in chats.items():
                print(f"  {cid}  ({label})")
        else:
            print("No chats visible. Open Telegram, message the bot, press Start, "
                  "then re-run this test.")
    except Exception as exc:  # noqa: BLE001
        print(f"Telegram getUpdates error: {exc}")


def main():
    if TEST_MODE:
        diagnose_telegram()
        send_telegram(
            "🧪 <b>Test message</b>\n\n"
            f"Hi Ash! Your immigration watcher is set up and can reach you here. 👋\n\n"
            f"I'm looking for <b>{TARGET_NUMBER}</b>, and I'll also shout if anything "
            f"from <b>{NEAR_FROM}</b> upward shows up. 👀\n"
            f"Checks run at <b>11:00 AM</b> and <b>6:00 PM</b> PH time, with a "
            f"check-in every Saturday. 📅\n\n"
            f"🕒 {stamp()}"
        )
        return

    try:
        run()
    except Exception as exc:  # noqa: BLE001 - surface failures instead of dying silently
        print(f"ERROR: {exc}")
        try:
            send_telegram(
                f"⚠️ <b>Hmm, my check didn't go through</b>\n\n"
                f"Something went wrong while checking the immigration site:\n"
                f"<code>{type(exc).__name__}: {exc}</code>\n\n"
                f"Don't worry — I'll try again at the next check. 🔁\n"
                f"🕒 {stamp()}"
            )
        except Exception as send_exc:  # noqa: BLE001
            print(f"Could not send error alert: {send_exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
