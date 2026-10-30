import asyncio
import logging
import os
import socket
import tempfile
import sqlite3
import html
from datetime import datetime, timezone
from typing import Optional, Dict, Any, Tuple
import aiohttp
from aiohttp.resolver import DefaultResolver

from config import BOT_TOKEN, ADMIN_IDS, GROUP_CHAT_ID, BASE_URL
import database as db

logger = logging.getLogger(__name__)

class TgCustomResolver(DefaultResolver):
    """
    In Yandex Cloud datacenters, the default DNS IP for api.telegram.org (149.154.166.110)
    is filtered/dropped. Resolve directly to Telegram's responsive datacenter IP 149.154.167.220.
    """
    async def resolve(self, host, port=0, family=0):
        if host == "api.telegram.org":
            return [{"hostname": host, "host": "149.154.167.220", "port": port, "family": 2, "proto": 0, "flags": 0}]
        return await super().resolve(host, port, family=family)

_session: Optional[aiohttp.ClientSession] = None

def get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        connector = aiohttp.TCPConnector(
            resolver=TgCustomResolver(),
            keepalive_timeout=60.0,
            ttl_dns_cache=300
        )
        _session = aiohttp.ClientSession(connector=connector)
    return _session

async def close_bot():
    global _session
    if _session is not None and not _session.closed:
        try:
            await _session.close()
        except Exception:
            pass
        _session = None

async def call_tg_api(method: str, payload: dict, timeout: Optional[float] = None, retries: int = 3) -> dict:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    total_timeout = timeout or 25.0
    connect_timeout = 4.0

    for attempt in range(retries):
        try:
            session = get_session()
            client_timeout = aiohttp.ClientTimeout(total=total_timeout, connect=connect_timeout)
            async with session.post(url, json=payload, timeout=client_timeout) as resp:
                data = await resp.json()
                if not data.get("ok"):
                    logger.warning(f"Telegram API {method} returned non-ok: {data}")
                return data
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            if attempt < retries - 1:
                logger.warning(f"Telegram API {method} attempt {attempt + 1} failed ({e}), retrying...")
                await asyncio.sleep(0.3)
                continue
            logger.error(f"Telegram API {method} failed after {retries} attempts: {e}")
            return {"ok": False, "description": str(e)}
        except Exception as e:
            logger.error(f"Unexpected error in call_tg_api {method}: {e}", exc_info=True)
            return {"ok": False, "description": str(e)}
    return {"ok": False, "description": "Max retries reached"}

async def send_tg_document(
    chat_id: int,
    file_or_path: Any,
    filename: Optional[str] = None,
    caption: str = "",
    retries: int = 3
) -> dict:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument"

    if isinstance(file_or_path, (bytes, bytearray)):
        file_bytes = bytes(file_or_path)
        doc_filename = filename or "document.xlsx"
    elif isinstance(file_or_path, str) and os.path.exists(file_or_path):
        with open(file_or_path, "rb") as f:
            file_bytes = f.read()
        doc_filename = filename or os.path.basename(file_or_path)
    else:
        logger.error(f"send_tg_document: invalid file_or_path: {file_or_path}")
        return {"ok": False, "description": "Invalid file or path"}

    for attempt in range(retries):
        try:
            session = get_session()
            data = aiohttp.FormData()
            data.add_field("chat_id", str(chat_id))
            if caption:
                data.add_field("caption", caption)
                data.add_field("parse_mode", "HTML")
            data.add_field("document", file_bytes, filename=doc_filename)

            async with session.post(url, data=data, timeout=aiohttp.ClientTimeout(total=45.0, connect=5.0)) as resp:
                res = await resp.json()
                if not res.get("ok"):
                    logger.warning(f"Telegram API sendDocument error: {res}")
                return res
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            if attempt < retries - 1:
                logger.warning(f"sendDocument attempt {attempt + 1} failed ({e}), retrying...")
                await asyncio.sleep(0.5)
                continue
            logger.error(f"sendDocument failed after {retries} attempts: {e}")
            return {"ok": False, "error": str(e)}
        except Exception as e:
            logger.error(f"sendDocument exception: {e}", exc_info=True)
            return {"ok": False, "error": str(e)}
    return {"ok": False, "description": "Max retries reached"}

