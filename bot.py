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
from telegram.ext import (
    Application,
    CallbackQueryHandler,
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

# Flood control + group admin cache (in-memory, MVP ke liye kaafi hai)
_flood: dict[tuple[int, int], list[float]] = {}
_admin_cache: dict[int, tuple[float, set[int]]] = {}


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
    text = (
        f"Welcome to *{group['title']}*\n\n"
        "Apna plan select karo. Payment ke baad admin approve karega "
        "aur tumhe private join link mil jayega (link sirf tumhare liye hoga)."
    )
    markup = plan_buttons(group["id"], plans)
    if update.callback_query:
        await update.callback_query.message.reply_text(text, reply_markup=markup, parse_mode="Markdown")
    else:
        await update.message.reply_text(text, reply_markup=markup, parse_mode="Markdown")
    return WAIT_PLAN


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    group = None
    if args and args[0].startswith("g") and args[0][1:].isdigit():
        group = get_group_by_id(int(args[0][1:]))
    if group:
        context.user_data["join_group_id"] = group["id"]
        return await show_plans(update, context, group)

    # Bina join link ke /start par sirf "Welcome"
    await update.message.reply_text("Welcome")
    return ConversationHandler.END


async def renew_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reminder message ke Renew button se flow dobara shuru."""
    query = update.callback_query
    await safe_answer(query)
    group_id = int(query.data.split(":")[1])
    group = get_group_by_id(group_id)
    if not group:
        await query.message.reply_text("Ye group ab active nahi hai. Admin se rabta karo.")
        return ConversationHandler.END
    context.user_data["join_group_id"] = group_id
    return await show_plans(update, context, group)


async def plan_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await safe_answer(query)
    _, group_id_s, label = query.data.split(":")
    group_id = int(group_id_s)
    group = get_group_by_id(group_id)
    if not group:
        await query.message.reply_text("Group nahi mila. Admin se rabta karo.")
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
    if not group:
        await update.message.reply_text("Session expire ho gaya. Join link se dobara /start karo.")
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
                InlineKeyboardButton("Approve", callback_data=f"payapp:{payment_id}"),
                InlineKeyboardButton("Reject", callback_data=f"payrej:{payment_id}"),
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
    try:
        await context.bot.send_photo(
            chat_id=group["owner_user_id"],
            photo=context.user_data.get("screenshot_file_id"),
            caption=caption,
            reply_markup=buttons,
        )
    except Exception:
        logger.exception("owner notify failed")

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
    if not group or group["owner_user_id"] != query.from_user.id:
        await safe_answer(query, "Sirf group admin hi approve/reject kar sakta hai.", alert=True)
        return
    if payment["status"] != "PENDING":
        await safe_answer(query, f"Ye payment pehle hi {payment['status']} hai.", alert=True)
        return

    member_id = payment["user_id"]
    if action == "payrej":
        sb.table("payments").update(
            {"status": "REJECTED", "reviewed_by": query.from_user.id, "reviewed_at": now_utc().isoformat()}
        ).eq("id", payment_id).execute()
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
    plans = get_plans(group)
    days = plans.get(payment["plan_label"], DEFAULT_PLANS["1m"])["days"]
    existing = get_member(group["id"], member_id)
    base = now_utc()
    if existing and existing.get("status") == "ACTIVE":
        old_expiry = parse_dt(existing.get("expiry_date"))
        if old_expiry and old_expiry > base:
            base = old_expiry  # renewal: purani expiry se aage badhao
    expiry = base + timedelta(days=days)

    sb.table("payments").update(
        {"status": "APPROVED", "reviewed_by": query.from_user.id, "reviewed_at": now_utc().isoformat()}
    ).eq("id", payment_id).execute()

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

    try:
        sb.table("pending_leads").update(
            {"stage": "CONVERTED", "updated_at": now_utc().isoformat()}
        ).eq("group_id", group["id"]).eq("user_id", member_id).execute()
    except Exception:
        pass

    # ID-locked join link: join request sirf usi user ke liye approve hoga
    link_text = ""
    try:
        link = await context.bot.create_chat_invite_link(
            chat_id=group["chat_id"],
            name=f"paid-{member_id}"[:32],
            expire_date=now_utc() + timedelta(hours=24),
            creates_join_request=True,
        )
        link_text = link.invite_link
    except Exception:
        logger.exception("invite link create failed")

    await safe_answer(query, "Approve ho gaya, member ko link bhej diya.")
    try:
        await query.edit_message_caption(
            caption=(query.message.caption or "") + f"\n\nStatus: APPROVED\nExpiry: {fmt_ist(expiry)}"
        )
    except Exception:
        pass

    msg = (
        f"Payment approve ho gayi! {group['title']} me tumhara swagat hai.\n"
        f"Membership expiry: {fmt_ist(expiry)}\n\n"
    )
    if link_text:
        msg += (
            f"Join link (sirf tumhare liye, 24 ghante valid):\n{link_text}\n\n"
            "Link dabao aur 'Join Request' bhejo, bot tumhe auto-approve kar dega. "
            "Link kisi ke saath share mat karna, wo kisi aur ke liye kaam nahi karega."
        )
    else:
        msg += "Join link banane me masla aaya hai, admin se rabta karo."
    try:
        await context.bot.send_message(chat_id=member_id, text=msg)
    except Exception:
        logger.exception("member approve DM failed")


async def join_request_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Join request sirf approved (payment wale) member ki hi approve hogi."""
    req = update.chat_join_request
    group = get_group_by_chat(req.chat.id)
    approved = False
    if group:
        member = get_member(group["id"], req.from_user.id)
        if member and member.get("status") == "ACTIVE":
            expiry = parse_dt(member.get("expiry_date"))
            if expiry and expiry > now_utc():
                approved = True
    try:
        if approved:
            await context.bot.approve_chat_join_request(req.chat.id, req.from_user.id)
        else:
            await context.bot.decline_chat_join_request(req.chat.id, req.from_user.id)
            await context.bot.send_message(
                chat_id=req.from_user.id,
                text="Tumhari join request approve nahi ho saki kyunki tumhari paid "
                "membership record nahi mili. Pehle bot se payment flow complete karo.",
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
    try:
        member = await context.bot.get_chat_member(chat.id, user.id)
        if member.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
            await update.message.reply_text("Sirf group admin hi /setup kar sakta hai.")
            return
    except Exception:
        await update.message.reply_text("Admin status check nahi ho saka. Bot ko group admin banao pehle.")
        return

    existing = get_group_by_chat(chat.id)
    if existing:
        await update.message.reply_text(
            f"Ye group pehle se registered hai: {existing['title']}\n"
            f"Members ke liye join link:\nhttps://t.me/{context.bot.username}?start=g{existing['id']}\n\n"
            "Settings ke liye bot ko DM me /admin likho."
        )
        return

    try:
        res = sb.table("groups").insert(
            {"chat_id": chat.id, "title": chat.title or "", "owner_user_id": user.id}
        ).execute()
        gid = res.data[0]["id"]
    except Exception:
        logger.exception("group insert failed")
        await update.message.reply_text("Registration me masla aaya, dobara try karo.")
        return

    await update.message.reply_text(
        f"Group registered ho gaya: {chat.title}\n\n"
        f"Members ke liye join link:\nhttps://t.me/{context.bot.username}?start=g{gid}\n\n"
        "Ab bot ko DM me /admin likh kar UPI ID/QR aur plans set karo. "
        "Dhyaan: expiry par auto-remove ke liye bot ke paas 'Ban users' permission hona chahiye."
    )


# --------------------------------------------------------------------------
# ADMIN DASHBOARD: /admin (bot DM)
# --------------------------------------------------------------------------
def dashboard_markup(group_id: int) -> InlineKeyboardMarkup:
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
            [InlineKeyboardButton("Broadcast Message", callback_data=f"adm:bcast:{group_id}")],
            [InlineKeyboardButton("<< Mere Groups", callback_data="adm:back")],
        ]
    )


