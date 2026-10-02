import os
import random
import logging
import asyncio
import time
import html
import json
import copy
from threading import Thread

from flask import Flask

import firebase_admin
from firebase_admin import credentials, db

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import RetryAfter
from telegram.ext import (
    Application,
    MessageHandler,
    CommandHandler,
    CallbackQueryHandler,
    ChatMemberHandler,
    filters,
    ContextTypes,
)

from dotenv import load_dotenv
from cryptography.fernet import Fernet, InvalidToken


# =========================================================
# BASIC CONFIG
# =========================================================

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

logger = logging.getLogger(__name__)

OWNER_ID = 8223664417

IMAGE_URL = "https://telegra.ph/file/07f45aef0cc6323c21c78.jpg"
SUPPORT_URL = "https://t.me/Jr_Auto_ReactionBot"


# =========================================================
# REACTIONS
# =========================================================

REACTION_EMOJIS = [
    "👍", "👎", "❤️", "🔥", "🥰", "👏", "😁", "🤔", "🤯", "😱",
    "🤬", "😢", "🎉", "🤩", "🤮", "💩", "🙏", "👌", "🕊", "🤡",
    "🥱", "🥴", "😍", "🐳", "❤️‍🔥", "🌚", "🌭", "💯", "🤣", "⚡",
    "🍌", "🏆", "💔", "🤨", "😐", "🍓", "🍾", "💋", "🖕", "😈",
    "😴", "😭", "🤓", "👻", "👨‍💻", "👀", "🎃", "🙈", "😇", "😨",
    "🤝", "✍", "🤗", "🫡", "🎅", "🎄", "☃", "💅", "🤪", "🗿",
    "🆒", "💘", "🙉", "🦄", "😘", "💊", "🙊", "😎", "👾", "🤷‍♂️",
    "🤷", "🤷‍♀️", "😡"
]


# =========================================================
# FLASK SERVER
# =========================================================

app_flask = Flask(__name__)


@app_flask.route("/")
def home():
    return "Reaction Bot is running perfectly! 🚀"


def run_server():
    try:
        port = int(os.environ.get("PORT", 8080))

        app_flask.run(
            host="0.0.0.0",
            port=port,
        )

    except Exception:
        logger.exception("Flask server failed.")


# =========================================================
# FIREBASE
# =========================================================

firebase_initialized = False


def init_firebase():
    global firebase_initialized

    if firebase_initialized:
        return

    database_url = os.getenv(
        "FIREBASE_DATABASE_URL",
        ""
    ).strip()

    service_account_json = os.getenv(
        "FIREBASE_SERVICE_ACCOUNT_JSON",
        ""
    ).strip()

    if not database_url:
        raise RuntimeError(
            "Missing environment variable: FIREBASE_DATABASE_URL"
        )

    if not service_account_json:
        raise RuntimeError(
            "Missing environment variable: FIREBASE_SERVICE_ACCOUNT_JSON"
        )

    try:
        service_account_data = json.loads(
            service_account_json
        )

    except json.JSONDecodeError as e:
        raise RuntimeError(
            "FIREBASE_SERVICE_ACCOUNT_JSON is not valid JSON: "
            f"{e}"
        ) from e

    try:
        firebase_admin.get_app()

        logger.info(
            "Firebase app already initialized."
        )

    except ValueError:
        cred = credentials.Certificate(
            service_account_data
        )

        firebase_admin.initialize_app(
            cred,
            {
                "databaseURL": database_url
            }
        )

        logger.info(
            "Firebase initialized successfully."
        )

    firebase_initialized = True


# =========================================================
# MANAGER / DYNAMIC BOT SYSTEM
# =========================================================

MANAGER_BOT_TOKEN = os.getenv("MANAGER_BOT_TOKEN", "").strip()
TOKEN_ENCRYPTION_SECRET = os.getenv("TOKEN_ENCRYPTION_SECRET", "").strip()

# Runtime map: Telegram bot id -> running Application
RUNNING_BOTS = {}

# Owner-only temporary state for the Manager Bot.
MANAGER_PENDING_ADD = set()


def _fernet():
    if not TOKEN_ENCRYPTION_SECRET:
        raise RuntimeError(
            "Missing environment variable: TOKEN_ENCRYPTION_SECRET"
        )

    # Stable 32-byte key derived from a Render secret.
    import base64
    import hashlib

    key = base64.urlsafe_b64encode(
        hashlib.sha256(
            TOKEN_ENCRYPTION_SECRET.encode("utf-8")
        ).digest()
    )
    return Fernet(key)


def encrypt_token(token):
    return _fernet().encrypt(
        token.encode("utf-8")
    ).decode("utf-8")


def decrypt_token(value):
    try:
        return _fernet().decrypt(
            value.encode("utf-8")
        ).decode("utf-8")
    except InvalidToken as e:
        raise RuntimeError(
            "Stored bot token cannot be decrypted. Check TOKEN_ENCRYPTION_SECRET."
        ) from e


def managed_bots_ref():
    return db.reference("managed_bots")


async def get_managed_bots():
    try:
        data = await asyncio.to_thread(
            managed_bots_ref().get
        )
    except Exception:
        logger.exception("Failed to read managed bot registry.")
        return {}

    return data if isinstance(data, dict) else {}


async def save_managed_bot(bot_id, data):
    await asyncio.to_thread(
        managed_bots_ref().child(str(bot_id)).set,
        data
    )


async def delete_managed_bot(bot_id):
    await asyncio.to_thread(
        managed_bots_ref().child(str(bot_id)).delete
    )