def create_db_backup() -> Tuple[str, str]:
    now_str = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    backup_filename = f"crm_backup_{now_str}.db"
    tmp_dir = tempfile.gettempdir()
    backup_path = os.path.join(tmp_dir, backup_filename)
    src_conn = db.get_db_connection()
    dst_conn = sqlite3.connect(backup_path)
    src_conn.backup(dst_conn)
    dst_conn.close()
    src_conn.close()
    return backup_path, backup_filename

async def send_backup_to_telegram(target_chat_id: int) -> dict:
    import s3_sync
    backup_path, backup_filename = create_db_backup()
    try:
        # 1. Also upload timestamped backup to S3 folder backups/
        try:
            client = s3_sync.get_s3_client()
            if client:
                client.upload_file(backup_path, s3_sync.S3_BUCKET, f"backups/{backup_filename}")
                logger.info(f"Timestamped backup uploaded to S3: backups/{backup_filename}")
        except Exception as e:
            logger.warning(f"Failed to upload timestamped backup to S3: {e}")

        # 2. Send document to Telegram chat
        stats = db.get_stats()
        caption = (
            f"📦 <b>Резервная копия базы данных CRM</b>\n\n"
            f"📅 Дата: <code>{datetime.now().strftime('%d.%m.%Y %H:%M:%S')}</code>\n"
            f"📁 Всего задач в базе: <b>{stats.get('total', 0)}</b>\n"
            f"💾 Файл: <code>{backup_filename}</code>\n\n"
            f"<i>Копия также сохранена в архив S3 (папка backups/).</i>"
        )
        return await send_tg_document(target_chat_id, backup_path, caption)
    finally:
        if os.path.exists(backup_path):
            try:
                os.remove(backup_path)
            except Exception:
                pass

_membership_cache: Dict[int, tuple] = {}
CACHE_TTL = 300.0  # 5 minutes

def get_target_group_chat_id() -> Optional[int]:
    chat_id = db.get_setting("group_chat_id")
    target_id = int(chat_id) if chat_id else GROUP_CHAT_ID
    return target_id if target_id != 0 else None

def invalidate_membership_cache(user_id: Optional[int] = None):
    if user_id:
        _membership_cache.pop(user_id, None)
    else:
        _membership_cache.clear()

async def get_user_group_status(user_id: int) -> Tuple[bool, bool]:
    """
    Checks whether a user is an active member and an administrator in the configured group chat.
    Returns (is_member, is_admin).
    Configured ADMIN_IDS automatically return (True, True).
    """
    if user_id in ADMIN_IDS:
        return True, True

    target_group_id = get_target_group_chat_id()
    if not target_group_id:
        # If no group chat is configured, do not block
        return True, False

    now = asyncio.get_event_loop().time()
    cached = _membership_cache.get(user_id)
    if cached and len(cached) == 3 and now < cached[2]:
        return cached[0], cached[1]

    try:
        res = await call_tg_api("getChatMember", {
            "chat_id": target_group_id,
            "user_id": user_id
        })
        if res.get("ok"):
            status = res.get("result", {}).get("status")
            is_member = status in ("creator", "administrator", "member", "restricted")
            is_admin = status in ("creator", "administrator")
        else:
            is_member = False
            is_admin = False

        _membership_cache[user_id] = (is_member, is_admin, now + CACHE_TTL)
        return is_member, is_admin
    except Exception as e:
        logger.warning(f"Error checking chat membership for user {user_id}: {e}")
        if cached and len(cached) == 3:
            return cached[0], cached[1]
        return False, False

async def is_user_in_group(user_id: int) -> bool:
    is_member, _ = await get_user_group_status(user_id)
    return is_member

async def is_user_group_admin(user_id: int) -> bool:
    _, is_admin = await get_user_group_status(user_id)
    return is_admin

async def sync_group_admins(chat_id: int) -> int:
    """
    Fetches all administrators from Telegram group chat and upserts them as admin in the DB.
    """
    try:
        res = await call_tg_api("getChatAdministrators", {"chat_id": chat_id})
        if res.get("ok"):
            admins = res.get("result", [])
            count = 0
            for item in admins:
                u = item.get("user", {})
                if u and not u.get("is_bot"):
                    u_id = u["id"]
                    db.upsert_user(
                        telegram_id=u_id,
                        username=u.get("username"),
                        first_name=u.get("first_name", "Администратор"),
                        last_name=u.get("last_name"),
                        role="admin"
                    )
                    invalidate_membership_cache(u_id)
                    count += 1
            return count
    except Exception as e:
        logger.warning(f"Error fetching group administrators for chat {chat_id}: {e}")
    return 0

