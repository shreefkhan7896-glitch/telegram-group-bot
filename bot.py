"""
Paid Telegram Group Management Bot
==================================
Admin (customer) ke liye:
  - /setup  (group ke andar)  : group register karo
  - /admin  (bot DM me)       : dashboard - UPI/QR, Plans, Members, Revenue, Broadcast

Member ke liye:
  - Admin ka join link: https://t.me/<bot_username>?start=g<group_id>
  - Plan select -> UPI payment -> screenshot + 12-digit UTR -> admin approve
    -> ID-locked join-request link -> auto expiry / renewal engine

Data: Supabase (PostgreSQL). Paisa hamesha seedha admin ke UPI me jata hai,
bot kabhi paisa touch nahi karta (zero commission model).

Render par Web Service ke liye chhota health server bhi chalta hai (PORT env).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from dotenv import load_dotenv
from supabase import Client, create_client
from telegram import (
    ChatPermissions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ChatMemberStatus
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
    ChatJoinRequestHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

load_dotenv()

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=os.getenv("LOG_LEVEL", "INFO"),
)
# httpx logs full Telegram API request URLs at INFO level, including the bot token.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger("groupbot")

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
BOT_TOKEN = os.environ["BOT_TOKEN"]
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]  # service_role key (backend only)

IST = timezone(timedelta(hours=5, minutes=30))
UTR_RE = re.compile(r"^\d{12}$")

PLAN_NAMES = {"1m": "1 Month", "3m": "3 Months", "1yr": "1 Year"}
DEFAULT_PLANS = {
    "1m": {"price": 499, "days": 30},
    "3m": {"price": 1299, "days": 90},
    "1yr": {"price": 4499, "days": 365},
}

sb: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Conversation states
WAIT_PLAN, WAIT_SCREENSHOT, WAIT_UTR = range(3)
A_NAV, A_UPI, A_QR, A_PLAN_PRICE, A_BCAST = range(10, 15)
BILLING_SCREENSHOT, BILLING_UTR = range(20, 22)
A_WELCOME, A_FAQ = range(30, 32)

SUPER_ADMIN_IDS = {
    int(value.strip())
    for value in os.getenv("SUPER_ADMIN_IDS", "").split(",")
    if value.strip().isdigit()
}
PLATFORM_UPI_ID = os.getenv("PLATFORM_UPI_ID", "").strip()
SAAS_MONTHLY_PRICE = int(os.getenv("SAAS_MONTHLY_PRICE", "299"))
SAAS_BILLING_DAYS = int(os.getenv("SAAS_BILLING_DAYS", "30"))
PLATFORM_QR_FILE_ID = os.getenv("PLATFORM_QR_FILE_ID", "").strip()
LINK_RE = re.compile(r"(?:https?://|www\.|t\.me/|telegram\.me/|(?<!\w)@[A-Za-z0-9_]{5,})", re.I)

# Flood control + group admin cache (in-memory, MVP ke liye kaafi hai)
_flood: dict[tuple[int, int], list[float]] = {}
_admin_cache: dict[int, tuple[float, set[int]]] = {}
_join_seen: dict[tuple[int, int], float] = {}


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_dt(value):
    """Supabase se aayi ISO string ko aware datetime me badlo."""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def fmt_ist(dt) -> str:
    dt = parse_dt(dt)
    if dt is None:
        return "-"
    return dt.astimezone(IST).strftime("%d %b %Y, %I:%M %p IST")


def get_group_by_id(group_id: int):
    try:
        res = sb.table("groups").select("*").eq("id", group_id).maybe_single().execute()
        return res.data
    except Exception:
        logger.exception("get_group_by_id failed")
        return None


def get_group_by_chat(chat_id: int):
    try:
        res = sb.table("groups").select("*").eq("chat_id", chat_id).maybe_single().execute()
        return res.data
    except Exception:
        logger.exception("get_group_by_chat failed")
        return None


def group_is_active(group: dict | None) -> bool:
    return bool(group and group.get("is_active", True))


def set_group_lifecycle(group: dict, *, active: bool, reason: str | None = None) -> bool:
    """Soft-disable inaccessible groups while retaining billing/member history."""
    was_active = group_is_active(group)
    if was_active == active:
        return False
    try:
        sb.table("groups").update(
            {"is_active": active}
        ).eq("id", group["id"]).execute()
        group["is_active"] = active
        logger.info(
            "group lifecycle changed group_id=%s active=%s reason=%s",
            group["id"],
            active,
            reason,
        )
        return True
    except Exception:
        logger.exception("group lifecycle update failed for group_id=%s", group.get("id"))
        return False


async def reconcile_group_access(bot, group: dict, *, notify_owner: bool = True) -> str:
    """Check Telegram's live membership state and hide groups the bot cannot manage."""
    try:
        bot_member = await bot.get_chat_member(group["chat_id"], bot.id)
    except (BadRequest, Forbidden) as exc:
        changed = set_group_lifecycle(
            group, active=False, reason="telegram_chat_unavailable"
        )
        logger.info(
            "group unavailable to bot group_id=%s error_type=%s",
            group.get("id"),
            type(exc).__name__,
        )
        if changed and notify_owner and group.get("owner_user_id"):
            try:
                await bot.send_message(
                    group["owner_user_id"],
                    f"{group.get('title') or 'A group'} dashboard se hide kar diya hai kyunki "
                    "bot ab us group ko access nahi kar sakta. Bot ko dobara admin banao "
                    "ya group delete ho gaya ho to koi action zaroori nahi.",
                )
            except TelegramError:
                logger.info("could not notify owner for inactive group_id=%s", group.get("id"))
        return "inactive"
    except TelegramError as exc:
        # Network/API outages are not evidence that a group was deleted.
        logger.warning(
            "group access check temporarily failed group_id=%s error_type=%s",
            group.get("id"),
            type(exc).__name__,
        )
        return "error"
    except Exception:
        logger.exception("unexpected group access check failure group_id=%s", group.get("id"))
        return "error"

    if bot_member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
        was_active = group_is_active(group)
        changed = set_group_lifecycle(group, active=True)
        if not was_active and not changed:
            return "error"
        if changed and notify_owner and group.get("owner_user_id"):
            try:
                await bot.send_message(
                    group["owner_user_id"],
                    f"{group.get('title') or 'Group'} bot dashboard me wapas active hai.",
                )
            except TelegramError:
                logger.info("could not notify owner for reactivated group_id=%s", group.get("id"))
        return "active"

    changed = set_group_lifecycle(group, active=False, reason="bot_not_admin")
    if changed and notify_owner and group.get("owner_user_id"):
        try:
            await bot.send_message(
                group["owner_user_id"],
                f"{group.get('title') or 'A group'} dashboard se hide kar diya hai kyunki "
                "bot ab admin nahi hai. Gatekeeper chalane ke liye bot ko dobara admin banao "
                "aur /setup chalao.",
            )
        except TelegramError:
            logger.info("could not notify owner for non-admin group_id=%s", group.get("id"))
    return "inactive"


def get_owned_groups(user_id: int) -> list[dict]:
    try:
        return (
            sb.table("groups")
            .select("id,title,chat_id,owner_user_id,is_active")
            .eq("owner_user_id", user_id)
            .order("id")
            .execute()
            .data
            or []
        )
    except Exception:
        logger.exception("owned groups query failed for user_id=%s", user_id)
        return []


async def get_live_owned_groups(user_id: int, bot) -> list[dict]:
    groups = get_owned_groups(user_id)
    active = []
    for group in groups:
        state = await reconcile_group_access(bot, group)
        if state == "active" or (state == "error" and group_is_active(group)):
            active.append(group)
    return active


def get_member(group_id: int, user_id: int):
    try:
        res = (
            sb.table("members")
            .select("*")
            .eq("group_id", group_id)
            .eq("user_id", user_id)
            .maybe_single()
            .execute()
        )
        return res.data
    except Exception:
        logger.exception("get_member failed")
        return None


def member_is_active(group_id: int, user_id: int) -> bool:
    member = get_member(group_id, user_id)
    if not member or member.get("status") != "ACTIVE":
        return False
    expiry = parse_dt(member.get("expiry_date"))
    return bool(expiry and expiry > now_utc())


# --------------------------------------------------------------------------
# Read-only mode, title sync, unban + join-link helpers (points 1, 3, 4, 6)
# --------------------------------------------------------------------------
def member_chat_enabled(group: dict | None) -> bool:
    """Dashboard toggle: False (default) = read-only/broadcast mode,
    True = paid members group me chat kar sakte hain."""
    return bool(group and group.get("member_chat_enabled", False))


async def apply_member_chat_mode(bot, group: dict) -> bool:
    """Group ke default permissions toggle ke hisaab se set karo.

    Read-only mode me Telegram members ko message typing box hi nahi dikhata —
    sirf Mute/Unmute button rehta hai. Group admins par ye pabandi laagu nahi hoti.
    """
    try:
        perms = (
            ChatPermissions.all_permissions()
            if member_chat_enabled(group)
            else ChatPermissions.no_permissions()
        )
        await bot.set_chat_permissions(chat_id=group["chat_id"], permissions=perms)
        return True
    except Exception:
        logger.exception("set_chat_permissions failed for group %s", group.get("id"))
        return False


async def sync_group_title(bot, group: dict) -> dict:
    """get_chat se taaza title lekar DB me sync karo (purana cached naam atak jata hai)."""
    try:
        chat = await bot.get_chat(group["chat_id"])
    except Exception:
        return group
    fresh = getattr(chat, "title", None)
    if fresh and fresh != group.get("title"):
        try:
            sb.table("groups").update({"title": fresh}).eq("id", group["id"]).execute()
            group["title"] = fresh
        except Exception:
            logger.exception("group title sync failed for group %s", group.get("id"))
    return group


async def ensure_unbanned(bot, chat_id: int, user_id: int):
    """Banned member ko pehle unban karo, warna naya invite link 'expired' dikhta hai."""
    try:
        await bot.unban_chat_member(chat_id=chat_id, user_id=user_id, only_if_banned=True)
    except Exception:
        pass


def render_custom_welcome(group: dict, first_name: str | None) -> str | None:
    template = (group or {}).get("welcome_message")
    if not template:
        return None
    return template.replace("{name}", first_name or "Member").replace(
        "{group}", group.get("title") or ""
    )


def save_invite_message_id(group_id: int, user_id: int, message_id: int):
    try:
        sb.table("members").update({"invite_message_id": message_id}).eq(
            "group_id", group_id
        ).eq("user_id", user_id).execute()
    except Exception:
        logger.info("invite_message_id save nahi hua (migration 000002 pending ho sakti hai)")


async def member_presence(bot, chat_id: int, user_id: int) -> bool:
    """User group ke andar hai ya nahi (restricted-but-joined bhi andar mana jayega)."""
    try:
        cm = await bot.get_chat_member(chat_id, user_id)
        return cm.status in (
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.OWNER,
        ) or (
            cm.status == ChatMemberStatus.RESTRICTED
            and getattr(cm, "is_member", False)
        )
    except Exception:
        return False


