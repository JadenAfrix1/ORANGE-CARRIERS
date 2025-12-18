#!/usr/bin/env python3
"""
Orange Carrier Telegram Bot
---------------------------
A comprehensive bot that monitors Orange Carrier accounts, fetches CDRs,
manages number ranges, and provides admin commands.

Signature: 𝐌ʀ 𝐀ғʀɪx 𝐓ᴇᴄʜ™
"""
import os
import asyncio
import logging
import json
from datetime import datetime
from typing import List, Dict, Any, Optional, Set

import httpx
from bs4 import BeautifulSoup
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

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

# OrangeCarrier credentials
ORANGE_EMAIL = os.getenv("ORANGE_EMAIL", "")
ORANGE_PASSWORD = os.getenv("ORANGE_PASSWORD", "")

POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "10"))
OWNER_ID = int(os.getenv("OWNER_ID", "0")) if os.getenv("OWNER_ID") else None

# Orange Carrier URLs
BASE_URL = "https://www.orangecarrier.com"
LOGIN_URL = f"{BASE_URL}/login"
CDR_PAGE = f"{BASE_URL}/CDR/mycdrs"
CDR_API = f"{BASE_URL}/CDR/mycdrs?start=0&length=50"
RANGES_URL = f"{BASE_URL}/myranges"
BALANCE_URL = f"{BASE_URL}/balance"
STATS_URL = f"{BASE_URL}/statistics"
PROFILE_URL = f"{BASE_URL}/profile"

# Hardcoded button links
SIGNATURE = "𝐌ʀ 𝐀ғʀɪx 𝐓ᴇᴄʜ™"
NUMBER_CHANNEL_URL = "https://t.me/mrafrix"
OTP_GROUP_URL = "https://t.me/+_76TZqOFTeBkMWFk"
OWNER_URL = "https://t.me/jadenafrix"

# -------------------------
# Global state
# -------------------------
seen_ids: Set[str] = set()
admin_users: Set[int] = set()
bot_stats = {
    "start_time": None,
    "login_count": 0,
    "cdr_count": 0,
    "message_count": 0,
    "last_login": None,
    "last_cdr_fetch": None,
}

# Add OWNER_ID to admin list
if OWNER_ID:
    admin_users.add(OWNER_ID)