async def clean_inactive_group_members(target_group_id: int) -> int:
    """
    Checks all existing registered designers in the DB. If they are no longer members
    in the configured group chat, removes them from the database.
    """
    if not target_group_id:
        return 0

    all_users = db.get_all_users()
    removed = 0
    for u in all_users:
        u_id = u["telegram_id"]
        # Do not delete system configured admins, any admins or dev mock users
        if u_id in ADMIN_IDS or u.get("role") == "admin" or u_id in (999999999, 10001, 10002):
            continue

        try:
            res = await call_tg_api("getChatMember", {
                "chat_id": target_group_id,
                "user_id": u_id
            })
            is_member = False
            if res.get("ok"):
                status = res.get("result", {}).get("status")
                is_member = status in ("creator", "administrator", "member", "restricted")

            if not is_member:
                db.delete_user(u_id)
                invalidate_membership_cache(u_id)
                removed += 1
        except Exception as e:
            logger.warning(f"Error checking membership for user {u_id} during cleanup: {e}")

    return removed

async def send_chat_notification(text: str, reply_markup: Optional[dict] = None, disable_notification: bool = True) -> Optional[dict]:
    """Sends a notification to the configured designer group chat (silent by default). Returns Telegram API response."""
    target_id = get_target_group_chat_id()

    if target_id:
        payload = {
            "chat_id": target_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_notification": disable_notification
        }
        thread_id = db.get_setting("group_thread_id")
        if thread_id and str(thread_id).isdigit():
            payload["message_thread_id"] = int(thread_id)
        if reply_markup:
            payload["reply_markup"] = reply_markup
        try:
            return await call_tg_api("sendMessage", payload)
        except Exception as e:
            logger.warning(f"Could not send group notification to {target_id}: {e}")
            return None
    return None

def render_pool_message(pool_name: str) -> str:
    """
    Renders an up-to-date live dashboard message for a task pool.
    Displays dynamic status badges and assignees next to each SKU.
    """
    conn = db.get_db_connection()
    tasks = conn.execute(
        "SELECT sku, status, assigned_to_name, assigned_to_username, is_archived, images_count, bitrix_url FROM tasks WHERE pool_name = ? ORDER BY id ASC",
        (pool_name,)
    ).fetchall()
    conn.close()

    if not tasks:
        return f"📁 <b>Пул задач: {pool_name}</b>\n<i>В этом пуле нет активных задач.</i>"

    bitrix_url = None
    for t in tasks:
        if t["bitrix_url"] and str(t["bitrix_url"]).strip():
            bitrix_url = str(t["bitrix_url"]).strip()
            break

    active_tasks = [t for t in tasks if t["is_archived"] == 0]
    display_tasks = active_tasks if active_tasks else tasks
    total = len(display_tasks)

    counts = {
        "available": 0,
        "in_progress": 0,
        "internal_review": 0,
        "client_review": 0,
        "export_ready": 0,
        "completed": 0
    }
    for t in display_tasks:
        st = t["status"]
        if st in counts:
            counts[st] += 1

    status_map = {
        "available": "⚪ Свободен",
        "in_progress": "🟡 В работе",
        "internal_review": "🔵 Внутр. ОС",
        "client_review": "🟣 ОС Клиента",
        "export_ready": "🟠 Экспорт",
        "completed": "🟢 Готово"
    }

    # Summary progress badges
    summary_parts = []
    if counts["available"] > 0:
        summary_parts.append(f"⚪ {counts['available']}")
    if counts["in_progress"] > 0:
        summary_parts.append(f"🟡 {counts['in_progress']}")
    if counts["internal_review"] > 0:
        summary_parts.append(f"🔵 {counts['internal_review']}")
    if counts["client_review"] > 0:
        summary_parts.append(f"🟣 {counts['client_review']}")
    if counts["export_ready"] > 0:
        summary_parts.append(f"🟠 {counts['export_ready']}")
    if counts["completed"] > 0:
        summary_parts.append(f"🟢 {counts['completed']}")

    summary_str = " | ".join(summary_parts) if summary_parts else f"{total}"

    is_archived_pool = (len(active_tasks) == 0 and len(tasks) > 0)
    title = f"📦 <b>Пул задач (в архиве):</b> {pool_name}" if is_archived_pool else f"📁 <b>Пул задач:</b> {pool_name}"

    bitrix_line = f"⏱ <b>Битрикс24:</b> <a href=\"{html.escape(bitrix_url)}\">Учет времени пула</a>\n" if bitrix_url else ""
    header = (
        f"{title}\n"
        f"{bitrix_line}"
        f"📊 <b>Артикулов:</b> {total}  ({summary_str})\n\n"
    )

    lines = []
    max_items = 35
    for t in display_tasks[:max_items]:
        sku = t["sku"]
        st = t["status"]
        st_text = status_map.get(st, st)
        assignee = ""
        if st != "available":
            if t["assigned_to_username"]:
                assignee = f" (@{t['assigned_to_username']})"
            elif t["assigned_to_name"]:
                assignee = f" ({t['assigned_to_name']})"
        img_str = f" [{t['images_count']} фото]" if t["images_count"] else ""
        lines.append(f"• <code>{sku}</code>{img_str} — {st_text}{assignee}")

    if len(display_tasks) > max_items:
        lines.append(f"<i>... и еще {len(display_tasks) - max_items} артикулов в приложении</i>")

    if is_archived_pool:
        footer = "\n\n📦 <i>Пул перемещен в архив.</i>"
    elif counts["completed"] == total and total > 0:
        footer = "\n\n🏁 <b>Все артикулы пула успешно согласованы и готовы!</b> 🎉"
    elif counts["available"] == 0 and total > 0:
        footer = "\n\n⚡ <i>Все артикулы в работе! Отслеживайте прогресс по кнопке ниже:</i>"
    else:
        footer = "\n\n👉 <i>Свободные дизайнеры могут брать артикулы в работу через кнопку «⚡ Открыть задачи» ниже:</i>"

    return header + "\n".join(lines) + footer

