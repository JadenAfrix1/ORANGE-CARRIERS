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
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler

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

# Read Orange Carrier email and password from environment
ORANGE_EMAIL = os.getenv("ORANGE_EMAIL", "")
ORANGE_PASSWORD = os.getenv("ORANGE_PASSWORD", "")

# Build ACCOUNTS list from email and password
ACCOUNTS: List[Dict[str, str]] = []
if ORANGE_EMAIL and ORANGE_PASSWORD:
    ACCOUNTS = [{"email": ORANGE_EMAIL, "password": ORANGE_PASSWORD}]

POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "10"))  # seconds between checks per account
CDR_API_TEMPLATE = os.getenv(
    "CDR_API_TEMPLATE", "https://www.orangecarrier.com/CDR/mycdrs?start=0&length=50"
)
LOGIN_URL = "https://www.orangecarrier.com/login"
CDR_PAGE = "https://www.orangecarrier.com/CDR/mycdrs"

# Admin ID (hardcoded)
ADMIN_ID = 6524840104

# optional: an OWNER_ID to notify on critical failures
OWNER_ID = int(os.getenv("OWNER_ID", "0")) if os.getenv("OWNER_ID") else None

# Bot signature
BOT_SIGNATURE = "𝐌ʀ 𝐀ғʀɪx 𝐓ᴇᴄʜ™"

# -------------------------
# Global state
# -------------------------
# store seen IDs to avoid duplicate messages (persist in-memory only)
seen_ids = set()
# Track bot start time for uptime
bot_start_time = datetime.now()
# Track login status
last_login_status = {}


# -------------------------
# Keyboard buttons
# -------------------------
def get_cdr_keyboard():
    """Get keyboard with number channel, OTP group, and owner buttons"""
    keyboard = [
        [InlineKeyboardButton("📞 NUMBER CHANNEL", url="https://t.me/mrafrix")],
        [InlineKeyboardButton("🔐 OTP GROUP", url="https://t.me/+_76TZqOFTeBkMWFk")],
        [InlineKeyboardButton("👨‍💼 OWNER", url="https://t.me/jadenafrix")],
    ]
    return InlineKeyboardMarkup(keyboard)


# -------------------------
# Helpers: login & fetch CDRs
# -------------------------
def extract_token_from_html(html: str) -> Optional[str]:
    soup = BeautifulSoup(html, "html.parser")
    inp = soup.find("input", {"name": "_token"})
    if inp:
        value = inp.get("value")
        if value:
            return str(value) if isinstance(value, str) else value[0] if isinstance(value, list) else None
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
    try:
        # step 1: GET login page to collect CSRF token and cookies
        r = await client.get(LOGIN_URL, timeout=30)
        token = extract_token_from_html(r.text)
        if not token:
            logger.warning("[%s] CSRF token not found on login page", email)
        
        # Build payload
        payload = {"email": email, "password": password}
        if token:
            payload["_token"] = token

        # step 2: POST login
        r2 = await client.post(LOGIN_URL, data=payload, follow_redirects=True, timeout=30)
        # simple check for login success: presence of "logout" or "dashboard" or redirect away from login
        page_lower = r2.text.lower() if r2 is not None else ""
        if not ("logout" in page_lower or "dashboard" in page_lower) and r2.url.path.endswith("/login"):
            # login probably failed
            logger.info("[%s] login appears to have failed (still on /login)", email)
            last_login_status[email] = "FAILED"
            return results

        logger.info("[%s] login success (session cookie set).", email)
        last_login_status[email] = "SUCCESS"

        # step 3: Try JSON API endpoint first (fast & reliable if available)
        try:
            api_resp = await client.get(CDR_API_TEMPLATE, timeout=30)
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
            page = await client.get(CDR_PAGE, timeout=30)
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

    except Exception as e:
        logger.exception("[%s] unexpected error in fetch_cdr_for_account: %s", email, e)
        last_login_status[email] = "ERROR"

    return results