async def issue_join_link(bot, group: dict, user_id: int, intro: str) -> str | None:
    """Unban -> ID-locked join link -> DM. DM ka message_id members me save hota hai
    taaki join approve hone par usi message ko 'safaltapurvak jud gaye' me edit kiya ja sake."""
    await ensure_unbanned(bot, group["chat_id"], user_id)
    try:
        link = await bot.create_chat_invite_link(
            chat_id=group["chat_id"],
            name=f"paid-{user_id}"[:32],
            expire_date=now_utc() + timedelta(hours=24),
            creates_join_request=True,
        )
    except Exception:
        logger.exception("invite link create failed")
        return None
    member = get_member(group["id"], user_id)
    expiry_line = ""
    if member and member.get("expiry_date"):
        expiry_line = f"\nMembership valid: {fmt_ist(member.get('expiry_date'))}"
    text = (
        f"{intro}{expiry_line}\n\n"
        f"Join link (sirf tumhare liye, 24 ghante valid):\n{link.invite_link}\n\n"
        "Link dabao aur 'Join Request' bhejo, bot tumhe auto-approve kar dega. "
        "Link kisi ke saath share mat karna, wo kisi aur ke liye kaam nahi karega."
    )
    try:
        sent = await bot.send_message(chat_id=user_id, text=text)
        save_invite_message_id(group["id"], user_id, sent.message_id)
    except Exception:
        logger.exception("join link DM failed")
    return link.invite_link


def get_admin_account(user_id: int, *, create: bool = True):
    try:
        res = (
            sb.table("admin_accounts")
            .select("*")
            .eq("user_id", user_id)
            .maybe_single()
            .execute()
        )
        if res.data:
            return res.data
        if not create:
            return None
        started = now_utc()
        try:
            sb.table("admin_accounts").insert(
                {
                    "user_id": user_id,
                    "trial_started_at": started.isoformat(),
                    "trial_ends_at": (started + timedelta(days=7)).isoformat(),
                    "subscription_status": "TRIAL",
                }
            ).execute()
        except Exception:
            # Another update may have initialized the row at the same time.
            pass
        res = (
            sb.table("admin_accounts")
            .select("*")
            .eq("user_id", user_id)
            .maybe_single()
            .execute()
        )
        return res.data
    except Exception:
        logger.exception("get_admin_account failed; schema migration may be missing")
        return None


def admin_account_access(
    user_id: int, *, create: bool = True
) -> tuple[bool, str, datetime | None]:
    account = get_admin_account(user_id, create=create)
    if not account:
        return False, "UNAVAILABLE", None
    # Keep existing groups operational until the platform has configured a
    # payment destination and at least one reviewer; trial enforcement starts
    # automatically as soon as SaaS billing can actually accept payments.
    if not SUPER_ADMIN_IDS or not PLATFORM_UPI_ID:
        return True, "BILLING_SETUP_REQUIRED", None
    now = now_utc()
    status = account.get("subscription_status")
    paid_until = parse_dt(account.get("paid_until"))
    trial_ends = parse_dt(account.get("trial_ends_at"))
    if status == "ACTIVE" and paid_until and paid_until > now:
        return True, "ACTIVE", paid_until
    if status == "TRIAL" and trial_ends and trial_ends > now:
        return True, "TRIAL", trial_ends
    return False, "LOCKED", trial_ends or paid_until


def get_plans(group: dict) -> dict:
    plans = group.get("plans") or {}
    merged = {}
    for label, default in DEFAULT_PLANS.items():
        p = plans.get(label) or default
        merged[label] = {"price": int(p["price"]), "days": int(p["days"])}
    return merged


def upsert_lead(group_id: int, user, plan_label=None, stage=None):
    row = {
        "group_id": group_id,
        "user_id": user.id,
        "username": user.username,
        "full_name": user.full_name,
        "updated_at": now_utc().isoformat(),
    }
    if plan_label:
        row["plan_label"] = plan_label
    if stage:
        row["stage"] = stage
    try:
        sb.table("pending_leads").upsert(row, on_conflict="group_id,user_id").execute()
    except Exception:
        logger.exception("upsert_lead failed")


def plan_buttons(group_id: int, plans: dict, prefix: str = "plan") -> InlineKeyboardMarkup:
    rows = []
    for label in ("1m", "3m", "1yr"):
        p = plans[label]
        rows.append(
            [
                InlineKeyboardButton(
                    f"{PLAN_NAMES[label]} - Rs {p['price']}",
                    callback_data=f"{prefix}:{group_id}:{label}",
                )
            ]
        )
    return InlineKeyboardMarkup(rows)


async def safe_answer(query, text: str | None = None, alert: bool = False):
    try:
        await query.answer(text, show_alert=alert)
    except Exception:
        pass


# --------------------------------------------------------------------------
# Health server (Render Web Service ko port chahiye hota hai)
# --------------------------------------------------------------------------
class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")

    def do_HEAD(self):  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()

    def log_message(self, *args):  # silence
        pass


def run_health_server():
    port = int(os.getenv("PORT", "10000"))
    try:
        ThreadingHTTPServer(("0.0.0.0", port), _HealthHandler).serve_forever()
    except Exception:
        logger.exception("health server failed")


# --------------------------------------------------------------------------
# MEMBER FLOW: /start -> plan -> screenshot -> UTR -> admin approve
# --------------------------------------------------------------------------
async def show_plans(update: Update, context: ContextTypes.DEFAULT_TYPE, group: dict):
    plans = get_plans(group)
    target = update.callback_query.message if update.callback_query else update.message
    # Point 5: admin ka custom welcome message plan wale message se pehle DM me dikhao.
    user = update.effective_user
    welcome = render_custom_welcome(group, user.first_name if user else None)
    if welcome:
        try:
            await target.reply_text(welcome)
        except Exception:
            pass
    text = (
        f"Welcome to *{group['title']}*\n\n"
        "Apna plan select karo. Payment ke baad admin approve karega "
        "aur tumhe private join link mil jayega (link sirf tumhare liye hoga)."
    )
    markup = plan_buttons(group["id"], plans)
    await target.reply_text(text, reply_markup=markup, parse_mode="Markdown")
    return WAIT_PLAN


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    group = None
    if args and args[0].startswith("g") and args[0][1:].isdigit():
        group = get_group_by_id(int(args[0][1:]))
    if group:
        if not group_is_active(group):
            await update.message.reply_text("Ye group filhaal bot par active nahi hai. Admin se rabta karo.")
            return ConversationHandler.END
        owner_access, _, _ = admin_account_access(group["owner_user_id"], create=False)
        if not owner_access:
            await update.message.reply_text(
                f"{group['title']} ka subscription process filhaal paused hai. Group owner se rabta karo."
            )
            return ConversationHandler.END
        group = await sync_group_title(context.bot, group)
        # Point 6: pehle se active paid member se dobara payment mat mango.
        member = get_member(group["id"], update.effective_user.id)
        if member and member.get("status") == "ACTIVE":
            expiry = parse_dt(member.get("expiry_date"))
            if expiry and expiry > now_utc():
                if await member_presence(context.bot, group["chat_id"], update.effective_user.id):
                    await update.message.reply_text(
                        f"Tum {group['title']} ke pehle se active member ho.\n"
                        f"Membership valid: {fmt_ist(expiry)}\n\n"
                        "Dobara payment karne ki zaroorat nahi hai."
                    )
                    return ConversationHandler.END
                await issue_join_link(
                    context.bot,
                    group,
                    update.effective_user.id,
                    f"Tumhari {group['title']} membership abhi active hai, lekin tum group se bahar ho. Ye raha naya join link:",
                )
                return ConversationHandler.END
        context.user_data["join_group_id"] = group["id"]
        return await show_plans(update, context, group)

    # Owners who open the bot directly get a live-checked group list.
    if get_owned_groups(update.effective_user.id):
        return await show_group_list(
            update.message, update.effective_user.id, context.bot
        )

    # Members who open the bot without a group join link get a simple greeting.
    await update.message.reply_text("Welcome")
    return ConversationHandler.END