# =========================================================
# STATE
# =========================================================

def create_empty_state(
    bot_id=None,
    bot_username=None
):
    return {
        "bot_id": bot_id,
        "bot_username": bot_username,
        "users": {},
        "channels": {},
        "groups": {},
    }


def normalize_state(
    data,
    bot_id=None,
    bot_username=None
):
    if not isinstance(data, dict):
        data = {}

    users = data.get(
        "users",
        {}
    )

    channels = data.get(
        "channels",
        {}
    )

    groups = data.get(
        "groups",
        {}
    )

    if not isinstance(users, dict):
        users = {}

    if not isinstance(channels, dict):
        channels = {}

    if not isinstance(groups, dict):
        groups = {}

    return {
        "bot_id": data.get(
            "bot_id",
            bot_id
        ),
        "bot_username": data.get(
            "bot_username",
            bot_username
        ),
        "users": users,
        "channels": channels,
        "groups": groups,
    }


async def load_state(
    firebase_ref,
    bot_id,
    bot_username
):
    try:
        data = await asyncio.to_thread(
            firebase_ref.get
        )
    except Exception:
        logger.exception(
            "Firebase read failed for bot @%s",
            bot_username or "unknown"
        )
        data = {}

    state = normalize_state(
        data,
        bot_id,
        bot_username
    )

    logger.info(
        "Firebase data loaded for bot @%s",
        bot_username or "unknown"
    )

    return state


async def save_state(context):
    firebase_ref = (
        context.application.bot_data.get(
            "firebase_ref"
        )
    )

    state = (
        context.application.bot_data.get(
            "state"
        )
    )

    if firebase_ref is None:
        logger.error(
            "Firebase reference is missing."
        )
        return

    if state is None:
        logger.error(
            "Bot state is missing."
        )
        return

    lock = (
        context.application.bot_data.get(
            "save_lock"
        )
    )

    if lock is None:
        logger.error(
            "Firebase save lock is missing."
        )
        return

    try:
        async with lock:
            payload = copy.deepcopy(
                state
            )

            await asyncio.to_thread(
                firebase_ref.set,
                payload
            )

    except Exception:
        logger.exception(
            "Firebase save failed."
        )


# =========================================================
# CHAT INFORMATION
# =========================================================

def get_chat_info(chat):
    title = getattr(
        chat,
        "title",
        None
    )

    username = getattr(
        chat,
        "username",
        None
    )

    if not title:
        title = (
            getattr(
                chat,
                "first_name",
                None
            )
            or getattr(
                chat,
                "full_name",
                None
            )
            or "Unknown"
        )

    return {
        "id": chat.id,
        "title": title,
        "username": username or "",
    }


def record_chat(
    state,
    chat
):
    if chat.type == "channel":
        collection = state[
            "channels"
        ]
    elif chat.type in (
        "group",
        "supergroup"
    ):
        collection = state[
            "groups"
        ]
    else:
        return False

    key = str(
        chat.id
    )

    new_info = get_chat_info(
        chat
    )

    old_info = collection.get(
        key
    )

    if old_info != new_info:
        collection[key] = new_info
        return True

    return False


def remove_chat(
    state,
    chat
):
    key = str(
        chat.id
    )

    changed = False

    if key in state["channels"]:
        del state["channels"][key]
        changed = True

    if key in state["groups"]:
        del state["groups"][key]
        changed = True

    return changed


# =========================================================
# UPTIME
# =========================================================

def get_uptime(context):
    start_time = (
        context.application.bot_data.get(
            "start_time",
            time.time()
        )
    )

    uptime_seconds = int(
        time.time() - start_time
    )

    hours, remainder = divmod(
        uptime_seconds,
        3600
    )

    minutes, seconds = divmod(
        remainder,
        60
    )

    return (
        f"{hours}h "
        f"{minutes}m "
        f"{seconds}s"
    )


# =========================================================
# MAIN MENU — ORIGINAL UI PRESERVED
# =========================================================

def get_main_menu(
    user_name,
    bot_username,
    user_id
):
    safe_name = html.escape(
        user_name or "User"
    )

    welcome_text = (
        f"Hey! 👋\n"
        f" ×º°”˜ ꯭👾꯭𝀣꯭𝘇ۗ𝗶ۗ𝗻ۗ𝗻𝅭ۗ𝆇𝆞꯭۵ۗܔ꯭ ˜”°º× 😎\n\n"
        f"✅ <b>Join official Channel</b> 👉\n"
        f"{SUPPORT_URL} ✅\n"
        f"I can seamlessly react to messages in channels or groups. Just add me as an admin! ✨"
    )

    keyboard = [
        [
            InlineKeyboardButton(
                "ADD TO CHANNEL",
                url=(
                    f"https://t.me/"
                    f"{bot_username}"
                    f"?startchannel=true"
                )
            ),

            InlineKeyboardButton(
                "ADD TO GROUP",
                url=(
                    f"https://t.me/"
                    f"{bot_username}"
                    f"?startgroup=true"
                )
            )
        ],

        [
            InlineKeyboardButton(
                "How To Use",
                callback_data="how_to_use"
            )
        ]
    ]

    if user_id == OWNER_ID:
        keyboard.append(
            [
                InlineKeyboardButton(
                    "📢 All Channels",
                    callback_data="all_channels:0"
                )
            ]
        )

    keyboard.append(
        [
            InlineKeyboardButton(
                "Support",
                url=SUPPORT_URL
            )
        ]
    )

    return (
        welcome_text,
        InlineKeyboardMarkup(
            keyboard
        )
    )