# -------------------------
# Session Manager Class
# -------------------------
class OrangeCarrierSession:
    """Manages authenticated session with Orange Carrier"""
    
    def __init__(self):
        self.client: Optional[httpx.AsyncClient] = None
        self.is_logged_in = False
        self.last_login_time: Optional[datetime] = None
        self.account_info: Dict[str, Any] = {}
        self.ranges: List[Dict[str, Any]] = []
        self.balance: str = "Unknown"
        
    async def initialize(self):
        """Initialize HTTP client with proper cookie handling"""
        if self.client is None:
            # Create cookies jar for session persistence
            cookies = httpx.Cookies()
            self.client = httpx.AsyncClient(
                timeout=30.0,
                follow_redirects=False,  # Handle redirects manually for cookie control
                cookies=cookies,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.5",
                    "Connection": "keep-alive",
                    "Cache-Control": "no-cache",
                }
            )
    
    async def _follow_redirect(self, response: httpx.Response) -> httpx.Response:
        """Follow redirect while preserving cookies"""
        max_redirects = 10
        current = response
        
        for _ in range(max_redirects):
            if current.status_code not in (301, 302, 303, 307, 308):
                break
            
            location = current.headers.get("location")
            if not location:
                break
            
            # Handle relative URLs
            if location.startswith("/"):
                location = BASE_URL + location
            
            current = await self.client.get(location)
        
        return current
    
    async def login(self, email: str, password: str) -> bool:
        """Login to Orange Carrier and maintain session"""
        await self.initialize()
        
        try:
            # Step 1: Get login page and extract CSRF token
            logger.info("[Session] Fetching login page...")
            login_page = await self.client.get(LOGIN_URL)
            login_page = await self._follow_redirect(login_page)
            
            token = self._extract_csrf_token(login_page.text)
            if not token:
                logger.warning("[Session] CSRF token not found")
            
            # Step 2: Perform login
            payload = {"email": email, "password": password}
            if token:
                payload["_token"] = token
            
            logger.info("[Session] Attempting login for %s...", email)
            response = await self.client.post(LOGIN_URL, data=payload)
            
            # Step 3: Follow redirect and verify login success
            final_url = str(response.url)
            final_html = response.text
            
            if response.status_code in (301, 302, 303, 307, 308):
                final_response = await self._follow_redirect(response)
                final_url = str(final_response.url)
                final_html = final_response.text
                logger.info("[Session] Redirected to: %s", final_url)
            
            # Step 4: Verify login actually succeeded
            login_failed = False
            
            # Check 1: If we ended up back on /login page, login failed
            if "/login" in final_url and "logout" not in final_url:
                login_failed = True
                logger.warning("[Session] ❌ Still on login page - credentials likely invalid")
            
            # Check 2: Look for error messages in HTML
            if "invalid" in final_html.lower() or "incorrect" in final_html.lower():
                login_failed = True
                logger.warning("[Session] ❌ Login error message detected in page")
            
            # Check 3: Look for authenticated markers (logout link, dashboard elements)
            has_logout = "logout" in final_html.lower() or "sign out" in final_html.lower()
            has_dashboard = "dashboard" in final_html.lower() or "my ranges" in final_html.lower()
            
            if login_failed:
                self.is_logged_in = False
                bot_stats["login_failures"] = bot_stats.get("login_failures", 0) + 1
                logger.error("[Session] ❌ LOGIN FAILED - Please verify ORANGE_EMAIL and ORANGE_PASSWORD")
                return False
            
            if has_logout or has_dashboard:
                logger.info("[Session] ✅ Authenticated markers found (logout/dashboard)")
            else:
                # Not on login page, but no clear authenticated markers - proceed cautiously
                logger.warning("[Session] ⚠️ Login may have succeeded but no authenticated markers found")
            
            self.is_logged_in = True
            self.last_login_time = datetime.now()
            bot_stats["login_count"] += 1
            bot_stats["last_login"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            logger.info("[Session] ✅ LOGIN SUCCESS")
            
            # Extract account info from dashboard if available
            await self._extract_account_info(final_html)
            
            return True
                
        except Exception as e:
            logger.exception("[Session] Login error: %s", e)
            self.is_logged_in = False
            return False
    
    def _extract_csrf_token(self, html: str) -> Optional[str]:
        """Extract CSRF token from HTML"""
        try:
            soup = BeautifulSoup(html, "html.parser")
            inp = soup.find("input", {"name": "_token"})
            if inp:
                value = inp.get("value")
                if value:
                    return str(value)
        except Exception as e:
            logger.debug("Error extracting token: %s", e)
        return None
    
    async def _extract_account_info(self, html: str):
        """Extract account information from dashboard"""
        try:
            soup = BeautifulSoup(html, "html.parser")
            
            # Try to find balance
            balance_elem = soup.find(string=lambda t: t and ("balance" in t.lower() or "$" in t or "€" in t))
            if balance_elem:
                self.balance = balance_elem.strip()[:50]
            
            # Try to find username/email
            user_elem = soup.find("span", class_="username") or soup.find("span", class_="user-name")
            if user_elem:
                self.account_info["username"] = user_elem.get_text(strip=True)
                
        except Exception as e:
            logger.debug("Error extracting account info: %s", e)
    
    async def fetch_cdrs(self) -> List[Dict[str, Any]]:
        """Fetch CDR records"""
        if not self.is_logged_in:
            login_success = await self.login(ORANGE_EMAIL, ORANGE_PASSWORD)
            if not login_success:
                logger.warning("[CDR] Cannot fetch CDRs - login failed")
                return []
        
        results: List[Dict[str, Any]] = []
        
        try:
            # Try JSON API first
            logger.info("[CDR] Fetching CDRs from API...")
            api_resp = await self.client.get(CDR_API)
            api_resp = await self._follow_redirect(api_resp)
            
            # Check if we got redirected back to login (session expired)
            if "/login" in str(api_resp.url):
                logger.warning("[CDR] Session expired, attempting re-login...")
                self.is_logged_in = False
                login_success = await self.login(ORANGE_EMAIL, ORANGE_PASSWORD)
                if not login_success:
                    logger.error("[CDR] Re-login failed - cannot fetch CDRs")
                    return []
                api_resp = await self.client.get(CDR_API)
                api_resp = await self._follow_redirect(api_resp)
            
            if api_resp.status_code == 200 and "/login" not in str(api_resp.url):
                try:
                    data = api_resp.json()
                    results = self._parse_cdr_json(data)
                    if results:
                        logger.info("[CDR] ✅ Fetched %d records from JSON API", len(results))
                        bot_stats["last_cdr_fetch"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        return results
                except json.JSONDecodeError:
                    pass
            
            # Fallback to HTML parsing
            logger.info("[CDR] Fetching CDRs from HTML page...")
            html_resp = await self.client.get(CDR_PAGE)
            html_resp = await self._follow_redirect(html_resp)
            
            # Check if we got redirected back to login
            if "/login" not in str(html_resp.url):
                results = self._parse_cdr_html(html_resp.text)
            
            if results:
                logger.info("[CDR] ✅ Parsed %d records from HTML", len(results))
                bot_stats["last_cdr_fetch"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            else:
                logger.info("[CDR] No CDR records found")
                
        except Exception as e:
            logger.exception("[CDR] Error fetching CDRs: %s", e)
            
        return results
    
    def _parse_cdr_json(self, data: Any) -> List[Dict[str, Any]]:
        """Parse CDR data from JSON response"""
        results = []
        
        try:
            data_array = None
            if isinstance(data, dict):
                if "data" in data and isinstance(data["data"], list):
                    data_array = data["data"]
                elif "aaData" in data and isinstance(data["aaData"], list):
                    data_array = data["aaData"]
            elif isinstance(data, list):
                data_array = data
                
            if data_array:
                for row in data_array:
                    record = self._normalize_cdr_row(row)
                    if record:
                        results.append(record)
        except Exception as e:
            logger.debug("Error parsing CDR JSON: %s", e)
            
        return results
    
    def _parse_cdr_html(self, html: str) -> List[Dict[str, Any]]:
        """Parse CDR data from HTML table"""
        results = []
        
        try:
            soup = BeautifulSoup(html, "html.parser")
            table = soup.find("table")
            
            if table:
                tbody = table.find("tbody") or table
                if tbody:
                    for tr in tbody.find_all("tr"):
                        cols = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
                        if cols:
                            record = {
                                "id": f"{ORANGE_EMAIL}_{cols[0] if cols else ''}_{cols[2] if len(cols) > 2 else ''}",
                                "cli": cols[0] if len(cols) > 0 else "",
                                "to": cols[1] if len(cols) > 1 else "",
                                "time": cols[2] if len(cols) > 2 else "",
                                "duration": cols[3] if len(cols) > 3 else "",
                                "type": cols[4] if len(cols) > 4 else "",
                                "account": ORANGE_EMAIL,
                            }
                            results.append(record)
        except Exception as e:
            logger.debug("Error parsing CDR HTML: %s", e)
            
        return results
    
    def _normalize_cdr_row(self, row: Any) -> Optional[Dict[str, Any]]:
        """Normalize CDR row to standard format"""
        try:
            if isinstance(row, list):
                cli = str(row[0]) if len(row) > 0 else ""
                to_num = str(row[1]) if len(row) > 1 else ""
                time_str = str(row[2]) if len(row) > 2 else ""
                duration = str(row[3]) if len(row) > 3 else ""
                call_type = str(row[4]) if len(row) > 4 else ""
            elif isinstance(row, dict):
                cli = str(row.get("cli") or row.get("source") or row.get("caller") or row.get("from") or "")
                to_num = str(row.get("to") or row.get("destination") or "")
                time_str = str(row.get("time") or row.get("timestamp") or row.get("start_time") or "")
                duration = str(row.get("duration") or "")
                call_type = str(row.get("type") or row.get("status") or "")
            else:
                return None
            
            return {
                "id": f"{ORANGE_EMAIL}_{cli}_{time_str}",
                "cli": cli,
                "to": to_num,
                "time": time_str,
                "duration": duration,
                "type": call_type,
                "account": ORANGE_EMAIL,
            }
        except Exception:
            return None
    
    async def fetch_ranges(self) -> List[Dict[str, Any]]:
        """Fetch available number ranges"""
        if not self.is_logged_in:
            await self.login(ORANGE_EMAIL, ORANGE_PASSWORD)
        
        ranges = []
        try:
            resp = await self.client.get(RANGES_URL)
            resp = await self._follow_redirect(resp)
            
            if "/login" in str(resp.url):
                logger.warning("[Ranges] Session expired")
                return ranges
            
            soup = BeautifulSoup(resp.text, "html.parser")
            
            # Try to find ranges table
            table = soup.find("table")
            if table:
                for tr in table.find_all("tr")[1:]:  # Skip header
                    cols = [td.get_text(strip=True) for td in tr.find_all("td")]
                    if cols:
                        ranges.append({
                            "number": cols[0] if len(cols) > 0 else "",
                            "country": cols[1] if len(cols) > 1 else "",
                            "type": cols[2] if len(cols) > 2 else "",
                            "status": cols[3] if len(cols) > 3 else "",
                            "payout": cols[4] if len(cols) > 4 else "",
                        })
            
            self.ranges = ranges
            logger.info("[Ranges] Found %d ranges", len(ranges))
            
        except Exception as e:
            logger.exception("[Ranges] Error fetching ranges: %s", e)
            
        return ranges
    
    async def fetch_balance(self) -> str:
        """Fetch account balance"""
        if not self.is_logged_in:
            await self.login(ORANGE_EMAIL, ORANGE_PASSWORD)
        
        try:
            resp = await self.client.get(BALANCE_URL)
            resp = await self._follow_redirect(resp)
            
            if "/login" not in str(resp.url):
                soup = BeautifulSoup(resp.text, "html.parser")
                
                # Look for balance element
                balance_elem = soup.find(class_="balance") or soup.find(string=lambda t: t and "$" in t)
                if balance_elem:
                    self.balance = str(balance_elem).strip()[:100]
                
                # Look for any currency values
                for elem in soup.find_all(string=lambda t: t and ("$" in t or "€" in t or "balance" in t.lower())):
                    text = str(elem).strip()
                    if any(c.isdigit() for c in text):
                        self.balance = text[:100]
                        break
                    
        except Exception as e:
            logger.exception("[Balance] Error fetching balance: %s", e)
            
        return self.balance
    
    async def fetch_stats(self) -> Dict[str, Any]:
        """Fetch account statistics"""
        stats = {
            "total_calls": "N/A",
            "total_minutes": "N/A",
            "earnings": "N/A",
            "active_ranges": len(self.ranges),
        }
        
        if not self.is_logged_in:
            await self.login(ORANGE_EMAIL, ORANGE_PASSWORD)
        
        try:
            resp = await self.client.get(STATS_URL)
            resp = await self._follow_redirect(resp)
            
            if "/login" not in str(resp.url):
                soup = BeautifulSoup(resp.text, "html.parser")
                
                # Look for statistics values
                for card in soup.find_all(class_=["card", "stat-card", "widget"]):
                    text = card.get_text(strip=True)
                    if "call" in text.lower():
                        stats["total_calls"] = text[:50]
                    elif "minute" in text.lower():
                        stats["total_minutes"] = text[:50]
                    elif any(c in text for c in ["$", "€", "earning"]):
                        stats["earnings"] = text[:50]
                    
        except Exception as e:
            logger.exception("[Stats] Error fetching stats: %s", e)
            
        return stats
    
    async def close(self):
        """Close the HTTP client"""
        if self.client:
            await self.client.aclose()
            self.client = None


# Global session instance
session = OrangeCarrierSession()


# -------------------------
# Markup helper
# -------------------------
def get_keyboard_markup() -> InlineKeyboardMarkup:
    """Create inline keyboard with 3 buttons - each on separate row"""
    keyboard = [
        [InlineKeyboardButton("📞 NUMBER CHANNEL", url=NUMBER_CHANNEL_URL)],
        [InlineKeyboardButton("🔐 OTP GROUP", url=OTP_GROUP_URL)],
        [InlineKeyboardButton("👤 OWNER", url=OWNER_URL)],
    ]
    return InlineKeyboardMarkup(keyboard)


def is_admin(user_id: int) -> bool:
    """Check if user is admin"""
    return user_id == OWNER_ID or user_id in admin_users


# -------------------------
# Telegram send helper
# -------------------------
async def send_record_to_telegram(app: Application, rec: Dict[str, Any]) -> bool:
    """Send CDR record to Telegram with inline buttons"""
    if not CHAT_ID:
        logger.error("CHAT_ID is not configured.")
        return False
    
    text = (
        f"📱 𝗡𝗲𝘄 𝗖𝗗𝗥 𝗥𝗲𝗰𝗼𝗿𝗱\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"👤 Account: {rec.get('account')}\n"
        f"📞 CLI: {rec.get('cli')}\n"
        f"➡️ To: {rec.get('to')}\n"
        f"⏱️ Time: {rec.get('time')}\n"
        f"⏳ Duration: {rec.get('duration')}\n"
        f"📌 Type: {rec.get('type')}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"📍 {SIGNATURE}"
    )
    
    try:
        await app.bot.send_message(
            chat_id=CHAT_ID, 
            text=text,
            reply_markup=get_keyboard_markup()
        )
        bot_stats["message_count"] += 1
        bot_stats["cdr_count"] += 1
        logger.info("✅ Sent record %s to chat", rec.get("id"))
        return True
    except Exception as e:
        logger.exception("❌ Failed to send message: %s", e)
        return False


# -------------------------
# Admin Commands
# -------------------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command"""
    try:
        user_id = update.effective_user.id
        username = update.effective_user.username or update.effective_user.first_name
        
        text = (
            f"👋 Welcome {username}!\n\n"
            f"🤖 𝗢𝗿𝗮𝗻𝗴𝗲 𝗖𝗮𝗿𝗿𝗶𝗲𝗿 𝗕𝗼𝘁\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"This bot monitors Orange Carrier CDRs and sends them to Telegram.\n\n"
            f"📌 Use /help to see available commands\n\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"📍 {SIGNATURE}"
        )
        await update.message.reply_text(text, reply_markup=get_keyboard_markup())
        logger.info("✅ /start from user %s (%d)", username, user_id)
    except Exception as e:
        logger.exception("Error in start_cmd: %s", e)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /help command"""
    user_id = update.effective_user.id
    
    admin_cmds = ""
    if is_admin(user_id):
        admin_cmds = (
            f"\n\n🔐 𝗔𝗱𝗺𝗶𝗻 𝗖𝗼𝗺𝗺𝗮𝗻𝗱𝘀:\n"
            f"• /addadmin <user_id> - Add admin\n"
            f"• /removeadmin <user_id> - Remove admin\n"
            f"• /admins - List all admins\n"
            f"• /broadcast <msg> - Send to all\n"
            f"• /login - Force re-login\n"
            f"• /setpoll <seconds> - Set poll interval"
        )
    
    text = (
        f"📚 𝗔𝘃𝗮𝗶𝗹𝗮𝗯𝗹𝗲 𝗖𝗼𝗺𝗺𝗮𝗻𝗱𝘀\n"
        f"━━━━━━━━━━━━━━━━\n\n"
        f"📊 𝗚𝗲𝗻𝗲𝗿𝗮𝗹:\n"
        f"• /start - Start the bot\n"
        f"• /help - Show this message\n"
        f"• /status - Bot status\n"
        f"• /ping - Check bot response\n\n"
        f"💰 𝗔𝗰𝗰𝗼𝘂𝗻𝘁:\n"
        f"• /balance - Check account balance\n"
        f"• /ranges - List number ranges\n"
        f"• /stats - Account statistics\n"
        f"• /cdrs - Fetch latest CDRs\n"
        f"• /info - Account info"
        f"{admin_cmds}\n\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"📍 {SIGNATURE}"
    )
    await update.message.reply_text(text, reply_markup=get_keyboard_markup())


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /status command"""
    uptime = "N/A"
    if bot_stats["start_time"]:
        delta = datetime.now() - bot_stats["start_time"]
        hours, remainder = divmod(int(delta.total_seconds()), 3600)
        minutes, seconds = divmod(remainder, 60)
        uptime = f"{hours}h {minutes}m {seconds}s"
    
    text = (
        f"📊 𝗕𝗼𝘁 𝗦𝘁𝗮𝘁𝘂𝘀\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"🟢 Status: Running\n"
        f"⏱️ Uptime: {uptime}\n"
        f"🔐 Logged In: {'✅' if session.is_logged_in else '❌'}\n"
        f"📧 Account: {ORANGE_EMAIL}\n"
        f"⏰ Poll Interval: {POLL_INTERVAL}s\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"📈 𝗦𝘁𝗮𝘁𝗶𝘀𝘁𝗶𝗰𝘀:\n"
        f"• Total Logins: {bot_stats['login_count']}\n"
        f"• CDRs Sent: {bot_stats['cdr_count']}\n"
        f"• Messages Sent: {bot_stats['message_count']}\n"
        f"• Last Login: {bot_stats['last_login'] or 'N/A'}\n"
        f"• Last CDR Fetch: {bot_stats['last_cdr_fetch'] or 'N/A'}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"📍 {SIGNATURE}"
    )
    await update.message.reply_text(text, reply_markup=get_keyboard_markup())


async def ping_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /ping command"""
    start = datetime.now()
    msg = await update.message.reply_text("🏓 Pong!")
    elapsed = (datetime.now() - start).total_seconds() * 1000
    
    text = f"🏓 𝗣𝗼𝗻𝗴!\n\n⚡ Response: {elapsed:.0f}ms\n\n📍 {SIGNATURE}"
    await msg.edit_text(text, reply_markup=get_keyboard_markup())


async def balance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /balance command"""
    await update.message.reply_text("💰 Fetching balance...")
    
    balance = await session.fetch_balance()
    
    text = (
        f"💰 𝗔𝗰𝗰𝗼𝘂𝗻𝘁 𝗕𝗮𝗹𝗮𝗻𝗰𝗲\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"📧 Account: {ORANGE_EMAIL}\n"
        f"💵 Balance: {balance}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"📍 {SIGNATURE}"
    )
    await update.message.reply_text(text, reply_markup=get_keyboard_markup())


async def ranges_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /ranges command"""
    await update.message.reply_text("📞 Fetching number ranges...")
    
    ranges = await session.fetch_ranges()
    
    if ranges:
        range_text = "\n".join([
            f"• {r.get('number', 'N/A')} ({r.get('country', 'N/A')}) - {r.get('status', 'N/A')}"
            for r in ranges[:20]  # Limit to 20
        ])
    else:
        range_text = "No ranges found or unable to fetch."
    
    text = (
        f"📞 𝗡𝘂𝗺𝗯𝗲𝗿 𝗥𝗮𝗻𝗴𝗲𝘀\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"📧 Account: {ORANGE_EMAIL}\n"
        f"📊 Total Ranges: {len(ranges)}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"{range_text}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"📍 {SIGNATURE}"
    )
    await update.message.reply_text(text, reply_markup=get_keyboard_markup())


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /stats command"""
    await update.message.reply_text("📊 Fetching statistics...")
    
    stats = await session.fetch_stats()
    
    text = (
        f"📊 𝗔𝗰𝗰𝗼𝘂𝗻𝘁 𝗦𝘁𝗮𝘁𝗶𝘀𝘁𝗶𝗰𝘀\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"📧 Account: {ORANGE_EMAIL}\n"
        f"📞 Total Calls: {stats.get('total_calls', 'N/A')}\n"
        f"⏱️ Total Minutes: {stats.get('total_minutes', 'N/A')}\n"
        f"💰 Earnings: {stats.get('earnings', 'N/A')}\n"
        f"📋 Active Ranges: {stats.get('active_ranges', 0)}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"📍 {SIGNATURE}"
    )
    await update.message.reply_text(text, reply_markup=get_keyboard_markup())


async def cdrs_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /cdrs command - fetch and display latest CDRs"""
    await update.message.reply_text("📱 Fetching latest CDRs...")
    
    cdrs = await session.fetch_cdrs()
    
    if cdrs:
        cdr_text = "\n".join([
            f"• {c.get('cli', 'N/A')} → {c.get('to', 'N/A')} ({c.get('time', 'N/A')})"
            for c in cdrs[:10]  # Limit to 10
        ])
    else:
        cdr_text = "No CDRs found or unable to fetch."
    
    text = (
        f"📱 𝗟𝗮𝘁𝗲𝘀𝘁 𝗖𝗗𝗥𝘀\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"📧 Account: {ORANGE_EMAIL}\n"
        f"📊 Total Records: {len(cdrs)}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"{cdr_text}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"📍 {SIGNATURE}"
    )
    await update.message.reply_text(text, reply_markup=get_keyboard_markup())


async def info_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /info command"""
    text = (
        f"ℹ️ 𝗔𝗰𝗰𝗼𝘂𝗻𝘁 𝗜𝗻𝗳𝗼𝗿𝗺𝗮𝘁𝗶𝗼𝗻\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"📧 Email: {ORANGE_EMAIL}\n"
        f"🔐 Logged In: {'✅' if session.is_logged_in else '❌'}\n"
        f"💰 Balance: {session.balance}\n"
        f"📞 Ranges: {len(session.ranges)}\n"
        f"⏰ Last Login: {bot_stats['last_login'] or 'N/A'}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"📍 {SIGNATURE}"
    )
    await update.message.reply_text(text, reply_markup=get_keyboard_markup())


# -------------------------
# Admin-only Commands
# -------------------------
async def addadmin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /addadmin command"""
    user_id = update.effective_user.id
    
    if not is_admin(user_id):
        await update.message.reply_text("❌ You are not authorized to use this command.")
        return
    
    if not context.args:
        await update.message.reply_text("Usage: /addadmin <user_id>")
        return
    
    try:
        new_admin = int(context.args[0])
        admin_users.add(new_admin)
        await update.message.reply_text(f"✅ User {new_admin} added as admin!\n\n📍 {SIGNATURE}")
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID. Please provide a number.")


async def removeadmin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /removeadmin command"""
    user_id = update.effective_user.id
    
    if not is_admin(user_id):
        await update.message.reply_text("❌ You are not authorized to use this command.")
        return
    
    if not context.args:
        await update.message.reply_text("Usage: /removeadmin <user_id>")
        return
    
    try:
        remove_id = int(context.args[0])
        if remove_id == OWNER_ID:
            await update.message.reply_text("❌ Cannot remove the owner!")
            return
        admin_users.discard(remove_id)
        await update.message.reply_text(f"✅ User {remove_id} removed from admins!\n\n📍 {SIGNATURE}")
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID. Please provide a number.")


async def admins_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /admins command"""
    user_id = update.effective_user.id
    
    if not is_admin(user_id):
        await update.message.reply_text("❌ You are not authorized to use this command.")
        return
    
    admin_list = "\n".join([f"• {uid} {'(Owner)' if uid == OWNER_ID else ''}" for uid in admin_users])
    
    text = (
        f"👥 𝗔𝗱𝗺𝗶𝗻 𝗟𝗶𝘀𝘁\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"{admin_list}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"Total: {len(admin_users)}\n\n"
        f"📍 {SIGNATURE}"
    )
    await update.message.reply_text(text)


async def login_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /login command - force re-login"""
    user_id = update.effective_user.id
    
    if not is_admin(user_id):
        await update.message.reply_text("❌ You are not authorized to use this command.")
        return
    
    await update.message.reply_text("🔄 Attempting to login...")
    
    success = await session.login(ORANGE_EMAIL, ORANGE_PASSWORD)
    
    if success:
        await update.message.reply_text(f"✅ Login successful!\n\n📍 {SIGNATURE}", reply_markup=get_keyboard_markup())
    else:
        await update.message.reply_text(f"❌ Login failed. Check credentials.\n\n📍 {SIGNATURE}")


async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /broadcast command"""
    user_id = update.effective_user.id
    
    if not is_admin(user_id):
        await update.message.reply_text("❌ You are not authorized to use this command.")
        return
    
    if not context.args:
        await update.message.reply_text("Usage: /broadcast <message>")
        return
    
    message = " ".join(context.args)
    
    text = (
        f"📢 𝗕𝗿𝗼𝗮𝗱𝗰𝗮𝘀𝘁 𝗠𝗲𝘀𝘀𝗮𝗴𝗲\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"{message}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"📍 {SIGNATURE}"
    )
    
    try:
        await update.effective_chat.send_message(text, reply_markup=get_keyboard_markup())
        if CHAT_ID and CHAT_ID != update.effective_chat.id:
            await context.bot.send_message(chat_id=CHAT_ID, text=text, reply_markup=get_keyboard_markup())
        await update.message.reply_text("✅ Broadcast sent!")
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to broadcast: {e}")


# -------------------------
# Worker task
# -------------------------
async def cdr_worker(app: Application):
    """Worker task that polls for CDRs"""
    logger.info("🚀 Starting CDR worker...")
    login_failures = 0
    max_failures = 5
    backoff_time = 60
    
    while True:
        try:
            # Login if needed
            if not session.is_logged_in:
                login_success = await session.login(ORANGE_EMAIL, ORANGE_PASSWORD)
                if not login_success:
                    login_failures += 1
                    if login_failures >= max_failures:
                        logger.error(
                            "[Worker] Login failed %d times - backing off for %d seconds. "
                            "Please verify ORANGE_EMAIL and ORANGE_PASSWORD are correct.",
                            login_failures, backoff_time
                        )
                        await asyncio.sleep(backoff_time)
                        backoff_time = min(backoff_time * 2, 3600)
                    else:
                        logger.warning("[Worker] Login failed, will retry in %d seconds", POLL_INTERVAL)
                    await asyncio.sleep(POLL_INTERVAL)
                    continue
                else:
                    login_failures = 0
                    backoff_time = 60
            
            # Fetch CDRs
            cdrs = await session.fetch_cdrs()
            
            # Send new CDRs
            for cdr in cdrs:
                if cdr["id"] not in seen_ids:
                    seen_ids.add(cdr["id"])
                    await send_record_to_telegram(app, cdr)
                    
        except Exception as e:
            logger.exception("Worker error: %s", e)
            session.is_logged_in = False
            
        await asyncio.sleep(POLL_INTERVAL)


async def heartbeat_task(app: Application):
    """Send periodic heartbeat messages"""
    while True:
        try:
            if CHAT_ID:
                text = (
                    f"💓 𝗛𝗲𝗮𝗿𝘁𝗯𝗲𝗮𝘁\n"
                    f"━━━━━━━━━━━━━━━━\n"
                    f"✅ Bot is active and monitoring\n"
                    f"📧 Account: {ORANGE_EMAIL}\n"
                    f"🔐 Status: {'Online' if session.is_logged_in else 'Reconnecting...'}\n"
                    f"━━━━━━━━━━━━━━━━\n"
                    f"📍 {SIGNATURE}"
                )
                await app.bot.send_message(chat_id=CHAT_ID, text=text, reply_markup=get_keyboard_markup())
                logger.info("✅ Heartbeat sent")
        except Exception:
            logger.exception("❌ Heartbeat failed")
            
        await asyncio.sleep(3600)  # 1 hour


# -------------------------
# App entrypoint
# -------------------------
def main():
    """Main entry point"""
    logger.info("=" * 50)
    logger.info("🚀 Orange Carrier Bot Starting")
    logger.info("=" * 50)
    
    if not BOT_TOKEN:
        logger.error("❌ Missing BOT_TOKEN")
        return
    
    if not CHAT_ID:
        logger.error("❌ Missing CHAT_ID")
        return
    
    if not ORANGE_EMAIL or not ORANGE_PASSWORD:
        logger.error("❌ Missing ORANGE_EMAIL or ORANGE_PASSWORD")
        return

    bot_stats["start_time"] = datetime.now()
    logger.info("✅ All environment variables set")
    logger.info("📧 Email: %s", ORANGE_EMAIL)
    logger.info("💬 Chat ID: %s", CHAT_ID)
    logger.info("👤 Owner ID: %s", OWNER_ID)
    logger.info("⏱️ Poll Interval: %d seconds", POLL_INTERVAL)

    try:
        app = Application.builder().token(BOT_TOKEN).build()
        
        # Register command handlers
        app.add_handler(CommandHandler("start", start_cmd))
        app.add_handler(CommandHandler("help", help_cmd))
        app.add_handler(CommandHandler("status", status_cmd))
        app.add_handler(CommandHandler("ping", ping_cmd))
        app.add_handler(CommandHandler("balance", balance_cmd))
        app.add_handler(CommandHandler("ranges", ranges_cmd))
        app.add_handler(CommandHandler("stats", stats_cmd))
        app.add_handler(CommandHandler("cdrs", cdrs_cmd))
        app.add_handler(CommandHandler("info", info_cmd))
        
        # Admin commands
        app.add_handler(CommandHandler("addadmin", addadmin_cmd))
        app.add_handler(CommandHandler("removeadmin", removeadmin_cmd))
        app.add_handler(CommandHandler("admins", admins_cmd))
        app.add_handler(CommandHandler("login", login_cmd))
        app.add_handler(CommandHandler("broadcast", broadcast_cmd))

        # Post-init: start workers
        async def on_post_init(_: Application):
            logger.info("🔧 Initializing...")
            asyncio.create_task(cdr_worker(app))
            asyncio.create_task(heartbeat_task(app))
            logger.info("✅ Bot initialized and monitoring")

        app.post_init = on_post_init

        logger.info("🔄 Starting Telegram polling...")
        app.run_polling()
        
    except Exception as e:
        logger.exception("❌ Fatal error: %s", e)
        raise


if __name__ == "__main__":
    main()