async def renew_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reminder message ke Renew button se flow dobara shuru."""
    query = update.callback_query
    await safe_answer(query)
    group_id = int(query.data.split(":")[1])
    group = get_group_by_id(group_id)
    if not group_is_active(group):
        await query.message.reply_text("Ye group ab active nahi hai. Admin se rabta karo.")
        return ConversationHandler.END
    owner_access, _, _ = admin_account_access(group["owner_user_id"], create=False)
    if not owner_access:
        await query.message.reply_text("Group owner ka SaaS plan paused hai. Owner se rabta karo.")
        return ConversationHandler.END
    group = await sync_group_title(context.bot, group)
    context.user_data["join_group_id"] = group_id
    return await show_plans(update, context, group)


async def plan_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await safe_answer(query)
    _, group_id_s, label = query.data.split(":")
    group_id = int(group_id_s)
    group = get_group_by_id(group_id)
    if not group_is_active(group):
        await query.message.reply_text("Group nahi mila. Admin se rabta karo.")
        return ConversationHandler.END
    owner_access, _, _ = admin_account_access(group["owner_user_id"], create=False)
    if not owner_access:
        await query.message.reply_text("Group owner ka SaaS plan paused hai. Owner se rabta karo.")
        return ConversationHandler.END

    plans = get_plans(group)
    plan = plans[label]
    context.user_data.update(
        {"pay_group_id": group_id, "pay_plan": label, "pay_amount": plan["price"]}
    )
    upsert_lead(group_id, query.from_user, plan_label=label, stage="PLAN_VIEWED")

    if not group.get("upi_id"):
        await query.message.reply_text(
            "Admin ne abhi payment setup complete nahi kiya. Thodi der baad try karo "
            "ya admin ko message karo."
        )
        return ConversationHandler.END

    caption = (
        f"Plan: *{PLAN_NAMES[label]}* - *Rs {plan['price']}*\n\n"
        f"Is UPI par payment karo:\n`{group['upi_id']}`\n\n"
        "Payment ke baad pehle *screenshot* bhejo, phir *12-digit UTR number*.\n"
        "Note: Ek UTR sirf ek baar use ho sakta hai."
    )
    if group.get("upi_qr_file_id"):
        await context.bot.send_photo(
            chat_id=query.from_user.id,
            photo=group["upi_qr_file_id"],
            caption=caption,
            parse_mode="Markdown",
        )
    else:
        await query.message.reply_text(caption, parse_mode="Markdown")
    await query.message.reply_text("Ab payment ka screenshot bhejo:")
    return WAIT_SCREENSHOT


async def screenshot_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["screenshot_file_id"] = update.message.photo[-1].file_id
    await update.message.reply_text("Screenshot mil gaya. Ab 12-digit UTR number likh kar bhejo:")
    return WAIT_UTR


async def utr_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    utr = (update.message.text or "").strip()
    if not UTR_RE.match(utr):
        await update.message.reply_text(
            "Ye valid UTR nahi lag raha. UTR exactly 12 digits ka hota hai, dobara bhejo:"
        )
        return WAIT_UTR

    group_id = context.user_data.get("pay_group_id")
    label = context.user_data.get("pay_plan")
    amount = context.user_data.get("pay_amount")
    group = get_group_by_id(group_id) if group_id else None
    if not group_is_active(group):
        await update.message.reply_text("Session expire ho gaya. Join link se dobara /start karo.")
        return ConversationHandler.END
    owner_access, _, _ = admin_account_access(group["owner_user_id"], create=False)
    if not owner_access:
        await update.message.reply_text("Group owner ka SaaS plan paused hai. Owner se rabta karo.")
        return ConversationHandler.END

    # Anti-fraud: UTR pehle use ho chuka hai?
    try:
        dup = (
            sb.table("payments").select("id,status").eq("utr_number", utr).maybe_single().execute()
        )
        if dup and dup.data:
            await update.message.reply_text(
                "Ye UTR number pehle hi use ho chuka hai. Agar ye tumhari payment hai "
                "to admin se rabta karo. Nayi payment ka naya UTR bhejo."
            )
            return WAIT_UTR
    except Exception:
        logger.exception("UTR duplicate check failed")

    payment = {
        "group_id": group_id,
        "user_id": user.id,
        "username": user.username,
        "full_name": user.full_name,
        "plan_label": label,
        "amount": amount,
        "utr_number": utr,
        "screenshot_file_id": context.user_data.get("screenshot_file_id"),
        "status": "PENDING",
    }
    try:
        res = sb.table("payments").insert(payment).execute()
        payment_id = res.data[0]["id"]
    except Exception:
        logger.exception("payment insert failed")
        await update.message.reply_text("Kuch masla ho gaya, thodi der baad dobara try karo.")
        return ConversationHandler.END

    upsert_lead(group_id, user, plan_label=label, stage="PAYMENT_SUBMITTED")

    buttons = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Approve", callback_data=f"payapp:{payment_id}"),
                InlineKeyboardButton("❌ Reject", callback_data=f"payrej:{payment_id}"),
            ]
        ]
    )
    caption = (
        f"New payment - {group['title']}\n"
        f"Member: {user.full_name} (@{user.username or '-'})\n"
        f"Plan: {PLAN_NAMES.get(label, label)} - Rs {amount}\n"
        f"UTR: {utr}\n"
        "Screenshot check karke Approve/Reject karo."
    )
    await notify_group_admins(
        context,
        group,
        photo=context.user_data.get("screenshot_file_id"),
        caption=caption,
        reply_markup=buttons,
    )

    await update.message.reply_text(
        "Payment admin ko bhej di gayi hai. Approve hote hi tumhe private join link "
        "yahi mil jayega. Aam tor par kuch ghanto me ho jata hai."
    )
    return ConversationHandler.END


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Theek hai, cancel kar diya. Dobara /start kar sakte ho.")
    return ConversationHandler.END


# --------------------------------------------------------------------------
# ADMIN DECISION: Approve / Reject
# --------------------------------------------------------------------------
async def pay_decision(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    action, payment_id_s = query.data.split(":")
    payment_id = int(payment_id_s)

    try:
        res = sb.table("payments").select("*").eq("id", payment_id).maybe_single().execute()
        payment = res.data if res else None
    except Exception:
        payment = None
    if not payment:
        await safe_answer(query, "Payment record nahi mila.", alert=True)
        return

    group = get_group_by_id(payment["group_id"])
    if not group or (
        group["owner_user_id"] != query.from_user.id
        and query.from_user.id not in await get_admin_ids(context, group["chat_id"])
    ):
        await safe_answer(query, "Sirf group admin hi approve/reject kar sakta hai.", alert=True)
        return
    if payment["status"] != "PENDING":
        await safe_answer(query, f"Ye payment pehle hi {payment['status']} hai.", alert=True)
        return

    member_id = payment["user_id"]
    if action == "payrej":
        try:
            sb.table("payments").update(
                {"status": "REJECTED", "reviewed_by": query.from_user.id, "reviewed_at": now_utc().isoformat()}
            ).eq("id", payment_id).eq("status", "PENDING").execute()
        except Exception:
            logger.exception("payment rejection update failed")
            await safe_answer(query, "Database error. Dobara try karo.", alert=True)
            return
        await safe_answer(query, "Reject kar diya.")
        try:
            await query.edit_message_caption(
                caption=(query.message.caption or "") + "\n\nStatus: REJECTED"
            )
        except Exception:
            pass
        try:
            await context.bot.send_message(
                chat_id=member_id,
                text=(
                    f"Tumhari payment ({group['title']}) approve nahi ho saki. "
                    "UTR/screenshot check karke dobara try karo ya admin se rabta karo."
                ),
            )
        except Exception:
            pass
        return

    # APPROVE
    group = await sync_group_title(context.bot, group)
    plans = get_plans(group)
    days = plans.get(payment["plan_label"], DEFAULT_PLANS["1m"])["days"]
    existing = get_member(group["id"], member_id)
    base = now_utc()
    if existing and existing.get("status") == "ACTIVE":
        old_expiry = parse_dt(existing.get("expiry_date"))
        if old_expiry and old_expiry > base:
            base = old_expiry  # renewal: purani expiry se aage badhao
    expiry = base + timedelta(days=days)

    try:
        claimed = (
            sb.table("payments")
            .update(
                {"status": "APPROVED", "reviewed_by": query.from_user.id, "reviewed_at": now_utc().isoformat()}
            )
            .eq("id", payment_id)
            .eq("status", "PENDING")
            .select("id")
            .execute()
        )
        if not claimed.data:
            await safe_answer(query, "Ye payment kisi aur admin ne abhi review kar di.", alert=True)
            return
        sb.table("members").upsert(
            {
                "group_id": group["id"],
                "user_id": member_id,
                "username": payment.get("username"),
                "full_name": payment.get("full_name"),
                "plan_label": payment["plan_label"],
                "expiry_date": expiry.isoformat(),
                "status": "ACTIVE",
                "reminder_2d_sent": False,
                "reminder_1d_sent": False,
            },
            on_conflict="group_id,user_id",
        ).execute()
    except Exception:
        logger.exception("payment approval persistence failed")
        try:
            sb.table("payments").update(
                {"status": "PENDING", "reviewed_by": None, "reviewed_at": None}
            ).eq("id", payment_id).eq("status", "APPROVED").execute()
        except Exception:
            logger.exception("failed to restore payment after approval error")
        await safe_answer(query, "Database error; payment approval nahi hui.", alert=True)
        return

    try:
        sb.table("pending_leads").update(
            {"stage": "CONVERTED", "updated_at": now_utc().isoformat()}
        ).eq("group_id", group["id"]).eq("user_id", member_id).execute()
    except Exception:
        pass

    # Member group ke andar pehle se hai ya nahi?
    already_in_group = await member_presence(context.bot, group["chat_id"], member_id)
    if already_in_group and member_chat_enabled(group):
        # Chat mode ON hai to gate hata do; read-only mode me group defaults hi kaafi hain.
        await unrestrict_if_joined(context, group["chat_id"], member_id)

    link_text = ""
    if not already_in_group:
        # Point 4: pehle unban (warna naya link 'expired' dikhta hai), phir ID-locked link + DM.
        link_text = (
            await issue_join_link(
                context.bot,
                group,
                member_id,
                f"Payment approve ho gayi! {group['title']} me tumhara swagat hai.",
            )
            or ""
        )

    await safe_answer(query, "Approve ho gaya, member ko link bhej diya.")
    try:
        await query.edit_message_caption(
            caption=(query.message.caption or "") + f"\n\nStatus: APPROVED\nExpiry: {fmt_ist(expiry)}"
        )
    except Exception:
        pass

    if already_in_group:
        note = (
            "Tum group me pehle se ho; ab restrictions hata di gayi hain."
            if member_chat_enabled(group)
            else "Tum group me pehle se ho. Ye group read-only mode me hai, isme sirf admins post kar sakte hain."
        )
        try:
            await context.bot.send_message(
                chat_id=member_id,
                text=(
                    f"Payment approve ho gayi! {group['title']} me tumhara swagat hai.\n"
                    f"Membership expiry: {fmt_ist(expiry)}\n\n{note}"
                ),
            )
        except Exception:
            logger.exception("member approve DM failed")
    elif not link_text:
        try:
            await context.bot.send_message(
                chat_id=member_id,
                text="Payment approve ho gayi, lekin join link banane me masla aaya hai. Admin se rabta karo.",
            )
        except Exception:
            logger.exception("member approve DM failed")


async def notify_group_admins(context, group, *, photo, caption, reply_markup):
    """Send private payment review buttons to all group admins who started the bot."""
    admin_ids = await get_admin_ids(context, group["chat_id"])
    reviewer_ids = admin_ids | {group["owner_user_id"]}
    sent = 0
    for admin_id in reviewer_ids:
        try:
            await context.bot.send_photo(
                chat_id=admin_id,
                photo=photo,
                caption=caption,
                reply_markup=reply_markup,
            )
            sent += 1
        except Exception:
            logger.info("Unable to DM payment review to admin %s", admin_id)
    if not sent:
        logger.warning("No group admin could be reached for payment review (%s)", group["id"])


async def unrestrict_if_joined(context, chat_id: int, user_id: int) -> bool:
    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
        if member.status == ChatMemberStatus.RESTRICTED and getattr(member, "is_member", False):
            await context.bot.restrict_chat_member(
                chat_id=chat_id,
                user_id=user_id,
                permissions=ChatPermissions.all_permissions(),
                use_independent_chat_permissions=True,
            )
            return True
        return member.status in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)
    except Exception:
        logger.info("Member is not currently present/unrestrictable in chat %s", chat_id)
        return False


async def billing_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query:
        await safe_answer(query)
        reply = query.message.reply_text
    else:
        reply = update.message.reply_text
    if not PLATFORM_UPI_ID or not SUPER_ADMIN_IDS:
        await reply(
            "Platform SaaS billing abhi configure nahi hui. Platform admin ko "
            "SUPER_ADMIN_IDS aur PLATFORM_UPI_ID set karne honge."
        )
        return ConversationHandler.END

    account = get_admin_account(update.effective_user.id, create=False)
    if not account:
        await reply("Billing database ready nahi hai. Platform admin se rabta karo.")
        return ConversationHandler.END
    context.user_data["saas_amount"] = SAAS_MONTHLY_PRICE
    text = (
        f"SecuredGroup SaaS plan: Rs {SAAS_MONTHLY_PRICE} / {SAAS_BILLING_DAYS} din\n\n"
        f"Platform UPI par payment karo: {PLATFORM_UPI_ID}\n\n"
        "Payment ke baad screenshot bhejo, phir 12-digit UTR submit karo. "
        "Super-admin verify karke dashboard unlock karega."
    )
    if PLATFORM_QR_FILE_ID:
        await context.bot.send_photo(
            chat_id=update.effective_user.id,
            photo=PLATFORM_QR_FILE_ID,
            caption=text,
        )
    else:
        await reply(text)
    await context.bot.send_message(chat_id=update.effective_user.id, text="Ab payment screenshot bhejo:")
    return BILLING_SCREENSHOT


async def billing_screenshot_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["saas_screenshot_file_id"] = update.message.photo[-1].file_id
    await update.message.reply_text("Screenshot mil gaya. Ab 12-digit UTR number bhejo:")
    return BILLING_UTR


async def billing_utr_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    utr = (update.message.text or "").strip()
    if not UTR_RE.fullmatch(utr):
        await update.message.reply_text("UTR exactly 12 digits ka hona chahiye. Dobara bhejo:")
        return BILLING_UTR
    user = update.effective_user
    try:
        duplicate = (
            sb.table("saas_payments")
            .select("id")
            .eq("utr_number", utr)
            .maybe_single()
            .execute()
        )
        if duplicate.data:
            await update.message.reply_text("Ye UTR pehle submit ho chuka hai. Naya UTR bhejo:")
            return BILLING_UTR
        res = sb.table("saas_payments").insert(
            {
                "user_id": user.id,
                "amount": context.user_data.get("saas_amount", SAAS_MONTHLY_PRICE),
                "billing_days": SAAS_BILLING_DAYS,
                "utr_number": utr,
                "screenshot_file_id": context.user_data.get("saas_screenshot_file_id"),
                "status": "PENDING",
            }
        ).execute()
        payment_id = res.data[0]["id"]
    except Exception:
        logger.exception("SaaS payment insert failed")
        await update.message.reply_text("Payment request save nahi hui. Thodi der baad dobara try karo.")
        return ConversationHandler.END

    buttons = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ Approve", callback_data=f"saasapp:{payment_id}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"saasrej:{payment_id}"),
        ]]
    )
    caption = (
        f"SaaS subscription payment\nAdmin: {user.full_name} (@{user.username or '-'})\n"
        f"User ID: {user.id}\nAmount: Rs {SAAS_MONTHLY_PRICE}\nUTR: {utr}"
    )
    sent = 0
    for super_admin_id in SUPER_ADMIN_IDS:
        try:
            await context.bot.send_photo(
                chat_id=super_admin_id,
                photo=context.user_data.get("saas_screenshot_file_id"),
                caption=caption,
                reply_markup=buttons,
            )
            sent += 1
        except Exception:
            logger.info("Unable to send SaaS payment to super-admin %s", super_admin_id)
    await update.message.reply_text(
        "SaaS payment request super-admin ko bhej di gayi hai. Approval ke baad dashboard unlock hoga."
        if sent
        else "Payment save ho gayi, lekin super-admin DM deliver nahi hua. /superadmin ke pending queue se review ho sakti hai."
    )
    return ConversationHandler.END


async def superadmin_pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in SUPER_ADMIN_IDS:
        await update.message.reply_text("Ye command sirf platform super-admin ke liye hai.")
        return
    try:
        rows = (
            sb.table("saas_payments")
            .select("id,user_id,amount,utr_number,created_at")
            .eq("status", "PENDING")
            .order("created_at")
            .limit(20)
            .execute()
            .data
            or []
        )
    except Exception:
        logger.exception("SaaS pending queue failed")
        await update.message.reply_text("SaaS payment queue read nahi hui; migration check karo.")
        return
    if not rows:
        await update.message.reply_text("Koi SaaS payment pending nahi hai.")
        return
    for row in rows:
        caption = f"SaaS payment | Admin ID {row['user_id']} | Rs {row['amount']} | UTR {row['utr_number']}"
        buttons = InlineKeyboardMarkup(
            [[
                InlineKeyboardButton("✅ Approve", callback_data=f"saasapp:{row['id']}"),
                InlineKeyboardButton("❌ Reject", callback_data=f"saasrej:{row['id']}"),
            ]]
        )
        try:
            await context.bot.send_photo(
                chat_id=update.effective_chat.id,
                photo=(
                    sb.table("saas_payments")
                    .select("screenshot_file_id")
                    .eq("id", row["id"])
                    .single()
                    .execute()
                    .data["screenshot_file_id"]
                ),
                caption=caption,
                reply_markup=buttons,
            )
        except Exception:
            logger.exception("SaaS pending payment display failed")


async def saas_payment_decision(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    action, payment_id_s = query.data.split(":")
    if query.from_user.id not in SUPER_ADMIN_IDS:
        await safe_answer(query, "Sirf platform super-admin review kar sakta hai.", alert=True)
        return
    try:
        payment = (
            sb.table("saas_payments")
            .select("*")
            .eq("id", int(payment_id_s))
            .maybe_single()
            .execute()
            .data
        )
    except Exception:
        payment = None
    if not payment or payment.get("status") != "PENDING":
        await safe_answer(query, "Payment nahi mili ya pehle review ho chuki hai.", alert=True)
        return
    if action == "saasrej":
        try:
            sb.table("saas_payments").update(
                {"status": "REJECTED", "reviewed_by": query.from_user.id, "reviewed_at": now_utc().isoformat()}
            ).eq("id", payment["id"]).eq("status", "PENDING").execute()
        except Exception:
            logger.exception("SaaS payment rejection failed")
            await safe_answer(query, "Database error.", alert=True)
            return
        await safe_answer(query, "Payment reject kar di.")
        await context.bot.send_message(
            chat_id=payment["user_id"],
            text="SaaS payment approve nahi hui. UTR/screenshot verify karke platform admin se rabta karo.",
        )
        try:
            await query.edit_message_caption(caption=(query.message.caption or "") + "\n\nStatus: REJECTED")
        except Exception:
            pass
        return

    account = get_admin_account(payment["user_id"])
    if account is None:
        await safe_answer(query, "Admin billing account nahi mila.", alert=True)
        return
    now = now_utc()
    old_until = parse_dt(account.get("paid_until"))
    start = old_until if old_until and old_until > now else now
    paid_until = start + timedelta(days=int(payment.get("billing_days") or SAAS_BILLING_DAYS))
    try:
        claimed = (
            sb.table("saas_payments")
            .update(
                {"status": "APPROVED", "reviewed_by": query.from_user.id, "reviewed_at": now.isoformat()}
            )
            .eq("id", payment["id"])
            .eq("status", "PENDING")
            .select("id")
            .execute()
        )
        if not claimed.data:
            await safe_answer(query, "Ye payment pehle review ho chuki hai.", alert=True)
            return
        sb.table("admin_accounts").update(
            {
                "subscription_status": "ACTIVE",
                "paid_until": paid_until.isoformat(),
                "updated_at": now.isoformat(),
            },
        ).eq("user_id", payment["user_id"]).execute()
    except Exception:
        logger.exception("SaaS payment approval persistence failed")
        try:
            sb.table("saas_payments").update(
                {"status": "PENDING", "reviewed_by": None, "reviewed_at": None}
            ).eq("id", payment["id"]).eq("status", "APPROVED").execute()
        except Exception:
            logger.exception("SaaS payment rollback failed")
        await safe_answer(query, "Database error; subscription unlock nahi hui.", alert=True)
        return
    await safe_answer(query, "Admin SaaS plan approve ho gaya.")
    try:
        await query.edit_message_caption(caption=(query.message.caption or "") + f"\n\nStatus: APPROVED\nUntil: {fmt_ist(paid_until)}")
    except Exception:
        pass
    try:
        await context.bot.send_message(
            chat_id=payment["user_id"],
            text=f"SaaS payment approve ho gayi. Dashboard {fmt_ist(paid_until)} tak unlock hai. /dashboard kholo.",
        )
    except Exception:
        logger.exception("SaaS approval DM failed")


async def join_request_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Join request sirf approved (payment wale) member ki hi approve hogi."""
    req = update.chat_join_request
    group = get_group_by_chat(req.chat.id)
    if not group:
        return  # Is bot ke liye register na kiye gaye groups ko touch na karein.
    group = await sync_group_title(context.bot, group)
    if not group_is_active(group):
        try:
            await context.bot.decline_chat_join_request(req.chat.id, req.from_user.id)
            await context.bot.send_message(
                chat_id=req.from_user.id,
                text=f"{group['title']} ka access process filhaal available nahi hai. Group owner se rabta karo.",
            )
        except Exception:
            logger.exception("inactive group join request handling failed")
        return
    if not group.get("gatekeeper_enabled", True):
        try:
            await context.bot.approve_chat_join_request(req.chat.id, req.from_user.id)
        except Exception:
            logger.exception("unmanaged join request approval failed")
        return
    owner_access, _, _ = admin_account_access(group["owner_user_id"], create=False)
    approved = owner_access and member_is_active(group["id"], req.from_user.id)
    try:
        if approved:
            await context.bot.approve_chat_join_request(req.chat.id, req.from_user.id)
            # Point 4+5: join-link wale DM ko 'safaltapurvak jud gaye' me badlo
            # (taaki user dobara link na dabaye) + welcome aur validity DM me do.
            member = get_member(group["id"], req.from_user.id)
            expiry = parse_dt(member.get("expiry_date")) if member else None
            joined_text = f"✅ Tum safaltapurvak {group['title']} me jud chuke ho!"
            welcome = render_custom_welcome(group, req.from_user.first_name)
            if welcome:
                joined_text += f"\n\n{welcome}"
            if expiry:
                joined_text += f"\n\nMembership valid: {fmt_ist(expiry)}"
            edited = False
            invite_mid = (member or {}).get("invite_message_id")
            if invite_mid:
                try:
                    await context.bot.edit_message_text(
                        chat_id=req.from_user.id, message_id=invite_mid, text=joined_text
                    )
                    edited = True
                except Exception:
                    edited = False
                try:
                    sb.table("members").update({"invite_message_id": None}).eq(
                        "group_id", group["id"]
                    ).eq("user_id", req.from_user.id).execute()
                except Exception:
                    pass
            if not edited:
                try:
                    await context.bot.send_message(chat_id=req.from_user.id, text=joined_text)
                except Exception:
                    pass
        else:
            await context.bot.decline_chat_join_request(req.chat.id, req.from_user.id)
            await context.bot.send_message(
                chat_id=req.from_user.id,
                text=(
                    f"{group['title']} me access ke liye pehle paid plan select aur verify karo. "
                    f"Process yahan se shuru karo: https://t.me/{context.bot.username}?start=g{group['id']}"
                ),
            )
    except Exception:
        logger.exception("join request handling failed")


