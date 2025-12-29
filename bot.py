#!/usr/bin/env python3
import os
import json
import asyncio
import logging
from datetime import datetime
from typing import List, Dict, Any, Optional

import httpx
from bs4 import BeautifulSoup
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes

# -------------------------
# Logging
# -------------------------
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s"
)
logger = logging.getLogger("orange-bot")

# -------------------------
# Config (Hardcoded + ENV)
# -------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID_RAW = os.getenv("CHAT_ID", "")
try:
    CHAT_ID = int(CHAT_ID_RAW)
except Exception:
    CHAT_ID = CHAT_ID_RAW or None

# Hardcoded account details as requested
HARDCODED_ACCOUNTS = [
    {"email": "jadenafrix1@gmail.com", "password": "cybixtech"}
]

# ACCOUNTS should be JSON string from ENV or fallback to hardcoded
try:
    env_accounts = os.getenv("ACCOUNTS")
    if env_accounts:
        ACCOUNTS = json.loads(env_accounts)
    else:
        ACCOUNTS = HARDCODED_ACCOUNTS
except Exception:
    logger.exception("ACCOUNTS env var not valid JSON; defaulting to hardcoded")
    ACCOUNTS = HARDCODED_ACCOUNTS

POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "10"))  # seconds between checks per account
CDR_API_TEMPLATE = os.getenv(
    "CDR_API_TEMPLATE", "https://www.orangecarrier.com/CDR/mycdrs?start=0&length=50"
)
LOGIN_URL = "https://www.orangecarrier.com/login"
CDR_PAGE = "https://www.orangecarrier.com/CDR/mycdrs"

# OWNER_ID hardcoded as requested
OWNER_ID = 6524840104

# -------------------------
# Global state
# -------------------------
seen_ids = set()


# -------------------------
# Helpers: login & fetch CDRs
# -------------------------
def extract_token_from_html(html: str) -> Optional[str]:
    soup = BeautifulSoup(html, "html.parser")
    inp = soup.find("input", {"name": "_token"})
    if inp and inp.get("value"):
        return inp["value"]
    return None


def safe_text(x: Any) -> str:
    try:
        return str(x)
    except Exception:
        return ""


async def fetch_cdr_for_account(client: httpx.AsyncClient, email: str, password: str, app: Application) -> List[Dict[str, Any]]:
    """
    Attempt to login and fetch CDRs for a single account.
    Returns list of records (dicts) with at least keys: id, cli, to, time, duration, type, account
    """
    results: List[Dict[str, Any]] = []
    # step 1: GET login page to collect CSRF token and cookies
    try:
        r = await client.get(LOGIN_URL)
        token = extract_token_from_html(r.text)
        
        # Build payload
        payload = {"email": email, "password": password}
        if token:
            payload["_token"] = token

        # step 2: POST login
        r2 = await client.post(LOGIN_URL, data=payload, follow_redirects=True)
        # simple check for login success: presence of "logout" or "dashboard" or redirect away from login
        page_lower = r2.text.lower() if r2 is not None else ""
        if not ("logout" in page_lower or "dashboard" in page_lower) and r2.url.path.endswith("/login"):
            logger.info("[%s] login appears to have failed", email)
            return results

        logger.info("[%s] login success (session cookie set).", email)
        # Notify owner on successful login once
        if CHAT_ID:
            await app.bot.send_message(chat_id=OWNER_ID, text=f"✅ Successful Login to OrangeCarrier: {email}")

    except Exception as e:
        logger.error("[%s] Connection error during login: %s", email, e)
        return results

    # step 3: Try JSON API endpoint first
    try:
        api_resp = await client.get(CDR_API_TEMPLATE)
        if api_resp.status_code == 200:
            try:
                j = api_resp.json()
                data_array = None
                if isinstance(j, dict):
                    if "data" in j and isinstance(j["data"], list):
                        data_array = j["data"]
                    elif "aaData" in j and isinstance(j["aaData"], list):
                        data_array = j["aaData"]
                
                if data_array is not None:
                    for row in data_array:
                        if isinstance(row, list):
                            cli = safe_text(row[0]) if len(row) > 0 else ""
                            to_num = safe_text(row[1]) if len(row) > 1 else ""
                            time_str = safe_text(row[2]) if len(row) > 2 else ""
                            duration = safe_text(row[3]) if len(row) > 3 else ""
                            call_type = safe_text(row[4]) if len(row) > 4 else ""
                        elif isinstance(row, dict):
                            cli = safe_text(row.get("cli") or row.get("source") or row.get("caller") or row.get("from") or "")
                            to_num = safe_text(row.get("to") or row.get("destination") or "")
                            time_str = safe_text(row.get("time") or row.get("timestamp") or row.get("start_time") or "")
                            duration = safe_text(row.get("duration") or "")
                            call_type = safe_text(row.get("type") or row.get("status") or "")
                        else:
                            continue

                        uid = f"{email}_{cli}_{time_str}"
                        results.append({
                            "id": uid,
                            "cli": cli,
                            "to": to_num,
                            "time": time_str,
                            "duration": duration,
                            "type": call_type,
                            "account": email,
                        })
                    if results:
                        return results
            except Exception:
                pass
    except Exception:
        pass

    # step 4: fallback — fetch CDR page HTML
    try:
        page = await client.get(CDR_PAGE)
        soup = BeautifulSoup(page.text, "html.parser")
        table = soup.find("table")
        if table:
            tbody = table.find("tbody") or table
            for tr in tbody.find_all("tr"):
                cols = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
                if not cols:
                    continue
                cli = cols[0] if len(cols) > 0 else ""
                to_num = cols[1] if len(cols) > 1 else ""
                time_str = cols[2] if len(cols) > 2 else ""
                duration = cols[3] if len(cols) > 3 else ""
                call_type = cols[4] if len(cols) > 4 else ""
                uid = f"{email}_{cli}_{time_str}"
                results.append({
                    "id": uid,
                    "cli": cli,
                    "to": to_num,
                    "time": time_str,
                    "duration": duration,
                    "type": call_type,
                    "account": email,
                })
            return results
    except Exception:
        pass

    return results