async def update_pool_message(pool_name: str, max_retries: int = 3) -> bool:
    """
    Safely edits the existing Telegram message for a pool with the latest task statuses.
    Includes up to 3 retries in case of temporary network timeout or jitter.
    100% fail-safe: logs any issues and never raises an exception.
    """
    if not pool_name:
        return False

    info = db.get_pool_message(pool_name)
    if not info:
        return False

    chat_id = info["chat_id"]
    message_id = info["message_id"]

    for attempt in range(max_retries):
        try:
            updated_text = render_pool_message(pool_name)
            payload = {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": updated_text,
                "parse_mode": "HTML",
                "reply_markup": get_group_keyboard()
            }
            res = await call_tg_api("editMessageText", payload)
            if not res.get("ok"):
                desc = res.get("description", "")
                if "message is not modified" in desc:
                    return True
                if "message to edit not found" in desc or "chat not found" in desc:
                    logger.warning(f"editMessageText permanent error for pool '{pool_name}' (msg {message_id}): {desc}")
                    return False
                if attempt < max_retries - 1:
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                logger.warning(f"editMessageText for pool '{pool_name}' (msg {message_id}): {desc}")
                return False
            return True
        except Exception as e:
            if attempt < max_retries - 1:
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            logger.warning(f"Failed to update pool message for '{pool_name}' after {max_retries} attempts: {e}")
            return False
    return False