# --------------------------------------------------------------------------
# GROUP REGISTRATION: group ke andar /setup
# --------------------------------------------------------------------------
async def setup_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    if chat.type not in ("group", "supergroup"):
        await update.message.reply_text("/setup group ke andar chalana hai, jahan bot admin ho.")
        return
    if chat.type != "supergroup":
        await update.message.reply_text(
            "Per-member mute aur paid join approvals ke liye Telegram Supergroup chahiye. "
            "Group ko Supergroup me upgrade karke /setup dobara chalao."
        )
        return

    # Point 2 (Clean Group): /setup wala message group se turant delete karo aur
    # saari jaankari (registration + trial) admin ke personal DM me bhejo,
    # taaki members ko group me kabhi setup/trial ka koi message na dikhe.
    try:
        await update.message.delete()
    except Exception:
        pass

    async def reply_admin(text: str):
        try:
            await context.bot.send_message(chat_id=user.id, text=text)
            return
        except Exception:
            pass  # admin ne bot DM start nahi kiya — group me fallback (auto-delete ke saath)
        try:
            sent = await context.bot.send_message(chat_id=chat.id, text=text)
            schedule_message_delete(context, chat.id, sent.message_id, delay=120)
        except Exception:
            pass

    try:
        member = await context.bot.get_chat_member(chat.id, user.id)
        if member.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
            await reply_admin("Sirf group admin hi /setup kar sakta hai.")
            return
        bot_member = await context.bot.get_chat_member(chat.id, context.bot.id)
        if bot_member.status != ChatMemberStatus.ADMINISTRATOR:
            await reply_admin(
                "Pehle bot ko group admin banao. Gatekeeper ke liye Restrict members, "
                "Delete messages aur Invite users permissions zaroori hain."
            )
            return
        if not getattr(bot_member, "can_restrict_members", False):
            await reply_admin(
                "Bot admin hai, lekin Restrict members permission nahi hai. "
                "Is permission ko enable karke /setup dobara chalao."
            )
            return
        if not getattr(bot_member, "can_delete_messages", False):
            await reply_admin(
                "Bot ko Delete messages permission bhi do, taaki spam aur service messages hata sake."
            )
            return
        if not getattr(bot_member, "can_invite_users", False):
            await reply_admin(
                "Bot ko Invite users permission bhi do, taaki paid join requests approve ho saken."
            )
            return
    except Exception:
        await reply_admin("Admin status check nahi ho saka. Bot ko group admin banao pehle.")
        return

    existing = get_group_by_chat(chat.id)
    if existing and group_is_active(existing):
        if chat.title and chat.title != existing.get("title"):
            try:
                sb.table("groups").update({"title": chat.title}).eq("id", existing["id"]).execute()
                existing["title"] = chat.title
            except Exception:
                pass
        await apply_member_chat_mode(context.bot, existing)
        await reply_admin(
            f"Ye group pehle se registered hai: {existing['title']}\n"
            f"Members ke liye join link:\nhttps://t.me/{context.bot.username}?start=g{existing['id']}\n\n"
            "Settings ke liye bot ko DM me /admin likho."
        )
        return
    if existing and existing.get("owner_user_id") != user.id:
        await reply_admin(
            "Ye group kisi aur admin account se register hua tha. Purane registered owner "
            "ko /setup chalane ko kaho; ownership transfer ke liye platform support se rabta karo."
        )
        return

    has_access, _, trial_until = admin_account_access(user.id)
    if not has_access:
        await reply_admin(
            "Tumhara SaaS trial khatm ho gaya hai. Naya group register karne ke liye "
            f"bot DM me /billing kholo (trial end: {fmt_ist(trial_until)})."
        )
        return

    try:
        if existing:
            res = (
                sb.table("groups")
                .update(
                    {
                        "title": chat.title or existing.get("title") or "",
                        "is_active": True,
                    }
                )
                .eq("id", existing["id"])
                .execute()
            )
            gid = existing["id"]
        else:
            res = sb.table("groups").insert(
                {
                    "chat_id": chat.id,
                    "title": chat.title or "",
                    "owner_user_id": user.id,
                    "gatekeeper_enabled": True,
                    "anti_spam_enabled": True,
                    "flood_control_enabled": True,
                    "member_chat_enabled": False,
                    "is_active": True,
                }
            ).execute()
            gid = res.data[0]["id"]
    except Exception:
        logger.exception("group insert failed")
        await reply_admin("Registration me masla aaya, dobara try karo.")
        return

    account = get_admin_account(user.id)
    if not account:
        logger.error("Admin trial could not be initialized for user %s", user.id)

    # Point 1: registration ke saath hi read-only (broadcast) mode default apply karo.
    fresh_group = get_group_by_chat(chat.id)
    if fresh_group:
        await apply_member_chat_mode(context.bot, fresh_group)

    await reply_admin(
        f"{'Purana group wapas active ho gaya' if existing else 'Group registered ho gaya'}: {chat.title}\n\n"
        f"Members ke liye join link:\nhttps://t.me/{context.bot.username}?start=g{gid}\n\n"
        "Group abhi READ-ONLY mode me hai: members ko message box nahi dikhega, sirf Mute/Unmute button rahega. "
        "Members ki chat kholni ho to dashboard me 'Member Chat' toggle ON kar do.\n\n"
        "Ab bot ko DM me /dashboard likh kar UPI ID/QR, plans, welcome, FAQ aur moderation set karo. "
        f"{'Purani group settings aur member history preserve hain.' if existing else 'Tumhara 7-day free trial ab shuru ho gaya hai.'}"
    )


