"""Run the real app against an isolated database, without Telegram or S3 calls."""
import sys
import types
from pathlib import Path


def isolated_app(database_path):
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    config = types.ModuleType("config")
    values = {
        "DATABASE_PATH": str(database_path), "ADMIN_IDS": [999999999],
        "BOT_TOKEN": "", "DEV_MODE": True, "HOST": "127.0.0.1",
        "PORT": 8001, "BASE_URL": "http://127.0.0.1:8001",
        "WEBHOOK_URL": "", "GROUP_CHAT_ID": None, "BASE_DIR": root,
        "USE_POLLING": False,
    }
    config.__dict__.update(values)
    sys.modules["config"] = config
    storage = types.ModuleType("s3_sync")
    storage.download_db = lambda: None
    storage.upload_db = lambda: None
    storage.check_and_sync_db = lambda: None
    sys.modules["s3_sync"] = storage
    bot = types.ModuleType("bot")

    async def noop(*args, **kwargs):
        return None

    async def true_fn(*args, **kwargs):
        return True

    async def send_chat_ok(*args, **kwargs):
        return {"ok": True, "result": {"message_id": 12345, "chat": {"id": -100123456789}}}

    for name in ["send_user_notification", "setup_webhook",
                 "get_webhook_info", "process_telegram_update", "close_bot",
                 "start_polling", "stop_polling", "send_tg_document"]:
        setattr(bot, name, noop)
    bot.send_chat_notification = send_chat_ok
    bot.update_pool_message = true_fn
    bot.render_pool_message = lambda *args, **kwargs: "Mock pool message"
    bot.is_user_in_group = true_fn
    bot.is_user_group_admin = true_fn
    bot.invalidate_membership_cache = noop
    bot.get_group_keyboard = lambda *args, **kwargs: None
    bot.get_webapp_keyboard = lambda *args, **kwargs: None
    bot.get_target_group_chat_id = lambda: None
    bot.sync_group_admins = true_fn
    bot.clean_inactive_group_members = lambda *args, **kwargs: 0
    async def ok_dict(*args, **kwargs):
        return {"ok": True}

    bot.send_backup_to_telegram = ok_dict
    bot.create_db_backup = lambda *args, **kwargs: ("/tmp/test.db", "test.db")
    sys.modules["bot"] = bot
    import main
    import database
    database.DATABASE_PATH = str(database_path)
    database.init_db()
    return main.app, database
