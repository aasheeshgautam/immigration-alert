"""Immigration status watcher.

Checks the Philippine Bureau of Immigration agenda PDFs twice a day and pings
Telegram when application number TARGET_NUMBER shows up — or when the queue gets
close to it.

Once the number is found the alert repeats every run until you reply "stop" in
Telegram. That is deliberate: a single message is easy to miss.
"""

import datetime as dt
import html
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
# The last N numbers before the target are the "you're basically next" tier.
NEAREST_WINDOW = int(os.environ.get("NEAREST_WINDOW", "10"))
# Heartbeat check-in cadence, in days (was weekly; now every 2 days).
CHECKIN_EVERY_DAYS = int(os.environ.get("CHECKIN_EVERY_DAYS", "2"))
# immigration.gov.ph goes down for minutes at a time and recovers on its own.
# Don't cry wolf on every blip: only send the ⚠️ alert once the site has been
# unreachable this many runs in a row. A lone transient outage stays quiet.
SITE_FAIL_ALERT_AFTER = int(os.environ.get("SITE_FAIL_ALERT_AFTER", "2"))

PAGE_URL = "https://immigration.gov.ph/resources/visa-application-status/"
STATE_FILE = "state.json"

# How many of the newest PDFs to scan each run. 0 (or negative) means scan the
# WHOLE current-year section — the safest setting: the immigration lists aren't
# in strict number order, so the target can land on any agenda, and scanning
# only the newest few risks missing it if several lists publish between runs.
SCAN_COUNT = int(os.environ.get("SCAN_COUNT", "0"))
TEST_MODE = os.environ.get("TEST_MODE", "").lower() in ("1", "true", "yes")

# ------------------------------------------------------------------- emoji legend
# One glyph per situation, so every alert is recognisable at a glance.
E_ALIVE = "\U0001F493"    # 💓  periodic heartbeat: still alive & watching
E_RUNNING = "\U0001F7E2"  # 🟢  a normal check ran clean, nothing new
E_CLOSE = "\U0001F440"    # 👀  someone in the wider near window showed up
E_NEAREST = "\U0001F525"  # 🔥  within NEAREST_WINDOW of your number — you're next
E_HERE = "\U0001F389"     # 🎉  YOUR number is on the list
E_ERROR = "⚠️"  # ⚠️  the check itself failed

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


def send_telegram_photo(png_bytes, caption):
    """Send a cropped screenshot of the row so it's easy to find by hand."""
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        raise RuntimeError("Telegram is not configured (missing token or chat id).")
    r = requests.post(
        f"{TG_API}/sendPhoto",
        data={
            "chat_id": TELEGRAM_CHAT_ID,
            "caption": caption[:1024],
            "parse_mode": "HTML",
        },
        files={"photo": ("row.png", png_bytes, "image/png")},
        timeout=60,
    )
    if not r.ok:
        raise RuntimeError(f"Telegram photo failed: HTTP {r.status_code} {r.text}")
    print("Telegram photo sent.")


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


def _pdf_links(html_text):
    """PDF hrefs in the order they appear in this chunk of HTML, de-duped."""
    seen, links = set(), []
    for href in re.findall(r'href="([^"]+\.pdf)"', html_text, flags=re.IGNORECASE):
        if not href.startswith("http"):
            href = "https://immigration.gov.ph" + href
        if href not in seen:
            seen.add(href)
            links.append(href)
    return links


def _current_year_section(html_text):
    """Slice the page down to the 'Agenda Verification <year>' accordion for the
    current year, so we only ever read the list that's actually current.

    The page groups agenda PDFs by year in accordion panels (2026, 2025, ...).
    We target this year's panel and fall back to the newest panel if, for
    whatever reason, one for this exact year isn't there yet. Falls back to the
    whole page if the accordion markup ever changes."""
    titles = list(re.finditer(r"Agenda Verification\s*(20\d{2})", html_text))
    if not titles:
        print("  (no year sections found — scanning the whole page)")
        return html_text, None

    year = str(now_ph().year)
    chosen = next((m for m in titles if m.group(1) == year), None)
    if chosen is None:
        # No panel for this year yet; use the newest year present.
        chosen = max(titles, key=lambda m: m.group(1))
        print(f"  (no {year} section yet — using newest: {chosen.group(1)})")

    start = chosen.end()
    later = [m.start() for m in titles if m.start() > start]
    end = min(later) if later else len(html_text)
    return html_text[start:end], chosen.group(1)


