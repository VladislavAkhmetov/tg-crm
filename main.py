import asyncio
import socket
import logging

# In Yandex Cloud datacenters, the default DNS IP for api.telegram.org (149.154.166.110)
# times out due to network route filtering. Route directly to working Telegram IP 149.154.167.220.
_orig_getaddrinfo = socket.getaddrinfo
def _tg_getaddrinfo(host, port, *args, **kwargs):
    if host == "api.telegram.org":
        return _orig_getaddrinfo("149.154.167.220", port, *args, **kwargs)
    return _orig_getaddrinfo(host, port, *args, **kwargs)
socket.getaddrinfo = _tg_getaddrinfo

import re
import html
from contextlib import asynccontextmanager
from typing import Optional, List, Dict, Any
from fastapi import FastAPI, Depends, HTTPException, Header, Request, status, Response
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from urllib.parse import quote
from datetime import datetime, timezone

from config import BOT_TOKEN, DEV_MODE, HOST, PORT, BASE_URL, WEBHOOK_URL, ADMIN_IDS, USE_POLLING
import database as db
import auth
import s3_sync
import exporter
from bot import (
    send_chat_notification,
    send_user_notification,
    get_group_keyboard,
    get_webapp_keyboard,
    setup_webhook,
    get_webhook_info,
    process_telegram_update,
    start_polling,
    stop_polling,
    send_tg_document,
    close_bot,
    is_user_in_group,
    is_user_group_admin,
    invalidate_membership_cache,
    get_target_group_chat_id,
    sync_group_admins,
    clean_inactive_group_members,
    send_backup_to_telegram,
    create_db_backup,
    update_pool_message,
    render_pool_message
)
from fastapi import BackgroundTasks
from starlette.background import BackgroundTask

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("tg_crm")

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: Fast DB init
    try:
        s3_sync.download_db()
    except Exception as e:
        logger.warning(f"S3 download error: {e}")
    db.init_db()
    logger.info("Database initialized successfully.")

    if USE_POLLING:
        await start_polling()
    elif WEBHOOK_URL:
        await setup_webhook(WEBHOOK_URL)

    yield

    # Shutdown
    if USE_POLLING:
        await stop_polling()
    await close_bot()
    logger.info("Application shutdown complete.")

app = FastAPI(title="Creative UGC CRM", lifespan=lifespan)

@app.get("/health")
@app.post("/health")
@app.head("/health")
async def health():
    return {"status": "ok"}

@app.post("/api/telegram/webhook")
async def telegram_webhook(request: Request):
    try:
        data = await request.json()
        await process_telegram_update(data)
    except Exception as e:
        logger.error(f"Error processing webhook: {e}")
    return {"ok": True}

@app.get("/api/telegram/setup-webhook")
async def force_setup_webhook(custom_url: Optional[str] = None):
    target = custom_url or WEBHOOK_URL
    try:
        if target:
            await setup_webhook(target)
        info = await get_webhook_info()
        return {
            "success": True,
            "webhook_url": info.get("url"),
            "pending_update_count": info.get("pending_update_count", 0),
            "last_error_message": info.get("last_error_message"),
            "last_error_date": info.get("last_error_date")
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


# Mount static files
app.mount("/static", StaticFiles(directory="static"), name="static")

# Dependency: Authenticate user from Telegram initData
async def get_current_user(
    request: Request,
    x_telegram_init_data: Optional[str] = Header(None, alias="X-Telegram-Init-Data")
) -> Dict[str, Any]:
    # 1. Try real Telegram initData validation
    if x_telegram_init_data:
        parsed = auth.parse_and_verify_init_data(x_telegram_init_data)
        if parsed:
            user_id = parsed["telegram_id"]
            if user_id not in ADMIN_IDS:
                is_member = await is_user_in_group(user_id)
                if not is_member:
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="Доступ открыт только для участников рабочей группы команды в Telegram."
                    )

            is_group_admin = (user_id in ADMIN_IDS) or (await is_user_group_admin(user_id))
            role_hint = "admin" if is_group_admin else None

            tg_user = db.upsert_user(
                telegram_id=user_id,
                username=parsed.get("username"),
                first_name=parsed.get("first_name", "Дизайнер"),
                last_name=parsed.get("last_name"),
                role=role_hint
            )
            if not tg_user.get("is_approved", 1):
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Доступ не одобрен администратором")
            return tg_user

    # 2. In DEV_MODE, allow testing via custom header or fallback test admin
    if DEV_MODE:
        dev_user_id = request.headers.get("X-Dev-User-Id")
        if dev_user_id and dev_user_id.isdigit():
            user = db.get_user(int(dev_user_id))
            if not user:
                if dev_user_id == "10001":
                    user = db.upsert_user(10001, "designer_anna", "Анна (Дизайнер)", role="designer")
                elif dev_user_id == "10002":
                    user = db.upsert_user(10002, "designer_misha", "Михаил (Дизайнер)", role="designer")
            if user:
                return user
        
        # Default mock admin user for browser preview
        existing = db.get_user(999999999)
        if existing:
            return existing
        mock_user = db.upsert_user(
            telegram_id=999999999,
            username="art_lead",
            first_name="Алексей (Лид)",
            role="admin"
        )
        return mock_user

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Требуется авторизация через Telegram Mini App"
    )