# --------------------------------------------------------------------------
# ADMIN DASHBOARD: /admin (bot DM)
# --------------------------------------------------------------------------
def dashboard_markup(group: dict) -> InlineKeyboardMarkup:
    group_id = group["id"]
    links = bool(group.get("anti_spam_enabled", True))
    flood = bool(group.get("flood_control_enabled", True))
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Set UPI ID", callback_data=f"adm:upi:{group_id}"),
                InlineKeyboardButton("Set QR Photo", callback_data=f"adm:qr:{group_id}"),
            ],
            [
                InlineKeyboardButton("Set Plans", callback_data=f"adm:plans:{group_id}"),
                InlineKeyboardButton("Join Link", callback_data=f"adm:link:{group_id}"),
            ],
            [
                InlineKeyboardButton("Active Members", callback_data=f"adm:members:{group_id}"),
                InlineKeyboardButton("Revenue Stats", callback_data=f"adm:rev:{group_id}"),
            ],
            [InlineKeyboardButton("Pending Approvals", callback_data=f"adm:pending:{group_id}")],
            [
                InlineKeyboardButton(
                    f"Gatekeeper: {'ON' if group.get('gatekeeper_enabled', True) else 'OFF'}",
                    callback_data=f"adm:gatekeeper:{group_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    f"Member Chat: {'ON' if group.get('member_chat_enabled', False) else 'OFF (Read-Only)'}",
                    callback_data=f"adm:memberchat:{group_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    f"Link Blocker: {'ON' if links else 'OFF'}",
                    callback_data=f"adm:linkblock:{group_id}",
                ),
                InlineKeyboardButton(
                    f"Flood Control: {'ON' if flood else 'OFF'}",
                    callback_data=f"adm:flood:{group_id}",
                ),
            ],
            [
                InlineKeyboardButton("Set Welcome Message", callback_data=f"adm:welcome:{group_id}"),
                InlineKeyboardButton("Auto-Reply FAQ", callback_data=f"adm:faq:{group_id}"),
            ],
            [InlineKeyboardButton("Broadcast Message", callback_data=f"adm:bcast:{group_id}")],
            [InlineKeyboardButton("SaaS Billing", callback_data=f"adm:billing:{group_id}")],
            [InlineKeyboardButton("<< Mere Groups", callback_data="adm:back")],
        ]
    )


async def show_group_list(message_or_query, user_id: int, bot, groups=None):
    if groups is None:
        groups = await get_live_owned_groups(user_id, bot)
    for g in groups:
        await sync_group_title(bot, g)
    if not groups:
        text = (
            "Tumhara koi active group registered nahi hai.\n\n"
            "Steps:\n1) Bot ko Supergroup me add karke admin banao (Restrict members, Delete messages, Invite users).\n"
            "2) Group ke andar /setup likho.\n3) Phir yahan /dashboard se settings karo."
        )
        if hasattr(message_or_query, "reply_text"):
            await message_or_query.reply_text(text)
        else:
            await message_or_query.message.reply_text(text)
        return ConversationHandler.END
    buttons = [
        [InlineKeyboardButton(g["title"] or f"Group {g['id']}", callback_data=f"adm:grp:{g['id']}")]
        for g in groups
    ]
    text = "Apna active group select karo. Live check ke liye /cleanup likho:"
    markup = InlineKeyboardMarkup(buttons)
    if hasattr(message_or_query, "data"):  # CallbackQuery
        await message_or_query.edit_message_text(text, reply_markup=markup)
    else:
        await message_or_query.reply_text(text, reply_markup=markup)
    return A_NAV


async def admin_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    owned_groups = await get_live_owned_groups(
        update.effective_user.id, context.bot
    )
    if not owned_groups:
        return await show_group_list(
            update.message, update.effective_user.id, context.bot, groups=[]
        )
    access, status, until = admin_account_access(update.effective_user.id)
    if not access:
        account = get_admin_account(update.effective_user.id)
        if account is None:
            await update.message.reply_text(
                "Admin billing database setup nahi hua. Platform admin se rabta karo."
            )
            return ConversationHandler.END
        await update.message.reply_text(
            f"Tumhara 7-day trial khatm ho gaya hai. Dashboard unlock karne ke liye "
            f"Rs {SAAS_MONTHLY_PRICE}/30 din ka SaaS plan lo.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("💳 SaaS Plan / Billing", callback_data="billing:open")]]
            ),
        )
        return ConversationHandler.END
    return await show_group_list(
        update.message, update.effective_user.id, context.bot, groups=owned_groups
    )


async def show_dashboard(query, group: dict, bot=None):
    if bot is not None:
        group = await sync_group_title(bot, group)
    plans = get_plans(group)
    plan_lines = "\n".join(
        f"  {PLAN_NAMES[l]}: Rs {plans[l]['price']} ({plans[l]['days']} din)" for l in ("1m", "3m", "1yr")
    )
    _, billing_status, billing_until = admin_account_access(query.from_user.id)
    billing_line = (
        f"SaaS: {billing_status} until {fmt_ist(billing_until)}"
        if billing_until
        else f"SaaS: {billing_status}"
    )
    if billing_status == "BILLING_SETUP_REQUIRED":
        billing_line = "SaaS billing setup pending; trial lock will start after configuration"
    text = (
        f"*{group['title']}*\n"
        f"{billing_line}\n"
        f"UPI ID: `{group.get('upi_id') or 'set nahi hai'}`\n"
        f"QR Photo: {'set hai' if group.get('upi_qr_file_id') else 'set nahi hai'}\n"
        f"Plans:\n{plan_lines}\n\n"
        f"Welcome message: {'custom' if group.get('welcome_message') else 'default'}\n"
        f"FAQ keywords: {len(group.get('faq_replies') or {})}\n"
        f"Gatekeeper: {'ON' if group.get('gatekeeper_enabled', True) else 'OFF'}\n"
        f"Member Chat: {'ON' if group.get('member_chat_enabled', False) else 'OFF (Read-Only — sirf admins post kar sakte hain)'}"
    )
    await query.edit_message_text(
        text, reply_markup=dashboard_markup(group), parse_mode="Markdown"
    )


