import sqlite3
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any
from config import DATABASE_PATH, ADMIN_IDS

def get_db_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn

def sync_db_to_s3():
    try:
        import s3_sync
        s3_sync.upload_db()
    except Exception as e:
        import logging
        logging.getLogger("database").warning(f"S3 sync failed: {e}")

def init_db():
    conn = get_db_connection()
    with conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                role TEXT DEFAULT 'designer',
                is_approved INTEGER DEFAULT 1,
                last_active_at TEXT,
                created_at TEXT
            );
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sku TEXT NOT NULL,
                source_url TEXT,
                pool_name TEXT,
                status TEXT NOT NULL DEFAULT 'available',
                assigned_to_id INTEGER,
                assigned_to_name TEXT,
                assigned_to_username TEXT,
                assigned_at TEXT,
                internal_figma_url TEXT,
                client_figma_url TEXT,
                yandex_disk_url TEXT,
                images_count INTEGER,
                bitrix_url TEXT,
                notes TEXT,
                is_archived INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
        """)

        # Migration: ensure is_archived, images_count, and bitrix_url columns exist in existing databases
        try:
            conn.execute("ALTER TABLE tasks ADD COLUMN is_archived INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE tasks ADD COLUMN images_count INTEGER")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE tasks ADD COLUMN bitrix_url TEXT")
        except sqlite3.OperationalError:
            pass

        conn.execute("""
            CREATE TABLE IF NOT EXISTS activity_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER,
                user_id INTEGER,
                user_name TEXT,
                action TEXT NOT NULL,
                details TEXT,
                created_at TEXT NOT NULL
            );
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS system_settings (
                key TEXT PRIMARY KEY,
                value TEXT
            );
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS pool_messages (
                pool_name TEXT PRIMARY KEY,
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                thread_id INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
        """)

        # Seed initial admins from config if any
        now = datetime.now(timezone.utc).isoformat()
        for admin_id in ADMIN_IDS:
            conn.execute("""
                INSERT OR IGNORE INTO users (telegram_id, username, first_name, role, is_approved, created_at, last_active_at)
                VALUES (?, 'admin', 'Администратор', 'admin', 1, ?, ?)
            """, (admin_id, now, now))

    conn.close()

# User helpers
def get_user(telegram_id: int) -> Optional[Dict[str, Any]]:
    conn = get_db_connection()
    row = conn.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)).fetchone()
    conn.close()
    return dict(row) if row else None

def upsert_user(telegram_id: int, username: Optional[str], first_name: str, last_name: Optional[str] = None, role: Optional[str] = None) -> Dict[str, Any]:
    conn = get_db_connection()
    now = datetime.now(timezone.utc).isoformat()
    needs_s3_sync = False
    with conn:
        existing = conn.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)).fetchone()
        if existing:
            current_role = existing["role"]
            if role and role != current_role:
                current_role = role
                needs_s3_sync = True
            elif telegram_id in ADMIN_IDS and current_role != "admin":
                current_role = "admin"
                needs_s3_sync = True
            
            conn.execute("""
                UPDATE users 
                SET username = ?, first_name = ?, last_name = ?, role = ?, last_active_at = ?
                WHERE telegram_id = ?
            """, (username, first_name, last_name, current_role, now, telegram_id))
        else:
            default_role = 'admin' if (telegram_id in ADMIN_IDS or role == 'admin' or not conn.execute("SELECT 1 FROM users").fetchone()) else (role or 'designer')
            conn.execute("""
                INSERT INTO users (telegram_id, username, first_name, last_name, role, is_approved, created_at, last_active_at)
                VALUES (?, ?, ?, ?, ?, 1, ?, ?)
            """, (telegram_id, username, first_name, last_name, default_role, now, now))
            needs_s3_sync = True
            
    conn.close()
    if needs_s3_sync:
        sync_db_to_s3()
    return get_user(telegram_id)

def get_all_users() -> List[Dict[str, Any]]:
    conn = get_db_connection()
    rows = conn.execute("""
        SELECT u.*,
               (SELECT COUNT(*) FROM tasks t
                WHERE t.assigned_to_id = u.telegram_id
                  AND t.is_archived = 0
                  AND t.status IN ('in_progress', 'internal_review', 'client_review', 'export_ready')
               ) AS active_tasks_count
        FROM users u
        ORDER BY created_at DESC
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def update_user_role(telegram_id: int, role: str) -> Optional[Dict[str, Any]]:
    conn = get_db_connection()
    now = datetime.now(timezone.utc).isoformat()
    with conn:
        conn.execute("UPDATE users SET role = ?, last_active_at = ? WHERE telegram_id = ?", (role, now, telegram_id))
    conn.close()
    sync_db_to_s3()
    return get_user(telegram_id)

def delete_user(telegram_id: int) -> bool:
    conn = get_db_connection()
    with conn:
        conn.execute("DELETE FROM users WHERE telegram_id = ?", (telegram_id,))
    conn.close()
    sync_db_to_s3()
    return True

# Task helpers
def check_skus_and_pool(skus: List[str], parent_url: Optional[str] = None) -> Dict[str, Any]:
    conn = get_db_connection()
    existing_pool_name = None
    if parent_url:
        import cloud_storage as sc
        token = sc.extract_share_token(parent_url)
        if token:
            row = conn.execute(
                "SELECT pool_name FROM tasks WHERE source_url LIKE ? ORDER BY id DESC LIMIT 1",
                (f"%{token}%",)
            ).fetchone()
            if row:
                existing_pool_name = row["pool_name"]

    seen = set()
    unique_skus = []
    for s in skus:
        s_clean = s.strip()
        if s_clean and s_clean.upper() not in seen:
            seen.add(s_clean.upper())
            unique_skus.append(s_clean)

    active_conflicts = []
    completed_or_archived = []
    new_skus = []

    if unique_skus:
        placeholders = ",".join("?" * len(unique_skus))
        rows = conn.execute(
            f"""
            SELECT id, sku, pool_name, status, assigned_to_name, assigned_to_username, is_archived
            FROM tasks
            WHERE UPPER(sku) IN ({placeholders})
            ORDER BY id DESC
            """,
            [s.upper() for s in unique_skus]
        ).fetchall()

        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for r in rows:
            grouped.setdefault(r["sku"].upper(), []).append(dict(r))

        active_statuses = {'available', 'in_progress', 'internal_review', 'client_review', 'export_ready'}

        for s in unique_skus:
            s_records = grouped.get(s.upper(), [])
            if not s_records:
                new_skus.append(s)
            else:
                active_rec = next((r for r in s_records if r["is_archived"] == 0 and r["status"] in active_statuses), None)
                if active_rec:
                    active_conflicts.append({
                        "sku": s,
                        "task_id": active_rec["id"],
                        "status": active_rec["status"],
                        "assigned_to_name": active_rec["assigned_to_name"] or active_rec["assigned_to_username"],
                        "pool_name": active_rec["pool_name"]
                    })
                else:
                    latest_rec = s_records[0]
                    completed_or_archived.append({
                        "sku": s,
                        "task_id": latest_rec["id"],
                        "status": latest_rec["status"],
                        "pool_name": latest_rec["pool_name"],
                        "is_archived": bool(latest_rec["is_archived"])
                    })

    conn.close()
    return {
        "existing_pool_name": existing_pool_name,
        "total": len(unique_skus),
        "new_skus": new_skus,
        "active_conflicts": active_conflicts,
        "completed_or_archived": completed_or_archived
    }

def create_tasks_bulk(
    tasks_data: List[Dict[str, Any]],
    created_by_id: int,
    created_by_name: str,
    skip_active_duplicates: bool = True,
    assigned_to_id: Optional[int] = None,
    assigned_to_name: Optional[str] = None,
    assigned_to_username: Optional[str] = None
) -> List[int]:
    conn = get_db_connection()
    now = datetime.now(timezone.utc).isoformat()
    inserted_ids = []

    active_skus = set()
    if skip_active_duplicates and tasks_data:
        skus = [t["sku"].strip().upper() for t in tasks_data if t.get("sku")]
        if skus:
            placeholders = ",".join("?" * len(skus))
            active_statuses = ('available', 'in_progress', 'internal_review', 'client_review', 'export_ready')
            status_placeholders = ",".join("?" * len(active_statuses))
            rows = conn.execute(
                f"""
                SELECT DISTINCT UPPER(sku) as sku_upper
                FROM tasks
                WHERE is_archived = 0
                  AND status IN ({status_placeholders})
                  AND UPPER(sku) IN ({placeholders})
                """,
                (*active_statuses, *skus)
            ).fetchall()
            active_skus = {r["sku_upper"] for r in rows}

    with conn:
        for t in tasks_data:
            sku = t["sku"].strip()
            if skip_active_duplicates and sku.upper() in active_skus:
                continue

            bitrix_url = (t.get("bitrix_url") or "").strip() or None
            item_assigned_id = t.get("assigned_to_id") or assigned_to_id
            item_assigned_name = t.get("assigned_to_name") or assigned_to_name
            item_assigned_username = t.get("assigned_to_username") or assigned_to_username
            status = t.get("status") or ("in_progress" if item_assigned_id else "available")
            assigned_at = now if item_assigned_id else None

            cur = conn.execute("""
                INSERT INTO tasks (sku, source_url, pool_name, bitrix_url, status, assigned_to_id, assigned_to_name, assigned_to_username, assigned_at, notes, images_count, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (sku, t.get("source_url"), t.get("pool_name", "Новый пул"), bitrix_url, status, item_assigned_id, item_assigned_name, item_assigned_username, assigned_at, t.get("notes"), t.get("images_count"), now, now))
            task_id = cur.lastrowid
            inserted_ids.append(task_id)

            log_details = f"Создана задача для артикула {sku} и назначена дизайнеру {item_assigned_name}" if item_assigned_id else f"Создана задача для артикула {sku}"
            conn.execute("""
                INSERT INTO activity_log (task_id, user_id, user_name, action, details, created_at)
                VALUES (?, ?, ?, 'created', ?, ?)
            """, (task_id, created_by_id, created_by_name, log_details, now))
    conn.close()
    sync_db_to_s3()
    return inserted_ids

def get_tasks(status: Optional[str] = None, user_id: Optional[int] = None, search: Optional[str] = None) -> List[Dict[str, Any]]:
    conn = get_db_connection()
    query = "SELECT * FROM tasks WHERE 1=1"
    params = []

    if status and status != "all":
        query += " AND status = ?"
        params.append(status)
    if user_id:
        query += " AND assigned_to_id = ?"
        params.append(user_id)
    if search:
        query += " AND (sku LIKE ? OR pool_name LIKE ?)"
        term = f"%{search.strip()}%"
        params.extend([term, term])

    query += " ORDER BY id DESC"
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_task(task_id: int) -> Optional[Dict[str, Any]]:
    conn = get_db_connection()
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    conn.close()
    return dict(row) if row else None

def update_task_status(task_id: int, new_status: str, user_id: int, user_name: str, **kwargs) -> Optional[Dict[str, Any]]:
    conn = get_db_connection()
    now = datetime.now(timezone.utc).isoformat()
    with conn:
        task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not task:
            conn.close()
            return None

        update_fields = ["status = ?", "updated_at = ?"]
        params = [new_status, now]

        if new_status == "in_progress" and not kwargs.get("preserve_assignee", False):
            update_fields.extend(["assigned_to_id = ?", "assigned_to_name = ?", "assigned_to_username = ?", "assigned_at = ?"])
            params.extend([user_id, user_name, kwargs.get("username"), now])
        elif new_status == "available":
            # Returned to pool
            update_fields.extend(["assigned_to_id = NULL", "assigned_to_name = NULL", "assigned_to_username = NULL", "assigned_at = NULL"])

        if "internal_figma_url" in kwargs and kwargs["internal_figma_url"] is not None:
            update_fields.append("internal_figma_url = ?")
            params.append(kwargs["internal_figma_url"])
        if "client_figma_url" in kwargs and kwargs["client_figma_url"] is not None:
            update_fields.append("client_figma_url = ?")
            params.append(kwargs["client_figma_url"])
        if "yandex_disk_url" in kwargs and kwargs["yandex_disk_url"] is not None:
            update_fields.append("yandex_disk_url = ?")
            params.append(kwargs["yandex_disk_url"])
        if "images_count" in kwargs and kwargs["images_count"] is not None:
            update_fields.append("images_count = ?")
            params.append(kwargs["images_count"])
        if "notes" in kwargs and kwargs["notes"] is not None:
            update_fields.append("notes = ?")
            params.append(kwargs["notes"])

        params.append(task_id)
        conn.execute(f"UPDATE tasks SET {', '.join(update_fields)} WHERE id = ?", params)

        log_details = kwargs.get("details", f"Статус изменен на {new_status}")
        conn.execute("""
            INSERT INTO activity_log (task_id, user_id, user_name, action, details, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (task_id, user_id, user_name, new_status, log_details, now))

    conn.close()
    sync_db_to_s3()
    return get_task(task_id)

def assign_task(task_id: int, designer_id: int, admin_id: int, admin_name: str) -> Optional[Dict[str, Any]]:
    conn = get_db_connection()
    now = datetime.now(timezone.utc).isoformat()
    with conn:
        task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not task:
            conn.close()
            return None

        designer = conn.execute("SELECT * FROM users WHERE telegram_id = ?", (designer_id,)).fetchone()
        if not designer:
            conn.close()
            return None

        old_assigned_to_id = task["assigned_to_id"]
        old_assigned_to_name = task["assigned_to_name"] or task["assigned_to_username"]

        d_name = f"{designer['first_name'] or ''} {designer['last_name'] or ''}".strip() or "Дизайнер"
        d_username = designer["username"]
        designer_full = f"{d_name} (@{d_username})" if d_username else d_name

        conn.execute("""
            UPDATE tasks 
            SET status = 'in_progress',
                assigned_to_id = ?,
                assigned_to_name = ?,
                assigned_to_username = ?,
                assigned_at = ?,
                updated_at = ?
            WHERE id = ?
        """, (designer["telegram_id"], d_name, d_username, now, now, task_id))

        if old_assigned_to_id and old_assigned_to_id != designer["telegram_id"]:
            log_details = f"Администратор переназначил задачу с {old_assigned_to_name or old_assigned_to_id} на {designer_full}"
        else:
            log_details = f"Администратор назначил задачу дизайнеру {designer_full}"

        conn.execute("""
            INSERT INTO activity_log (task_id, user_id, user_name, action, details, created_at)
            VALUES (?, ?, ?, 'assigned', ?, ?)
        """, (task_id, admin_id, admin_name, log_details, now))

    conn.close()
    sync_db_to_s3()
    return {
        "task": get_task(task_id),
        "old_assigned_to_id": old_assigned_to_id,
        "designer": dict(designer)
    }

def update_task_images_count(task_id: int, images_count: Optional[int], user_id: int, user_name: str) -> Optional[Dict[str, Any]]:
    conn = get_db_connection()
    now = datetime.now(timezone.utc).isoformat()
    with conn:
        cur = conn.execute("UPDATE tasks SET images_count = ?, updated_at = ? WHERE id = ?", (images_count, now, task_id))
        if cur.rowcount == 0:
            conn.close()
            return None
        details = f"Количество изображений изменено на {images_count} шт." if images_count is not None else "Количество изображений очищено"
        conn.execute("""
            INSERT INTO activity_log (task_id, user_id, user_name, action, details, created_at)
            VALUES (?, ?, ?, 'update_images_count', ?, ?)
        """, (task_id, user_id, user_name, details, now))
    conn.close()
    sync_db_to_s3()
    return get_task(task_id)

def set_task_archived(task_id: int, is_archived: bool, user_id: int = 0, user_name: str = "Администратор") -> Optional[Dict[str, Any]]:
    conn = get_db_connection()
    now = datetime.now(timezone.utc).isoformat()
    val = 1 if is_archived else 0
    with conn:
        cur = conn.execute("UPDATE tasks SET is_archived = ?, updated_at = ? WHERE id = ?", (val, now, task_id))
        if cur.rowcount == 0:
            conn.close()
            return None
        action = "archived" if is_archived else "unarchived"
        conn.execute("""
            INSERT INTO activity_log (task_id, user_id, user_name, action, details, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (task_id, user_id, user_name, action, "Задача перенесена в архив" if is_archived else "Задача восстановлена из архива", now))
    conn.close()
    sync_db_to_s3()
    return get_task(task_id)

def delete_task(task_id: int, user_id: int = 0, user_name: str = "Администратор") -> bool:
    conn = get_db_connection()
    now = datetime.now(timezone.utc).isoformat()
    with conn:
        task = conn.execute("SELECT sku FROM tasks WHERE id = ?", (task_id,)) .fetchone()
        if not task:
            conn.close()
            return False
        sku = task["sku"]
        conn.execute("DELETE FROM activity_log WHERE task_id = ?", (task_id,))
        conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        conn.execute("""
            INSERT INTO activity_log (task_id, user_id, user_name, action, details, created_at)
            VALUES (NULL, ?, ?, 'deleted', ?, ?)
        """, (user_id, user_name, f"Удалена задача {sku} (ID {task_id})", now))
    conn.close()
    sync_db_to_s3()
    return True

def set_pool_archived(pool_name: str, is_archived: bool, user_id: int = 0, user_name: str = "Администратор") -> int:
    conn = get_db_connection()
    now = datetime.now(timezone.utc).isoformat()
    val = 1 if is_archived else 0
    with conn:
        cur = conn.execute("UPDATE tasks SET is_archived = ?, updated_at = ? WHERE pool_name = ?", (val, now, pool_name))
        affected = cur.rowcount
        action = "pool_archived" if is_archived else "pool_unarchived"
        conn.execute("""
            INSERT INTO activity_log (task_id, user_id, user_name, action, details, created_at)
            VALUES (NULL, ?, ?, ?, ?, ?)
        """, (user_id, user_name, action, f"Пул '{pool_name}' ({affected} задач) {'перенесен в архив' if is_archived else 'восстановлен из архива'}", now))
    conn.close()
    sync_db_to_s3()
    return affected

def delete_pool(pool_name: str, user_id: int = 0, user_name: str = "Администратор") -> int:
    conn = get_db_connection()
    now = datetime.now(timezone.utc).isoformat()
    with conn:
        task_rows = conn.execute("SELECT id FROM tasks WHERE pool_name = ?", (pool_name,)).fetchall()
        task_ids = [r["id"] for r in task_rows]
        if task_ids:
            placeholders = ",".join("?" * len(task_ids))
            conn.execute(f"DELETE FROM activity_log WHERE task_id IN ({placeholders})", task_ids)
        cur = conn.execute("DELETE FROM tasks WHERE pool_name = ?", (pool_name,))
        deleted_count = cur.rowcount
        conn.execute("DELETE FROM pool_messages WHERE pool_name = ?", (pool_name,))
        conn.execute("""
            INSERT INTO activity_log (task_id, user_id, user_name, action, details, created_at)
            VALUES (NULL, ?, ?, 'pool_deleted', ?, ?)
        """, (user_id, user_name, f"Удален пул '{pool_name}' ({deleted_count} задач)", now))
    conn.close()
    sync_db_to_s3()
    return deleted_count

def rename_pool(old_name: str, new_name: str, user_id: int = 0, user_name: str = "Администратор") -> int:
    conn = get_db_connection()
    now = datetime.now(timezone.utc).isoformat()
    old_name = old_name.strip()
    new_name = new_name.strip()
    if not old_name or not new_name or old_name == new_name:
        conn.close()
        return 0
    with conn:
        cur = conn.execute("UPDATE tasks SET pool_name = ?, updated_at = ? WHERE pool_name = ?", (new_name, now, old_name))
        affected = cur.rowcount

        existing = conn.execute("SELECT pool_name FROM pool_messages WHERE pool_name = ?", (new_name,)).fetchone()
        if existing:
            conn.execute("DELETE FROM pool_messages WHERE pool_name = ?", (old_name,))
        else:
            conn.execute("UPDATE pool_messages SET pool_name = ?, updated_at = ? WHERE pool_name = ?", (new_name, now, old_name))

        conn.execute("""
            INSERT INTO activity_log (task_id, user_id, user_name, action, details, created_at)
            VALUES (NULL, ?, ?, 'pool_renamed', ?, ?)
        """, (user_id, user_name, f"Пул переименован: '{old_name}' -> '{new_name}' ({affected} задач)", now))
    conn.close()
    sync_db_to_s3()
    return affected

def get_pool_bitrix_url(pool_name: str) -> Optional[str]:
    conn = get_db_connection()
    row = conn.execute(
        "SELECT bitrix_url FROM tasks WHERE pool_name = ? AND bitrix_url IS NOT NULL AND bitrix_url != '' ORDER BY id DESC LIMIT 1",
        (pool_name,)
    ).fetchone()
    conn.close()
    return row["bitrix_url"] if row else None

def update_pool_bitrix_url(pool_name: str, bitrix_url: Optional[str], user_id: int = 0, user_name: str = "Администратор") -> int:
    conn = get_db_connection()
    now = datetime.now(timezone.utc).isoformat()
    clean_url = (bitrix_url or "").strip() or None
    pool_name = pool_name.strip()
    with conn:
        cur = conn.execute("UPDATE tasks SET bitrix_url = ?, updated_at = ? WHERE pool_name = ?", (clean_url, now, pool_name))
        affected = cur.rowcount
        conn.execute("""
            INSERT INTO activity_log (task_id, user_id, user_name, action, details, created_at)
            VALUES (NULL, ?, ?, 'pool_bitrix_updated', ?, ?)
        """, (user_id, user_name, f"Обновлена ссылка Битрикс24 для пула '{pool_name}' ({affected} задач)", now))
    conn.close()
    sync_db_to_s3()
    return affected


def get_stats() -> Dict[str, Any]:
    conn = get_db_connection()
    stats = {}
    rows = conn.execute("SELECT status, count(*) as count FROM tasks WHERE is_archived = 0 GROUP BY status").fetchall()
    for r in rows:
        stats[r["status"]] = r["count"]

    total = conn.execute("SELECT count(*) as count FROM tasks WHERE is_archived = 0").fetchone()["count"]
    stats["total"] = total
    stats["archived"] = conn.execute("SELECT count(*) as count FROM tasks WHERE is_archived = 1").fetchone()["count"]
    total_images = conn.execute("SELECT sum(images_count) as total_img FROM tasks WHERE is_archived = 0 AND images_count IS NOT NULL").fetchone()["total_img"] or 0
    stats["total_images"] = int(total_images)

    # Designer stats
    designer_stats = conn.execute("""
        SELECT assigned_to_name, assigned_to_username, count(*) as completed_count
        FROM tasks
        WHERE status = 'completed' AND assigned_to_name IS NOT NULL AND is_archived = 0
        GROUP BY assigned_to_id
    """).fetchall()
    stats["designers"] = [dict(d) for d in designer_stats]

    conn.close()
    return stats

def get_setting(key: str, default: Optional[str] = None) -> Optional[str]:
    conn = get_db_connection()
    row = conn.execute("SELECT value FROM system_settings WHERE key = ?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else default

def set_setting(key: str, value: str):
    conn = get_db_connection()
    with conn:
        conn.execute("INSERT OR REPLACE INTO system_settings (key, value) VALUES (?, ?)", (key, value))
    conn.close()
    sync_db_to_s3()

def save_pool_message(pool_name: str, chat_id: int, message_id: int, thread_id: Optional[int] = None) -> None:
    conn = get_db_connection()
    now = datetime.now(timezone.utc).isoformat()
    with conn:
        conn.execute("""
            INSERT INTO pool_messages (pool_name, chat_id, message_id, thread_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(pool_name) DO UPDATE SET
                chat_id = excluded.chat_id,
                message_id = excluded.message_id,
                thread_id = excluded.thread_id,
                updated_at = excluded.updated_at
        """, (pool_name, chat_id, message_id, thread_id, now, now))
    conn.close()
    sync_db_to_s3()

def get_pool_message(pool_name: str) -> Optional[Dict[str, Any]]:
    conn = get_db_connection()
    row = conn.execute("SELECT * FROM pool_messages WHERE pool_name = ?", (pool_name,)).fetchone()
    conn.close()
    return dict(row) if row else None

def delete_pool_message(pool_name: str) -> None:
    conn = get_db_connection()
    with conn:
        conn.execute("DELETE FROM pool_messages WHERE pool_name = ?", (pool_name,))
    conn.close()
    sync_db_to_s3()

def get_pools_summary() -> List[Dict[str, Any]]:
    conn = get_db_connection()
    rows = conn.execute("""
        SELECT 
            COALESCE(pool_name, 'Без пула') as pool_name,
            COUNT(*) as total_tasks,
            SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) as completed_tasks,
            SUM(CASE WHEN status IN ('in_progress', 'internal_review', 'client_review', 'export_ready') THEN 1 ELSE 0 END) as active_tasks,
            SUM(CASE WHEN status = 'available' THEN 1 ELSE 0 END) as available_tasks,
            COALESCE(SUM(images_count), 0) as total_images,
            MAX(bitrix_url) as bitrix_url,
            MIN(is_archived) as has_active,
            MAX(is_archived) as all_archived,
            MAX(updated_at) as last_updated_at
        FROM tasks
        GROUP BY pool_name
        ORDER BY last_updated_at DESC
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_tasks_for_export(pool_names: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    conn = get_db_connection()
    if pool_names:
        clean_names = [p.strip() for p in pool_names if p and p.strip()]
        if clean_names:
            placeholders = ",".join("?" * len(clean_names))
            rows = conn.execute(
                f"SELECT * FROM tasks WHERE pool_name IN ({placeholders}) ORDER BY pool_name ASC, id ASC",
                clean_names
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM tasks ORDER BY pool_name ASC, id ASC").fetchall()
    else:
        rows = conn.execute("SELECT * FROM tasks ORDER BY pool_name ASC, id ASC").fetchall()
    conn.close()
    return [dict(r) for r in rows]