# =========================================================
# /START
# =========================================================

async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if not update.effective_chat:
        return

    state = context.application.bot_data[
        "state"
    ]

    if (
        update.effective_chat.type
        == "private"
    ):
        user_id = (
            update.effective_chat.id
        )

        user_key = str(
            user_id
        )

        first_name = (
            update.effective_user.first_name
            if update.effective_user
            else "User"
        )

        username = (
            update.effective_user.username
            if update.effective_user
            else ""
        )

        user_data = {
            "id": user_id,
            "first_name": (
                first_name or ""
            ),
            "username": (
                username or ""
            ),
        }

        if state["users"].get(user_key) != user_data:
            state["users"][user_key] = user_data
            await save_state(context)

    first_name = (
        update.effective_user.first_name
        if update.effective_user
        else "User"
    )

    bot_username = (
        context.bot.username
        or "ReactionBot"
    )

    user_id = (
        update.effective_user.id
        if update.effective_user
        else 0
    )

    welcome_text, reply_markup = (
        get_main_menu(
            first_name,
            bot_username,
            user_id
        )
    )

    try:
        await context.bot.send_photo(
            chat_id=update.effective_chat.id,
            photo=IMAGE_URL,
            caption=welcome_text,
            parse_mode="HTML",
            reply_markup=reply_markup
        )

    except Exception as e:
        logger.warning(
            "Photo send failed: %s",
            e
        )

        if update.message:
            await update.message.reply_text(
                text=welcome_text,
                parse_mode="HTML",
                reply_markup=reply_markup,
                disable_web_page_preview=True
            )


# =========================================================
# HOW TO USE — ORIGINAL UI PRESERVED
# =========================================================

async def how_to_use_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query

    if not query:
        return

    await query.answer()

    how_to_text = (
        "╭━━━━━━━━━━━━━━━━━━━━╮\n"
        "   ✦ REACTION BOT • GUIDE ✦\n"
        "╰━━━━━━━━━━━━━━━━━━━━╯\n\n"

        "➊ ADD THE BOT\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "• Add the bot to your Telegram Channel or Group.\n"
        "• Give the bot the necessary Admin Permissions.\n"
        "• Make sure the bot is allowed to manage reactions.\n\n"

        "➋ REACTION SETTINGS\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "For automatic reactions to work properly, make sure the supported reactions are available in your Channel or Group.\n\n"

        "♡ Supported Reactions\n"
        "👍 👎 ❤️ 🔥 🥰 👏 😁 🤔 🤯 😱 🤬 😢\n"
        "🎉 🤩 🤮 💩 🙏 👌 🕊 🤡 🥱 🥴 😍 🐳\n"
        "❤️‍🔥 🌚 🌭 💯 🤣 ⚡ 🍌 🏆 💔 🤨 😐 🍓\n"
        "🍾 💋 🖕 😈 😴 😭 🤓 👻 👨‍💻 👀 🎃 🙈\n"
        "😇 😨 🤝 ✍ 🤗 🫡 🎅 🎄 ☃ 💅 🤪 🗿 🆒\n"
        "💘 🙉 🦄 😘 💊 🙊 😎 👾 🤷‍♂️ 🤷 🤷‍♀️ 😡\n\n"

        "➌ AUTOMATIC REACTION\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "After setup, the bot automatically chooses a random reaction from the supported list and applies it to new messages. ⚡\n\n"

        "⚠️ REACTION NOT WORKING?\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "The most common reason is that the selected emoji is not available in the target Channel or Group.\n\n"
        "Check your Telegram reaction settings and make sure the emojis used by the bot are allowed there.\n\n"
        "✦ Tip: Keep your allowed reactions and the bot's supported list synchronized for smoother operation. 🚀"
    )

    keyboard = [
        [
            InlineKeyboardButton(
                "‹  BACK TO MENU",
                callback_data="back_to_menu"
            )
        ]
    ]

    markup = InlineKeyboardMarkup(
        keyboard
    )

    if (
        query.message
        and query.message.photo
    ):
        try:
            await query.message.edit_caption(
                caption=how_to_text,
                parse_mode="HTML",
                reply_markup=markup
            )
            return

        except Exception as e:
            logger.warning(
                "Guide caption edit failed: %s",
                e
            )

    if query.message:
        await query.message.edit_text(
            text=how_to_text,
            parse_mode="HTML",
            reply_markup=markup,
            disable_web_page_preview=True
        )


# =========================================================
# ALL CHANNELS
# =========================================================

def truncate_text(
    text,
    max_length
):
    text = str(
        text or ""
    )

    if len(text) <= max_length:
        return text

    return (
        text[:max_length - 3]
        + "..."
    )