async def show_group_list(message_or_query, user_id: int):
    try:
        groups = (
            sb.table("groups").select("id,title,chat_id").eq("owner_user_id", user_id).execute().data
            or []
        )
    except Exception:
        groups = []
    if not groups:
        text = (
            "Tumhara koi group registered nahi hai.\n\n"
            "Steps:\n1) Bot ko apne group me add karo aur admin banao (Ban users permission ke saath).\n"
            "2) Group ke andar /setup likho.\n3) Phir yahan /admin se settings karo."
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
    text = "Apna group select karo:"
    markup = InlineKeyboardMarkup(buttons)
    if hasattr(message_or_query, "data"):  # CallbackQuery
        await message_or_query.edit_message_text(text, reply_markup=markup)
    else:
        await message_or_query.reply_text(text, reply_markup=markup)
    return A_NAV


async def admin_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await show_group_list(update.message, update.effective_user.id)


async def show_dashboard(query, group: dict):
    plans = get_plans(group)
    plan_lines = "\n".join(
        f"  {PLAN_NAMES[l]}: Rs {plans[l]['price']} ({plans[l]['days']} din)" for l in ("1m", "3m", "1yr")
    )
    text = (
        f"*{group['title']}*\n"
        f"UPI ID: `{group.get('upi_id') or 'set nahi hai'}`\n"
        f"QR Photo: {'set hai' if group.get('upi_qr_file_id') else 'set nahi hai'}\n"
        f"Plans:\n{plan_lines}"
    )
    await query.edit_message_text(
        text, reply_markup=dashboard_markup(group["id"]), parse_mode="Markdown"
    )


async def admin_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await safe_answer(query)
    parts = query.data.split(":")
    action = parts[1]

    if action == "back":
        return await show_group_list(query, query.from_user.id)

    group_id = int(parts[2])
    group = get_group_by_id(group_id)
    if not group or group["owner_user_id"] != query.from_user.id:
        await safe_answer(query, "Ye group tumhara nahi hai.", alert=True)
        return A_NAV
    context.user_data["adm_group_id"] = group_id

    if action == "grp":
        await show_dashboard(query, group)
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
            reply_markup=dashboard_markup(group_id),
        )
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
        await query.edit_message_text(text, reply_markup=dashboard_markup(group_id))
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
        await query.edit_message_text(text, reply_markup=dashboard_markup(group_id))
        return A_NAV

    if action == "bcast":
        await query.edit_message_text(
            "Broadcast message likh kar bhejo. Ye tumhare saare ACTIVE members ko bot DM me jayega."
        )
        return A_BCAST

    return A_NAV


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
    if now_utc().timestamp() - ts < 300 and cached:
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
    if user_id in await get_admin_ids(context, chat_id):
        return  # admins par koi pabandi nahi

    should_delete = False
    # Links / forwards block
    if msg.forward_origin is not None:
        should_delete = True
    if msg.entities:
        for ent in msg.entities:
            if ent.type in ("url", "text_link"):
                should_delete = True
                break

    # Flood control: 10 second me 5 se zyada message = 10 min mute
    key = (chat_id, user_id)
    now_ts = now_utc().timestamp()
    hits = [t for t in _flood.get(key, []) if now_ts - t < 10]
    hits.append(now_ts)
    _flood[key] = hits
    if len(hits) > 5:
        _flood[key] = []
        should_delete = True
        try:
            await context.bot.restrict_chat_member(
                chat_id,
                user_id,
                permissions=ChatPermissions(can_send_messages=False),
                until_date=now_utc() + timedelta(minutes=10),
            )
            await context.bot.send_message(
                chat_id,
                f"{msg.from_user.first_name}, flood karne par 10 minute ke liye mute kar diya gaya hai.",
            )
        except Exception:
            logger.exception("flood restrict failed")

    if should_delete:
        try:
            await msg.delete()
        except Exception:
            pass  # bot ke paas delete permission na ho to chup chaap aage badho


