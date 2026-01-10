#!/usr/bin/env python3
import os
import json
import asyncio
import logging
from datetime import datetime
from typing import List, Dict, Any, Optional

import httpx
from bs4 import BeautifulSoup
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# -------------------------
# Logging
# -------------------------
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s"
)
logger = logging.getLogger("orange-bot")

# -------------------------
# Config (from ENV)
# -------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID_RAW = os.getenv("CHAT_ID", "")
try:
    CHAT_ID = int(CHAT_ID_RAW)
except Exception:
    CHAT_ID = CHAT_ID_RAW or None

# ACCOUNTS should be JSON string: [{"email":"x","password":"y"}, ...]
try:
    ACCOUNTS: List[Dict[str, str]] = json.loads(os.getenv("ACCOUNTS", "[]"))
except Exception:
    logger.exception("ACCOUNTS env var not valid JSON; defaulting to empty list")
    ACCOUNTS = []

POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "10"))  # seconds between checks per account
CDR_API_TEMPLATE = os.getenv(
    "CDR_API_TEMPLATE", "https://www.orangecarrier.com/CDR/mycdrs?start=0&length=50"
)
LOGIN_URL = "https://www.orangecarrier.com/login"
CDR_PAGE = "https://www.orangecarrier.com/CDR/mycdrs"

# optional: an OWNER_ID to notify on critical failures
OWNER_ID = int(os.getenv("OWNER_ID", "0")) if os.getenv("OWNER_ID") else None

# -------------------------
# Global state
# -------------------------
# store seen IDs to avoid duplicate messages (persist in-memory only)
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


async def fetch_cdr_for_account(client: httpx.AsyncClient, email: str, password: str) -> List[Dict[str, Any]]:
    """
    Attempt to login and fetch CDRs for a single account.
    Returns list of records (dicts) with at least keys: id, cli, to, time, duration, type, account
    """
    results: List[Dict[str, Any]] = []
    # step 1: GET login page to collect CSRF token and cookies
    r = await client.get(LOGIN_URL)
    token = extract_token_from_html(r.text)
    if not token:
        logger.warning("[%s] CSRF token not found on login page", email)
        # still attempt login without token (some setups might not require)
    # Build payload
    payload = {"email": email, "password": password}
    if token:
        payload["_token"] = token

    # step 2: POST login
    r2 = await client.post(LOGIN_URL, data=payload, follow_redirects=True)
    # simple check for login success: presence of "logout" or "dashboard" or redirect away from login
    page_lower = r2.text.lower() if r2 is not None else ""
    if not ("logout" in page_lower or "dashboard" in page_lower) and r2.url.path.endswith("/login"):
        # login probably failed
        logger.info("[%s] login appears to have failed (still on /login)", email)
        return results

    logger.info("[%s] login success (session cookie set).", email)

    # step 3: Try JSON API endpoint first (fast & reliable if available)
    try:
        api_resp = await client.get(CDR_API_TEMPLATE)
        if api_resp.status_code == 200:
            # attempt JSON parse
            try:
                j = api_resp.json()
                # Common patterns: {"data":[ ... ]} or {"aaData": [...]}
                data_array = None
                if isinstance(j, dict):
                    if "data" in j and isinstance(j["data"], list):
                        data_array = j["data"]
                    elif "aaData" in j and isinstance(j["aaData"], list):
                        data_array = j["aaData"]
                if data_array is not None:
                    # each row may be a list of columns (strings) or dict
                    for row in data_array:
                        # Prepare normalized fields based on common ordering:
                        # We'll try to detect CLI in first column, time in third etc. This can be tuned later.
                        if isinstance(row, list):
                            cli = safe_text(row[0]) if len(row) > 0 else ""
                            to_num = safe_text(row[1]) if len(row) > 1 else ""
                            time_str = safe_text(row[2]) if len(row) > 2 else ""
                            duration = safe_text(row[3]) if len(row) > 3 else ""
                            call_type = safe_text(row[4]) if len(row) > 4 else ""
                        elif isinstance(row, dict):
                            # attempt common keys
                            cli = safe_text(row.get("cli") or row.get("source") or row.get("caller") or row.get("from") or "")
                            to_num = safe_text(row.get("to") or row.get("destination") or "")
                            time_str = safe_text(row.get("time") or row.get("timestamp") or row.get("start_time") or "")
                            duration = safe_text(row.get("duration") or "")
                            call_type = safe_text(row.get("type") or row.get("status") or "")
                        else:
                            continue

                        # create id (account+cli+time) to dedupe
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
                        logger.info("[%s] fetched %d records via JSON API", email, len(results))
                        return results
            except Exception as e:
                logger.debug("[%s] JSON parse failed for CDR API: %s", email, e)
        else:
            logger.debug("[%s] CDR API request returned status %s", email, api_resp.status_code)
    except Exception as e:
        logger.debug("[%s] CDR API request exception: %s", email, e)

    # step 4: fallback — fetch CDR page HTML and parse table (if any)
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
                # map columns: assume common order cli, to, time, duration, type
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
            logger.info("[%s] parsed %d rows from HTML table", email, len(results))
            return results
        else:
            logger.info("[%s] no <table> found in CDR page HTML", email)
    except Exception as e:
        logger.exception("[%s] error fetching/parsing CDR page HTML", email)

    return results