def build_all_channels_page(
    state,
    page
):
    channels = list(
        state.get(
            "channels",
            {}
        ).values()
    )

    channels.sort(
        key=lambda x: str(
            x.get(
                "title",
                ""
            )
        ).lower()
    )

    per_page = 5
    total = len(
        channels
    )

    total_pages = max(
        1,
        (
            total
            + per_page
            - 1
        ) // per_page
    )

    page = max(
        0,
        min(
            page,
            total_pages - 1
        )
    )

    start = (
        page * per_page
    )

    page_channels = channels[
        start:start + per_page
    ]

    text = (
        "╭━━━━━━━━━━━━━━━━━━━━╮\n"
        "   ✦ <b>📢 ALL CHANNELS</b> ✦\n"
        "╰━━━━━━━━━━━━━━━━━━━━╯\n\n"
    )

    if not channels:
        text += (
            "❌ <b>No channels recorded yet.</b>\n\n"
            "The bot will automatically save a "
            "channel when it receives a new "
            "channel update."
        )

    else:
        text += (
            f"📊 <b>Total:</b> "
            f"<code>{total}</code>\n\n"
        )

        for number, channel in enumerate(
            page_channels,
            start=start + 1
        ):
            title = html.escape(
                truncate_text(
                    channel.get(
                        "title",
                        "Unknown Channel"
                    ),
                    45
                )
            )

            username = channel.get(
                "username",
                ""
            )

            channel_id = channel.get(
                "id",
                ""
            )

            if username:
                username_text = (
                    "@"
                    + html.escape(
                        truncate_text(
                            username,
                            35
                        )
                    )
                )
            else:
                username_text = (
                    "🔒 Private / No Username"
                )

            text += (
                f"<b>{number}. {title}</b>\n"
                f"   👤 <code>{username_text}</code>\n"
                f"   🆔 <code>{channel_id}</code>\n\n"
            )

        text += (
            f"📄 <b>Page:</b> "
            f"{page + 1}/{total_pages}"
        )

    keyboard = []
    navigation = []

    if page > 0:
        navigation.append(
            InlineKeyboardButton(
                "‹ Previous",
                callback_data=(
                    f"all_channels:{page - 1}"
                )
            )
        )

    if page < total_pages - 1:
        navigation.append(
            InlineKeyboardButton(
                "Next ›",
                callback_data=(
                    f"all_channels:{page + 1}"
                )
            )
        )

    if navigation:
        keyboard.append(
            navigation
        )

    keyboard.append(
        [
            InlineKeyboardButton(
                "‹  BACK TO MENU",
                callback_data="back_to_menu"
            )
        ]
    )

    markup = InlineKeyboardMarkup(
        keyboard
    )

    return text, markup


async def all_channels_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query

    if not query:
        return

    if (
        not query.from_user
        or query.from_user.id != OWNER_ID
    ):
        await query.answer(
            "This section is owner only.",
            show_alert=True
        )
        return

    await query.answer()

    try:
        page = int(
            query.data.split(":")[1]
        )
    except Exception:
        page = 0

    state = context.application.bot_data[
        "state"
    ]

    text, markup = build_all_channels_page(
        state,
        page
    )

    if (
        query.message
        and query.message.photo
    ):
        try:
            await query.message.edit_caption(
                caption=text,
                parse_mode="HTML",
                reply_markup=markup
            )
            return

        except Exception as e:
            logger.warning(
                "All Channels caption edit failed: %s",
                e
            )

    if query.message:
        await query.message.edit_text(
            text=text,
            parse_mode="HTML",
            reply_markup=markup,
            disable_web_page_preview=True
        )


# =========================================================
# BACK TO MENU
# =========================================================

async def back_to_menu_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query

    if not query:
        return

    await query.answer()

    first_name = (
        query.from_user.first_name
        if query.from_user
        else "User"
    )

    bot_username = (
        context.bot.username
        or "ReactionBot"
    )

    user_id = (
        query.from_user.id
        if query.from_user
        else 0
    )

    welcome_text, reply_markup = (
        get_main_menu(
            first_name,
            bot_username,
            user_id
        )
    )

    if (
        query.message
        and query.message.photo
    ):
        try:
            await query.message.edit_caption(
                caption=welcome_text,
                parse_mode="HTML",
                reply_markup=reply_markup
            )
            return

        except Exception as e:
            logger.warning(
                "Menu caption edit failed: %s",
                e
            )

    if query.message:
        await query.message.edit_text(
            text=welcome_text,
            parse_mode="HTML",
            reply_markup=reply_markup,
            disable_web_page_preview=True
        )


# =========================================================
# /STATS
# =========================================================

async def stats_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if not update.effective_user:
        return

    if update.effective_user.id != OWNER_ID:
        return

    state = context.application.bot_data[
        "state"
    ]

    stats_text = (
        "📊 <b><u>PREMIUM BOT STATISTICS</u></b> 📊\n\n"
        "<blockquote>"
        "👥 <b>Audience & Reach:</b>\n"
        f"👤 <b>Total Users:</b> "
        f"<code>{len(state['users'])}</code>\n"
        f"📢 <b>Channels Added:</b> "
        f"<code>{len(state['channels'])}</code>\n"
        f"👥 <b>Groups Added:</b> "
        f"<code>{len(state['groups'])}</code>\n"
        "</blockquote>\n"
        "<blockquote>"
        "⚙️ <b>System Information:</b>\n"
        f"⏱ <b>Uptime:</b> "
        f"<code>{get_uptime(context)}</code>\n"
        "⚡ <b>Server Status:</b> "
        "<code>Ultra Fast 🟢</code>\n"
        "🤖 <b>Bot Version:</b> "
        "<code>v2.0.1 (Pro)</code>\n"
        "</blockquote>"
    )

    if update.message:
        await update.message.reply_text(
            text=stats_text,
            parse_mode="HTML"
        )


# =========================================================
# BOT ADDED / REMOVED TRACKING
# =========================================================

