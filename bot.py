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

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
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
# MANAGED BOT REGISTRY / TOKEN SECURITY
# =========================================================

MANAGER_BOT_TOKEN = os.getenv("MANAGER_BOT_TOKEN", "").strip()
TOKEN_ENCRYPTION_SECRET = os.getenv("TOKEN_ENCRYPTION_SECRET", "").strip()

RUNNING_BOTS = {}
MANAGER_PENDING_TOKEN = {}


def _get_fernet():
    if not TOKEN_ENCRYPTION_SECRET:
        raise RuntimeError(
            "Missing environment variable: TOKEN_ENCRYPTION_SECRET"
        )

    # Derive a stable Fernet key from the Render secret.
    import base64
    import hashlib

    key = base64.urlsafe_b64encode(
        hashlib.sha256(TOKEN_ENCRYPTION_SECRET.encode("utf-8")).digest()
    )
    return Fernet(key)


def encrypt_token(token):
    return _get_fernet().encrypt(
        token.encode("utf-8")
    ).decode("utf-8")


def decrypt_token(value):
    try:
        return _get_fernet().decrypt(
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

    try:
        manager=await build_manager_bot()
        await start_all_managed_bots()

        await manager.start()
        if manager.updater is None:
            raise RuntimeError("Manager updater is unavailable.")

        await manager.updater.start_polling(drop_pending_updates=True)

        logger.info(
            "Manager running. Managed reaction bots: %s",
            len(RUNNING_BOTS)
        )

        await asyncio.Event().wait()

    finally:
        # Gracefully stop every managed bot on Render shutdown.
        for bot_id in list(RUNNING_BOTS.keys()):
            try:
                await stop_managed_bot(bot_id)
            except Exception:
                logger.exception("Failed to stop managed bot %s", bot_id)

# =========================================================
# ENTRY POINT
# =========================================================

if __name__ == "__main__":
    asyncio.run(main())