# --- Pydantic Models ---
class TaskCreateBatch(BaseModel):
    pool_name: str
    parent_url: Optional[str] = None
    skus_text: str  # Line separated or space/comma separated
    bitrix_url: Optional[str] = None
    notes: Optional[str] = None
    skip_active_duplicates: bool = True
    assign_to_id: Optional[int] = None

class SkuCheckRequest(BaseModel):
    skus_text: str
    parent_url: Optional[str] = None

class TaskTakeRequest(BaseModel):
    task_id: int

class TaskAssignRequest(BaseModel):
    task_id: int
    designer_id: int

class TaskSubmitInternalRequest(BaseModel):
    task_id: int
    internal_figma_url: str
    images_count: Optional[int] = None
    notes: Optional[str] = None

class TaskSubmitClientRequest(BaseModel):
    task_id: int
    client_figma_url: Optional[str] = None
    images_count: Optional[int] = None
    notes: Optional[str] = None

class TaskSubmitExportRequest(BaseModel):
    task_id: int
    images_count: Optional[int] = None
    notes: Optional[str] = None

class TaskCompleteRequest(BaseModel):
    task_id: int
    yandex_disk_url: str
    images_count: Optional[int] = None
    notes: Optional[str] = None

class TaskImagesCountRequest(BaseModel):
    images_count: int

class TaskImagesCountBodyRequest(BaseModel):
    task_id: int
    images_count: int

class TaskRejectRequest(BaseModel):
    task_id: int
    return_to_pool: bool = False
    comment: Optional[str] = None

class SettingUpdateRequest(BaseModel):
    group_chat_id: Optional[str] = None
    base_url: Optional[str] = None

class UserRoleUpdateRequest(BaseModel):
    role: str

class TaskArchiveRequest(BaseModel):
    is_archived: bool = True

class PoolArchiveRequest(BaseModel):
    pool_name: str
    is_archived: bool = True

class PoolDeleteRequest(BaseModel):
    pool_name: str

class PoolRenameRequest(BaseModel):
    old_pool_name: str
    new_pool_name: str

class PoolAddSkusRequest(BaseModel):
    pool_name: str
    skus_text: str
    source_url: Optional[str] = None
    bitrix_url: Optional[str] = None
    notes: Optional[str] = None
    assign_to_id: Optional[int] = None

class PoolUpdateBitrixRequest(BaseModel):
    pool_name: str
    bitrix_url: Optional[str] = None

class ExportExcelRequest(BaseModel):
    pool_names: Optional[List[str]] = None
    send_to_telegram: Optional[bool] = True

class ExportGoogleSheetsRequest(BaseModel):
    webhook_url: Optional[str] = None
    pool_names: Optional[List[str]] = None
    save_as_default: bool = False

# --- API Endpoints ---

@app.get("/")
async def serve_index():
    return FileResponse("static/index.html", headers={"Cache-Control": "no-cache, must-revalidate"})

@app.get("/api/me")
async def get_me(user: Dict[str, Any] = Depends(get_current_user)):
    return {
        "user": user,
        "is_admin": user.get("role") == "admin",
        "dev_mode": DEV_MODE
    }

@app.get("/api/tasks")
async def list_tasks(
    status: Optional[str] = None,
    user_id: Optional[int] = None,
    search: Optional[str] = None,
    user: Dict[str, Any] = Depends(get_current_user)
):
    tasks = db.get_tasks(status=status, user_id=user_id, search=search)
    return {"tasks": tasks}

@app.get("/api/stats")
async def get_statistics(user: Dict[str, Any] = Depends(get_current_user)):
    return db.get_stats()

@app.get("/api/users")
async def list_users(user: Dict[str, Any] = Depends(get_current_user)):
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Доступно только администраторам")
    return {"users": db.get_all_users()}

@app.post("/api/users/sync")
async def sync_users_endpoint(user: Dict[str, Any] = Depends(get_current_user)):
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Доступно только администраторам")

    target_group_id = get_target_group_chat_id()
    if not target_group_id:
        return {"users": db.get_all_users(), "synced": False, "message": "Рабочая группа еще не подключена"}

    await sync_group_admins(target_group_id)
    await clean_inactive_group_members(target_group_id)
    db.sync_db_to_s3()
    return {"users": db.get_all_users(), "synced": True}

@app.post("/api/admin/backup")
async def trigger_admin_backup(user: Dict[str, Any] = Depends(get_current_user)):
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Доступно только администраторам")
    res = await send_backup_to_telegram(user["telegram_id"])
    if not res.get("ok"):
        raise HTTPException(
            status_code=500,
            detail=f"Не удалось отправить файл бэкапа в Telegram: {res.get('description', 'убедитесь, что вы нажали /start в боте')}"
        )
    return {"ok": True, "message": "Резервная копия успешно отправлена в ваш Telegram"}