async def welcome_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg or not msg.new_chat_members:
        return
    group = get_group_by_chat(msg.chat_id)
    title = group["title"] if group else msg.chat.title
    for member in msg.new_chat_members:
        if member.is_bot:
            continue
        try:
            await context.bot.send_message(
                msg.chat_id,
                f"Welcome {member.first_name}! Tum {title} me aa gaye ho.\n"
                "Rules: koi spam/link nahi, sab ki izzat karo. "
                "Membership expiry se pehle bot tumhe renewal reminder bhejega.",
            )
        except Exception:
            pass


async def delete_service_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await update.effective_message.delete()
    except Exception:
        pass


# --------------------------------------------------------------------------
# BACKGROUND JOBS: abandoned-cart follow-up + expiry/renewal engine
# --------------------------------------------------------------------------
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
            try:
                await context.bot.ban_chat_member(group["chat_id"], m["user_id"])
                await context.bot.unban_chat_member(group["chat_id"], m["user_id"], only_if_banned=True)
            except Exception:
                logger.exception("auto-kick failed for member %s", m["id"])
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
        entry_points=[CommandHandler("admin", admin_start, filters=filters.ChatType.PRIVATE)],
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
        },
        fallbacks=[CommandHandler("cancel", cancel_cmd)],
        allow_reentry=True,
    )

    app.add_handler(payment_conv)
    app.add_handler(admin_conv)
    app.add_handler(CallbackQueryHandler(pay_decision, pattern=r"^(payapp|payrej):\d+$"))
    app.add_handler(CommandHandler("setup", setup_cmd, filters=filters.ChatType.GROUPS))
    app.add_handler(ChatJoinRequestHandler(join_request_handler))

    # Group moderation (status updates pehle, phir aam messages)
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_member), group=0)
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
    app.job_queue.run_repeating(expiry_job, interval=3600, first=120, name="expiry")

    app.add_error_handler(error_handler)
    logger.info("Bot start ho raha hai...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