# -------------------------
# Telegram send helper
# -------------------------
async def send_record_to_telegram(app: Application, rec: Dict[str, Any]) -> bool:
    if not CHAT_ID:
        return False
        
    text = (
        f"👤 Account: {rec.get('account')}\n"
        f"📞 CLI: {rec.get('cli')}\n"
        f"➡ To: {rec.get('to')}\n"
        f"⏱ Time: {rec.get('time')}\n"
        f"⏳ Duration: {rec.get('duration')}\n"
        f"📌 Type: {rec.get('type')}\n\n"
        f"Powered By Afrix Tech"
    )
    
    # Inline keyboard buttons
    keyboard = [
        [InlineKeyboardButton("NUMBER CHANNEL", url="https://t.me/mrafrixtech")],
        [InlineKeyboardButton("CALL GROUP", url="https://t.me/afrixotpgc")],
        [InlineKeyboardButton("BACKUP CHANNEL", url="https://t.me/mrafrix")],
        [InlineKeyboardButton("CONTACT OWNER", url="https://t.me/jadenafrix")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    try:
        await app.bot.send_message(chat_id=CHAT_ID, text=text, reply_markup=reply_markup)
        return True
    except Exception:
        return False


# -------------------------
# Worker per-account
# -------------------------
async def account_worker(app: Application, acc: Dict[str, str]):
    email = acc.get("email")
    password = acc.get("password")
    if not email or not password:
        return

    async with httpx.AsyncClient(timeout=30.0) as client:
        client.headers.update({"User-Agent": "Mozilla/5.0 (compatible; OrangeBot/1.0)"})
        while True:
            try:
                records = await fetch_cdr_for_account(client, email, password, app)
                for rec in records:
                    if rec["id"] not in seen_ids:
                        seen_ids.add(rec["id"])
                        await send_record_to_telegram(app, rec)
            except Exception:
                logger.exception("Worker error for %s", email)
            await asyncio.sleep(POLL_INTERVAL)


# -------------------------
# Heartbeat & /start handler
# -------------------------
async def heartbeat_task(app: Application):
    while True:
        try:
            if CHAT_ID:
                await app.bot.send_message(chat_id=CHAT_ID, text="✅ Bot active hai — monitoring OrangeCarrier CDRs.\n\nPowered By Afrix Tech")
        except Exception:
            pass
        await asyncio.sleep(3600)


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = "🤖 Bot is running and monitoring OrangeCarrier accounts.\n\nPowered By Afrix Tech"
    keyboard = [
        [InlineKeyboardButton("NUMBER CHANNEL", url="https://t.me/mrafrixtech")],
        [InlineKeyboardButton("CALL GROUP", url="https://t.me/afrixotpgc")],
        [InlineKeyboardButton("BACKUP CHANNEL", url="https://t.me/mrafrix")],
        [InlineKeyboardButton("CONTACT OWNER", url="https://t.me/jadenafrix")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(text, reply_markup=reply_markup)


# -------------------------
# App entrypoint
# -------------------------
def main():
    if not BOT_TOKEN:
        logger.error("Missing BOT_TOKEN")
        return

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))

    async def on_post_init(_: Application):
        logger.info("Starting workers for %d accounts", len(ACCOUNTS))
        for acc in ACCOUNTS:
            asyncio.create_task(account_worker(app, acc))
        asyncio.create_task(heartbeat_task(app))

    app.post_init = on_post_init
    logger.info("Starting polling...")
    app.run_polling()

if __name__ == "__main__":
    main()