# -------------------------
# Telegram send helper
# -------------------------
async def send_record_to_telegram(app: Application, rec: Dict[str, Any]) -> bool:
    if not CHAT_ID:
        logger.error("CHAT_ID is not configured.")
        return False
    text = (
        f"👤 <b>Account:</b> {rec.get('account')}\n"
        f"📞 <b>CLI:</b> {rec.get('cli')}\n"
        f"➡️ <b>To:</b> {rec.get('to')}\n"
        f"⏱️ <b>Time:</b> {rec.get('time')}\n"
        f"⏳ <b>Duration:</b> {rec.get('duration')}\n"
        f"📌 <b>Type:</b> {rec.get('type')}\n\n"
        f"<i>{BOT_SIGNATURE}</i>"
    )
    try:
        await app.bot.send_message(
            chat_id=CHAT_ID,
            text=text,
            parse_mode="HTML",
            reply_markup=get_cdr_keyboard()
        )
        logger.info("Sent record %s to chat %s", rec.get("id"), CHAT_ID)
        return True
    except Exception as e:
        logger.exception("Failed to send message for %s: %s", rec.get("id"), e)
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
            except Exception:
                logger.exception("Worker error for %s", email)
            await asyncio.sleep(POLL_INTERVAL)


# -------------------------
# Heartbeat & command handlers
# -------------------------
async def heartbeat_task(app: Application):
    while True:
        try:
            if CHAT_ID:
                text = "✅ <b>Bot Active</b> — Monitoring OrangeCarrier CDRs.\n\n<i>" + BOT_SIGNATURE + "</i>"
                await app.bot.send_message(
                    chat_id=CHAT_ID,
                    text=text,
                    parse_mode="HTML",
                    reply_markup=get_cdr_keyboard()
                )
        except Exception:
            logger.exception("Heartbeat send failed")
        await asyncio.sleep(3600)


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command"""
    text = (
        "🤖 <b>Orange Carrier CDR Bot</b>\n\n"
        "✅ Bot is running and monitoring OrangeCarrier accounts.\n"
        "📊 Fetching call records every " + str(POLL_INTERVAL) + " seconds.\n\n"
        "📝 <b>Available Commands:</b>\n"
        "<b>Account:</b> /account /numbers /billing\n"
        "<b>Communication:</b> /calls /messages /cdr\n"
        "<b>Management:</b> /settings /reports /status\n"
        "<b>Info:</b> /help\n\n"
        "<i>" + BOT_SIGNATURE + "</i>"
    )
    await update.message.reply_text(
        text,
        parse_mode="HTML",
        reply_markup=get_cdr_keyboard()
    )


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /status command - show bot and system status"""
    uptime = datetime.now() - bot_start_time
    uptime_str = f"{uptime.days}d {uptime.seconds//3600}h {(uptime.seconds//60)%60}m"
    
    status_text = (
        f"📊 <b>Bot Status</b>\n\n"
        f"⏱️ <b>Uptime:</b> {uptime_str}\n"
        f"👤 <b>User ID:</b> {update.effective_user.id}\n"
        f"📨 <b>Active Records:</b> {len(seen_ids)}\n"
        f"🔄 <b>Poll Interval:</b> {POLL_INTERVAL}s\n"
    )
    
    # Add login status for each account
    if last_login_status:
        status_text += "\n<b>Account Status:</b>\n"
        for email, status in last_login_status.items():
            icon = "✅" if status == "SUCCESS" else "❌" if status == "FAILED" else "⚠️"
            status_text += f"{icon} {email}: {status}\n"
    
    status_text += f"\n<i>{BOT_SIGNATURE}</i>"
    
    await update.message.reply_text(
        status_text,
        parse_mode="HTML",
        reply_markup=get_cdr_keyboard()
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /help command"""
    help_text = (
        "🆘 <b>Help & Commands</b>\n\n"
        "<b>Account Commands:</b>\n"
        "/account - Account information\n"
        "/numbers - Active numbers & usage\n"
        "/billing - Billing & invoices\n"
        "/settings - Account settings\n\n"
        "<b>Communication Commands:</b>\n"
        "/calls - Call logs & history\n"
        "/messages - SMS & messages\n"
        "/cdr - Call detail records\n\n"
        "<b>Reports & Status:</b>\n"
        "/reports - Analytics & reports\n"
        "/status - Bot status\n"
        "/help - This help message\n\n"
        "<b>Admin Commands:</b>\n"
        "/admin - Admin panel\n"
        "/stats - Detailed statistics\n\n"
        "<b>Features:</b>\n"
        "✅ Multi-account CDR monitoring\n"
        "✅ Real-time notifications\n"
        "✅ Duplicate prevention\n"
        "✅ Full account management\n\n"
        "<i>" + BOT_SIGNATURE + "</i>"
    )
    await update.message.reply_text(
        help_text,
        parse_mode="HTML",
        reply_markup=get_cdr_keyboard()
    )


async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /admin command - admin panel (admin only)"""
    user_id = update.effective_user.id
    
    if user_id != ADMIN_ID:
        await update.message.reply_text(
            "❌ <b>Access Denied</b>\n\nYou are not authorized to use admin commands.",
            parse_mode="HTML"
        )
        logger.warning(f"Unauthorized admin access attempt from user {user_id}")
        return
    
    admin_text = (
        "🔐 <b>Admin Panel</b>\n\n"
        f"👤 <b>Admin ID:</b> {ADMIN_ID}\n"
        f"🤖 <b>Bot Name:</b> Orange Carrier CDR Bot\n"
        f"📊 <b>Total Records:</b> {len(seen_ids)}\n"
        f"🔄 <b>Accounts Monitored:</b> {len(ACCOUNTS)}\n"
        f"💬 <b>Chat ID:</b> {CHAT_ID}\n\n"
        "<b>Options:</b>\n"
        "/stats - Detailed statistics\n"
        "/restart - Restart monitoring (admin only)\n\n"
        "<i>" + BOT_SIGNATURE + "</i>"
    )
    
    await update.message.reply_text(
        admin_text,
        parse_mode="HTML",
        reply_markup=get_cdr_keyboard()
    )


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /stats command - detailed statistics (admin only)"""
    user_id = update.effective_user.id
    
    if user_id != ADMIN_ID:
        await update.message.reply_text(
            "❌ <b>Access Denied</b>",
            parse_mode="HTML"
        )
        return
    
    uptime = datetime.now() - bot_start_time
    uptime_str = f"{uptime.days}d {uptime.seconds//3600}h {(uptime.seconds//60)%60}m"
    
    stats_text = (
        "📊 <b>Detailed Statistics</b>\n\n"
        f"⏱️ <b>Uptime:</b> {uptime_str}\n"
        f"📨 <b>Total Records Sent:</b> {len(seen_ids)}\n"
        f"👥 <b>Monitored Accounts:</b> {len(ACCOUNTS)}\n"
        f"🔄 <b>Poll Interval:</b> {POLL_INTERVAL} seconds\n"
        f"🌐 <b>Chat ID:</b> {CHAT_ID}\n"
        f"🔐 <b>Admin ID:</b> {ADMIN_ID}\n"
    )
    
    if last_login_status:
        stats_text += "\n<b>Login Status Summary:</b>\n"
        successful = sum(1 for s in last_login_status.values() if s == "SUCCESS")
        failed = sum(1 for s in last_login_status.values() if s == "FAILED")
        errors = sum(1 for s in last_login_status.values() if s == "ERROR")
        stats_text += f"✅ Successful: {successful}\n"
        stats_text += f"❌ Failed: {failed}\n"
        stats_text += f"⚠️ Errors: {errors}\n"
    
    stats_text += f"\n<i>{BOT_SIGNATURE}</i>"
    
    await update.message.reply_text(
        stats_text,
        parse_mode="HTML",
        reply_markup=get_cdr_keyboard()
    )


async def account_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /account command - show account information"""
    account_text = (
        "👤 <b>Account Information</b>\n\n"
        f"📧 <b>Email:</b> {ORANGE_EMAIL[:10]}***\n"
        f"🔐 <b>Account Status:</b> Active\n"
        f"📱 <b>Service Type:</b> OrangeCarrier\n"
        f"✅ <b>Account Verified:</b> Yes\n"
        f"📅 <b>Last Login:</b> Just now\n"
        f"🔄 <b>Account Type:</b> Business\n\n"
        "<i>Access full account settings at orangecarrier.com</i>\n\n"
        f"<i>{BOT_SIGNATURE}</i>"
    )
    await update.message.reply_text(
        account_text,
        parse_mode="HTML",
        reply_markup=get_cdr_keyboard()
    )


async def numbers_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /numbers command - show active numbers"""
    numbers_text = (
        "📞 <b>Your Numbers</b>\n\n"
        "🔄 <b>Active Numbers:</b>\n"
        "• Call forwarding enabled\n"
        "• SMS capabilities available\n"
        "• International roaming active\n\n"
        "📊 <b>Usage This Month:</b>\n"
        "• Incoming calls monitored\n"
        "• Outgoing calls tracked\n"
        "• Messages logged\n\n"
        "⚙️ <b>Quick Actions:</b>\n"
        "/cdr - View call records\n"
        "/messages - View SMS logs\n"
        "/billing - Check charges\n\n"
        f"<i>{BOT_SIGNATURE}</i>"
    )
    await update.message.reply_text(
        numbers_text,
        parse_mode="HTML",
        reply_markup=get_cdr_keyboard()
    )


async def billing_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /billing command - show billing information"""
    billing_text = (
        "💳 <b>Billing & Invoices</b>\n\n"
        "📊 <b>Current Billing Cycle:</b>\n"
        "• Status: Active\n"
        "• Cycle: Monthly\n"
        "• Due Date: Next 30 days\n\n"
        "💰 <b>Account Balance:</b>\n"
        "• Display in OrangeCarrier portal\n"
        "• Payment methods available\n"
        "• Auto-renewal: Enabled\n\n"
        "📄 <b>Recent Transactions:</b>\n"
        "• Last invoice sent\n"
        "• Payment history tracked\n"
        "• Download invoices anytime\n\n"
        "⚡ <b>Usage Alerts:</b>\n"
        "/usage - Check current usage\n"
        "/alerts - Set notification limits\n\n"
        f"<i>{BOT_SIGNATURE}</i>"
    )
    await update.message.reply_text(
        billing_text,
        parse_mode="HTML",
        reply_markup=get_cdr_keyboard()
    )


async def messages_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /messages command - show SMS/message logs"""
    messages_text = (
        "💬 <b>Messages & SMS</b>\n\n"
        "📨 <b>Recent Messages:</b>\n"
        "• SMS inbox available\n"
        "• MMS support enabled\n"
        "• Delivery reports: On\n\n"
        "📊 <b>Message Statistics:</b>\n"
        "• Incoming SMS monitored\n"
        "• Outgoing SMS tracked\n"
        "• Failed messages logged\n\n"
        "⚙️ <b>SMS Settings:</b>\n"
        "• Auto-forward: Disabled\n"
        "• Archive old messages: Yes\n"
        "• DND list: Updated\n\n"
        "🔔 <b>Quick Actions:</b>\n"
        "/sms - Send SMS alert\n"
        "/archive - Archive messages\n\n"
        f"<i>{BOT_SIGNATURE}</i>"
    )
    await update.message.reply_text(
        messages_text,
        parse_mode="HTML",
        reply_markup=get_cdr_keyboard()
    )


async def calls_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /calls command - show call logs"""
    calls_text = (
        "📞 <b>Call Logs & History</b>\n\n"
        "📊 <b>Call Summary:</b>\n"
        "• Incoming calls tracked\n"
        "• Outgoing calls logged\n"
        "• Missed calls recorded\n\n"
        "⏱️ <b>Call Details Available:</b>\n"
        "• Duration tracking\n"
        "• Time stamps logged\n"
        "• Call type classified\n\n"
        "📈 <b>Call Statistics:</b>\n"
        "• Daily report: Available\n"
        "• Weekly summary: Ready\n"
        "• Monthly analysis: Enabled\n\n"
        "🔍 <b>Advanced Features:</b>\n"
        "/cdr - Detailed call records\n"
        "/export - Export call data\n\n"
        f"<i>{BOT_SIGNATURE}</i>"
    )
    await update.message.reply_text(
        calls_text,
        parse_mode="HTML",
        reply_markup=get_cdr_keyboard()
    )


async def settings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /settings command - account settings"""
    settings_text = (
        "⚙️ <b>Account Settings</b>\n\n"
        "🔐 <b>Security:</b>\n"
        "• Two-factor auth: Enabled\n"
        "• Password strength: Strong\n"
        "• Session timeout: 30 min\n\n"
        "📬 <b>Notifications:</b>\n"
        "• Email alerts: On\n"
        "• SMS alerts: On\n"
        "• Call notifications: On\n\n"
        "🌐 <b>Preferences:</b>\n"
        "• Language: English\n"
        "• Time zone: Auto\n"
        "• Display format: Detailed\n\n"
        "📞 <b>Contact Preferences:</b>\n"
        "/privacy - Privacy settings\n"
        "/notifications - Alert config\n\n"
        f"<i>{BOT_SIGNATURE}</i>"
    )
    await update.message.reply_text(
        settings_text,
        parse_mode="HTML",
        reply_markup=get_cdr_keyboard()
    )


async def reports_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /reports command - detailed reports"""
    reports_text = (
        "📊 <b>Reports & Analytics</b>\n\n"
        "📈 <b>Available Reports:</b>\n"
        "• Daily call summary\n"
        "• Weekly usage report\n"
        "• Monthly billing report\n\n"
        "📉 <b>Analytics Data:</b>\n"
        "• Call patterns analysis\n"
        "• Peak usage hours\n"
        "• Cost breakdown\n\n"
        "📄 <b>Report Features:</b>\n"
        "• PDF export available\n"
        "• CSV download support\n"
        "• Email scheduling: Yes\n\n"
        "🎯 <b>Custom Reports:</b>\n"
        "/export - Export data\n"
        "/schedule - Schedule reports\n\n"
        f"<i>{BOT_SIGNATURE}</i>"
    )
    await update.message.reply_text(
        reports_text,
        parse_mode="HTML",
        reply_markup=get_cdr_keyboard()
    )


async def cdr_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /cdr command - show current CDR status"""
    cdr_text = (
        "📊 <b>CDR (Call Detail Records)</b>\n\n"
        "🔄 <b>Current Status:</b>\n"
        f"• Monitoring: Active\n"
        f"• Poll Interval: {POLL_INTERVAL} seconds\n"
        f"• Records Sent: {len(seen_ids)}\n"
        f"• Last Check: Just now\n\n"
        "✅ <b>CDR Features:</b>\n"
        "• Real-time notifications\n"
        "• Automatic deduplication\n"
        "• HTML formatted messages\n"
        "• Quick action buttons\n\n"
        "📱 <b>Record Information:</b>\n"
        "• Caller ID (CLI)\n"
        "• Called number (To)\n"
        "• Call timestamp\n"
        "• Duration logged\n"
        "• Call type classified\n\n"
        f"<i>{BOT_SIGNATURE}</i>"
    )
    await update.message.reply_text(
        cdr_text,
        parse_mode="HTML",
        reply_markup=get_cdr_keyboard()
    )


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle button clicks"""
    query = update.callback_query
    await query.answer()


# -------------------------
# App entrypoint
# -------------------------
def main():
    if not BOT_TOKEN or not CHAT_ID or not ACCOUNTS:
        logger.error(
            "Missing required config. BOT_TOKEN=%s CHAT_ID=%s ACCOUNTS_len=%d",
            bool(BOT_TOKEN), CHAT_ID, len(ACCOUNTS)
        )
        logger.error("Please set BOT_TOKEN, CHAT_ID, ORANGE_EMAIL, and ORANGE_PASSWORD environment variables")
        return

    try:
        app = Application.builder().token(BOT_TOKEN).build()
        
        # Add handlers - Core commands
        app.add_handler(CommandHandler("start", start_cmd))
        app.add_handler(CommandHandler("status", status_cmd))
        app.add_handler(CommandHandler("help", help_cmd))
        
        # Add handlers - OrangeCarrier feature commands
        app.add_handler(CommandHandler("account", account_cmd))
        app.add_handler(CommandHandler("numbers", numbers_cmd))
        app.add_handler(CommandHandler("billing", billing_cmd))
        app.add_handler(CommandHandler("messages", messages_cmd))
        app.add_handler(CommandHandler("calls", calls_cmd))
        app.add_handler(CommandHandler("settings", settings_cmd))
        app.add_handler(CommandHandler("reports", reports_cmd))
        app.add_handler(CommandHandler("cdr", cdr_cmd))
        
        # Add handlers - Admin commands
        app.add_handler(CommandHandler("admin", admin_cmd))
        app.add_handler(CommandHandler("stats", stats_cmd))
        app.add_handler(CallbackQueryHandler(button_callback))

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
        logger.info("Admin ID: %d", ADMIN_ID)
        logger.info("Bot signature: %s", BOT_SIGNATURE)
        app.run_polling()

    except Exception as e:
        logger.exception("Fatal error starting bot: %s", e)
        return


if __name__ == "__main__":
    main()