# -------------------------
# Telegram send helper
# -------------------------
async def send_record_to_telegram(app: Application, rec: Dict[str, Any]) -> bool:
    if not CHAT_ID:
        logger.error("CHAT_ID is not configured.")
        return False
    text = (
        f"👤 Account: {rec.get('account')}\n"
        f"📞 CLI: {rec.get('cli')}\n"
        f"➡ To: {rec.get('to')}\n"
        f"⏱ Time: {rec.get('time')}\n"
        f"⏳ Duration: {rec.get('duration')}\n"
        f"📌 Type: {rec.get('type')}"
    )
    try:
        await app.bot.send_message(chat_id=CHAT_ID, text=text)
        logger.info("Sent record %s to chat %s", rec.get("id"), CHAT_ID)
        return True
    except Exception as e:
        logger.exception("Failed to send message for %s", rec.get("id"))
        # optionally try plain fallback or notify owner
        try:
            await app.bot.send_message(chat_id=CHAT_ID, text=text)
        except Exception:
            if OWNER_ID:
                try:
                    await app.bot.send_message(chat_id=OWNER_ID, text=f"Failed to send record {rec.get('id')}: {e}")
                except Exception:
                    logger.warning("Also failed to notify OWNER")
        return False


# -------------------------
# Worker per-account
# -------------------------
async def account_worker(app: Application, acc: Dict[str, str]):
    email = acc.get("email")
    password = acc.get("password")
    if not email or not password:
        logger.warning("Invalid account entry (missing email/password): %s", acc)
        return

    # create per-worker httpx client, reuse cookies/sessions
    async with httpx.AsyncClient(timeout=30.0) as client:
        # set UA header to mimic real browser
        client.headers.update({"User-Agent": "Mozilla/5.0 (compatible; OrangeBot/1.0)"})

        # loop forever
        while True:
            try:
                records = await fetch_cdr_for_account(client, email, password)
                if not records:
                    logger.debug("[%s] no records fetched this cycle", email)
                for rec in records:
                    if rec["id"] not in seen_ids:
                        # dedupe and send
                        seen_ids.add(rec["id"])
                        # send to telegram
                        await send_record_to_telegram(app, rec)
                # small delay
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
                await app.bot.send_message(chat_id=CHAT_ID, text="✅ Bot active hai — monitoring OrangeCarrier CDRs.")
        except Exception:
            logger.exception("Heartbeat send failed")
        await asyncio.sleep(3600)


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤖 Bot is running and monitoring OrangeCarrier accounts.")


# -------------------------
# App entrypoint
# -------------------------
def main():
    if not BOT_TOKEN or not CHAT_ID or not ACCOUNTS:
        logger.error("Missing BOT_TOKEN / CHAT_ID / ACCOUNTS. BOT_TOKEN=%s CHAT_ID=%s ACCOUNTS_len=%d",
                     bool(BOT_TOKEN), CHAT_ID, len(ACCOUNTS))
        return

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))

    # Post-init: start workers + heartbeat after Application is ready
    async def on_post_init(_: Application):
        logger.info("Starting workers for %d accounts", len(ACCOUNTS))
        for acc in ACCOUNTS:
            # schedule a worker task
            asyncio.create_task(account_worker(app, acc))
        # heartbeat
        asyncio.create_task(heartbeat_task(app))

    app.post_init = on_post_init

    logger.info("Starting polling (blocking)...")
    app.run_polling()

if __name__ == "__main__":
    main()