async def admin_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await safe_answer(query)
    parts = query.data.split(":")
    action = parts[1]

    access, _, until = admin_account_access(query.from_user.id)
    if not access and action != "billing":
        await query.edit_message_text(
            f"Tumhara trial/subscription khatm ho gaya hai. Dashboard unlock karne ke liye "
            f"Rs {SAAS_MONTHLY_PRICE}/{SAAS_BILLING_DAYS} din ka plan lo.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("💳 SaaS Billing", callback_data="billing:open")]]
            ),
        )
        return ConversationHandler.END

    if action == "back":
        return await show_group_list(query, query.from_user.id, context.bot)

    group_id = int(parts[2])
    group = get_group_by_id(group_id)
    if not group or group["owner_user_id"] != query.from_user.id:
        await safe_answer(query, "Ye group tumhara nahi hai.", alert=True)
        return A_NAV
    live_state = await reconcile_group_access(context.bot, group)
    if live_state == "inactive" or (live_state == "error" and not group_is_active(group)):
        await query.edit_message_text(
            "Ye group ab active nahi hai. Bot ko group me admin bana kar /setup chalao."
        )
        return ConversationHandler.END
    context.user_data["adm_group_id"] = group_id

    if action == "billing":
        account = get_admin_account(query.from_user.id) or {}
        billing_until = parse_dt(account.get("paid_until")) or parse_dt(account.get("trial_ends_at"))
        details = f"Current status: {account.get('subscription_status', 'UNKNOWN')}"
        if billing_until:
            details += f"\nValid until: {fmt_ist(billing_until)}"
        await query.edit_message_text(
            f"SaaS Billing\n{details}\n\n"
            f"Plan: Rs {SAAS_MONTHLY_PRICE} / {SAAS_BILLING_DAYS} din",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("💳 Pay and Submit UTR", callback_data="billing:open")]]
            ),
        )
        return A_NAV

    if action == "grp":
        await show_dashboard(query, group, bot=context.bot)
        return A_NAV

    if action == "upi":
        await query.edit_message_text(
            f"Current UPI ID: {group.get('upi_id') or 'set nahi'}\n\nApna naya UPI ID likh kar bhejo "
            "(masalan name@okhdfcbank). Yahi ID members ko payment ke liye dikhegi."
        )
        return A_UPI

    if action == "qr":
        await query.edit_message_text("Apne UPI QR code ki photo bhejo. Yehi photo members ko payment ke waqt dikhegi.")
        return A_QR

    if action == "plans":
        plans = get_plans(group)
        buttons = [
            [
                InlineKeyboardButton(
                    f"{PLAN_NAMES[l]} - Rs {plans[l]['price']}",
                    callback_data=f"adm:plan:{group_id}:{l}",
                )
            ]
            for l in ("1m", "3m", "1yr")
        ]
        buttons.append([InlineKeyboardButton("<< Dashboard", callback_data=f"adm:grp:{group_id}")])
        await query.edit_message_text("Kis plan ki price badalni hai?", reply_markup=InlineKeyboardMarkup(buttons))
        return A_NAV

    if action == "plan":
        label = parts[3]
        context.user_data["adm_plan_label"] = label
        await query.edit_message_text(f"{PLAN_NAMES[label]} ki nayi price likho (sirf number, masalan 599):")
        return A_PLAN_PRICE

    if action == "link":
        link = f"https://t.me/{context.bot.username}?start=g{group_id}"
        await query.edit_message_text(
            f"Members ke liye join link:\n{link}\n\nYe link apne free channel/YouTube/Instagram par lagao.",
            reply_markup=dashboard_markup(group),
        )
        return A_NAV

    if action in ("gatekeeper", "linkblock", "flood"):
        field = {
            "gatekeeper": "gatekeeper_enabled",
            "linkblock": "anti_spam_enabled",
            "flood": "flood_control_enabled",
        }[action]
        try:
            new_value = not bool(group.get(field, True))
            sb.table("groups").update({field: new_value}).eq("id", group_id).execute()
            group[field] = new_value
        except Exception:
            logger.exception("moderation setting update failed")
            await query.edit_message_text(
                "Setting save nahi hui. Supabase migration apply hui hai ya nahi check karo.",
                reply_markup=dashboard_markup(group),
            )
            return A_NAV
        await show_dashboard(query, group, bot=context.bot)
        return A_NAV

    if action == "memberchat":
        # Point 1 toggle: DB flag flip + Telegram par default permissions turant apply.
        try:
            new_value = not member_chat_enabled(group)
            sb.table("groups").update({"member_chat_enabled": new_value}).eq("id", group_id).execute()
            group["member_chat_enabled"] = new_value
        except Exception:
            logger.exception("member chat toggle save failed")
            await query.edit_message_text(
                "Setting save nahi hui. Supabase migration 000002 apply hui hai ya nahi check karo.",
                reply_markup=dashboard_markup(group),
            )
            return A_NAV
        if not await apply_member_chat_mode(context.bot, group):
            await safe_answer(
                query,
                "Toggle save ho gaya, lekin Telegram permissions apply nahi hue. Bot ke admin rights check karke dobara try karo.",
                alert=True,
            )
        await show_dashboard(query, group, bot=context.bot)
        return A_NAV

    if action == "welcome":
        await query.edit_message_text(
            "Custom welcome message bhejo (max 500 characters).\n"
            "`{name}` aur `{group}` placeholders use kar sakte ho.\n"
            "Default welcome restore karne ke liye /clear bhejo."
        )
        return A_WELCOME

    if action == "faq":
        await query.edit_message_text(
            "Auto-reply entries ek-ek line me bhejo: `keyword = reply`\n"
            "Misal: `price = Plans ke liye /start kholo`\n"
            "Existing entries update hongi. FAQ saaf karne ke liye /clear bhejo."
        )
        return A_FAQ

    if action == "pending":
        try:
            rows = (
                sb.table("payments")
                .select("id,full_name,username,plan_label,amount,utr_number,created_at")
                .eq("group_id", group_id)
                .eq("status", "PENDING")
                .order("created_at", desc=True)
                .limit(10)
                .execute()
                .data
                or []
            )
        except Exception:
            logger.exception("pending payment list failed")
            rows = []
        if not rows:
            await query.edit_message_text(
                "Koi pending approval nahi hai.", reply_markup=dashboard_markup(group)
            )
            return A_NAV
        lines = ["Pending approvals (latest 10):"]
        buttons = []
        for row in rows:
            lines.append(
                f"\n{row.get('full_name') or '-'} (@{row.get('username') or '-'}) | "
                f"{PLAN_NAMES.get(row.get('plan_label'), row.get('plan_label'))} | "
                f"Rs {row.get('amount')} | UTR {row.get('utr_number')}"
            )
            buttons.append(
                [
                    InlineKeyboardButton("✅ Approve", callback_data=f"payapp:{row['id']}"),
                    InlineKeyboardButton("❌ Reject", callback_data=f"payrej:{row['id']}"),
                ]
            )
        buttons.append([InlineKeyboardButton("<< Dashboard", callback_data=f"adm:grp:{group_id}")])
        await query.edit_message_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(buttons))
        return A_NAV

    if action == "members":
        try:
            rows = (
                sb.table("members")
                .select("full_name,username,plan_label,expiry_date")
                .eq("group_id", group_id)
                .eq("status", "ACTIVE")
                .order("expiry_date")
                .limit(30)
                .execute()
                .data
                or []
            )
        except Exception:
            rows = []
        if not rows:
            text = "Abhi koi active member nahi hai."
        else:
            lines = [f"Active members ({len(rows)}):"]
            for r in rows:
                lines.append(
                    f"- {r.get('full_name') or ''} (@{r.get('username') or '-'}) | "
                    f"{PLAN_NAMES.get(r.get('plan_label'), r.get('plan_label'))} | "
                    f"expiry {fmt_ist(r.get('expiry_date'))}"
                )
            text = "\n".join(lines)
        await query.edit_message_text(text, reply_markup=dashboard_markup(group))
        return A_NAV

    if action == "rev":
        try:
            pays = (
                sb.table("payments")
                .select("amount,created_at,status")
                .eq("group_id", group_id)
                .execute()
                .data
                or []
            )
        except Exception:
            pays = []
        approved = [p for p in pays if p["status"] == "APPROVED"]
        pending = [p for p in pays if p["status"] == "PENDING"]
        total = sum(float(p["amount"]) for p in approved)
        month_start = now_utc().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        month_total = sum(
            float(p["amount"]) for p in approved if (parse_dt(p["created_at"]) or month_start) >= month_start
        )
        text = (
            f"Revenue - {group['title']}\n\n"
            f"Total approved: Rs {total:,.0f} ({len(approved)} payments)\n"
            f"Is mahine: Rs {month_total:,.0f}\n"
            f"Pending approval: {len(pending)}\n\n"
            "Note: Paisa seedha tumhare UPI me aata hai, bot sirf record rakhta hai."
        )
        await query.edit_message_text(text, reply_markup=dashboard_markup(group))
        return A_NAV

    if action == "bcast":
        await query.edit_message_text(
            "Broadcast message likh kar bhejo. Ye tumhare saare ACTIVE members ko bot DM me jayega."
        )
        return A_BCAST

    return A_NAV


async def admin_welcome_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    group_id = context.user_data.get("adm_group_id")
    text = (update.message.text or "").strip()
    value = None if text.lower() == "/clear" else text
    if value and len(value) > 500:
        await update.message.reply_text("Message 500 characters se chhota rakho:")
        return A_WELCOME
    try:
        sb.table("groups").update({"welcome_message": value}).eq("id", group_id).execute()
    except Exception:
        logger.exception("welcome message update failed")
        await update.message.reply_text("Save nahi hua. Supabase migration apply hui hai ya nahi check karo.")
        return ConversationHandler.END
    await update.message.reply_text("Welcome message save ho gaya. Dashboard ke liye /dashboard kholo.")
    return ConversationHandler.END


async def admin_faq_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    group_id = context.user_data.get("adm_group_id")
    text = (update.message.text or "").strip()
    current_group = get_group_by_id(group_id)
    faq = dict((current_group or {}).get("faq_replies") or {})
    if text.lower() == "/clear":
        faq = {}
    else:
        parsed = {}
        for line in text.splitlines():
            if "=" not in line:
                await update.message.reply_text("Har line `keyword = reply` format me likho:")
                return A_FAQ
            keyword, reply = (piece.strip() for piece in line.split("=", 1))
            if not keyword or not reply or len(keyword) > 64 or len(reply) > 500:
                await update.message.reply_text("Keyword 1–64 aur reply 1–500 characters ka hona chahiye:")
                return A_FAQ
            parsed[keyword.lower()] = reply
        faq.update(parsed)
        if len(faq) > 30:
            await update.message.reply_text("Ek group me maximum 30 FAQ keywords rakh sakte ho.")
            return A_FAQ
    try:
        sb.table("groups").update({"faq_replies": faq}).eq("id", group_id).execute()
    except Exception:
        logger.exception("FAQ update failed")
        await update.message.reply_text("FAQ save nahi hui. Supabase migration apply hui hai ya nahi check karo.")
        return ConversationHandler.END
    await update.message.reply_text(f"FAQ save ho gaya ({len(faq)} keyword). Dashboard ke liye /dashboard kholo.")
    return ConversationHandler.END


async def admin_upi_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    group_id = context.user_data.get("adm_group_id")
    upi = (update.message.text or "").strip()
    if "@" not in upi or len(upi) < 5:
        await update.message.reply_text("Ye UPI ID sahi nahi lag raha (isme @ hona chahiye). Dobara bhejo:")
        return A_UPI
    sb.table("groups").update({"upi_id": upi}).eq("id", group_id).execute()
    await update.message.reply_text(f"UPI ID set ho gaya: {upi}\nDashboard ke liye /admin likho.")
    return ConversationHandler.END


async def admin_qr_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    group_id = context.user_data.get("adm_group_id")
    file_id = update.message.photo[-1].file_id
    sb.table("groups").update({"upi_qr_file_id": file_id}).eq("id", group_id).execute()
    await update.message.reply_text("QR photo set ho gayi. Dashboard ke liye /admin likho.")
    return ConversationHandler.END


