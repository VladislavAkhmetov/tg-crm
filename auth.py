import json
from typing import Optional, Dict, Any
from config import BOT_TOKEN, DEV_MODE
from aiogram.utils.web_app import safe_parse_webapp_init_data

def parse_and_verify_init_data(init_data: str) -> Optional[Dict[str, Any]]:
    """
    Validates initData received from Telegram WebApp according to Telegram specification:
    https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
    """
    if not init_data:
        return None

    try:
        data = safe_parse_webapp_init_data(BOT_TOKEN, init_data)
        if data and data.user:
            return {
                "telegram_id": data.user.id,
                "username": data.user.username,
                "first_name": data.user.first_name or "",
                "last_name": data.user.last_name or "",
                "auth_date": int(data.auth_date.timestamp()) if data.auth_date else 0
            }
        return None
    except Exception as e:
        print(f"Error validating initData: {e}")
        return None
