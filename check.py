import requests
from bs4 import BeautifulSoup
import pdfplumber
import os
import re
import io

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
TARGET_NUMBER = "2026229963"
PAGE_URL = "https://immigration.gov.ph/resources/visa-application-status/"

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"})

def get_latest_pdf_url():
    response = requests.get(PAGE_URL, timeout=30)
    soup = BeautifulSoup(response.text, "html.parser")
    pdf_links = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.lower().endswith(".pdf"):
            pdf_links.append(href)
    return pdf_links

def search_pdf(pdf_url):
    if not pdf_url.startswith("http"):
        pdf_url = "https://immigration.gov.ph" + pdf_url

    response = requests.get(pdf_url, timeout=60)
    all_numbers = []

    with pdfplumber.open(io.BytesIO(response.content)) as pdf:
        full_text = ""
        for page in pdf.pages:
            text = page.extract_text()
            if text:
                full_text += text + "\n"

    numbers = re.findall(r'\b202\d{7}\b', full_text)
    all_numbers = sorted(set(numbers))

    exact_found = TARGET_NUMBER in all_numbers

    nearest = None
    for n in all_numbers:
        if n > TARGET_NUMBER:
            nearest = n
            break

    return exact_found, nearest, all_numbers, pdf_url

def main():
    print("Checking immigration website...")
    pdf_urls = get_latest_pdf_url()

    if not pdf_urls:
        print("No PDF found on the page.")
        return

    print(f"Found {len(pdf_urls)} PDF(s): {pdf_urls}")

    for pdf_url in pdf_urls:
        print(f"Scanning: {pdf_url}")
        exact_found, nearest, all_numbers, full_url = search_pdf(pdf_url)

        if exact_found:
            msg = (
                f"🚨🎉 <b>FOUND IT, ASH!</b> 🎉🚨\n\n"
                f"Your number <b>{TARGET_NUMBER}</b> was found in the visa application list!\n\n"
                f"📄 PDF: {full_url}\n\n"
                f"✅ Please visit the immigration office as soon as possible!"
            )
            send_telegram(msg)
            print("EXACT MATCH FOUND! Telegram sent.")

        elif nearest:
            msg = (
                f"⚠️ <b>Close Match Found!</b>\n\n"
                f"Your number <b>{TARGET_NUMBER}</b> was NOT found yet.\n"
                f"But the nearest number above yours is: <b>{nearest}</b>\n\n"
                f"📄 PDF: {full_url}\n\n"
                f"🔍 Keep watching — you might be very close!"
            )
            send_telegram(msg)
            print(f"Nearest match found: {nearest}. Telegram sent.")

        else:
            print(f"Number not found in {pdf_url}. No match or nearby number.")

if __name__ == "__main__":
    main()
