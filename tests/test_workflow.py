import tempfile
import unittest
from pathlib import Path
from fastapi.testclient import TestClient
from support import isolated_app


class WorkflowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix="ugc-workflow-test-")
        cls.app, cls.db = isolated_app(Path(cls.directory.name) / "crm.db")
        cls.client = TestClient(cls.app)
        cls.designer = {"X-Dev-User-Id": "10001"}
        cls.admin = {"X-Dev-User-Id": "999999999"}

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        cls.directory.cleanup()

    def test_full_workflow_and_revision_preserves_designer(self):
        self.client.get("/api/me", headers=self.admin)
        self.client.get("/api/me", headers=self.designer)
        task_id = self.db.create_tasks_bulk([{"sku": "130754OUT-99", "pool_name": "Обувь"}], 999999999, "Лид")[0]

        def post(endpoint, body, headers):
            response = self.client.post(f"/api/tasks/{endpoint}", json={"task_id": task_id, **body}, headers=headers)
            self.assertEqual(response.status_code, 200, response.text)
            return response.json()["task"]

        task = post("take", {}, self.designer)
        self.assertEqual(task["assigned_to_id"], 10001)
        self.assertEqual(self.client.post("/api/tasks/take", json={"task_id": task_id}, headers=self.admin).status_code, 400)
        # Without images_count, submit-internal must fail with 400
        res_no_img = self.client.post("/api/tasks/submit-internal", json={"task_id": task_id, "internal_figma_url": "https://www.figma.com/"}, headers=self.designer)
        self.assertEqual(res_no_img.status_code, 400)
        task = post("submit-internal", {"internal_figma_url": "https://www.figma.com/", "images_count": 10, "notes": "Готово к проверке"}, self.designer)
        self.assertEqual(task["status"], "internal_review")
        self.assertEqual(task["images_count"], 10)
        task = post("reject-feedback", {"comment": "Исправить заголовок", "return_to_pool": False}, self.admin)
        self.assertEqual(task["status"], "in_progress")
        self.assertEqual(task["assigned_to_id"], 10001, "Review must not reassign the task to the lead")
        self.assertIn("Анна", task["assigned_to_name"])
        self.assertEqual(task["notes"], "Правки: Исправить заголовок")
        post("submit-internal", {"internal_figma_url": "https://www.figma.com/", "images_count": 10, "notes": ""}, self.designer)
        task = post("submit-client", {"client_figma_url": "https://www.figma.com/"}, self.admin)
        self.assertEqual(task["status"], "client_review")
        task = post("submit-export", {}, self.admin)
        self.assertEqual(task["status"], "export_ready")
        task = post("complete", {"yandex_disk_url": "https://disk.yandex.ru/"}, self.designer)
        self.assertEqual(task["status"], "completed")
        self.assertEqual(task["assigned_to_id"], 10001)

    def test_task_and_pool_archive_and_delete(self):
        # 1. Non-admin gets 403
        task_id = self.db.create_tasks_bulk([{"sku": "999001OUT-01", "pool_name": "Тестовый пул"}], 999999999, "Лид")[0]
        res = self.client.post(f"/api/tasks/{task_id}/archive", json={"is_archived": True}, headers=self.designer)
        self.assertEqual(res.status_code, 403)
        res = self.client.delete(f"/api/tasks/{task_id}", headers=self.designer)
        self.assertEqual(res.status_code, 403)

        # 2. Admin archives task
        res = self.client.post(f"/api/tasks/{task_id}/archive", json={"is_archived": True}, headers=self.admin)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["task"]["is_archived"], 1)

        # 3. Admin unarchives task
        res = self.client.post(f"/api/tasks/{task_id}/archive", json={"is_archived": False}, headers=self.admin)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["task"]["is_archived"], 0)

        # 4. Admin deletes task
        res = self.client.delete(f"/api/tasks/{task_id}", headers=self.admin)
        self.assertEqual(res.status_code, 200)
        self.assertIsNone(self.db.get_task(task_id))

        # 5. Pool archiving and deletion
        pool_tasks = [
            {"sku": "POOL-01", "pool_name": "Пул на удаление"},
            {"sku": "POOL-02", "pool_name": "Пул на удаление"}
        ]
        created_ids = self.db.create_tasks_bulk(pool_tasks, 999999999, "Лид")
        self.assertEqual(len(created_ids), 2)

        # Designer cannot archive or delete pool
        res = self.client.post("/api/pools/archive", json={"pool_name": "Пул на удаление", "is_archived": True}, headers=self.designer)
        self.assertEqual(res.status_code, 403)
        res = self.client.post("/api/pools/delete", json={"pool_name": "Пул на удаление"}, headers=self.designer)
        self.assertEqual(res.status_code, 403)

        # Admin archives pool
        res = self.client.post("/api/pools/archive", json={"pool_name": "Пул на удаление", "is_archived": True}, headers=self.admin)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["affected"], 2)
        for tid in created_ids:
            self.assertEqual(self.db.get_task(tid)["is_archived"], 1)

        # Admin unarchives pool
        res = self.client.post("/api/pools/archive", json={"pool_name": "Пул на удаление", "is_archived": False}, headers=self.admin)
        self.assertEqual(res.status_code, 200)
        for tid in created_ids:
            self.assertEqual(self.db.get_task(tid)["is_archived"], 0)

        # Admin deletes pool
        res = self.client.post("/api/pools/delete", json={"pool_name": "Пул на удаление"}, headers=self.admin)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["deleted_count"], 2)
        for tid in created_ids:
            self.assertIsNone(self.db.get_task(tid))

    def test_pool_rename_and_add_skus(self):
        created_ids = self.db.create_tasks_bulk([
            {"sku": "RENAME-01", "pool_name": "Старый пул"},
            {"sku": "RENAME-02", "pool_name": "Старый пул"}
        ], 999999999, "Лид")

        # Designer cannot rename or add skus to pool
        res = self.client.post("/api/pools/rename", json={"old_pool_name": "Старый пул", "new_pool_name": "Новый пул"}, headers=self.designer)
        self.assertEqual(res.status_code, 403)
        res = self.client.post("/api/pools/add-skus", json={"pool_name": "Старый пул", "skus_text": "RENAME-03"}, headers=self.designer)
        self.assertEqual(res.status_code, 403)

        # Admin renames pool
        res = self.client.post("/api/pools/rename", json={"old_pool_name": "Старый пул", "new_pool_name": "Новый пул"}, headers=self.admin)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["affected"], 2)
        for tid in created_ids:
            self.assertEqual(self.db.get_task(tid)["pool_name"], "Новый пул")

        # Admin adds SKUs to pool
        res = self.client.post("/api/pools/add-skus", json={
            "pool_name": "Новый пул",
            "skus_text": "RENAME-03, RENAME-04\nRENAME-05",
            "source_url": "https://cloud.example.com/"
        }, headers=self.admin)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["created_count"], 3)
        for tid in res.json()["task_ids"]:
            task = self.db.get_task(tid)
            self.assertEqual(task["pool_name"], "Новый пул")
            self.assertEqual(task["status"], "available")
            self.assertEqual(task["source_url"], "https://cloud.example.com/")


    def test_cloud_direct_url_and_single_sku(self):
        import cloud_storage as sc
        parent = "https://cloud.example.com/index.php/s/oEbdwTHHFM2N2zK?dir=/%D1%84%D0%BE%D1%82%D0%BE"
        url1 = sc.build_direct_sku_url(parent, "130754OUT-99")
        self.assertIn("130754OUT-99", url1)
        self.assertNotIn("130754OUT-99/130754OUT-99", url1)

        # Calling with single-sku path must not duplicate the SKU
        single_path_url = "https://cloud.example.com/index.php/s/oEbdwTHHFM2N2zK?dir=/%D1%84%D0%BE%D1%82%D0%BE/130754OUT-99"
        url2 = sc.build_direct_sku_url(single_path_url, "130754OUT-99")
        self.assertEqual(url2, single_path_url)

    def test_duplicate_detection_and_smart_batch_filter(self):
        pool_url = "https://cloud.example.com/index.php/s/shareTokenTest?dir=/фото"
        # 1. Create initial task
        initial_id = self.db.create_tasks_bulk(
            [{"sku": "SKU-DUP-1", "pool_name": "Первый пул", "source_url": pool_url}],
            999999999, "Лид"
        )[0]
        self.assertIsNotNone(initial_id)

        # 2. Check SKUs endpoint
        res = self.client.post("/api/tasks/check-skus", json={
            "skus_text": "SKU-DUP-1\nSKU-NEW-1",
            "parent_url": pool_url
        }, headers=self.admin)
        self.assertEqual(res.status_code, 200)
        analysis = res.json()["analysis"]
        self.assertEqual(analysis["existing_pool_name"], "Первый пул")
        self.assertEqual(len(analysis["active_conflicts"]), 1)
        self.assertEqual(analysis["active_conflicts"][0]["sku"], "SKU-DUP-1")
        self.assertEqual(analysis["new_skus"], ["SKU-NEW-1"])

        # 3. Create batch with mixed SKUs (should skip active duplicate)
        res = self.client.post("/api/tasks/create-batch", json={
            "pool_name": "Второй пул",
            "parent_url": pool_url,
            "skus_text": "SKU-DUP-1\nSKU-NEW-1",
            "skip_active_duplicates": True
        }, headers=self.admin)
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["skipped_active"], 1)
        self.assertIn("SKU-DUP-1", data["skipped_skus"])

        # 4. Attempt to add batch where ALL SKUs are active duplicates
        res = self.client.post("/api/tasks/create-batch", json={
            "pool_name": "Третий пул",
            "parent_url": pool_url,
            "skus_text": "SKU-DUP-1\nSKU-NEW-1",  # now both are active!
            "skip_active_duplicates": True
        }, headers=self.admin)
        self.assertEqual(res.status_code, 400)
        self.assertIn("уже находятся в активной работе", res.json()["detail"])

    def test_backup_endpoints(self):
        # Designer cannot trigger backup
        res = self.client.post("/api/admin/backup", headers=self.designer)
        self.assertEqual(res.status_code, 403)

        # Admin can trigger backup
        res = self.client.post("/api/admin/backup", headers=self.admin)
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()["ok"])

    def test_images_count_workflow_and_editing_on_any_stage(self):
        other_designer = {"X-Dev-User-Id": "10002"}
        self.client.get("/api/me", headers=other_designer)

        task_id = self.db.create_tasks_bulk([{"sku": "IMG-COUNT-01", "pool_name": "Пул Изображений"}], 999999999, "Лид")[0]

        # 1. Take task
        res = self.client.post("/api/tasks/take", json={"task_id": task_id}, headers=self.designer)
        self.assertEqual(res.status_code, 200)

        # 2. Submit internal review with images_count = 10
        res = self.client.post("/api/tasks/submit-internal", json={
            "task_id": task_id,
            "internal_figma_url": "https://figma.com/design/test",
            "images_count": 10,
            "notes": "Пак готов"
        }, headers=self.designer)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["task"]["images_count"], 10)
        self.assertEqual(res.json()["task"]["status"], "internal_review")

        # 3. Designer edits images_count while in internal_review (to 12)
        res = self.client.post(f"/api/tasks/{task_id}/images-count", json={"images_count": 12}, headers=self.designer)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["task"]["images_count"], 12)

        # 4. Another designer cannot edit images_count (403)
        res = self.client.post(f"/api/tasks/{task_id}/images-count", json={"images_count": 9}, headers=other_designer)
        self.assertEqual(res.status_code, 403)

        # 5. Admin moves to client_review
        res = self.client.post("/api/tasks/submit-client", json={
            "task_id": task_id,
            "client_figma_url": "https://figma.com/design/client"
        }, headers=self.admin)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["task"]["status"], "client_review")
        # images_count is preserved
        self.assertEqual(res.json()["task"]["images_count"], 12)

        # 6. Admin edits images_count on client_review stage (to 11) using body endpoint
        res = self.client.post("/api/tasks/images-count", json={"task_id": task_id, "images_count": 11}, headers=self.admin)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["task"]["images_count"], 11)

        # 7. Validation: invalid count (0 or negative or >500) returns 400
        res = self.client.post(f"/api/tasks/{task_id}/images-count", json={"images_count": 0}, headers=self.designer)
        self.assertEqual(res.status_code, 400)
        res = self.client.post(f"/api/tasks/{task_id}/images-count", json={"images_count": 999}, headers=self.designer)
        self.assertEqual(res.status_code, 400)

        # 8. Check stats includes total_images
        stats_res = self.client.get("/api/stats", headers=self.designer)
        self.assertEqual(stats_res.status_code, 200)
        self.assertGreaterEqual(stats_res.json()["total_images"], 11)

    def test_export_endpoints(self):
        # 1. Non-admin gets 403
        self.assertEqual(self.client.get("/api/export/pools", headers=self.designer).status_code, 403)
        self.assertEqual(self.client.post("/api/export/excel", json={}, headers=self.designer).status_code, 403)
        self.assertEqual(self.client.post("/api/export/google-sheets", json={}, headers=self.designer).status_code, 403)
        self.assertEqual(self.client.get("/api/settings/google-sheets", headers=self.designer).status_code, 403)

        # Create a sample task in pool "Экспорт-Пул"
        self.db.create_tasks_bulk([{"sku": "EXP-001", "pool_name": "Экспорт-Пул"}], 999999999, "Лид")

        # 2. Admin gets pool summary
        res = self.client.get("/api/export/pools", headers=self.admin)
        self.assertEqual(res.status_code, 200)
        pools = res.json()["pools"]
        self.assertIsInstance(pools, list)
        self.assertTrue(any(p["pool_name"] == "Экспорт-Пул" for p in pools))

        # 3. Admin exports single pool to Excel
        res_excel = self.client.post("/api/export/excel", json={"pool_names": ["Экспорт-Пул"]}, headers=self.admin)
        self.assertEqual(res_excel.status_code, 200)
        self.assertEqual(res_excel.headers["content-type"], "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        self.assertGreater(len(res_excel.content), 1000)

        # 4. Admin exports all pools to Excel
        res_excel_all = self.client.post("/api/export/excel", json={"pool_names": None}, headers=self.admin)
        self.assertEqual(res_excel_all.status_code, 200)
        self.assertGreater(len(res_excel_all.content), 1000)

        # 5. Admin gets Google Sheets settings and template
        res_gs = self.client.get("/api/settings/google-sheets", headers=self.admin)
        self.assertEqual(res_gs.status_code, 200)
        self.assertIn("script_template", res_gs.json())

        # 6. Google Sheets export without configured webhook returns 400
        res_gs_err = self.client.post("/api/export/google-sheets", json={"pool_names": ["Обувь"]}, headers=self.admin)
        self.assertEqual(res_gs_err.status_code, 400)

    def test_pool_bitrix_url_workflow(self):
        pool_name = "Битрикс-Тест"
        bitrix_url_1 = "https://b24-company.bitrix24.ru/company/personal/user/1/tasks/task/view/101/"
        bitrix_url_2 = "https://b24-company.bitrix24.ru/company/personal/user/1/tasks/task/view/202/"

        # 1. Create batch with bitrix_url
        res = self.client.post("/api/tasks/create-batch", json={
            "pool_name": pool_name,
            "skus_text": "BTRX-01\nBTRX-02",
            "bitrix_url": bitrix_url_1,
            "skip_active_duplicates": True
        }, headers=self.admin)
        self.assertEqual(res.status_code, 200)
        t_ids = res.json()["ids"]
        self.assertEqual(len(t_ids), 2)

        for tid in t_ids:
            task = self.db.get_task(tid)
            self.assertEqual(task["bitrix_url"], bitrix_url_1)

        # 2. Add new SKUs without explicit bitrix_url -> should inherit pool's bitrix_url
        res_add = self.client.post("/api/pools/add-skus", json={
            "pool_name": pool_name,
            "skus_text": "BTRX-03"
        }, headers=self.admin)
        self.assertEqual(res_add.status_code, 200)
        new_ids = res_add.json()["task_ids"]
        self.assertEqual(len(new_ids), 1)
        task3 = self.db.get_task(new_ids[0])
        self.assertEqual(task3["bitrix_url"], bitrix_url_1)

        # 3. Designer cannot update pool bitrix url (403)
        res_forbidden = self.client.post("/api/pools/update-bitrix", json={
            "pool_name": pool_name,
            "bitrix_url": bitrix_url_2
        }, headers=self.designer)
        self.assertEqual(res_forbidden.status_code, 403)

        # 4. Admin updates pool bitrix url (200)
        res_update = self.client.post("/api/pools/update-bitrix", json={
            "pool_name": pool_name,
            "bitrix_url": bitrix_url_2
        }, headers=self.admin)
        self.assertEqual(res_update.status_code, 200)
        self.assertEqual(res_update.json()["affected"], 3)

        # Check all 3 tasks in pool now have updated bitrix_url
        for tid in t_ids + new_ids:
            task = self.db.get_task(tid)
            self.assertEqual(task["bitrix_url"], bitrix_url_2)

        # 5. Check pools summary returns bitrix_url
        summary = self.db.get_pools_summary()
        b_pool = next((p for p in summary if p["pool_name"] == pool_name), None)
        self.assertIsNotNone(b_pool)
        self.assertEqual(b_pool["bitrix_url"], bitrix_url_2)

    def test_designer_rework_and_optional_comment(self):
        designer_misha = {"X-Dev-User-Id": "10002"}
        self.client.get("/api/me", headers=self.admin)
        self.client.get("/api/me", headers=self.designer)
        self.client.get("/api/me", headers=designer_misha)

        # 1. Create a task and take it
        task_id = self.db.create_tasks_bulk([{"sku": "REWORK-TEST-01", "pool_name": "Тест"}], 999999999, "Лид")[0]
        res = self.client.post("/api/tasks/take", json={"task_id": task_id}, headers=self.designer)
        self.assertEqual(res.status_code, 200)

        # Cannot return for revision when status is in_progress
        res = self.client.post("/api/tasks/reject-feedback", json={"task_id": task_id, "return_to_pool": False}, headers=self.designer)
        self.assertEqual(res.status_code, 400)

        # Submit to internal review
        res = self.client.post("/api/tasks/submit-internal", json={
            "task_id": task_id,
            "internal_figma_url": "https://figma.com/internal",
            "images_count": 5
        }, headers=self.designer)
        self.assertEqual(res.status_code, 200)

        # Another designer cannot take back task (403)
        res = self.client.post("/api/tasks/reject-feedback", json={"task_id": task_id, "return_to_pool": False}, headers=designer_misha)
        self.assertEqual(res.status_code, 403)

        # Assigned designer takes task back from internal review WITHOUT comment (optional comment)
        res = self.client.post("/api/tasks/reject-feedback", json={"task_id": task_id, "return_to_pool": False}, headers=self.designer)
        self.assertEqual(res.status_code, 200)
        t = res.json()["task"]
        self.assertEqual(t["status"], "in_progress")
        self.assertEqual(t["assigned_to_id"], 10001)
        self.assertEqual(t["notes"], "Требуются правки")

        # Designer resubmits to internal review
        res = self.client.post("/api/tasks/submit-internal", json={
            "task_id": task_id,
            "internal_figma_url": "https://figma.com/internal",
            "images_count": 5
        }, headers=self.designer)
        self.assertEqual(res.status_code, 200)

        # Admin sends to client review
        res = self.client.post("/api/tasks/submit-client", json={
            "task_id": task_id,
            "client_figma_url": "https://figma.com/client"
        }, headers=self.admin)
        self.assertEqual(res.status_code, 200)

        # Assigned designer takes task back from client review WITH comment
        res = self.client.post("/api/tasks/reject-feedback", json={
            "task_id": task_id,
            "return_to_pool": False,
            "comment": "Поправить цвета по запросу клиента"
        }, headers=self.designer)
        self.assertEqual(res.status_code, 200)
        t = res.json()["task"]
        self.assertEqual(t["status"], "in_progress")
        self.assertEqual(t["assigned_to_id"], 10001)
        self.assertEqual(t["notes"], "Правки: Поправить цвета по запросу клиента")

        # Admin can also return without comment
        res = self.client.post("/api/tasks/submit-internal", json={
            "task_id": task_id,
            "internal_figma_url": "https://figma.com/internal",
            "images_count": 5
        }, headers=self.designer)
        self.assertEqual(res.status_code, 200)

        res = self.client.post("/api/tasks/reject-feedback", json={
            "task_id": task_id,
            "return_to_pool": False,
            "comment": ""
        }, headers=self.admin)
        self.assertEqual(res.status_code, 200)
        t = res.json()["task"]
        self.assertEqual(t["status"], "in_progress")
        self.assertEqual(t["notes"], "Требуются правки")

        # Test return_to_pool by assigned designer
        res = self.client.post("/api/tasks/reject-feedback", json={
            "task_id": task_id,
            "return_to_pool": True,
            "comment": "Освобождаю задачу"
        }, headers=self.designer)
        self.assertEqual(res.status_code, 200)
        t = res.json()["task"]
        self.assertEqual(t["status"], "available")
        self.assertIsNone(t["assigned_to_id"])

        # Test return_to_pool by admin when assigned to another user
        res = self.client.post("/api/tasks/take", json={"task_id": task_id}, headers=designer_misha)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["task"]["assigned_to_id"], 10002)

        res = self.client.post("/api/tasks/reject-feedback", json={
            "task_id": task_id,
            "return_to_pool": True,
            "comment": "Администратор вернул в пул"
        }, headers=self.admin)
        self.assertEqual(res.status_code, 200)
        t = res.json()["task"]
        self.assertEqual(t["status"], "available")
        self.assertIsNone(t["assigned_to_id"])


if __name__ == "__main__":
    unittest.main()