@app.get("/api/admin/backup/download")
async def download_admin_backup(user: Dict[str, Any] = Depends(get_current_user)):
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Доступно только администраторам")
    backup_path, backup_filename = create_db_backup()
    return FileResponse(
        backup_path,
        filename=backup_filename,
        media_type="application/x-sqlite3",
        background=BackgroundTask(lambda: os.remove(backup_path) if os.path.exists(backup_path) else None)
    )

@app.post("/api/users/{target_telegram_id}/role")
async def update_user_role(
    target_telegram_id: int,
    data: UserRoleUpdateRequest,
    user: Dict[str, Any] = Depends(get_current_user)
):
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Только администратор может менять роли")

    if data.role not in ("admin", "designer"):
        raise HTTPException(status_code=400, detail="Недопустимая роль. Допустимо: 'admin' или 'designer'")

    target_user = db.get_user(target_telegram_id)
    if not target_user:
        raise HTTPException(status_code=404, detail="Пользователь не найден")

    # Protection: Cannot remove the last remaining admin
    if data.role != "admin" and target_user.get("role") == "admin":
        all_users = db.get_all_users()
        admins = [u for u in all_users if u.get("role") == "admin"]
        if len(admins) <= 1:
            raise HTTPException(status_code=400, detail="Нельзя снять роль с единственного администратора в системе")

    updated = db.update_user_role(target_telegram_id, data.role)
    return {"ok": True, "user": updated}

@app.delete("/api/users/{target_telegram_id}")
async def remove_user(
    target_telegram_id: int,
    user: Dict[str, Any] = Depends(get_current_user)
):
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Доступно только администраторам")
    if target_telegram_id == user["telegram_id"]:
        raise HTTPException(status_code=400, detail="Нельзя удалить самого себя")
    db.delete_user(target_telegram_id)
    invalidate_membership_cache(target_telegram_id)
    return {"success": True}

import cloud_storage as sc

class CloudScanRequest(BaseModel):
    url: str

@app.post("/api/cloud/scan")
async def scan_cloud_folders(data: CloudScanRequest, user: Dict[str, Any] = Depends(get_current_user)):
    url = data.url.strip()
    scan_res = sc.scan_cloud_share(url)
    folders = scan_res.get("folders", [])
    analysis = db.check_skus_and_pool(folders, url)
    return {
        "type": scan_res.get("type", "pool"),
        "is_single": scan_res.get("is_single", False),
        "single_sku": scan_res.get("single_sku"),
        "suggested_pool_name": scan_res.get("suggested_pool_name"),
        "folders": folders,
        "analysis": analysis
    }

@app.post("/api/tasks/check-skus")
async def check_skus_endpoint(data: SkuCheckRequest, user: Dict[str, Any] = Depends(get_current_user)):
    raw_lines = data.skus_text.replace(",", "\n").replace(";", "\n").splitlines()
    skus = [line.strip() for line in raw_lines if line.strip()]
    parent_url = data.parent_url.strip() if data.parent_url else None
    analysis = db.check_skus_and_pool(skus, parent_url)
    return {"analysis": analysis}