def get_pdf_urls():
    """Agenda PDFs from the current year's section, newest first."""
    response = fetch(PAGE_URL)
    section, year = _current_year_section(response.text)
    links = _pdf_links(section)
    if year:
        print(f"  scanning the 'Agenda Verification {year}' section ({len(links)} PDFs)")

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


def capture_target_row(pdf_content, target):
    """Find the target number in the PDF and render a full-width strip around its
    row, so Ash can locate it by hand. Returns (page_number, row_text, png_bytes)
    or None if anything goes wrong (screenshots must never block the main alert).
    """
    try:
        import fitz  # PyMuPDF
    except Exception as exc:  # noqa: BLE001
        print(f"  (row screenshot skipped — PyMuPDF unavailable: {exc})")
        return None

    doc = None
    try:
        doc = fitz.open(stream=pdf_content, filetype="pdf")
        for pno in range(doc.page_count):
            page = doc.load_page(pno)
            # words: (x0, y0, x1, y1, "text", block_no, line_no, word_no)
            words = page.get_text("words")
            hits = [w for w in words if target in w[4]]
            if not hits:
                continue

            hx0, hy0, hx1, hy1 = hits[0][:4]
            cy = (hy0 + hy1) / 2
            line_h = max(hy1 - hy0, 6)

            # Everything sitting on the same visual row as the number.
            row_words = sorted(
                (w for w in words if abs(((w[1] + w[3]) / 2) - cy) <= line_h * 0.9),
                key=lambda w: w[0],
            )
            row_text = " ".join(w[4] for w in row_words).strip()

            # Box the number so it's obvious in the crop, then clip a horizontal
            # band across the full page width.
            page.draw_rect(fitz.Rect(hx0 - 2, hy0 - 2, hx1 + 2, hy1 + 2),
                           color=(1, 0, 0), width=1.5)
            # A little more headroom above (wrapped description cells sit there)
            # than below, so the whole row lands in frame around the red box.
            clip = fitz.Rect(
                0,
                max(0, hy0 - line_h * 2.8),
                page.rect.width,
                min(page.rect.height, hy1 + line_h * 1.4),
            )
            pix = page.get_pixmap(matrix=fitz.Matrix(3, 3), clip=clip)
            return pno + 1, row_text, pix.tobytes("png")
    except Exception as exc:  # noqa: BLE001 - never let a screenshot break the alert
        print(f"  (row screenshot failed: {exc})")
        return None
    finally:
        if doc is not None:
            doc.close()
    return None


# ------------------------------------------------------------------ messages

def found_message(pdf_url, first_time, snap=None):
    opener = (
        f"{E_HERE}{E_HERE} <b>ASH, IT'S HERE!</b> {E_HERE}{E_HERE}"
        if first_time
        else "\U0001F514 <b>Still reminding you, Ash!</b>"  # 🔔
    )
    where = ""
    if snap:
        page_no, row_text, _ = snap
        where = (
            f"\U0001F4D1 It's on <b>page {page_no}</b> of the PDF.\n"  # 📑
            f"Your row reads:\n<code>{html.escape(row_text)}</code>\n\n"
        )
    tail = (
        "I'll keep nudging you every check until you reply <b>stop</b> here. \U0001F64C"
        if first_time
        else "You haven't told me to stop yet, so here I am again. \U0001F604\n"
        "Reply <b>stop</b> when you've sorted it and I'll go quiet."
    )
    return (
        f"{opener}\n\n"
        f"Your number <b>{TARGET_NUMBER}</b> is on the immigration list! \U0001F973\n\n"
        f"{where}"
        f"\U0001F4C4 See it here: {pdf_url}\n"  # 📄
        f"\U0001F3E2 Head to the immigration office as soon as you can.\n"  # 🏢
        f"\U0001F552 Spotted {stamp()}\n\n"  # 🕒
        f"{tail}"
    )


def nearest_message(hits, pdf_url):
    """The 'you're basically next' tier — within NEAREST_WINDOW of the target."""
    listed = ", ".join(f"<b>{n}</b>" for n in hits)
    return (
        f"{E_NEAREST} <b>ASH — YOU'RE BASICALLY NEXT!</b> {E_NEAREST}\n\n"
        f"A number within {NEAREST_WINDOW} of yours just appeared: {listed}\n"
        f"Yours is <b>{TARGET_NUMBER}</b> — you could be on the very next list. \U0001F91E\n\n"
        f"\U0001F552 <b>Spotted {stamp()}</b>\n"  # 🕒 — the moment it turned up
        f"\U0001F4C4 List: {pdf_url}\n\n"  # 📄
        f"Have every document ready to go. \U0001F4AA"  # 💪
    )