async def my_chat_member_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    chat = update.effective_chat
    member_update = update.my_chat_member

    if not chat or not member_update:
        return

    state = context.application.bot_data[
        "state"
    ]

    new_status = (
        member_update.new_chat_member.status
    )

    if new_status in (
        "member",
        "administrator",
        "creator"
    ):
        changed = record_chat(
            state,
            chat
        )

        if changed:
            await save_state(context)

        logger.info(
            "Bot active in %s | %s | %s",
            chat.type,
            getattr(chat, "title", "Unknown"),
            chat.id
        )

    elif new_status in (
        "left",
        "kicked"
    ):
        changed = remove_chat(
            state,
            chat
        )

        if changed:
            await save_state(context)

        logger.info(
            "Bot removed from %s | %s | %s",
            chat.type,
            getattr(chat, "title", "Unknown"),
            chat.id
        )


# =========================================================
# INCOMING MESSAGE / AUTOMATIC REACTION
# =========================================================

async def handle_incoming(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    target = update.channel_post or update.message

    if not target or not update.effective_chat:
        return

    chat = update.effective_chat

    if chat.type not in (
        "channel",
        "group",
        "supergroup"
    ):
        return

    state = context.application.bot_data[
        "state"
    ]

    changed = record_chat(
        state,
        chat
    )

    if changed:
        await save_state(context)

    await asyncio.sleep(
        random.uniform(0.1, 1.0)
    )

    # Try a different random reaction after a normal failure.
    # Do not retry the same emoji five times.
    reactions_to_try = random.sample(
        REACTION_EMOJIS,
        k=len(REACTION_EMOJIS)
    )

    for reaction in reactions_to_try:
        try:
            await context.bot.set_message_reaction(
                chat_id=target.chat_id,
                message_id=target.message_id,
                reaction=reaction,
                is_big=True
            )
            return

        except RetryAfter as e:
            wait_seconds = max(
                1,
                int(e.retry_after)
            )

            logger.warning(
                "Reaction rate limited; waiting %ss before retrying %s.",
                wait_seconds,
                reaction
            )

            await asyncio.sleep(
                wait_seconds
            )

            # A rate limit is not an unsupported-reaction error, so retry
            # the same emoji once after Telegram's requested wait.
            try:
                await context.bot.set_message_reaction(
                    chat_id=target.chat_id,
                    message_id=target.message_id,
                    reaction=reaction,
                    is_big=True
                )
                return
            except Exception as retry_error:
                logger.warning(
                    "Reaction retry failed for %s: %s",
                    reaction,
                    retry_error
                )
                continue

        except Exception as e:
            logger.warning(
                "Reaction failed for %s, trying another emoji: %s",
                reaction,
                e
            )

            # Try the next random emoji.
            continue


# =========================================================
# ERROR HANDLER
# =========================================================

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE
):
    logger.error(
        "Unhandled Telegram error: %s",
        context.error,
        exc_info=context.error
    )


# =========================================================
# INITIAL FIREBASE SAVE
# =========================================================

async def save_state_for_app(app):
    firebase_ref = app.bot_data.get(
        "firebase_ref"
    )

    state = app.bot_data.get(
        "state"
    )

    if firebase_ref is None or state is None:
        return

    try:
        await asyncio.to_thread(
            firebase_ref.set,
            copy.deepcopy(state)
        )
    except Exception:
        logger.exception(
            "Initial Firebase state save failed."
        )


# =========================================================
# BUILD ONE BOT
# =========================================================

async def build_bot(
    token_index,
    token
):
    app = (
        Application.builder()
        .token(token)
        .concurrent_updates(True)
        .build()
    )

    await app.initialize()

    me = await app.bot.get_me()

    bot_id = me.id
    bot_username = me.username or f"bot_{bot_id}"

    # Each bot gets its own Firebase branch.
    firebase_ref = db.reference(
        f"reaction_bots/{bot_id}"
    )

    state = await load_state(
        firebase_ref,
        bot_id,
        bot_username
    )

    # Refresh current identity on every startup.
    state["bot_id"] = bot_id
    state["bot_username"] = bot_username

    app.bot_data["firebase_ref"] = firebase_ref
    app.bot_data["state"] = state
    app.bot_data["save_lock"] = asyncio.Lock()
    app.bot_data["start_time"] = time.time()
    app.bot_data["token_index"] = token_index

    await save_state_for_app(
        app
    )

    # Commands
    app.add_handler(
        CommandHandler(
            "start",
            start_command
        )
    )

    app.add_handler(
        CommandHandler(
            "stats",
            stats_command
        )
    )

    # Callback buttons
    app.add_handler(
        CallbackQueryHandler(
            how_to_use_callback,
            pattern=r"^how_to_use$"
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            all_channels_callback,
            pattern=r"^all_channels:\d+$"
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            back_to_menu_callback,
            pattern=r"^back_to_menu$"
        )
    )

    # Bot added/removed from a chat.
    app.add_handler(
        ChatMemberHandler(
            my_chat_member_handler,
            ChatMemberHandler.MY_CHAT_MEMBER
        )
    )

    # Channel posts + group/supergroup messages.
    app.add_handler(
        MessageHandler(
            filters.ChatType.CHANNEL
            | filters.ChatType.GROUPS,
            handle_incoming
        )
    )

    app.add_error_handler(
        error_handler
    )

    return app


# =========================================================
# MANAGER BOT
# =========================================================