@app.post("/api/tasks/create-batch")
async def create_batch_tasks(
    data: TaskCreateBatch,
    user: Dict[str, Any] = Depends(get_current_user)
):
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Только администратор может добавлять паки задач")

    # Parse SKUs from text
    raw_lines = data.skus_text.replace(",", "\n").replace(";", "\n").splitlines()
    skus = [line.strip() for line in raw_lines if line.strip()]

    if not skus:
        raise HTTPException(status_code=400, detail="Список артикулов пуст")

    parent_url = data.parent_url.strip() if data.parent_url else ""

    target_designer = None
    if data.assign_to_id:
        target_designer = db.get_user(data.assign_to_id)
        if not target_designer:
            raise HTTPException(status_code=400, detail="Указанный дизайнер не найден")

    # Check duplicates
    analysis = db.check_skus_and_pool(skus, parent_url)
    active_skus = {c["sku"].upper() for c in analysis.get("active_conflicts", [])}

    tasks_to_insert = []
    skipped_skus = []
    for sku in skus:
        if data.skip_active_duplicates and sku.upper() in active_skus:
            skipped_skus.append(sku)
            continue

        # Build direct public link that works without login
        source_url = sc.build_direct_sku_url(parent_url, sku) if parent_url else "https://cloud.example.com/"

        task_data = {
            "sku": sku,
            "source_url": source_url,
            "pool_name": data.pool_name.strip() or "Новый пул",
            "bitrix_url": data.bitrix_url.strip() if data.bitrix_url else None,
            "notes": data.notes
        }
        if target_designer:
            d_name = f"{target_designer.get('first_name', '')} {target_designer.get('last_name', '')}".strip() or "Дизайнер"
            task_data["assigned_to_id"] = target_designer["telegram_id"]
            task_data["assigned_to_name"] = d_name
            task_data["assigned_to_username"] = target_designer.get("username")
            task_data["status"] = "in_progress"
        tasks_to_insert.append(task_data)

    if not tasks_to_insert:
        sample = ", ".join(skipped_skus[:3])
        raise HTTPException(
            status_code=400,
            detail=f"Все указанные артикулы ({len(skipped_skus)} шт.) уже находятся в активной работе ({sample}). Создание дубликатов заблокировано."
        )

    user_name = user.get("first_name", "") + (f" (@{user['username']})" if user.get("username") else "")
    inserted_ids = db.create_tasks_bulk(tasks_to_insert, user["telegram_id"], user_name, skip_active_duplicates=False)

    pool_name = data.pool_name.strip() or "Новый пул"

    if target_designer and inserted_ids:
        folder_link = parent_url or 'https://cloud.example.com/'
        batch_count = len(inserted_ids)
        d_msg = (
            f"🎨 <b>Вам назначен новый пул задач ({batch_count} артикулов)!</b>\n\n"
            f"📁 Пул: <b>{pool_name}</b>\n"
            f"🔗 <a href='{folder_link}'>Открыть папку с фото</a>\n\n"
            f"Задачи добавлены в вашу вкладку <b>«Мои»</b>. После создания макетов отправьте их на проверку со ссылкой на внутреннюю Figma."
        )
        asyncio.create_task(send_user_notification(target_designer["telegram_id"], d_msg))

    # Send notification to Telegram group and track message_id for in-place updates
    msg_text = render_pool_message(pool_name)
    if skipped_skus:
        msg_text += f"\n\n⚠️ <i>Пропущено дубликатов в работе: {len(skipped_skus)}</i>"

    async def _send_and_track_pool():
        try:
            tg_res = await send_chat_notification(msg_text, reply_markup=get_group_keyboard())
            if tg_res and tg_res.get("ok"):
                msg_obj = tg_res.get("result", {})
                m_id = msg_obj.get("message_id")
                c_id = msg_obj.get("chat", {}).get("id")
                t_id = msg_obj.get("message_thread_id")
                if m_id and c_id:
                    db.save_pool_message(pool_name, c_id, m_id, t_id)
        except Exception as e:
            logger.warning(f"Failed to send and track pool message for '{pool_name}': {e}")

    asyncio.create_task(_send_and_track_pool())

    return {
        "success": True,
        "count": len(inserted_ids),
        "ids": inserted_ids,
        "skipped_active": len(skipped_skus),
        "skipped_skus": skipped_skus
    }