async def send_user_notification(user_id: int, text: str, reply_markup: Optional[dict] = None):
    """Sends a private notification to a specific designer."""
    payload = {"chat_id": user_id, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        await call_tg_api("sendMessage", payload)
    except Exception as e:
        logger.warning(f"Could not send private notification to {user_id}: {e}")

def get_webapp_keyboard() -> dict:
    """For 1-on-1 private chat with the bot."""
    web_url = db.get_setting("base_url") or BASE_URL
    if web_url:
        return {
            "inline_keyboard": [[
                {"text": "⚡ Открыть задачи", "web_app": {"url": web_url}}
            ]]
        }
    return {
        "inline_keyboard": [[
            {"text": "⚡ Открыть задачи (Настройте BASE_URL)", "callback_data": "no_url"}
        ]]
    }

def get_group_keyboard() -> dict:
    """
    For group chats.
    Uses the registered BotFather Mini App direct link (t.me/<BOT_USERNAME>/tasks),
    which opens the Mini App directly inside the group chat as a modal popup window!
    """
    bot_username = os.getenv("BOT_USERNAME", "").strip() or "creative_ugc_bot"
    return {
        "inline_keyboard": [[
            {"text": "⚡ Открыть задачи", "url": f"https://t.me/{bot_username}/tasks"},
            {"text": "💬 Личный чат", "url": f"https://t.me/{bot_username}"}
        ]]
    }

async def setup_webhook(webhook_url: str):
    endpoint = f"{webhook_url.rstrip('/')}/api/telegram/webhook"
    try:
        info = await call_tg_api("getWebhookInfo", {})
        current = info.get("result", {}).get("url", "")
        logger.info(f"Checking Telegram webhook... Current: {current}")
        if current != endpoint:
            res = await call_tg_api("setWebhook", {
                "url": endpoint,
                "drop_pending_updates": False,
                "allowed_updates": ["message", "edited_message", "callback_query", "chat_member"]
            })
            logger.info(f"Telegram Webhook successfully configured: {endpoint} -> {res}")
        else:
            logger.info("Telegram Webhook is already configured correctly.")
    except Exception as e:
        logger.error(f"Error setting Telegram webhook: {e}")

async def get_webhook_info() -> dict:
    res = await call_tg_api("getWebhookInfo", {})
    return res.get("result", {})

_polling_task: Optional[asyncio.Task] = None
_polling_stop_event = asyncio.Event()

async def start_polling():
    global _polling_task
    if _polling_task is not None and not _polling_task.done():
        logger.info("Telegram polling is already running.")
        return

    _polling_stop_event.clear()

    # 1. Ensure webhook is removed
    try:
        del_res = await call_tg_api("deleteWebhook", {"drop_pending_updates": False}, timeout=15.0)
        logger.info(f"Telegram webhook removed for polling: {del_res}")
    except Exception as e:
        logger.warning(f"Error removing webhook for polling: {e}")

    # 2. Launch polling worker
    _polling_task = asyncio.create_task(polling_loop())
    logger.info("Telegram long polling background task started.")

async def stop_polling():
    global _polling_task
    _polling_stop_event.set()
    if _polling_task is not None:
        _polling_task.cancel()
        try:
            await _polling_task
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning(f"Error stopping polling: {e}")
        _polling_task = None
        logger.info("Telegram long polling task stopped.")

async def polling_loop():
    logger.info("Telegram polling loop active. Waiting for updates...")
    offset = None
    while not _polling_stop_event.is_set():
        try:
            params = {
                "timeout": 20,
                "allowed_updates": ["message", "edited_message", "callback_query", "chat_member"]
            }
            if offset is not None:
                params["offset"] = offset

            res = await call_tg_api("getUpdates", params, timeout=35.0, retries=2)
            if res.get("ok"):
                for upd in res.get("result", []):
                    offset = upd["update_id"] + 1
                    try:
                        await process_telegram_update(upd)
                    except Exception as e:
                        logger.error(f"Error processing update {upd.get('update_id')}: {e}", exc_info=True)
            else:
                await asyncio.sleep(2.0)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning(f"Error in Telegram polling loop: {e}")
            await asyncio.sleep(2.0)
    logger.info("Telegram polling loop ended.")

async def process_telegram_update(update: dict):
    target_group_id = get_target_group_chat_id()

    # 1. Handle chat_member updates (member added / joined)
    chat_member = update.get("chat_member")
    if chat_member:
        chat = chat_member.get("chat", {})
        chat_id = chat.get("id")
        if target_group_id and chat_id == target_group_id:
            new_user = chat_member.get("new_chat_member", {}).get("user", {})
            status = chat_member.get("new_chat_member", {}).get("status")
            if new_user and not new_user.get("is_bot"):
                u_id = new_user.get("id")
                if status in ("creator", "administrator", "member", "restricted"):
                    db.upsert_user(
                        telegram_id=u_id,
                        username=new_user.get("username"),
                        first_name=new_user.get("first_name", "Дизайнер"),
                        last_name=new_user.get("last_name"),
                        role="designer"
                    )
                    invalidate_membership_cache(u_id)
                elif status in ("left", "kicked"):
                    db.delete_user(u_id)
                    invalidate_membership_cache(u_id)
        return

    message = update.get("message")
    if not message:
        return

    chat = message.get("chat", {})
    chat_id = chat.get("id")
    chat_type = chat.get("type", "private") # "private", "group", "supergroup"
    user = message.get("from", {})
    user_id = user.get("id")

    # 2. If new members were added in group
    if chat_type in ("group", "supergroup") and target_group_id and chat_id == target_group_id:
        for m in message.get("new_chat_members", []):
            if not m.get("is_bot"):
                db.upsert_user(
                    telegram_id=m["id"],
                    username=m.get("username"),
                    first_name=m.get("first_name", "Дизайнер"),
                    last_name=m.get("last_name"),
                    role="designer"
                )
                invalidate_membership_cache(m["id"])

        # 3. If any group member sends a message in the group, auto-register them
        if user_id and not user.get("is_bot"):
            existing = db.get_user(user_id)
            if not existing:
                db.upsert_user(
                    telegram_id=user_id,
                    username=user.get("username"),
                    first_name=user.get("first_name", "Дизайнер"),
                    last_name=user.get("last_name"),
                    role="designer"
                )
                invalidate_membership_cache(user_id)

    text = (message.get("text") or "").strip()
    if not text.startswith("/"):
        return

    cmd = text.split()[0].split("@")[0].lower()
    chat = message.get("chat", {})
    chat_id = chat.get("id")
    chat_type = chat.get("type", "private") # "private", "group", "supergroup"
    user = message.get("from", {})
    user_id = user.get("id")
    if not user_id:
        return

    first_name = user.get("first_name", "Дизайнер")
    last_name = user.get("last_name")
    username = user.get("username", "")

    if cmd in ("/start", "/tasks", "/app", "/crm", "/help"):
        if chat_type == "private" and (user_id not in ADMIN_IDS):
            is_member = await is_user_in_group(user_id)
            if not is_member:
                await call_tg_api("sendMessage", {
                    "chat_id": chat_id,
                    "text": (
                        "⛔ <b>Доступ ограничен</b>\n\n"
                        "Вы не состоите в рабочей группе команды Creative UGC CRM.\n"
                        "Доступ к системе открыт только для участников закрытого командного чата."
                    ),
                    "parse_mode": "HTML"
                })
                return

        is_admin = user_id in ADMIN_IDS
        role = "admin" if is_admin else "designer"
        existing = db.get_user(user_id)
        if not existing:
            user_record = db.upsert_user(
                telegram_id=user_id,
                username=username,
                first_name=first_name,
                last_name=last_name,
                role=role
            )
        else:
            user_record = db.upsert_user(
                telegram_id=user_id,
                username=username,
                first_name=first_name,
                last_name=last_name
            )

        if chat_type in ("group", "supergroup"):
            greeting = (
                f"👋 <b>Creative UGC CRM</b>\n\n"
                f"Рабочая панель задач для генерации и оформления изображений.\n\n"
                f"🔹 Нажмите <b>«⚡ Открыть задачи»</b> ниже, чтобы запустить приложение и взять артикулы в работу."
            )
            msg_payload = {
                "chat_id": chat_id,
                "text": greeting,
                "parse_mode": "HTML",
                "reply_markup": get_group_keyboard(),
                "reply_to_message_id": message.get("message_id"),
                "allow_sending_without_reply": True,
                "disable_notification": True
            }
            if message.get("message_thread_id"):
                msg_payload["message_thread_id"] = message["message_thread_id"]
            await call_tg_api("sendMessage", msg_payload)
            return

        # Private chat
        if not user_record.get("is_approved", 1):
            await call_tg_api("sendMessage", {
                "chat_id": chat_id,
                "text": "⛔ <b>Доступ ограничен</b>\n\nВаш аккаунт ожидает подтверждения администратором команды.",
                "parse_mode": "HTML"
            })
            return

        greeting = (
            f"👋 Привет, <b>{first_name}</b>!\n\n"
            f"Это рабочая панель задач для генерации и оформления визуалов.\n\n"
            f"🔹 <b>Роль:</b> {'👑 Администратор' if user_record['role'] == 'admin' else '🎨 Дизайнер'}\n"
            f"🔹 Нажмите кнопку ниже или синюю кнопку «⚡ Задачи» в меню, чтобы открыть приложение."
        )
        await call_tg_api("sendMessage", {
            "chat_id": chat_id,
            "text": greeting,
            "parse_mode": "HTML",
            "reply_markup": get_webapp_keyboard()
        })

    elif cmd == "/stats":
        stats = db.get_stats()
        text_stats = (
            f"📊 <b>Текущая статистика:</b>\n\n"
            f"• Всего артикулов: <b>{stats.get('total', 0)}</b>\n"
            f"• ⚪ Свободно: <b>{stats.get('available', 0)}</b>\n"
            f"• 🟡 В работе: <b>{stats.get('in_progress', 0)}</b>\n"
            f"• 🔵 Внутренняя ОС: <b>{stats.get('internal_review', 0)}</b>\n"
            f"• 🟣 ОС Заказчика: <b>{stats.get('client_review', 0)}</b>\n"
            f"• 🟠 На экспорт (Я.Диск): <b>{stats.get('export_ready', 0)}</b>\n"
            f"• 🟢 Готово: <b>{stats.get('completed', 0)}</b>\n"
        )
        kb = get_group_keyboard() if chat_type in ("group", "supergroup") else get_webapp_keyboard()
        stats_payload = {
            "chat_id": chat_id,
            "text": text_stats,
            "parse_mode": "HTML",
            "reply_markup": kb,
            "reply_to_message_id": message.get("message_id"),
            "allow_sending_without_reply": True,
            "disable_notification": True
        }
        if message.get("message_thread_id"):
            stats_payload["message_thread_id"] = message["message_thread_id"]
        await call_tg_api("sendMessage", stats_payload)

    elif cmd == "/desktop":
        base = (db.get_setting("base_url") or BASE_URL or "").strip()
        if "/static/" in base:
            desktop_url = base.split("/static/")[0] + "/static/desktop.html"
        elif base:
            desktop_url = base.rstrip("/") + "/static/desktop.html"
        else:
            desktop_url = ""

        kb = None
        if desktop_url:
            kb = {
                "inline_keyboard": [[
                    {"text": "💻 Открыть Desktop CRM", "web_app": {"url": desktop_url}}
                ]]
            }

        text_desk = (
            "💻 <b>Широкоэкранный дашборд Creative UGC CRM</b>\n\n"
            "Оптимизирован для работы на ПК и в Telegram Desktop:\n"
            "• Канбан-доска по 6 стадиям пайплайна\n"
            "• Реестр пулов и сводная аналитика\n"
            "• Slide-over карточки задач без модалок"
        )
        if desktop_url:
            text_desk += f"\n\nПрямая ссылка для браузера:\n<code>{desktop_url}</code>"

        await call_tg_api("sendMessage", {
            "chat_id": chat_id,
            "text": text_desk,
            "parse_mode": "HTML",
            "reply_markup": kb,
            "reply_to_message_id": message.get("message_id"),
            "allow_sending_without_reply": True,
            "disable_notification": True
        })

    elif cmd == "/setgroup":
        is_admin = (user_id in ADMIN_IDS) or (await is_user_group_admin(user_id)) or (db.get_user(user_id) and db.get_user(user_id).get("role") == "admin")
        if not is_admin:
            denied_payload = {
                "chat_id": chat_id,
                "text": "⛔ Команда доступна только администраторам.",
                "reply_to_message_id": message.get("message_id"),
                "allow_sending_without_reply": True,
                "disable_notification": True
            }
            if message.get("message_thread_id"):
                denied_payload["message_thread_id"] = message["message_thread_id"]
            await call_tg_api("sendMessage", denied_payload)
            return

        if chat_type in ("group", "supergroup"):
            db.set_setting("group_chat_id", str(chat_id))
            if message.get("message_thread_id"):
                db.set_setting("group_thread_id", str(message["message_thread_id"]))
            invalidate_membership_cache()
            
            # Sync admins of this group automatically
            admin_count = await sync_group_admins(chat_id)
            # Remove members from former groups who are not in this group
            cleaned_count = await clean_inactive_group_members(chat_id)
            db.sync_db_to_s3()

            report = (
                f"✅ <b>Группа подключена!</b>\n\n"
                f"• ID чата: <code>{chat_id}</code>\n"
                f"• Администраторов актуализировано: <b>{admin_count}</b>\n"
            )
            if cleaned_count > 0:
                report += f"• Участников из других групп очищено: <b>{cleaned_count}</b>\n"
            report += "\nВсе задачи и уведомления теперь привязаны к этой рабочей группе (в тихом режиме без звука 🔕)."

            success_payload = {
                "chat_id": chat_id,
                "text": report,
                "parse_mode": "HTML",
                "reply_markup": get_group_keyboard(),
                "reply_to_message_id": message.get("message_id"),
                "allow_sending_without_reply": True,
                "disable_notification": True
            }
            if message.get("message_thread_id"):
                success_payload["message_thread_id"] = message["message_thread_id"]
            await call_tg_api("sendMessage", success_payload)
        else:
            await call_tg_api("sendMessage", {
                "chat_id": chat_id,
                "text": "ℹ Отправьте эту команду внутри общего группового чата с дизайнерами."
            })

    elif cmd in ("/sync", "/refresh"):
        is_admin = (user_id in ADMIN_IDS) or (db.get_user(user_id) and db.get_user(user_id).get("role") == "admin")
        if not is_admin:
            await call_tg_api("sendMessage", {
                "chat_id": chat_id,
                "text": "⛔ Команда синхронизации доступна только администраторам.",
                "reply_to_message_id": message.get("message_id"),
                "allow_sending_without_reply": True
            })
            return

        target_id = get_target_group_chat_id() or (chat_id if chat_type in ("group", "supergroup") else None)
        if not target_id:
            await call_tg_api("sendMessage", {
                "chat_id": chat_id,
                "text": "⚠️ Рабочая группа еще не привязана. Отправьте <code>/setgroup</code> в целевом чате.",
                "parse_mode": "HTML"
            })
            return

        admin_count = await sync_group_admins(target_id)
        cleaned_count = await clean_inactive_group_members(target_id)
        current_users = db.get_all_users()

        await call_tg_api("sendMessage", {
            "chat_id": chat_id,
            "text": (
                f"🔄 <b>Состав команды синхронизирован с группой!</b>\n\n"
                f"• Администраторов чата: <b>{admin_count}</b>\n"
                f"• Удалено выбывших: <b>{cleaned_count}</b>\n"
                f"• Всего активных участников в CRM: <b>{len(current_users)}</b>"
            ),
            "parse_mode": "HTML",
            "reply_markup": get_group_keyboard() if chat_type in ("group", "supergroup") else get_webapp_keyboard(),
            "reply_to_message_id": message.get("message_id"),
            "allow_sending_without_reply": True
        })

    elif cmd in ("/backup", "/dump"):
        is_admin = (user_id in ADMIN_IDS) or (db.get_user(user_id) and db.get_user(user_id).get("role") == "admin")
        if not is_admin:
            await call_tg_api("sendMessage", {
                "chat_id": chat_id,
                "text": "⛔ Команда создания бэкапа доступна только администраторам.",
                "reply_to_message_id": message.get("message_id"),
                "allow_sending_without_reply": True
            })
            return

        await call_tg_api("sendChatAction", {"chat_id": chat_id, "action": "upload_document"})
        res = await send_backup_to_telegram(chat_id)
        if not res.get("ok"):
            await call_tg_api("sendMessage", {
                "chat_id": chat_id,
                "text": f"❌ Ошибка отправки бэкапа: {res.get('description', 'Неизвестная ошибка')}",
                "reply_to_message_id": message.get("message_id"),
                "allow_sending_without_reply": True
            })

    elif cmd in ("/export", "/report"):
        is_admin = (user_id in ADMIN_IDS) or (db.get_user(user_id) and db.get_user(user_id).get("role") == "admin")
        if not is_admin:
            await call_tg_api("sendMessage", {
                "chat_id": chat_id,
                "text": "⛔ Команда экспорта отчетов доступна только администраторам.",
                "reply_to_message_id": message.get("message_id"),
                "allow_sending_without_reply": True
            })
            return

        import exporter
        await call_tg_api("sendChatAction", {"chat_id": chat_id, "action": "upload_document"})
        tasks = db.get_tasks_for_export()
        excel_bytes = exporter.generate_excel(tasks)
        now_tag = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
        filename = f"crm_report_{now_tag}.xlsx"
        total_tasks = len(tasks)
        total_images = sum((t.get("images_count") or 0) for t in tasks)
        caption = (
            f"📊 <b>Отчет по задачам и пулам</b>\n\n"
            f"• Всего задач: <b>{total_tasks}</b>\n"
            f"• Отрисовано изображений: <b>{total_images}</b>\n"
            f"• Сформирован: {datetime.now(timezone.utc).strftime('%d.%m.%Y %H:%M UTC')}"
        )
        res = await send_tg_document(chat_id, excel_bytes, filename=filename, caption=caption)
        if not res.get("ok"):
            await call_tg_api("sendMessage", {
                "chat_id": chat_id,
                "text": f"❌ Ошибка отправки отчета: {res.get('description', 'Неизвестная ошибка')}",
                "reply_to_message_id": message.get("message_id"),
                "allow_sending_without_reply": True
            })

