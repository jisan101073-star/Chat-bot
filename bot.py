import os
import random
import logging
import asyncio
import time
import html
import json
import copy
import re
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

# Short in-memory cache so UI/Manager button presses do not hit Firebase
# on every click. Writes always invalidate the cache immediately.
MANAGED_BOTS_CACHE = None
MANAGED_BOTS_CACHE_EXPIRES = 0.0
MANAGED_BOTS_CACHE_TTL = 12.0

# Limits startup bursts so one Render instance does not try to initialize
# every managed bot at exactly the same moment.
MANAGED_BOT_START_CONCURRENCY = 2
MANAGED_BOT_START_DELAY = 0.35

# Analytics counters are kept in memory and flushed periodically so a busy
# reaction bot does not write to Firebase for every message.
METRICS_SAVE_INTERVAL = 60.0


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


async def get_managed_bots(force=False):
    global MANAGED_BOTS_CACHE, MANAGED_BOTS_CACHE_EXPIRES

    now = time.monotonic()
    if (
        not force
        and MANAGED_BOTS_CACHE is not None
        and now < MANAGED_BOTS_CACHE_EXPIRES
    ):
        return copy.deepcopy(MANAGED_BOTS_CACHE)

    try:
        data = await asyncio.to_thread(
            managed_bots_ref().get
        )
    except Exception:
        logger.exception("Failed to read managed bot registry.")
        return copy.deepcopy(MANAGED_BOTS_CACHE or {})

    if not isinstance(data, dict):
        data = {}

    MANAGED_BOTS_CACHE = copy.deepcopy(data)
    MANAGED_BOTS_CACHE_EXPIRES = now + MANAGED_BOTS_CACHE_TTL
    return copy.deepcopy(data)


def invalidate_managed_bots_cache():
    global MANAGED_BOTS_CACHE_EXPIRES
    MANAGED_BOTS_CACHE_EXPIRES = 0.0


async def save_managed_bot(bot_id, data):
    await asyncio.to_thread(
        managed_bots_ref().child(str(bot_id)).set,
        data
    )
    invalidate_managed_bots_cache()


async def delete_managed_bot(bot_id):
    await asyncio.to_thread(
        managed_bots_ref().child(str(bot_id)).delete
    )
    invalidate_managed_bots_cache()


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
        "metrics": {
            "messages_seen": 0,
            "reactions_sent": 0,
            "reaction_failures": 0,
        },
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

    metrics = data.get("metrics", {})
    if not isinstance(metrics, dict):
        metrics = {}

    def metric_int(name):
        try:
            value = int(metrics.get(name, 0))
            return max(0, value)
        except (TypeError, ValueError):
            return 0

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
        "metrics": {
            "messages_seen": metric_int("messages_seen"),
            "reactions_sent": metric_int("reactions_sent"),
            "reaction_failures": metric_int("reaction_failures"),
        },
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


async def schedule_state_save(context, delay=1.25):
    """Batch closely spaced state changes into one Firebase write."""
    existing = context.application.bot_data.get("state_save_task")
    if existing and not existing.done():
        return

    async def _flush():
        try:
            await asyncio.sleep(delay)
            await save_state(context)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Debounced Firebase save failed.")

    context.application.bot_data["state_save_task"] = asyncio.create_task(_flush())


def increment_metric(context, name, amount=1):
    """Update per-bot analytics in RAM; a shared 60-second flusher persists it."""
    state = context.application.bot_data.get("state")
    if not isinstance(state, dict):
        return

    metrics = state.setdefault("metrics", {})
    try:
        metrics[name] = max(0, int(metrics.get(name, 0)) + int(amount))
    except (TypeError, ValueError):
        metrics[name] = max(0, int(amount))

    # Revision lets the periodic flusher detect updates that happen while a
    # Firebase write is in progress, so no fresh metric increment is lost.
    context.application.bot_data["metrics_dirty"] = True
    context.application.bot_data["metrics_revision"] = int(
        context.application.bot_data.get("metrics_revision", 0)
    ) + 1


def get_metric_int(state, name):
    try:
        return max(0, int((state.get("metrics") or {}).get(name, 0)))
    except (TypeError, ValueError):
        return 0