@app.post("/api/tasks/take")
async def take_task(
    req: TaskTakeRequest,
    user: Dict[str, Any] = Depends(get_current_user)
):
    task = db.get_task(req.task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Задача не найдена")

    if task["status"] != "available":
        raise HTTPException(status_code=400, detail=f"Задача уже взята в работу ({task['assigned_to_name'] or 'другим дизайнером'})")

    designer_name = user.get("first_name", "Дизайнер")
    if user.get("last_name"):
        designer_name += f" {user['last_name']}"
    if user.get("username"):
        designer_name += f" (@{user['username']})"

    updated = db.update_task_status(
        task_id=req.task_id,
        new_status="in_progress",
        user_id=user["telegram_id"],
        user_name=designer_name,
        username=user.get("username"),
        details="Дизайнер взял артикул в работу"
    )

    # Always send private DM to the designer
    folder_link = updated.get('source_url') or 'https://cloud.example.com/'
    asyncio.create_task(send_user_notification(
        user["telegram_id"],
        f"🎨 <b>Вы взяли артикул {task['sku']} в работу!</b>\n\n"
        f"📁 <a href='{folder_link}'>Открыть папку с фото</a>\n\n"
        f"Задача теперь находится во вкладке <b>«Мои»</b>. После создания макета отправьте его на проверку со ссылкой на внутреннюю Figma."
    ))

    if task.get("pool_name"):
        asyncio.create_task(update_pool_message(task["pool_name"]))

    return {"success": True, "task": updated}

@app.post("/api/tasks/assign")
async def assign_task_endpoint(
    req: TaskAssignRequest,
    user: Dict[str, Any] = Depends(get_current_user)
):
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Только администратор может назначать задачи")

    admin_name = user.get("first_name", "Администратор")
    if user.get("username"):
        admin_name += f" (@{user['username']})"

    result = db.assign_task(
        task_id=req.task_id,
        designer_id=req.designer_id,
        admin_id=user["telegram_id"],
        admin_name=admin_name
    )
    if not result:
        raise HTTPException(status_code=404, detail="Задача или дизайнер не найдены")

    task = result["task"]
    designer = result["designer"]
    old_assigned_to_id = result.get("old_assigned_to_id")

    # 1. Send notification to the newly assigned designer
    folder_link = task.get('source_url') or 'https://cloud.example.com/'
    d_msg = (
        f"🎨 <b>Вам назначена задача: артикул {task['sku']}!</b>\n\n"
        f"📁 <a href='{folder_link}'>Открыть папку с фото</a>\n\n"
        f"Задача добавлена в вашу вкладку <b>«Мои»</b>. После создания макета отправьте его на проверку со ссылкой на внутреннюю Figma."
    )
    asyncio.create_task(send_user_notification(designer["telegram_id"], d_msg))

    # 2. If reassigned, notify the previous assignee
    if old_assigned_to_id and old_assigned_to_id != designer["telegram_id"]:
        prev_msg = f"ℹ️ Артикул <b>{task['sku']}</b> был переназначен администратором другому исполнителю."
        asyncio.create_task(send_user_notification(old_assigned_to_id, prev_msg))

    # 3. Update pool live message in group
    if task.get("pool_name"):
        asyncio.create_task(update_pool_message(task["pool_name"]))

    return {"success": True, "task": task}

@app.post("/api/tasks/submit-internal")
async def submit_internal_review(
    req: TaskSubmitInternalRequest,
    user: Dict[str, Any] = Depends(get_current_user)
):
    task = db.get_task(req.task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Задача не найдена")

    # Allow assigned designer or admin
    if task["assigned_to_id"] != user["telegram_id"] and user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Только назначенный дизайнер может отправить задачу на ревью")

    if req.images_count is None or req.images_count <= 0 or req.images_count > 500:
        raise HTTPException(status_code=400, detail="Укажите количество изображений (от 1 до 500)")

    details_msg = f"Передано на внутреннее ревью админу ({req.images_count} фото)"
    updated = db.update_task_status(
        task_id=req.task_id,
        new_status="internal_review",
        user_id=user["telegram_id"],
        user_name=user.get("first_name", "Дизайнер"),
        internal_figma_url=req.internal_figma_url.strip(),
        images_count=req.images_count,
        notes=req.notes,
        details=details_msg
    )

    if task.get("pool_name"):
        asyncio.create_task(update_pool_message(task["pool_name"]))

    return {"success": True, "task": updated}

@app.post("/api/tasks/submit-client")
async def submit_client_review(
    req: TaskSubmitClientRequest,
    user: Dict[str, Any] = Depends(get_current_user)
):
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Только администратор передает задачи заказчику")

    task = db.get_task(req.task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Задача не найдена")

    if req.images_count is not None and (req.images_count <= 0 or req.images_count > 500):
        raise HTTPException(status_code=400, detail="Количество изображений должно быть положительным числом (от 1 до 500)")

    updated = db.update_task_status(
        task_id=req.task_id,
        new_status="client_review",
        user_id=user["telegram_id"],
        user_name=user.get("first_name", "Админ"),
        client_figma_url=req.client_figma_url.strip() if req.client_figma_url else None,
        images_count=req.images_count,
        notes=req.notes,
        details="Передано на проверку заказчику"
    )

    # Notify designer privately
    if task["assigned_to_id"]:
        asyncio.create_task(send_user_notification(
            task["assigned_to_id"],
            f"🚀 Администратор передал ваш артикул <b>{task['sku']}</b> на согласование заказчику!"
        ))

    if task.get("pool_name"):
        asyncio.create_task(update_pool_message(task["pool_name"]))

    return {"success": True, "task": updated}

@app.post("/api/tasks/submit-export")
async def submit_export_ready(
    req: TaskSubmitExportRequest,
    user: Dict[str, Any] = Depends(get_current_user)
):
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Только администратор подтверждает согласование заказчика")

    task = db.get_task(req.task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Задача не найдена")

    if req.images_count is not None and (req.images_count <= 0 or req.images_count > 500):
        raise HTTPException(status_code=400, detail="Количество изображений должно быть положительным числом (от 1 до 500)")

    updated = db.update_task_status(
        task_id=req.task_id,
        new_status="export_ready",
        user_id=user["telegram_id"],
        user_name=user.get("first_name", "Админ"),
        images_count=req.images_count,
        notes=req.notes,
        details="Заказчик утвердил! Готово к экспорту на Я.Диск"
    )

    # Notify designer
    if task["assigned_to_id"]:
        asyncio.create_task(send_user_notification(
            task["assigned_to_id"],
            f"🎉 Заказчик согласовал артикул <b>{task['sku']}</b>!\n"
            f"Пожалуйста, экспортируйте изображения в папку и прикрепите ссылку на Яндекс.Диск."
        ))

    if task.get("pool_name"):
        asyncio.create_task(update_pool_message(task["pool_name"]))

    return {"success": True, "task": updated}

@app.post("/api/tasks/complete")
async def complete_task(
    req: TaskCompleteRequest,
    user: Dict[str, Any] = Depends(get_current_user)
):
    task = db.get_task(req.task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Задача не найдена")

    if req.images_count is not None and (req.images_count <= 0 or req.images_count > 500):
        raise HTTPException(status_code=400, detail="Количество изображений должно быть положительным числом (от 1 до 500)")

    updated = db.update_task_status(
        task_id=req.task_id,
        new_status="completed",
        user_id=user["telegram_id"],
        user_name=user.get("first_name", "Дизайнер"),
        yandex_disk_url=req.yandex_disk_url.strip(),
        images_count=req.images_count,
        notes=req.notes,
        details="Файлы экспортированы и загружены на Я.Диск. Задача завершена."
    )

    if task.get("pool_name"):
        asyncio.create_task(update_pool_message(task["pool_name"]))

    return {"success": True, "task": updated}

@app.post("/api/tasks/{task_id}/images-count")
async def update_task_images_count_endpoint(
    task_id: int,
    req: TaskImagesCountRequest,
    user: Dict[str, Any] = Depends(get_current_user)
):
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Задача не найдена")

    if task["assigned_to_id"] != user["telegram_id"] and user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Только назначенный дизайнер или администратор могут изменять количество изображений")

    if req.images_count <= 0 or req.images_count > 500:
        raise HTTPException(status_code=400, detail="Количество изображений должно быть положительным числом (от 1 до 500)")

    user_name = user.get("first_name", "Дизайнер")
    updated = db.update_task_images_count(
        task_id=task_id,
        images_count=req.images_count,
        user_id=user["telegram_id"],
        user_name=user_name
    )
    if updated and updated.get("pool_name"):
        asyncio.create_task(update_pool_message(updated["pool_name"]))

    return {"success": True, "task": updated}

@app.post("/api/tasks/images-count")
async def update_task_images_count_body_endpoint(
    req: TaskImagesCountBodyRequest,
    user: Dict[str, Any] = Depends(get_current_user)
):
    return await update_task_images_count_endpoint(
        task_id=req.task_id,
        req=TaskImagesCountRequest(images_count=req.images_count),
        user=user
    )

@app.post("/api/tasks/reject-feedback")
async def reject_task(
    req: TaskRejectRequest,
    user: Dict[str, Any] = Depends(get_current_user)
):
    task = db.get_task(req.task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Задача не найдена")

    if req.return_to_pool:
        # Designer releases task back to pool
        if task.get("assigned_to_id") != user["telegram_id"] and user.get("role") != "admin":
            raise HTTPException(status_code=403, detail="Только назначенный дизайнер или администратор могут вернуть задачу в пул")

        updated = db.update_task_status(
            task_id=req.task_id,
            new_status="available",
            user_id=user["telegram_id"],
            user_name=user.get("first_name", "Дизайнер"),
            details=f"Возвращено в общий пул: {req.comment or 'без комментария'}"
        )
        if task.get("pool_name"):
            asyncio.create_task(update_pool_message(task["pool_name"]))
    else:
        # Return for revision
        is_admin = user.get("role") == "admin"
        is_assigned = task.get("assigned_to_id") == user["telegram_id"]

        if not is_admin and not is_assigned:
            raise HTTPException(
                status_code=403,
                detail="Только назначенный дизайнер или администратор могут возвращать задачу на доработку"
            )

        if task.get("status") not in ("internal_review", "client_review"):
            raise HTTPException(
                status_code=400,
                detail="Вернуть задачу на доработку можно только со стадии внутреннего или клиентского ревью"
            )

        comment_clean = req.comment.strip() if req.comment else ""
        if comment_clean.startswith("Правки:"):
            comment_clean = comment_clean[len("Правки:"):].strip()

        notes_text = f"Правки: {comment_clean}" if comment_clean else "Требуются правки"

        actor_name = user.get("first_name") or ("Админ" if is_admin else "Дизайнер")
        if user.get("last_name"):
            actor_name += f" {user['last_name']}"

        if is_admin and not is_assigned:
            details_text = f"Возвращено на доработку: {comment_clean}" if comment_clean else "Возвращено на доработку"
        else:
            details_text = f"Дизайнер забрал на доработку: {comment_clean}" if comment_clean else "Дизайнер забрал на доработку"

        updated = db.update_task_status(
            task_id=req.task_id,
            new_status="in_progress",
            user_id=user["telegram_id"],
            user_name=actor_name,
            preserve_assignee=True,
            notes=notes_text,
            details=details_text
        )

        if task.get("assigned_to_id") and task["assigned_to_id"] != user["telegram_id"]:
            notice_reason = comment_clean if comment_clean else "Поправьте макет в Фигме"
            asyncio.create_task(send_user_notification(
                task["assigned_to_id"],
                f"⚠️ По артикулу <b>{task['sku']}</b> есть замечания:\n<i>{notice_reason}</i>"
            ))

        if task.get("pool_name"):
            asyncio.create_task(update_pool_message(task["pool_name"]))

    return {"success": True, "task": updated}

@app.post("/api/tasks/{task_id}/archive")
async def archive_task_endpoint(
    task_id: int,
    req: TaskArchiveRequest,
    user: Dict[str, Any] = Depends(get_current_user)
):
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Доступно только администраторам")
    updated = db.set_task_archived(
        task_id=task_id,
        is_archived=req.is_archived,
        user_id=user["telegram_id"],
        user_name=user.get("first_name", "Администратор")
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Задача не найдена")
    if updated.get("pool_name"):
        asyncio.create_task(update_pool_message(updated["pool_name"]))
    return {"ok": True, "task": updated}

@app.delete("/api/tasks/{task_id}")
async def delete_task_endpoint(
    task_id: int,
    user: Dict[str, Any] = Depends(get_current_user)
):
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Доступно только администраторам")
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Задача не найдена")
    success = db.delete_task(
        task_id=task_id,
        user_id=user["telegram_id"],
        user_name=user.get("first_name", "Администратор")
    )
    if task.get("pool_name"):
        asyncio.create_task(update_pool_message(task["pool_name"]))
    return {"ok": True, "task_id": task_id}

@app.post("/api/pools/archive")
async def archive_pool_endpoint(
    req: PoolArchiveRequest,
    user: Dict[str, Any] = Depends(get_current_user)
):
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Доступно только администраторам")
    name = req.pool_name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Название пула не может быть пустым")
    affected = db.set_pool_archived(
        pool_name=name,
        is_archived=req.is_archived,
        user_id=user["telegram_id"],
        user_name=user.get("first_name", "Администратор")
    )
    asyncio.create_task(update_pool_message(name))
    return {"ok": True, "pool_name": name, "affected": affected}

@app.post("/api/pools/delete")
@app.delete("/api/pools")
async def delete_pool_endpoint(
    req: PoolDeleteRequest,
    user: Dict[str, Any] = Depends(get_current_user)
):
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Доступно только администраторам")
    name = req.pool_name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Название пула не может быть пустым")
    deleted_count = db.delete_pool(
        pool_name=name,
        user_id=user["telegram_id"],
        user_name=user.get("first_name", "Администратор")
    )
    db.delete_pool_message(name)
    return {"ok": True, "pool_name": name, "deleted_count": deleted_count}

@app.post("/api/pools/rename")
async def rename_pool_endpoint(
    req: PoolRenameRequest,
    user: Dict[str, Any] = Depends(get_current_user)
):
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Доступно только администраторам")
    old_name = req.old_pool_name.strip()
    new_name = req.new_pool_name.strip()
    if not old_name or not new_name:
        raise HTTPException(status_code=400, detail="Названия пулов не могут быть пустыми")
    affected = db.rename_pool(
        old_name=old_name,
        new_name=new_name,
        user_id=user["telegram_id"],
        user_name=user.get("first_name", "Администратор")
    )
    asyncio.create_task(update_pool_message(new_name))
    return {"ok": True, "old_name": old_name, "new_name": new_name, "affected": affected}

@app.post("/api/pools/add-skus")
async def add_skus_to_pool_endpoint(
    req: PoolAddSkusRequest,
    user: Dict[str, Any] = Depends(get_current_user)
):
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Доступно только администраторам")
    pool_name = req.pool_name.strip()
    if not pool_name:
        raise HTTPException(status_code=400, detail="Название пула не может быть пустым")
    raw_lines = re.split(r"[\n,]+", req.skus_text or "")
    skus = [s.strip().upper() for s in raw_lines if s.strip()]
    if not skus:
        raise HTTPException(status_code=400, detail="Не указано ни одного артикула")

    target_designer = None
    if req.assign_to_id:
        target_designer = db.get_user(req.assign_to_id)
        if not target_designer:
            raise HTTPException(status_code=400, detail="Указанный дизайнер не найден")

    pool_bitrix = req.bitrix_url.strip() if req.bitrix_url else db.get_pool_bitrix_url(pool_name)
    tasks_data = []
    for sku in skus:
        t_data = {
            "sku": sku,
            "source_url": req.source_url.strip() if req.source_url else None,
            "pool_name": pool_name,
            "bitrix_url": pool_bitrix,
            "notes": req.notes.strip() if req.notes else None
        }
        if target_designer:
            d_name = f"{target_designer.get('first_name', '')} {target_designer.get('last_name', '')}".strip() or "Дизайнер"
            t_data["assigned_to_id"] = target_designer["telegram_id"]
            t_data["assigned_to_name"] = d_name
            t_data["assigned_to_username"] = target_designer.get("username")
            t_data["status"] = "in_progress"
        tasks_data.append(t_data)

    created_ids = db.create_tasks_bulk(
        tasks_data,
        created_by_id=user["telegram_id"],
        created_by_name=user.get("first_name", "Админ"),
        skip_active_duplicates=True
    )

    if target_designer and created_ids:
        folder_link = req.source_url.strip() if req.source_url else 'https://cloud.example.com/'
        count = len(created_ids)
        d_msg = (
            f"🎨 <b>Вам назначены новые задачи ({count} артикулов) в пуле «{pool_name}»!</b>\n\n"
            f"🔗 <a href='{folder_link}'>Открыть папку с фото</a>\n\n"
            f"Задачи добавлены в вашу вкладку <b>«Мои»</b>."
        )
        asyncio.create_task(send_user_notification(target_designer["telegram_id"], d_msg))

    asyncio.create_task(update_pool_message(pool_name))
    return {"ok": True, "pool_name": pool_name, "created_count": len(created_ids), "task_ids": created_ids}

@app.post("/api/pools/update-bitrix")
async def update_pool_bitrix_endpoint(
    req: PoolUpdateBitrixRequest,
    user: Dict[str, Any] = Depends(get_current_user)
):
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Доступно только администраторам")
    pool_name = req.pool_name.strip()
    if not pool_name:
        raise HTTPException(status_code=400, detail="Название пула не может быть пустым")
    affected = db.update_pool_bitrix_url(
        pool_name=pool_name,
        bitrix_url=req.bitrix_url,
        user_id=user["telegram_id"],
        user_name=user.get("first_name", "Администратор")
    )
    asyncio.create_task(update_pool_message(pool_name))
    return {"ok": True, "pool_name": pool_name, "affected": affected, "bitrix_url": req.bitrix_url}

@app.post("/api/settings")
async def update_settings(
    req: SettingUpdateRequest,
    user: Dict[str, Any] = Depends(get_current_user)
):
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Доступно только администраторам")

    if req.group_chat_id is not None:
        db.set_setting("group_chat_id", req.group_chat_id.strip())
        invalidate_membership_cache()
    if req.base_url is not None:
        db.set_setting("base_url", req.base_url.strip().rstrip("/"))

    return {"success": True}
 
@app.get("/api/export/pools")
async def get_export_pools(user: Dict[str, Any] = Depends(get_current_user)):
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Доступно только администраторам")
    pools = db.get_pools_summary()
    return {"pools": pools}

@app.post("/api/export/excel")
async def export_excel_endpoint(
    req: ExportExcelRequest,
    user: Dict[str, Any] = Depends(get_current_user)
):
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Доступно только администраторам")
    tasks = db.get_tasks_for_export(req.pool_names)
    excel_bytes = exporter.generate_excel(tasks, req.pool_names)
    now_tag = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
    if req.pool_names and len(req.pool_names) == 1:
        safe_name = re.sub(r'[^a-zA-Z0-9_\u0400-\u04FF]', '_', req.pool_names[0])[:30]
        filename = f"pool_{safe_name}_{now_tag}.xlsx"
    else:
        filename = f"crm_report_{now_tag}.xlsx"

    sent_to_telegram = False
    admin_tg_id = user.get("telegram_id")
    if req.send_to_telegram and admin_tg_id and admin_tg_id > 0 and admin_tg_id != 999999999:
        try:
            total_tasks = len(tasks)
            total_images = sum((t.get("images_count") or 0) for t in tasks)
            pools_desc = ", ".join(req.pool_names) if req.pool_names else "Все пулы"
            caption = (
                f"📊 <b>Отчет по задачам и пулам</b>\n\n"
                f"• Пулы: <b>{html.escape(pools_desc)}</b>\n"
                f"• Всего задач: <b>{total_tasks}</b>\n"
                f"• Отрисовано изображений: <b>{total_images}</b>\n"
                f"• Сформирован: {datetime.now(timezone.utc).strftime('%d.%m.%Y %H:%M UTC')}"
            )
            tg_res = await send_tg_document(
                chat_id=admin_tg_id,
                file_or_path=excel_bytes,
                filename=filename,
                caption=caption
            )
            if tg_res.get("ok"):
                sent_to_telegram = True
                logger.info(f"Excel export report sent directly to Telegram user {admin_tg_id}")
            else:
                logger.warning(f"Could not send export document to TG user {admin_tg_id}: {tg_res}")
        except Exception as e:
            logger.warning(f"Error sending export to TG user {admin_tg_id}: {e}")

    encoded_filename = quote(filename)
    return Response(
        content=excel_bytes,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{encoded_filename}",
            "Access-Control-Expose-Headers": "Content-Disposition, X-Sent-To-Telegram",
            "X-Sent-To-Telegram": "true" if sent_to_telegram else "false"
        }
    )

@app.post("/api/export/google-sheets")
async def export_google_sheets_endpoint(
    req: ExportGoogleSheetsRequest,
    user: Dict[str, Any] = Depends(get_current_user)
):
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Доступно только администраторам")

    webhook_url = (req.webhook_url or "").strip()
    if not webhook_url:
        webhook_url = db.get_setting("google_sheets_webhook_url", "")

    if not webhook_url:
        raise HTTPException(status_code=400, detail="Не указана ссылка на веб-приложение Google Apps Script")

    if req.save_as_default and webhook_url:
        db.set_setting("google_sheets_webhook_url", webhook_url)

    tasks = db.get_tasks_for_export(req.pool_names)
    result = await exporter.export_to_google_apps_script(webhook_url, tasks, req.pool_names)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "Ошибка выгрузки в Google Таблицу"))

    return result

@app.get("/api/settings/google-sheets")
async def get_google_sheets_settings(user: Dict[str, Any] = Depends(get_current_user)):
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Доступно только администраторам")
    return {
        "webhook_url": db.get_setting("google_sheets_webhook_url", ""),
        "script_template": exporter.GOOGLE_APPS_SCRIPT_TEMPLATE
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host=HOST, port=PORT, reload=True)
