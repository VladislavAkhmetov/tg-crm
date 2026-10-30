import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = [
    int(x.strip()) for x in os.getenv("ADMIN_IDS", "").replace(";", ",").replace(":", ",").split(",") if x.strip().isdigit()
]
GROUP_CHAT_ID = int(os.getenv("GROUP_CHAT_ID", "0").strip() or 0)
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8080"))
BASE_URL = os.getenv("BASE_URL", "").rstrip("/")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").rstrip("/")
DEV_MODE = os.getenv("DEV_MODE", "False").lower() in ("true", "1", "yes")
USE_POLLING = os.getenv("USE_POLLING", "True").lower() in ("true", "1", "yes")

DATABASE_PATH = BASE_DIR / "crm.db"