def format_compact_number(value):
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = 0
    if value < 1000:
        return str(value)
    if value < 1_000_000:
        return f"{value / 1000:.1f}K".rstrip("0").rstrip(".")
    if value < 1_000_000_000:
        return f"{value / 1_000_000:.1f}M".rstrip("0").rstrip(".")
    return f"{value / 1_000_000_000:.1f}B".rstrip("0").rstrip(".")


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


def get_user_display_name(user):
    """Return the caller's Telegram display name, preserving styled Unicode."""
    if not user:
        return "there"

    first = str(getattr(user, "first_name", "") or "").strip()
    last = str(getattr(user, "last_name", "") or "").strip()
    name = " ".join(part for part in (first, last) if part)
    return name or "there"

# =========================================================
# MAIN MENU — PREMIUM UI
# =========================================================

REACTION_BOT_USERNAME_RE = re.compile(
    r"^Jr_Auto_Reaction_(\d+)_Bot$",
    re.IGNORECASE
)


def reaction_bot_serial(username):
    """Return the numeric serial from Jr_Auto_Reaction_<N>_Bot."""
    clean = str(username or "").strip().lstrip("@")
    match = REACTION_BOT_USERNAME_RE.fullmatch(clean)
    if not match:
        return None
    try:
        return int(match.group(1))
    except (TypeError, ValueError):
        return None


def get_serial_reaction_bots(
    registry,
    current_username=None,
    enabled_only=True
):
    """
    Only return numbered JR reaction bots, ordered by their numeric serial.

    Accepted username format:
        @Jr_Auto_Reaction_1_Bot
        @Jr_Auto_Reaction_2_Bot
        ...

    Any other username is intentionally ignored.
    """
    current = str(current_username or "").strip().lstrip("@").lower()
    found = {}

    for key, record in (registry or {}).items():
        if not isinstance(record, dict):
            continue

        username = str(record.get("bot_username") or "").strip().lstrip("@")
        serial = reaction_bot_serial(username)
        if serial is None:
            continue

        if username.lower() == current:
            continue

        if enabled_only and not record.get("enabled", False):
            continue

        # One username/serial should appear only once even if Firebase has
        # a duplicate record. Keep the first valid record encountered.
        found.setdefault(
            serial,
            {
                "serial": serial,
                "username": username,
                "bot_id": record.get("bot_id", key),
            }
        )

    return [
        found[number]
        for number in sorted(found)
    ]


def get_main_menu(
    user_name,
    bot_username,
    user_id
):
    display_name = html.escape(
        str(user_name or "there").strip()
    )
    welcome_text = (
        "<b>𝐇𝐞𝐲! 👋</b>\n"
        f"×º°”˜ {display_name} ˜”°º× 😎\n\n"
        "🌹 <b>⟨ 𝐉𝐑 ⟩ 𝐀𝐮𝐭𝐨 𝐑𝐞𝐚𝐜𝐭𝐢𝐨𝐧</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "✅ <b>𝐉𝐨𝐢𝐧 𝐨𝐟𝐟𝐢𝐜𝐢𝐚𝐥 𝐂𝐡𝐚𝐧𝐧𝐞𝐥</b> 👉\n"
        f'<a href="{SUPPORT_URL}">https://t.me/Jr_Auto_ReactionBot</a> ✅\n\n'
        "I can seamlessly react to messages in channels or groups. "
        "Just add me as an <b>admin</b>! ✨\n"
    )

    keyboard = [
        [
            InlineKeyboardButton(
                "📣 𝐀𝐃𝐃 𝐓𝐎 𝐂𝐇𝐀𝐍𝐍𝐄𝐋",
                url=(
                    f"https://t.me/"
                    f"{bot_username}"
                    f"?startchannel=true"
                ),
                style="primary"
            ),
            InlineKeyboardButton(
                "👥 𝐀𝐃𝐃 𝐓𝐎 𝐆𝐑𝐎𝐔𝐏",
                url=(
                    f"https://t.me/"
                    f"{bot_username}"
                    f"?startgroup=true"
                ),
                style="success"
            )
        ],
        [
            InlineKeyboardButton(
                "📘 𝐇𝐎𝐖 𝐓𝐎 𝐔𝐒𝐄",
                callback_data="how_to_use",
                style="primary"
            )
        ],
        [
            InlineKeyboardButton(
                "🤖 𝐌𝐎𝐑𝐄 𝐁𝐎𝐓𝐒",
                callback_data="more_bots",
                style="success"
            )
        ]
    ]

    if user_id == OWNER_ID:
        keyboard.append(
            [
                InlineKeyboardButton(
                    "📢 𝐀𝐋𝐋 𝐂𝐇𝐀𝐍𝐍𝐄𝐋𝐒",
                    callback_data="all_channels:0",
                    style="primary"
                ),
                InlineKeyboardButton(
                    "👥 𝐀𝐋𝐋 𝐆𝐑𝐎𝐔𝐏𝐒",
                    callback_data="all_groups:0",
                    style="success"
                )
            ]
        )

    keyboard.append(
        [
            InlineKeyboardButton(
                "💬 𝐒𝐔𝐏𝐏𝐎𝐑𝐓",
                url=SUPPORT_URL,
                style="primary"
            )
        ]
    )

    return (
        welcome_text,
        InlineKeyboardMarkup(keyboard)
    )