def near_message(hits, pdf_url):
    listed = ", ".join(f"<b>{n}</b>" for n in hits)
    return (
        f"{E_CLOSE} <b>Ooh, they're getting close!</b>\n\n"
        f"Numbers below yours just showed up: {listed}\n"
        f"Yours is <b>{TARGET_NUMBER}</b> — so you could be next. \U0001F91E\n\n"
        f"\U0001F552 Spotted {stamp()}\n"  # 🕒
        f"\U0001F4C4 List: {pdf_url}\n\n"  # 📄
        f"Keep your documents handy — I'm watching closely. \U0001F4AA"
    )


def checkin_message(highest, gap, pdf_url, near_hits):
    if near_hits:
        mood = (
            f"Numbers close to yours have appeared: "
            f"{', '.join(near_hits)} {E_CLOSE} You're nearly up!"
        )
    elif gap is None:
        mood = "I couldn't read any numbers off the latest list this time. \U0001F914"
    elif gap > 5000:
        mood = f"Still a fair way to go — about <b>{gap:,}</b> numbers ahead of you. \U0001F60C"
    elif gap > 0:
        mood = f"Getting closer! Only about <b>{gap:,}</b> numbers to go. \U0001F642"
    else:
        mood = "The list has moved past your range — worth a manual look. \U0001F9D0"

    return (
        f"{E_ALIVE} <b>Check-in</b> {E_RUNNING}\n\n"
        f"Hey Ash! Just letting you know I'm alive and running, still watching for "
        f"<b>{TARGET_NUMBER}</b>. \U00002705\n\n"  # ✅
        f"No sign of your number yet.\n"
        f"Latest list reaches <b>{highest}</b>.\n"
        f"{mood}\n\n"
        f"\U0001F4C4 Newest list: {pdf_url}\n"  # 📄
        f"\U0001F552 {stamp()}\n\n"  # 🕒
        f"Talk again in a couple of days! \U0001F44B"  # 👋
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
            "\U0001F44D <b>Got it — stopping here.</b>\n\n"  # 👍
            "I won't send any more immigration updates. "
            "All the best with the next steps, Ash! \U0001F64F"
        )
        print("Stop acknowledged.")
        return

    print("Checking immigration website...")
    pdf_urls = get_pdf_urls()
    if not pdf_urls:
        raise RuntimeError("No PDF links found on the page — layout may have changed.")

    # Reached the site: clear any outage streak, and if we'd previously alerted
    # about a sustained outage, tell you it's back.
    if state.get("site_fail_streak", 0) >= SITE_FAIL_ALERT_AFTER:
        try:
            send_telegram(
                f"{E_RUNNING} <b>Back online</b>\n\n"
                f"The immigration site is reachable again and I've resumed checking. "
                f"\U0001F44D\n\U0001F552 {stamp()}"
            )
        except Exception as exc:  # noqa: BLE001 - recovery notice is best-effort
            print(f"  could not send recovery notice: {exc}")
    state["site_fail_streak"] = 0

    recent = pdf_urls if SCAN_COUNT <= 0 else pdf_urls[:SCAN_COUNT]
    scope = "all" if SCAN_COUNT <= 0 else f"newest {len(recent)}"
    print(f"Found {len(pdf_urls)} PDFs. Scanning {scope}:")
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
    nearest_from = target - NEAREST_WINDOW
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

        # Grab a screenshot of the row the first time only — that's the big one;
        # the repeat reminders stay text-only so we don't re-send the image.
        snap = None
        if first_time:
            try:
                content = fetch(found_in, timeout=120).content
                snap = capture_target_row(content, TARGET_NUMBER)
            except Exception as exc:  # noqa: BLE001
                print(f"  could not build row screenshot: {exc}")

        send_telegram(found_message(found_in, first_time, snap))
        if first_time and snap:
            page_no, row_text, png = snap
            try:
                send_telegram_photo(
                    png,
                    f"{E_HERE} <b>{TARGET_NUMBER}</b> — page <b>{page_no}</b> of the PDF\n"
                    f"<code>{html.escape(row_text)}</code>",
                )
            except Exception as exc:  # noqa: BLE001
                print(f"  could not send row screenshot: {exc}")
        save_state(state)
        print(f"FOUND in {found_in} (first_time={first_time}).")
        return

    # 2. Anything below the target: split into the "you're next" tier (the last
    #    NEAREST_WINDOW numbers) and the wider near window. Once per number.
    near_hits = sorted(n for n in all_numbers if near_from <= int(n) < target)
    already = set(state.get("near_alerted", []))
    fresh = [n for n in near_hits if n not in already]
    fresh_nearest = sorted(n for n in fresh if int(n) >= nearest_from)
    fresh_wider = sorted(n for n in fresh if int(n) < nearest_from)
    print(
        f"Not found. Highest: {highest} (gap {gap}). "
        f"Near-window hits: {near_hits or 'none'}. "
        f"Fresh nearest-{NEAREST_WINDOW}: {fresh_nearest or 'none'}."
    )

    if fresh:
        if fresh_nearest:
            send_telegram(nearest_message(fresh_nearest, newest))
        if fresh_wider:
            send_telegram(near_message(fresh_wider, newest))
        state["near_alerted"] = sorted(already | set(fresh))
        save_state(state)
        return

    # 3. Heartbeat check-in every CHECKIN_EVERY_DAYS days, so silence never looks
    #    like breakage.
    today = now_ph().date()
    last_checkin = state.get("last_checkin")
    due = True
    if last_checkin:
        try:
            due = (today - dt.date.fromisoformat(last_checkin)).days >= CHECKIN_EVERY_DAYS
        except ValueError:
            due = True
    if due:
        send_telegram(checkin_message(highest, gap, newest, near_hits))
        state["last_checkin"] = today.isoformat()
        print("Check-in sent.")
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
            "\U0001F9EA <b>Test message</b>\n\n"  # 🧪
            f"Hi Ash! Your immigration watcher is set up and can reach you here. \U0001F44B\n\n"
            f"I'm looking for <b>{TARGET_NUMBER}</b> {E_HERE}, and I'll shout {E_CLOSE} if "
            f"anything from <b>{NEAR_FROM}</b> upward shows up — with an extra {E_NEAREST} "
            f"alert once a number within {NEAREST_WINDOW} of yours appears.\n"
            f"When your exact number lands I'll also send a screenshot of your row. \U0001F4F8\n\n"
            f"Checks run at <b>11:00 AM</b> and <b>6:00 PM</b> PH time, with a {E_ALIVE} "
            f"check-in every {CHECKIN_EVERY_DAYS} days.\n\n"
            f"\U0001F552 {stamp()}"
        )
        return

    try:
        run()
    except requests.exceptions.RequestException as exc:
        # The site is unreachable (timeout / connection error). It blips out for
        # minutes and recovers on its own, so a single failure isn't worth an
        # alert. Count consecutive failures in state.json and only cry wolf once
        # the outage is sustained. A lone blip stays quiet and exits green — no
        # ⚠️ ping, no GitHub failure email.
        print(f"SITE UNREACHABLE: {exc}")
        state = load_state()
        streak = state.get("site_fail_streak", 0) + 1
        state["site_fail_streak"] = streak
        state["last_site_fail"] = now_ph().isoformat()
        save_state(state)

        if streak == SITE_FAIL_ALERT_AFTER:
            # Cross the threshold exactly once — don't re-ping every run after.
            try:
                send_telegram(
                    f"{E_ERROR} <b>Immigration site looks down</b>\n\n"
                    f"I haven't been able to reach it for {streak} checks in a row. "
                    f"This is usually the site being flaky, not your application — "
                    f"I'll keep trying and tell you the moment it's back. \U0001F501\n"  # 🔁
                    f"\U0001F552 {stamp()}"  # 🕒
                )
            except Exception as send_exc:  # noqa: BLE001
                print(f"Could not send outage alert: {send_exc}")
        else:
            print(f"Transient outage (streak {streak}/{SITE_FAIL_ALERT_AFTER}); "
                  f"staying quiet.")
        # Exit clean for a transient blip so it doesn't show red / email you.
        # Once it's a confirmed sustained outage, let the run go red too.
        sys.exit(1 if streak >= SITE_FAIL_ALERT_AFTER else 0)

    except Exception as exc:  # noqa: BLE001 - a real error (bug, layout change): surface it now
        print(f"ERROR: {exc}")
        try:
            # Escape the exception text: it often contains <...> (e.g. a urllib
            # connection repr), which HTML parse_mode would otherwise reject —
            # and then the error alert itself would silently fail to send.
            detail = html.escape(f"{type(exc).__name__}: {exc}")
            send_telegram(
                f"{E_ERROR} <b>Hmm, my check didn't go through</b>\n\n"
                f"Something went wrong while checking the immigration site:\n"
                f"<code>{detail}</code>\n\n"
                f"Don't worry — I'll try again at the next check. \U0001F501\n"  # 🔁
                f"\U0001F552 {stamp()}"  # 🕒
            )
        except Exception as send_exc:  # noqa: BLE001
            print(f"Could not send error alert: {send_exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