async def admin_plan_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    group_id = context.user_data.get("adm_group_id")
    label = context.user_data.get("adm_plan_label")
    text = (update.message.text or "").strip().replace(",", "")
    if not text.isdigit() or int(text) <= 0:
        await update.message.reply_text("Sirf number me price likho, masalan 599:")
        return A_PLAN_PRICE
    group = get_group_by_id(group_id)
    plans = get_plans(group)
    plans[label]["price"] = int(text)
    sb.table("groups").update({"plans": plans}).eq("id", group_id).execute()
    await update.message.reply_text(
        f"{PLAN_NAMES[label]} ki price Rs {int(text)} set ho gayi. Dashboard ke liye /admin likho."
    )
    return ConversationHandler.END


async def admin_broadcast_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    group_id = context.user_data.get("adm_group_id")
    group = get_group_by_id(group_id)
    text = update.message.text or ""
    try:
        rows = (
            sb.table("members").select("user_id").eq("group_id", group_id).eq("status", "ACTIVE").execute().data
            or []
        )
    except Exception:
        rows = []
    sent = failed = 0
    for r in rows:
        try:
            await context.bot.send_message(
                chat_id=r["user_id"], text=f"[{group['title']}]\n\n{text}"
            )
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)  # Telegram rate limit ka khayal
    await update.message.reply_text(f"Broadcast complete: {sent} members ko gaya, {failed} fail (bot block waghera).")
    return ConversationHandler.END