# =========================================================
# MORE BOTS — NUMBERED JR BOTS ONLY
# =========================================================

async def more_bots_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    if not query:
        return

    await query.answer()

    registry = await get_managed_bots()
    current_username = context.bot.username or ""
    bots = get_serial_reaction_bots(
        registry,
        current_username=current_username,
        enabled_only=True
    )

    text = (
        "╭━━━━━━━━━━━━━━━━━━━━╮\n"
        "     ✦ <b>MORE 𝐉𝐑 𝐑𝐄𝐀𝐂𝐓𝐈𝐎𝐍 𝐁𝐎𝐓𝐒</b> ✦\n"
        "╰━━━━━━━━━━━━━━━━━━━━╯\n\n"
        "🌹 <i>Choose your Reaction Bot</i>\n"
        "━━━━━━━━━━━━━━━━━━━━"
    )

    keyboard = []
    for item in bots:
        serial = item["serial"]
        username = item["username"]
        keyboard.append([
            InlineKeyboardButton(
                f"{serial}. 🤖 @{username}",
                url=f"https://t.me/{username}",
                style="success"
            )
        ])

    if not bots:
        text += (
            "\n\n❌ <i>No numbered Reaction Bots are active yet.</i>"
        )

    keyboard.append([
        InlineKeyboardButton(
            "‹ BACK TO MENU",
            callback_data="back_to_menu",
            style="primary"
        )
    ])

    markup = InlineKeyboardMarkup(keyboard)
    msg = query.message
    if not msg:
        return

    if msg.photo:
        try:
            await msg.edit_caption(
                caption=text,
                parse_mode="HTML",
                reply_markup=markup
            )
            return
        except Exception as e:
            logger.warning("More bots caption edit failed: %s", e)

    try:
        await msg.edit_text(
            text=text,
            parse_mode="HTML",
            reply_markup=markup,
            disable_web_page_preview=True
        )
    except Exception as e:
        logger.warning("More bots text edit failed: %s", e)


