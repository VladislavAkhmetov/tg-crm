"""Local UI preview. All changes stay in a temporary, disposable database."""
import os
import tempfile
from pathlib import Path
import uvicorn
from support import isolated_app


if __name__ == "__main__":
    os.chdir(Path(__file__).resolve().parents[1])
    with tempfile.TemporaryDirectory(prefix="ugc-ui-preview-") as directory:
        app, db = isolated_app(Path(directory) / "crm.db")
        db.upsert_user(999999999, "art_lead", "Арт-лид", role="admin")
        db.upsert_user(10001, "designer_anna", "Анна", role="designer")
        db.upsert_user(10002, "designer_misha", "Михаил", role="designer")
        stages = ["internal_review", "available", "in_progress", "client_review", "export_ready", "completed", "internal_review", "available"]
        skus = ["130822OUT-99", "130754OUT-T4", "129829OUT-99", "129879OUT-99", "129889OUT-99", "129930OUT-99", "130754OUT-99", "129940OUT-99"]
        for index, (stage, sku) in enumerate(zip(stages, skus)):
            task_id = db.create_tasks_bulk([{
                "sku": sku, "pool_name": "Обувь · октябрь" if index < 6 else "Одежда · октябрь",
                "source_url": "https://cloud.example.com/",
            }], 999999999, "Арт-лид")[0]
            if stage != "available":
                user_id = 10001 if index % 2 == 0 else 10002
                db.update_task_status(task_id, "in_progress", user_id, "Анна" if user_id == 10001 else "Михаил")
                if stage != "in_progress":
                    db.update_task_status(task_id, stage, user_id, "Анна", internal_figma_url="https://www.figma.com/",
                                          client_figma_url="https://www.figma.com/" if stage in ["client_review", "export_ready", "completed"] else None,
                                          yandex_disk_url="https://disk.yandex.ru/" if stage == "completed" else None,
                                          notes="Проверь отступы на втором слайде" if index == 0 else None)
                else:
                    db.update_task_status(task_id, "in_progress", user_id, "Анна", notes="Правки: поправить размер заголовка и выровнять плашку по сетке.", preserve_assignee=True)
        print("Isolated preview: http://127.0.0.1:8001", flush=True)
        uvicorn.run(app, host="127.0.0.1", port=8001, log_level="warning")