def manager_menu():
    keyboard = [
        [
            InlineKeyboardButton("➕ Add Reaction Bot", callback_data="mgr:add"),
            InlineKeyboardButton("🤖 My Bots", callback_data="mgr:list:0"),
        ],
        [
            InlineKeyboardButton("📊 Manager Stats", callback_data="mgr:stats"),
            InlineKeyboardButton("🔄 Refresh", callback_data="mgr:home"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


def manager_home_text(first_name):
    safe_name = html.escape(first_name or "Owner")
    return (
        "╭━━━━━━━━━━━━━━━━━━━━╮\n"
        "   ✦ <b>REACTION BOT MANAGER</b> ✦\n"
        "╰━━━━━━━━━━━━━━━━━━━━╯\n\n"
        f"Hey <b>{safe_name}</b> 👋\n\n"
        "Control all of your Reaction Bots from one place.\n\n"
        "➕ Add a bot using its BotFather token\n"
        "▶️ Start / ⏸ Stop individual bots\n"
        "🔄 Restart a bot\n"
        "🗑 Remove a bot from the manager\n\n"
        "Each managed bot keeps its own reaction identity and its own Firebase state."
    )


def manager_is_owner(update):
    user = update.effective_user
    return bool(user and user.id == OWNER_ID)


async def manager_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not manager_is_owner(update):
        if update.message:
            await update.message.reply_text("⛔ This Manager Bot is owner only.")
        return

    MANAGER_PENDING_ADD.discard(OWNER_ID)
    first_name = update.effective_user.first_name if update.effective_user else "Owner"
    if update.message:
        await update.message.reply_text(
            manager_home_text(first_name),
            parse_mode="HTML",
            reply_markup=manager_menu(),
        )


async def manager_home_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not manager_is_owner(update):
        await update.callback_query.answer("Owner only.", show_alert=True)
        return

    await update.callback_query.answer()
    MANAGER_PENDING_ADD.discard(OWNER_ID)
    first_name = update.effective_user.first_name if update.effective_user else "Owner"
    msg = update.callback_query.message
    if not msg:
        return

    text = manager_home_text(first_name)
    try:
        await msg.edit_text(text, parse_mode="HTML", reply_markup=manager_menu())
    except Exception:
        await msg.reply_text(text, parse_mode="HTML", reply_markup=manager_menu())


async def manager_add_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not manager_is_owner(update):
        await update.callback_query.answer("Owner only.", show_alert=True)
        return

    await update.callback_query.answer()
    MANAGER_PENDING_ADD.add(OWNER_ID)

    msg = update.callback_query.message
    if msg:
        await msg.edit_text(
            "╭━━━━━━━━━━━━━━━━━━━━╮\n"
            "   ✦ <b>ADD REACTION BOT</b> ✦\n"
            "╰━━━━━━━━━━━━━━━━━━━━╯\n\n"
            "Send the <b>BotFather Bot Token</b> of the Reaction Bot you want to add.\n\n"
            "🔐 The token will be encrypted before it is stored.\n"
            "🗑️ After receiving it, the Manager will try to delete your token message.\n\n"
            "‹ Use /start to cancel."
            ,
            parse_mode="HTML",
        )


async def manager_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not manager_is_owner(update):
        return
    if not update.effective_chat or update.effective_chat.type != "private":
        return
    if OWNER_ID not in MANAGER_PENDING_ADD:
        return
    if not update.message or not update.message.text:
        return

    MANAGER_PENDING_ADD.discard(OWNER_ID)
    token = update.message.text.strip()

    # Remove the secret from the Manager chat as soon as possible.
    try:
        await update.message.delete()
    except Exception:
        logger.warning("Could not delete the submitted token message.")

    if not token or ":" not in token:
        await context.bot.send_message(
            OWNER_ID,
            "❌ That does not look like a valid BotFather token. Try again with ➕ Add Reaction Bot."
        )
        return

    await context.bot.send_message(
        OWNER_ID,
        "⏳ Validating the bot token and preparing its Reaction Bot instance..."
    )

    app = None
    bot_id = None
    try:
        # build_bot initializes + calls get_me(), so the token is validated by Telegram
        # before it is stored in the manager registry.
        app = await build_bot("dynamic", token)
        bot_id = app.bot.id
        bot_username = app.bot.username or f"bot_{bot_id}"

        registry = await get_managed_bots()
        existing = registry.get(str(bot_id))
        if existing:
            await stop_application(app)
            await context.bot.send_message(
                OWNER_ID,
                f"⚠️ @{bot_username} is already registered in the Manager."
            )
            return

        record = {
            "bot_id": bot_id,
            "bot_username": bot_username,
            "token": encrypt_token(token),
            "enabled": True,
            "added_by": OWNER_ID,
            "added_at": int(time.time()),
        }
        await save_managed_bot(bot_id, record)

        try:
            await start_application(app)
            RUNNING_BOTS[bot_id] = app
        except Exception:
            record["enabled"] = False
            await save_managed_bot(bot_id, record)
            await stop_application(app)
            raise

        await context.bot.send_message(
            OWNER_ID,
            "╭━━━━━━━━━━━━━━━━━━━━╮\n"
            "   ✦ <b>BOT ADDED SUCCESSFULLY</b> ✦\n"
            "╰━━━━━━━━━━━━━━━━━━━━╯\n\n"
            f"🤖 <b>Bot:</b> @{html.escape(bot_username)}\n"
            f"🆔 <b>ID:</b> <code>{bot_id}</code>\n"
            "🟢 <b>Status:</b> Running\n\n"
            "Now add this bot to your Channel/Group and give it the same permissions required by your original Reaction Bot."
            ,
            parse_mode="HTML",
            reply_markup=manager_menu(),
        )

    except Exception as e:
        if app is not None:
            try:
                await stop_application(app)
            except Exception:
                pass
        logger.exception("Failed to add managed bot.")
        await context.bot.send_message(
            OWNER_ID,
            "❌ Failed to add this bot. Make sure the BotFather token is correct and the Manager's Firebase/encryption settings are configured."
        )


async def build_manager_bot():
    if not MANAGER_BOT_TOKEN:
        raise RuntimeError("Missing environment variable: MANAGER_BOT_TOKEN")
    if not TOKEN_ENCRYPTION_SECRET:
        raise RuntimeError("Missing environment variable: TOKEN_ENCRYPTION_SECRET")

    app = (
        Application.builder()
        .token(MANAGER_BOT_TOKEN)
        .concurrent_updates(True)
        .build()
    )

    await app.initialize()
    me = await app.bot.get_me()
    logger.info("Manager bot ready: @%s", me.username)

    app.add_handler(CommandHandler("start", manager_start))
    app.add_handler(CallbackQueryHandler(manager_add_callback, pattern=r"^mgr:add$"))
    app.add_handler(CallbackQueryHandler(manager_home_callback, pattern=r"^mgr:home$"))
    app.add_handler(CallbackQueryHandler(manager_list_callback, pattern=r"^mgr:list:\d+$"))
    app.add_handler(CallbackQueryHandler(manager_stats_callback, pattern=r"^mgr:stats$"))
    app.add_handler(CallbackQueryHandler(manager_bot_action_callback, pattern=r"^mgrbot:\d+:(start|stop|restart|remove)$"))
    app.add_handler(MessageHandler(filters.TEXT & filters.ChatType.PRIVATE, manager_text_handler))
    app.add_error_handler(error_handler)
    return app


async def start_application(app):
    await app.start()
    if app.updater is None:
        raise RuntimeError("Telegram updater is unavailable.")
    await app.updater.start_polling(drop_pending_updates=True)


async def stop_application(app):
    try:
        if app.updater and app.updater.running:
            await app.updater.stop()
    finally:
        if app.running:
            await app.stop()
        await app.shutdown()


async def start_managed_bot_from_record(record):
    token_value = record.get("token")
    if not token_value:
        raise RuntimeError("Managed bot record has no encrypted token.")

    token = decrypt_token(token_value)
    app = await build_bot("dynamic", token)
    bot_id = app.bot.id

    await start_application(app)
    RUNNING_BOTS[bot_id] = app
    return app


async def start_all_managed_bots():
    registry = await get_managed_bots()
    if not registry:
        logger.info("No managed reaction bots registered yet.")
        return

    for bot_key, record in registry.items():
        if not isinstance(record, dict) or not record.get("enabled", False):
            continue

        bot_id = record.get("bot_id") or bot_key
        try:
            app = await start_managed_bot_from_record(record)
            actual_id = app.bot.id
            actual_username = app.bot.username or f"bot_{actual_id}"
            if str(actual_id) != str(bot_key):
                record["bot_id"] = actual_id
                record["bot_username"] = actual_username
                await save_managed_bot(actual_id, record)
            logger.info("Managed reaction bot started: @%s", actual_username)
        except Exception:
            logger.exception("Failed to start managed bot %s", bot_id)
            try:
                record["enabled"] = False
                await save_managed_bot(bot_id, record)
            except Exception:
                logger.exception("Failed to disable broken managed bot %s", bot_id)


async def stop_managed_bot(bot_id):
    app = RUNNING_BOTS.pop(int(bot_id), None)
    if app is None:
        app = RUNNING_BOTS.pop(str(bot_id), None)
    if app is not None:
        await stop_application(app)


async def render_bot_list(page=0):
    registry = await get_managed_bots()
    items = []
    for key, record in registry.items():
        if not isinstance(record, dict):
            continue
        bot_id = int(record.get("bot_id", key))
        username = record.get("bot_username") or f"bot_{bot_id}"
        enabled = bool(record.get("enabled", False))
        running = bot_id in RUNNING_BOTS
        items.append({
            "id": bot_id,
            "username": username,
            "enabled": enabled,
            "running": running,
        })

    items.sort(key=lambda x: x["username"].lower())
    per_page = 5
    total_pages = max(1, (len(items) + per_page - 1) // per_page)
    page = max(0, min(page, total_pages - 1))
    page_items = items[page * per_page:(page + 1) * per_page]

    text = (
        "╭━━━━━━━━━━━━━━━━━━━━╮\n"
        "   ✦ <b>MY REACTION BOTS</b> ✦\n"
        "╰━━━━━━━━━━━━━━━━━━━━╯\n\n"
    )
    if not items:
        text += "No Reaction Bots have been added yet."
    else:
        text += f"📊 <b>Total:</b> <code>{len(items)}</code>\n\n"
        for index, item in enumerate(page_items, start=page * per_page + 1):
            status = "🟢 Running" if item["running"] else ("🟡 Stopped" if item["enabled"] else "⚪ Disabled")
            text += (
                f"<b>{index}. @{html.escape(item['username'])}</b>\n"
                f"   🆔 <code>{item['id']}</code> • {status}\n\n"
            )

    keyboard = []
    for item in page_items:
        action = "stop" if item["running"] else "start"
        label = "⏸ Stop" if item["running"] else "▶️ Start"
        keyboard.append([
            InlineKeyboardButton(
                f"@{item['username']} • {label}",
                callback_data=f"mgrbot:{item['id']}:{action}"
            )
        ])
        keyboard.append([
            InlineKeyboardButton("🔄 Restart", callback_data=f"mgrbot:{item['id']}:restart"),
            InlineKeyboardButton("🗑 Remove", callback_data=f"mgrbot:{item['id']}:remove"),
        ])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("‹ Previous", callback_data=f"mgr:list:{page - 1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("Next ›", callback_data=f"mgr:list:{page + 1}"))
    if nav:
        keyboard.append(nav)

    keyboard.append([
        InlineKeyboardButton("➕ Add Bot", callback_data="mgr:add"),
        InlineKeyboardButton("‹ Main Menu", callback_data="mgr:home"),
    ])
    return text + (f"\n📄 <b>Page:</b> {page + 1}/{total_pages}" if items else ""), InlineKeyboardMarkup(keyboard)


async def manager_list_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not manager_is_owner(update):
        await update.callback_query.answer("Owner only.", show_alert=True)
        return
    await update.callback_query.answer()
    try:
        page = int(update.callback_query.data.split(":")[-1])
    except Exception:
        page = 0
    text, markup = await render_bot_list(page)
    msg = update.callback_query.message
    if msg:
        try:
            await msg.edit_text(text, parse_mode="HTML", reply_markup=markup)
        except Exception:
            await msg.reply_text(text, parse_mode="HTML", reply_markup=markup)


async def manager_stats_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not manager_is_owner(update):
        await update.callback_query.answer("Owner only.", show_alert=True)
        return
    await update.callback_query.answer()

    registry = await get_managed_bots()
    total = len(registry)
    running = sum(1 for key, r in registry.items() if isinstance(r, dict) and int(r.get("bot_id", key)) in RUNNING_BOTS)
    enabled = sum(1 for r in registry.values() if isinstance(r, dict) and r.get("enabled", False))

    text = (
        "╭━━━━━━━━━━━━━━━━━━━━╮\n"
        "   ✦ <b>MANAGER STATISTICS</b> ✦\n"
        "╰━━━━━━━━━━━━━━━━━━━━╯\n\n"
        f"🤖 <b>Total Bots:</b> <code>{total}</code>\n"
        f"🟢 <b>Running:</b> <code>{running}</code>\n"
        f"🟡 <b>Enabled:</b> <code>{enabled}</code>\n"
        f"⚪ <b>Stopped/Disabled:</b> <code>{max(0, total - running)}</code>\n"
    )
    msg = update.callback_query.message
    if msg:
        await msg.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("‹ Main Menu", callback_data="mgr:home")]
            ]),
        )


async def manager_bot_action_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not manager_is_owner(update):
        await update.callback_query.answer("Owner only.", show_alert=True)
        return

    await update.callback_query.answer()
    parts = update.callback_query.data.split(":")
    bot_id = int(parts[1])
    action = parts[2]
    registry = await get_managed_bots()
    record = registry.get(str(bot_id))
    if not record:
        await update.callback_query.message.reply_text("❌ Bot record not found.")
        return

    username = record.get("bot_username") or f"bot_{bot_id}"

    try:
        if action == "start":
            if bot_id in RUNNING_BOTS:
                await update.callback_query.message.reply_text(f"ℹ️ @{username} is already running.")
            else:
                app = await start_managed_bot_from_record(record)
                RUNNING_BOTS[bot_id] = app
                record["enabled"] = True
                await save_managed_bot(bot_id, record)
                await update.callback_query.message.reply_text(f"🟢 @{username} started.")

        elif action == "stop":
            await stop_managed_bot(bot_id)
            record["enabled"] = False
            await save_managed_bot(bot_id, record)
            await update.callback_query.message.reply_text(f"⏸ @{username} stopped.")

        elif action == "restart":
            await stop_managed_bot(bot_id)
            app = await start_managed_bot_from_record(record)
            RUNNING_BOTS[bot_id] = app
            record["enabled"] = True
            await save_managed_bot(bot_id, record)
            await update.callback_query.message.reply_text(f"🔄 @{username} restarted.")

        elif action == "remove":
            await stop_managed_bot(bot_id)
            await delete_managed_bot(bot_id)
            await update.callback_query.message.reply_text(
                f"🗑 @{username} removed from the Manager.\n\nFirebase reaction state is kept under reaction_bots/{bot_id}."
            )

    except Exception:
        logger.exception("Manager action failed for bot %s", bot_id)
        if action in ("start", "restart"):
            record["enabled"] = False
            try:
                await save_managed_bot(bot_id, record)
            except Exception:
                pass
        await update.callback_query.message.reply_text(
            f"❌ Could not {action} @{username}. Check the Render logs for the exact Telegram/Firebase error."
        )


# =========================================================
# MAIN
# =========================================================

async def main():
    Thread(
        target=run_server,
        daemon=True
    ).start()

    try:
        init_firebase()
    except Exception:
        logger.exception("Firebase initialization failed.")
        return

    if not MANAGER_BOT_TOKEN:
        logger.error("Missing MANAGER_BOT_TOKEN.")
        return

    if not TOKEN_ENCRYPTION_SECRET:
        logger.error("Missing TOKEN_ENCRYPTION_SECRET.")
        return

    try:
        manager = await build_manager_bot()
        await start_all_managed_bots()
        await start_application(manager)

        logger.info(
            "Manager running. Managed reaction bots: %s",
            len(RUNNING_BOTS)
        )

        await asyncio.Event().wait()

    finally:
        for bot_id in list(RUNNING_BOTS.keys()):
            try:
                await stop_managed_bot(bot_id)
            except Exception:
                logger.exception(
                    "Failed to stop managed bot %s",
                    bot_id
                )


# =========================================================
# ENTRY POINT
# =========================================================

if __name__ == "__main__":
    asyncio.run(main())