# =========================================================
# BOT ANALYTICS
# =========================================================



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

        first_name = get_user_display_name(update.effective_user)

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

    first_name = get_user_display_name(update.effective_user)

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
        "📘 <b>HOW TO USE — JR AUTO REACTION</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "<b>1. ADD THE BOT</b>\n"
        "• Add me to your Telegram Channel or Group.\n"
        "• Give me the required admin permissions.\n"
        "• Make sure reactions are allowed in the chat.\n\n"
        "<b>2. SUPPORTED REACTIONS</b>\n"
        "👍 👎 ❤️ 🔥 🥰 👏 😁 🤔 🤯 😱 🤬 😢\n"
        "🎉 🤩 🤮 💩 🙏 👌 🕊 🤡 🥱 🥴 😍 🐳\n"
        "❤️‍🔥 🌚 🌭 💯 🤣 ⚡ 🍌 🏆 💔 🤨 😐 🍓\n"
        "🍾 💋 🖕 😈 😴 😭 🤓 👻 👨‍💻 👀 🎃 🙈\n"
        "😇 😨 🤝 ✍ 🤗 🫡 🎅 🎄 ☃ 💅 🤪 🗿 🆒\n"
        "💘 🙉 🦄 😘 💊 🙊 😎 👾 🤷‍♂️ 🤷 🤷‍♀️ 😡\n\n"
        "<b>3. AUTOMATIC REACTION</b>\n"
        "After setup, I automatically choose a random supported reaction for new messages. ⚡\n\n"
        "<b>⚠️ NOT REACTING?</b>\n"
        "Check that the bot is still an admin and that the selected reactions are enabled in your Channel or Group.\n\n"
        "✨ <i>That's it — add me, allow reactions, and you're ready.</i>"
    )
    keyboard = [
        [
            InlineKeyboardButton(
                "‹  BACK TO MENU",
                callback_data="back_to_menu",
                style="primary"
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
# ALL GROUPS
# =========================================================

def build_all_groups_page(
    state,
    page
):
    groups = list(
        state.get(
            "groups",
            {}
        ).values()
    )

    groups.sort(
        key=lambda x: str(
            x.get(
                "title",
                ""
            )
        ).lower()
    )

    per_page = 5
    total = len(groups)
    total_pages = max(
        1,
        (total + per_page - 1) // per_page
    )

    page = max(
        0,
        min(page, total_pages - 1)
    )

    start = page * per_page
    page_groups = groups[start:start + per_page]

    text = (
        "╭━━━━━━━━━━━━━━━━━━━━╮\n"
        "     ✦ <b>👥 ALL GROUPS</b> ✦\n"
        "╰━━━━━━━━━━━━━━━━━━━━╯\n\n"
    )

    if not groups:
        text += (
            "❌ <b>No groups recorded yet.</b>\n\n"
            "The bot will automatically save a group when it receives a new group update."
        )
    else:
        text += f"📊 <b>Total:</b> <code>{total}</code>\n\n"

        for number, group in enumerate(
            page_groups,
            start=start + 1
        ):
            title = html.escape(
                truncate_text(
                    group.get(
                        "title",
                        "Unknown Group"
                    ),
                    45
                )
            )

            username = str(group.get("username") or "").strip()
            group_id = group.get("id", "")

            username_text = (
                "@" + html.escape(truncate_text(username, 35))
                if username
                else "🔒 Private / No Username"
            )

            text += (
                f"<b>{number}. {title}</b>\n"
                f"   👤 <code>{username_text}</code>\n"
                f"   🆔 <code>{group_id}</code>\n\n"
            )

        text += f"📄 <b>Page:</b> {page + 1}/{total_pages}"

    keyboard = []
    navigation = []

    if page > 0:
        navigation.append(
            InlineKeyboardButton(
                "‹ Previous",
                callback_data=f"all_groups:{page - 1}"
            )
        )

    if page < total_pages - 1:
        navigation.append(
            InlineKeyboardButton(
                "Next ›",
                callback_data=f"all_groups:{page + 1}"
            )
        )

    if navigation:
        keyboard.append(navigation)

    keyboard.append([
        InlineKeyboardButton(
            "‹ BACK TO MENU",
            callback_data="back_to_menu"
        )
    ])

    return text, InlineKeyboardMarkup(keyboard)


async def all_groups_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    if not query:
        return

    if not query.from_user or query.from_user.id != OWNER_ID:
        await query.answer(
            "This section is owner only.",
            show_alert=True
        )
        return

    await query.answer()

    try:
        page = int(query.data.split(":")[-1])
    except Exception:
        page = 0

    state = context.application.bot_data.get(
        "state",
        create_empty_state(
            getattr(context.bot, "id", None),
            getattr(context.bot, "username", None)
        )
    )

    text, markup = build_all_groups_page(
        state,
        page
    )

    msg = query.message
    if not msg:
        return

    if msg.photo:
        try:
            await msg.edit_caption(
                caption=text,
                parse_mode="HTML",
                reply_markup=markup
            )
            return
        except Exception as e:
            logger.warning("Groups caption edit failed: %s", e)

    await msg.edit_text(
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

    first_name = get_user_display_name(query.from_user)

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

    increment_metric(context, "messages_seen")

    changed = record_chat(
        state,
        chat
    )

    if changed:
        await schedule_state_save(context)

    await asyncio.sleep(
        random.uniform(0.1, 1.0)
    )

    reaction_semaphore = context.application.bot_data.get(
        "reaction_semaphore"
    )
    if reaction_semaphore is None:
        reaction_semaphore = asyncio.Semaphore(3)
        context.application.bot_data["reaction_semaphore"] = reaction_semaphore

    async with reaction_semaphore:
        await _perform_reaction(context, target)


async def _perform_reaction(context, target):
    # Try a different random reaction after a normal failure.
    # Keep the original reaction behavior while bounding simultaneous
    # reaction requests per bot via the semaphore in handle_incoming().
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
            increment_metric(context, "reactions_sent")
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
                increment_metric(context, "reactions_sent")
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
            continue

    increment_metric(context, "reaction_failures")

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
        # Keep update handling bounded instead of allowing a burst to spawn
        # hundreds of simultaneous handler tasks for one bot.
        .concurrent_updates(4)
        # Reaction bots mostly make lightweight Bot API calls. Smaller pools
        # reduce idle per-bot resource usage while still allowing concurrency.
        .connection_pool_size(8)
        .pool_timeout(2.0)
        .get_updates_connection_pool_size(1)
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
    app.bot_data["reaction_semaphore"] = asyncio.Semaphore(3)
    app.bot_data["metrics_dirty"] = False
    app.bot_data["metrics_revision"] = 0
    state.setdefault("metrics", {})
    state["metrics"].setdefault("messages_seen", 0)
    state["metrics"].setdefault("reactions_sent", 0)
    state["metrics"].setdefault("reaction_failures", 0)

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
            all_groups_callback,
            pattern=r"^all_groups:\d+$"
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            back_to_menu_callback,
            pattern=r"^back_to_menu$"
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            more_bots_callback,
            pattern=r"^more_bots$"
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
            InlineKeyboardButton("➕ 𝐀𝐃𝐃 𝐑𝐄𝐀𝐂𝐓𝐈𝐎𝐍 𝐁𝐎𝐓", callback_data="mgr:add", style="success"),
            InlineKeyboardButton("🤖 𝐌𝐘 𝐁𝐎𝐓𝐒", callback_data="mgr:list:0", style="primary"),
        ],
        [
            InlineKeyboardButton("🔄 𝐑𝐄𝐒𝐓𝐀𝐑𝐓 𝐀𝐋𝐋", callback_data="mgr:restartall", style="primary"),
            InlineKeyboardButton("📊 𝐒𝐓𝐀𝐓𝐒", callback_data="mgr:stats", style="success"),
        ],
        [
            InlineKeyboardButton("♻️ 𝐑𝐄𝐅𝐑𝐄𝐒𝐇", callback_data="mgr:home", style="primary"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


def manager_home_text(first_name):
    safe_name = html.escape(first_name or "Owner")
    return (
        "╭━━━━━━━━━━━━━━━━━━━━╮\n"
        "      🌹 <b>⟨ 𝐉𝐑 ⟩ 𝐌𝐀𝐍𝐀𝐆𝐄𝐑</b> 🌹\n"
        "╰━━━━━━━━━━━━━━━━━━━━╯\n\n"
        f"𝐖𝐞𝐥𝐜𝐨𝐦𝐞, <b>{safe_name}</b> 👋\n\n"
        "⚡ <b>One Manager • Multiple Reaction Bots</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "➕ Add & launch Reaction Bots\n"
        "▶️ Start • ⏸ Stop • 🔄 Restart\n"
        "🗑 Remove bots from the manager\n"
        "📌 Each bot keeps its own identity & Firebase state.\n\n"
        "╰─ 𝐉𝐢𝐬𝐚𝐧𝐗 𝐑𝐞𝐚𝐜𝐭𝐢𝐨𝐧 𝐁𝐨𝐭 𝐒𝐲𝐬𝐭𝐞𝐦 🤖"
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
        .concurrent_updates(4)
        .connection_pool_size(8)
        .pool_timeout(2.0)
        .get_updates_connection_pool_size(1)
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
    app.add_handler(CallbackQueryHandler(manager_restart_all_callback, pattern=r"^mgr:restartall$"))
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
    pending_save = app.bot_data.get("state_save_task") if hasattr(app, "bot_data") else None
    if pending_save and not pending_save.done():
        pending_save.cancel()
        try:
            await pending_save
        except asyncio.CancelledError:
            pass

    # Preserve the latest analytics/state snapshot before stopping a bot.
    try:
        await save_state_for_app(app)
    except Exception:
        logger.exception("Final Firebase state save failed during shutdown.")

    try:
        if app.updater and app.updater.running:
            await app.updater.stop()
    finally:
        if app.running:
            await app.stop()
        await app.shutdown()


async def flush_metrics_once(force=False):
    """Persist each bot's analytics separately, using one Firebase multi-path update.

    The data remains under reaction_bots/<bot_id>/metrics for each bot.  Nothing is
    aggregated into the Manager Bot.  A single root update reduces HTTP/Firebase
    overhead when many reaction bots are running on the same Render service.
    """
    updates = {}
    revisions = {}
    apps = []

    for bot_id, bot_app in list(RUNNING_BOTS.items()):
        if not getattr(bot_app, "bot_data", None):
            continue

        dirty = bool(bot_app.bot_data.get("metrics_dirty", False))
        if not force and not dirty:
            continue

        state = bot_app.bot_data.get("state")
        if not isinstance(state, dict):
            continue

        firebase_ref = bot_app.bot_data.get("firebase_ref")
        if firebase_ref is None:
            continue

        actual_bot_id = state.get("bot_id", bot_id)
        try:
            actual_bot_id = int(actual_bot_id)
        except (TypeError, ValueError):
            actual_bot_id = bot_id

        metrics = copy.deepcopy(state.get("metrics") or {})
        # Keep only numeric counters here; extra state remains untouched.
        clean_metrics = {}
        for key in ("messages_seen", "reactions_sent", "reaction_failures"):
            try:
                clean_metrics[key] = max(0, int(metrics.get(key, 0)))
            except (TypeError, ValueError):
                clean_metrics[key] = 0

        updates[f"reaction_bots/{actual_bot_id}/metrics"] = clean_metrics
        revisions[actual_bot_id] = int(
            bot_app.bot_data.get("metrics_revision", 0)
        )
        apps.append((actual_bot_id, bot_app))

    if not updates:
        return

    try:
        await asyncio.to_thread(
            db.reference("/").update,
            updates,
        )
    except Exception:
        logger.exception(
            "Periodic analytics Firebase flush failed for %d bot(s).",
            len(updates),
        )
        return

    for actual_bot_id, bot_app in apps:
        current_revision = int(
            bot_app.bot_data.get("metrics_revision", 0)
        )
        if current_revision == revisions.get(actual_bot_id):
            bot_app.bot_data["metrics_dirty"] = False

    logger.info(
        "Flushed separate analytics for %d reaction bot(s) to Firebase.",
        len(updates),
    )


async def metrics_flush_loop():
    """Flush changed bot metrics every 60 seconds without using the user's phone."""
    try:
        while True:
            await asyncio.sleep(METRICS_SAVE_INTERVAL)
            await flush_metrics_once()
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Analytics flush loop stopped unexpectedly.")


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
    registry = await get_managed_bots(force=True)
    if not registry:
        logger.info("No managed reaction bots registered yet.")
        return

    records = []
    for bot_key, record in registry.items():
        if not isinstance(record, dict) or not record.get("enabled", False):
            continue
        record = copy.deepcopy(record)
        record.setdefault("bot_id", bot_key)
        records.append(record)

    def startup_sort_key(record):
        serial = reaction_bot_serial(record.get("bot_username"))
        if serial is None:
            return (1, str(record.get("bot_username", "")).lower())
        return (0, serial, str(record.get("bot_username", "")).lower())

    records.sort(key=startup_sort_key)
    semaphore = asyncio.Semaphore(MANAGED_BOT_START_CONCURRENCY)

    async def start_one(record):
        bot_id = record.get("bot_id")
        username = record.get("bot_username") or f"bot_{bot_id}"
        async with semaphore:
            try:
                app = await start_managed_bot_from_record(record)
                actual_id = app.bot.id
                actual_username = app.bot.username or f"bot_{actual_id}"
                if str(actual_id) != str(bot_id):
                    record["bot_id"] = actual_id
                    record["bot_username"] = actual_username
                    await save_managed_bot(actual_id, record)
                    if str(bot_id) != str(actual_id):
                        try:
                            await delete_managed_bot(bot_id)
                        except Exception:
                            pass
                logger.info("Managed reaction bot started: @%s", actual_username)
            except Exception:
                logger.exception("Failed to start managed bot %s", username)
                record["enabled"] = False
                try:
                    await save_managed_bot(bot_id, record)
                except Exception:
                    logger.exception("Failed to disable broken managed bot %s", bot_id)
            finally:
                await asyncio.sleep(MANAGED_BOT_START_DELAY)

    # Small bounded batches: faster than purely serial startup, but avoids
    # a large CPU/network spike on one Render instance.
    await asyncio.gather(*(start_one(record) for record in records))


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

    def manager_sort_key(item):
        serial = reaction_bot_serial(item["username"])
        if serial is None:
            return (1, item["username"].lower())
        return (0, serial, item["username"].lower())

    items.sort(key=manager_sort_key)
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
            serial = reaction_bot_serial(item["username"])
            serial_label = f"#{serial} • " if serial is not None else ""
            text += (
                f"<b>{index}. {serial_label}@{html.escape(item['username'])}</b>\n"
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


async def manager_restart_all_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not manager_is_owner(update):
        await update.callback_query.answer("Owner only.", show_alert=True)
        return

    await update.callback_query.answer("Refreshing all managed bots…")
    msg = update.callback_query.message
    if not msg:
        return

    registry = await get_managed_bots(force=True)
    records = [
        record for record in registry.values()
        if isinstance(record, dict)
        and record.get("enabled", False)
        and record.get("token")
    ]
    records.sort(
        key=lambda record: (
            reaction_bot_serial(record.get("bot_username"))
            if reaction_bot_serial(record.get("bot_username")) is not None
            else 10**9,
            str(record.get("bot_username", "")).lower(),
        )
    )

    if not records:
        await msg.reply_text(
            "ℹ️ No enabled managed bots to restart.",
            reply_markup=manager_menu(),
        )
        return

    await msg.reply_text(
        f"🔄 <b>Refreshing {len(records)} managed bot(s)…</b>\n"
        "The manager will do this one-by-one so the Render service is not hit by a startup burst.",
        parse_mode="HTML",
    )

    success = 0
    failed = []
    for record in records:
        bot_id = int(record.get("bot_id"))
        username = record.get("bot_username") or f"bot_{bot_id}"
        try:
            await stop_managed_bot(bot_id)
            app = await start_managed_bot_from_record(record)
            RUNNING_BOTS[bot_id] = app
            success += 1
        except Exception as exc:
            logger.exception("Restart-all failed for %s", username)
            failed.append(username)
            record["enabled"] = False
            try:
                await save_managed_bot(bot_id, record)
            except Exception:
                pass

        await asyncio.sleep(MANAGED_BOT_START_DELAY)

    result = (
        "╭━━━━━━━━━━━━━━━━━━━━╮\n"
        "   ✦ <b>ALL BOTS REFRESHED</b> ✦\n"
        "╰━━━━━━━━━━━━━━━━━━━━╯\n\n"
        f"🟢 <b>Ready:</b> <code>{success}</code>\n"
        f"🔴 <b>Failed:</b> <code>{len(failed)}</code>"
    )
    if failed:
        result += "\n\n❌ " + ", ".join("@" + str(x) for x in failed)

    await msg.reply_text(
        result,
        parse_mode="HTML",
        reply_markup=manager_menu(),
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

    startup_task = None
    metrics_flush_task = None
    try:
        manager = await build_manager_bot()
        # Start the Manager first so it stays usable while existing Reaction Bots
        # are restored in the background with a small concurrency limit.
        await start_application(manager)
        startup_task = asyncio.create_task(
            start_all_managed_bots(),
            name="managed-bot-startup"
        )
        metrics_flush_task = asyncio.create_task(
            metrics_flush_loop(),
            name="metrics-flush-loop"
        )

        logger.info(
            "Manager running; restoring managed reaction bots in background."
        )

        await asyncio.Event().wait()

    finally:
        if metrics_flush_task and not metrics_flush_task.done():
            metrics_flush_task.cancel()
            try:
                await metrics_flush_task
            except asyncio.CancelledError:
                pass

        # Best-effort final per-bot analytics flush before workers stop.
        try:
            await flush_metrics_once(force=True)
        except Exception:
            logger.exception("Final analytics flush failed.")
        if startup_task and not startup_task.done():
            startup_task.cancel()
            try:
                await startup_task
            except asyncio.CancelledError:
                pass

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