# --------------------------------------------------------------------------
# GROUP MODERATION: anti-spam + flood control + welcome
# --------------------------------------------------------------------------
async def get_admin_ids(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> set[int]:
    ts, cached = _admin_cache.get(chat_id, (0, set()))
    if now_utc().timestamp() - ts < 300:
        return cached
    try:
        admins = await context.bot.get_chat_administrators(chat_id)
        ids = {a.user.id for a in admins}
    except Exception:
        ids = set()
    _admin_cache[chat_id] = (now_utc().timestamp(), ids)
    return ids


async def group_guard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg or not msg.from_user or msg.from_user.is_bot:
        return
    chat_id = msg.chat_id
    user_id = msg.from_user.id
    group = get_group_by_chat(chat_id)
    if not group_is_active(group):
        return
    is_admin = user_id == group["owner_user_id"] or user_id in await get_admin_ids(context, chat_id)
    if not is_admin and group.get("gatekeeper_enabled", True) and not member_is_active(group["id"], user_id):
        await gate_member(context, group, msg.from_user)
        try:
            await msg.delete()
        except Exception:
            logger.info("Unable to delete message from ungated user %s", user_id)
        return
    if is_admin:
        return  # admins par moderation nahi chalegi

    should_delete = False
    reason = None
    # Telegram entities cover hidden text links; the expression also catches plain t.me and @mentions.
    body = (msg.text or msg.caption or "").strip()
    entities = list(msg.entities or []) + list(msg.caption_entities or [])
    has_url_entity = any(getattr(entity, "type", "") in ("url", "text_link") for entity in entities)
    if group.get("anti_spam_enabled", True) and (
        msg.forward_origin is not None or has_url_entity or LINK_RE.search(body)
    ):
        should_delete = True
        reason = "Links aur forwarded messages is group me allowed nahi hain."

    # Flood control: 10 second me 5 se zyada message = 10 min mute
    if group.get("flood_control_enabled", True):
        key = (chat_id, user_id)
        now_ts = now_utc().timestamp()
        hits = [t for t in _flood.get(key, []) if now_ts - t < 10]
        hits.append(now_ts)
        _flood[key] = hits
        if len(hits) > 5:
            _flood[key] = []
            should_delete = True
            reason = "Flood control: 10 second me 5 se zyada messages par 10 minute mute hua."
            try:
                await context.bot.restrict_chat_member(
                    chat_id=chat_id,
                    user_id=user_id,
                    permissions=ChatPermissions.no_permissions(),
                    until_date=now_utc() + timedelta(minutes=10),
                    use_independent_chat_permissions=True,
                )
            except Exception:
                logger.exception("flood restrict failed")

    if should_delete:
        try:
            await msg.delete()
        except Exception:
            logger.info("Unable to delete spam message in chat %s", chat_id)
        warning = None
        try:
            warning = await context.bot.send_message(chat_id, reason or "Message delete kar diya gaya.")
        except Exception:
            logger.info("Unable to post moderation warning in chat %s", chat_id)
        if warning:
            schedule_message_delete(context, chat_id, warning.message_id, delay=60)
        return

    # Keyword FAQ auto-replies run only for ordinary, non-command messages.
    faq = group.get("faq_replies") or {}
    if body and isinstance(faq, dict):
        lowered = body.casefold()
        for keyword, reply in sorted(faq.items(), key=lambda item: len(item[0]), reverse=True):
            if keyword and keyword.casefold() in lowered:
                try:
                    answer = await context.bot.send_message(chat_id, reply, reply_to_message_id=msg.message_id)
                    schedule_message_delete(context, chat_id, answer.message_id, delay=120)
                except Exception:
                    logger.exception("FAQ reply failed")
                break


def schedule_message_delete(context, chat_id: int, message_id: int, delay: int = 120):
    if context.job_queue:
        context.job_queue.run_once(
            delete_message_job,
            when=delay,
            data={"chat_id": chat_id, "message_id": message_id},
            name=f"delete-{chat_id}-{message_id}",
        )


async def delete_message_job(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data
    try:
        await context.bot.delete_message(chat_id=data["chat_id"], message_id=data["message_id"])
    except Exception:
        pass


async def gate_member(context, group: dict, member, *, joined: bool = False):
    """Join/message par member ka access enforce karo.

    Koi bhi notification group ke andar post nahi hogi — sab kuch sirf member ke
    DM me jayega (clean group view). Group ke andar sirf admins bolenge jab
    read-only mode on ho."""
    chat_id = group["chat_id"]
    user_id = member.id
    now_ts = now_utc().timestamp()
    key = (chat_id, user_id)
    if now_ts - _join_seen.get(key, 0) < 60:
        return
    _join_seen[key] = now_ts
    if len(_join_seen) > 1000:
        for seen_key, seen_at in list(_join_seen.items()):
            if now_ts - seen_at > 300:
                _join_seen.pop(seen_key, None)
    if user_id == group["owner_user_id"] or user_id in await get_admin_ids(context, chat_id):
        return
    active = member_is_active(group["id"], user_id)
    gatekeeper = group.get("gatekeeper_enabled", True)

    async def restrict_user():
        try:
            await context.bot.restrict_chat_member(
                chat_id=chat_id,
                user_id=user_id,
                permissions=ChatPermissions.no_permissions(),
                use_independent_chat_permissions=True,
            )
        except Exception:
            logger.exception("member restriction failed for user %s", user_id)

    if member_chat_enabled(group):
        # Chat ON: paid members ko chat access milega, baaki gatekeeper par rahenge.
        if gatekeeper:
            if active:
                await unrestrict_if_joined(context, chat_id, user_id)
            else:
                await restrict_user()
    else:
        # Read-only (broadcast) mode: admins ke alawa har member sirf sun sakta hai.
        await restrict_user()

    # Notifications — sirf DM me (points 2 aur 5).
    try:
        if active:
            if joined:
                member_row = get_member(group["id"], user_id)
                welcome = render_custom_welcome(group, member.first_name) or (
                    f"Welcome {member.first_name or 'Member'} to {group['title']}!"
                )
                if member_row and member_row.get("expiry_date"):
                    welcome += f"\n\nMembership valid: {fmt_ist(member_row['expiry_date'])}"
                await context.bot.send_message(chat_id=user_id, text=welcome)
        elif gatekeeper:
            keyboard = InlineKeyboardMarkup(
                [[
                    InlineKeyboardButton(
                        "🚀 Complete Process / Join VIP",
                        url=f"https://t.me/{context.bot.username}?start=g{group['id']}",
                    )
                ]]
            )
            await context.bot.send_message(
                chat_id=user_id,
                text=(
                    f"{group['title']} ek paid group hai. Access paane ke liye "
                    "payment aur verification process complete karo:"
                ),
                reply_markup=keyboard,
            )
    except Exception:
        pass  # user ne bot start nahi kiya ho to DM fail hoga — koi baat nahi


async def welcome_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg or not msg.new_chat_members:
        return
    # Point 2: join wala service message group se turant hata do (clean view).
    try:
        await msg.delete()
    except Exception:
        schedule_message_delete(context, msg.chat_id, msg.message_id, delay=5)
    group = get_group_by_chat(msg.chat_id)
    if not group_is_active(group):
        return
    for member in msg.new_chat_members:
        if member.is_bot:
            continue
        await gate_member(context, group, member, joined=True)


async def chat_member_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    member_update = update.chat_member
    if not member_update:
        return
    old = member_update.old_chat_member
    new = member_update.new_chat_member
    was_member = old.status in (
        ChatMemberStatus.MEMBER,
        ChatMemberStatus.ADMINISTRATOR,
        ChatMemberStatus.OWNER,
    ) or (old.status == ChatMemberStatus.RESTRICTED and getattr(old, "is_member", False))
    is_member = new.status in (
        ChatMemberStatus.MEMBER,
        ChatMemberStatus.ADMINISTRATOR,
        ChatMemberStatus.OWNER,
    ) or (new.status == ChatMemberStatus.RESTRICTED and getattr(new, "is_member", False))
    if is_member and not was_member and not new.user.is_bot:
        group = get_group_by_chat(member_update.chat.id)
        if group:
            await gate_member(context, group, new.user, joined=True)


async def bot_chat_member_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Immediately deactivate/reactivate a registered group when bot rights change."""
    member_update = update.my_chat_member
    if not member_update or member_update.chat.type not in ("group", "supergroup"):
        return
    group = get_group_by_chat(member_update.chat.id)
    if not group:
        return

    new_status = member_update.new_chat_member.status
    bot_is_admin = new_status in (
        ChatMemberStatus.ADMINISTRATOR,
        ChatMemberStatus.OWNER,
    )
    reason = None if bot_is_admin else (
        "bot_removed" if new_status in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED)
        else "bot_not_admin"
    )
    changed = set_group_lifecycle(group, active=bot_is_admin, reason=reason)
    if not changed or not group.get("owner_user_id"):
        return

    if bot_is_admin:
        text = (
            f"{group.get('title') or 'Group'} dashboard wapas active hai. "
            "Settings kholne ke liye /admin use karo."
        )
    else:
        text = (
            f"{group.get('title') or 'Group'} dashboard inactive kar diya hai kyunki bot "
            "group se remove ho gaya hai ya admin nahi raha. Bot ko admin bana kar "
            "dobara /setup chalao."
        )
    try:
        await context.bot.send_message(group["owner_user_id"], text)
    except TelegramError:
        logger.info("could not send membership-change notice group_id=%s", group.get("id"))


async def delete_service_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await update.effective_message.delete()
    except Exception:
        pass


async def title_change_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Point 2+3: group ka naam badle to service message delete karo aur
    naya title foran database me sync kar do."""
    msg = update.effective_message
    if not msg or not msg.new_chat_title:
        return
    group = get_group_by_chat(msg.chat_id)
    if group and msg.new_chat_title != group.get("title"):
        try:
            sb.table("groups").update({"title": msg.new_chat_title}).eq("id", group["id"]).execute()
        except Exception:
            logger.exception("title change sync failed for group %s", group.get("id"))
    try:
        await msg.delete()
    except Exception:
        pass


async def cleanup_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Owner-triggered live reconciliation for all registered groups."""
    user_id = update.effective_user.id
    groups = get_owned_groups(user_id)
    if not groups:
        await update.message.reply_text("Tumhare account par koi registered group nahi mila.")
        return

    active = inactive = errors = 0
    for group in groups:
        result = await reconcile_group_access(context.bot, group)
        if result == "active":
            active += 1
        elif result == "inactive":
            inactive += 1
        else:
            errors += 1
    await update.message.reply_text(
        "Group cleanup complete.\n"
        f"Active: {active}\nInactive/hidden: {inactive}\n"
        f"Temporary API checks skipped: {errors}"
    )


# --------------------------------------------------------------------------
# BACKGROUND JOBS: abandoned-cart follow-up + expiry/renewal engine
# --------------------------------------------------------------------------
async def group_reconcile_job(context: ContextTypes.DEFAULT_TYPE):
    """Periodically hide groups removed while the bot was offline."""
    last_id = 0
    while True:
        try:
            groups = (
                sb.table("groups")
                .select("id,title,chat_id,owner_user_id,is_active")
                .eq("is_active", True)
                .gt("id", last_id)
                .order("id")
                .limit(100)
                .execute()
                .data
                or []
            )
        except Exception:
            logger.exception("group reconciliation query failed")
            return
        if not groups:
            return

        for group in groups:
            last_id = max(last_id, int(group["id"]))
            await reconcile_group_access(context.bot, group)
            await asyncio.sleep(0.05)
        if len(groups) < 100:
            return


async def followup_job(context: ContextTypes.DEFAULT_TYPE):
    cutoff = (now_utc() - timedelta(hours=2)).isoformat()
    try:
        leads = (
            sb.table("pending_leads")
            .select("*")
            .eq("stage", "PLAN_VIEWED")
            .eq("followup_sent", False)
            .lte("created_at", cutoff)
            .limit(50)
            .execute()
            .data
            or []
        )
    except Exception:
        logger.exception("followup query failed")
        return
    for lead in leads:
        group = get_group_by_id(lead["group_id"])
        if not group:
            continue
        markup = InlineKeyboardMarkup(
            [[InlineKeyboardButton("Plans Dekho aur Join Karo", callback_data=f"renew:{group['id']}")]]
        )
        try:
            await context.bot.send_message(
                chat_id=lead["user_id"],
                text=(
                    "Reminder: Aapne abhi tak group join karne ka process poora nahi kiya hai. "
                    "Kripya apna payment poora karein."
                ),
                reply_markup=markup,
            )
        except Exception:
            pass  # user ne bot block kiya ho to skip
        try:
            sb.table("pending_leads").update(
                {"followup_sent": True, "updated_at": now_utc().isoformat()}
            ).eq("id", lead["id"]).execute()
        except Exception:
            pass


async def expiry_job(context: ContextTypes.DEFAULT_TYPE):
    horizon = (now_utc() + timedelta(days=2)).isoformat()
    try:
        rows = (
            sb.table("members")
            .select("*")
            .eq("status", "ACTIVE")
            .lte("expiry_date", horizon)
            .limit(200)
            .execute()
            .data
            or []
        )
    except Exception:
        logger.exception("expiry query failed")
        return

    now = now_utc()
    for m in rows:
        expiry = parse_dt(m["expiry_date"])
        if not expiry:
            continue
        group = get_group_by_id(m["group_id"])
        if not group:
            continue
        renew_markup = InlineKeyboardMarkup(
            [[InlineKeyboardButton("Abhi Renew Karo", callback_data=f"renew:{group['id']}")]]
        )

        if expiry <= now:
            # Expire: kick (ban + unban, taaki renew karke dobara aa sake)
            enforcement_succeeded = False
            try:
                await context.bot.ban_chat_member(group["chat_id"], m["user_id"])
                enforcement_succeeded = True
                try:
                    await context.bot.unban_chat_member(group["chat_id"], m["user_id"], only_if_banned=True)
                except Exception:
                    logger.exception("Unable to unban kicked member %s for renewal", m["id"])
            except Exception:
                logger.exception("auto-kick failed for member %s; trying temporary restriction", m["id"])
                try:
                    await context.bot.restrict_chat_member(
                        chat_id=group["chat_id"],
                        user_id=m["user_id"],
                        permissions=ChatPermissions.no_permissions(),
                        use_independent_chat_permissions=True,
                    )
                    enforcement_succeeded = True
                except Exception:
                    logger.exception("auto-restrict failed for expired member %s", m["id"])
            if not enforcement_succeeded:
                continue  # retry next cycle; don't record expiry when access was not removed
            try:
                sb.table("members").update({"status": "EXPIRED"}).eq("id", m["id"]).execute()
                await context.bot.send_message(
                    chat_id=m["user_id"],
                    text=f"Tumhari {group['title']} membership khatm ho gayi hai, isliye tumhe group se remove kar diya gaya hai.\nRenew karna ho to:",
                    reply_markup=renew_markup,
                )
            except Exception:
                pass
        elif expiry <= now + timedelta(days=1) and not m.get("reminder_1d_sent"):
            try:
                await context.bot.send_message(
                    chat_id=m["user_id"],
                    text=f"Reminder: Tumhari {group['title']} membership KAL khatm ho jayegi ({fmt_ist(expiry)}). Group se remove hone se pehle renew kar lo:",
                    reply_markup=renew_markup,
                )
                sb.table("members").update({"reminder_1d_sent": True}).eq("id", m["id"]).execute()
            except Exception:
                pass
        elif expiry <= now + timedelta(days=2) and not m.get("reminder_2d_sent"):
            try:
                await context.bot.send_message(
                    chat_id=m["user_id"],
                    text=f"Reminder: Tumhari {group['title']} membership 2 din me khatm ho jayegi ({fmt_ist(expiry)}). Waqt par renew kar lo taaki access na ruke:",
                    reply_markup=renew_markup,
                )
                sb.table("members").update({"reminder_2d_sent": True}).eq("id", m["id"]).execute()
            except Exception:
                pass


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.exception("Update handling me error", exc_info=context.error)


# --------------------------------------------------------------------------
# MAIN
# --------------------------------------------------------------------------
def main():
    threading.Thread(target=run_health_server, daemon=True).start()

    app = Application.builder().token(BOT_TOKEN).build()

    if not SUPER_ADMIN_IDS or not PLATFORM_UPI_ID:
        logger.warning(
            "SaaS billing is not configured. Set SUPER_ADMIN_IDS and PLATFORM_UPI_ID in Render."
        )

    payment_conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", start_cmd),
            CallbackQueryHandler(renew_cb, pattern=r"^renew:\d+$"),
        ],
        states={
            WAIT_PLAN: [CallbackQueryHandler(plan_cb, pattern=r"^plan:\d+:(1m|3m|1yr)$")],
            WAIT_SCREENSHOT: [MessageHandler(filters.PHOTO, screenshot_received)],
            WAIT_UTR: [MessageHandler(filters.TEXT & ~filters.COMMAND, utr_received)],
        },
        fallbacks=[CommandHandler("cancel", cancel_cmd)],
        allow_reentry=True,
    )

    admin_conv = ConversationHandler(
        entry_points=[
            CommandHandler(["admin", "dashboard"], admin_start, filters=filters.ChatType.PRIVATE)
        ],
        states={
            A_NAV: [CallbackQueryHandler(admin_router, pattern=r"^adm:")],
            A_UPI: [
                CallbackQueryHandler(admin_router, pattern=r"^adm:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_upi_text),
            ],
            A_QR: [
                CallbackQueryHandler(admin_router, pattern=r"^adm:"),
                MessageHandler(filters.PHOTO, admin_qr_photo),
            ],
            A_PLAN_PRICE: [
                CallbackQueryHandler(admin_router, pattern=r"^adm:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_plan_price),
            ],
            A_BCAST: [
                CallbackQueryHandler(admin_router, pattern=r"^adm:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_broadcast_text),
            ],
            A_WELCOME: [
                CallbackQueryHandler(admin_router, pattern=r"^adm:"),
                CommandHandler("clear", admin_welcome_text),
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_welcome_text),
            ],
            A_FAQ: [
                CallbackQueryHandler(admin_router, pattern=r"^adm:"),
                CommandHandler("clear", admin_faq_text),
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_faq_text),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_cmd)],
        allow_reentry=True,
    )

    billing_conv = ConversationHandler(
        entry_points=[
            CommandHandler("billing", billing_start, filters=filters.ChatType.PRIVATE),
            CallbackQueryHandler(billing_start, pattern=r"^billing:open$"),
        ],
        states={
            BILLING_SCREENSHOT: [MessageHandler(filters.PHOTO, billing_screenshot_received)],
            BILLING_UTR: [MessageHandler(filters.TEXT & ~filters.COMMAND, billing_utr_received)],
        },
        fallbacks=[CommandHandler("cancel", cancel_cmd)],
        allow_reentry=True,
    )

    app.add_handler(payment_conv)
    app.add_handler(admin_conv)
    app.add_handler(billing_conv)
    app.add_handler(CallbackQueryHandler(pay_decision, pattern=r"^(payapp|payrej):\d+$"))
    app.add_handler(CallbackQueryHandler(saas_payment_decision, pattern=r"^saas(app|rej):\d+$"))
    app.add_handler(CommandHandler("superadmin", superadmin_pending, filters=filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("cleanup", cleanup_cmd, filters=filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("setup", setup_cmd, filters=filters.ChatType.GROUPS))
    app.add_handler(ChatJoinRequestHandler(join_request_handler))
    app.add_handler(ChatMemberHandler(chat_member_update, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(
        ChatMemberHandler(bot_chat_member_update, ChatMemberHandler.MY_CHAT_MEMBER)
    )

    # Group moderation (status updates pehle, phir aam messages)
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_member), group=0)
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_TITLE, title_change_handler), group=0)
    app.add_handler(MessageHandler(filters.StatusUpdate.LEFT_CHAT_MEMBER, delete_service_msg), group=0)
    app.add_handler(
        MessageHandler(
            filters.ChatType.GROUPS & ~filters.COMMAND & ~filters.StatusUpdate.ALL,
            group_guard,
        ),
        group=1,
    )

    # Background jobs
    app.job_queue.run_repeating(followup_job, interval=900, first=60, name="followup")
    app.job_queue.run_repeating(expiry_job, interval=900, first=120, name="expiry")
    app.job_queue.run_repeating(
        group_reconcile_job, interval=6 * 60 * 60, first=300, name="group_reconcile"
    )

    app.add_error_handler(error_handler)
    logger.info("Bot start ho raha hai...")
    app.run_polling(
        allowed_updates=["message", "callback_query", "chat_member", "chat_join_request", "my_chat_member"]
    )


if __name__ == "__main__":
    main()
