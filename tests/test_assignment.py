import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from fastapi.testclient import TestClient
from support import isolated_app


class AssignmentTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix="ugc-assignment-test-")
        cls.app, cls.db = isolated_app(Path(cls.directory.name) / "crm.db")
        cls.client = TestClient(cls.app)
        cls.designer1 = {"X-Dev-User-Id": "10001"}
        cls.designer2 = {"X-Dev-User-Id": "10002"}
        cls.admin = {"X-Dev-User-Id": "999999999"}

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        cls.directory.cleanup()

    def test_assign_and_reassign_task(self):
        # Register users
        self.client.get("/api/me", headers=self.admin)
        self.client.get("/api/me", headers=self.designer1)
        self.client.get("/api/me", headers=self.designer2)

        # Create an available task
        task_id = self.db.create_tasks_bulk([{"sku": "ASSIGN-001", "pool_name": "Тест назначения"}], 999999999, "Админ")[0]
        task = self.db.get_task(task_id)
        self.assertEqual(task["status"], "available")
        self.assertIsNone(task["assigned_to_id"])

        # 1. Non-admin cannot assign
        res_forbidden = self.client.post("/api/tasks/assign", json={"task_id": task_id, "designer_id": 10001}, headers=self.designer1)
        self.assertEqual(res_forbidden.status_code, 403)

        # 2. Admin assigns to designer 1
        res = self.client.post("/api/tasks/assign", json={"task_id": task_id, "designer_id": 10001}, headers=self.admin)
        self.assertEqual(res.status_code, 200, res.text)
        assigned_task = res.json()["task"]
        self.assertEqual(assigned_task["status"], "in_progress")
        self.assertEqual(assigned_task["assigned_to_id"], 10001)
        self.assertIn("Анна", assigned_task["assigned_to_name"])

        # 3. Admin reassigns task to designer 2
        res2 = self.client.post("/api/tasks/assign", json={"task_id": task_id, "designer_id": 10002}, headers=self.admin)
        self.assertEqual(res2.status_code, 200, res2.text)
        reassigned_task = res2.json()["task"]
        self.assertEqual(reassigned_task["status"], "in_progress")
        self.assertEqual(reassigned_task["assigned_to_id"], 10002)
        self.assertIn("Михаил", reassigned_task["assigned_to_name"])

    def test_create_batch_with_designer_assignment(self):
        # Register users
        self.client.get("/api/me", headers=self.admin)
        self.client.get("/api/me", headers=self.designer1)

        payload = {
            "pool_name": "Пак для дизайнера",
            "parent_url": "https://cloud.example.com/test-share",
            "skus_text": "BATCH-01, BATCH-02",
            "assign_to_id": 10001
        }
        res = self.client.post("/api/tasks/create-batch", json=payload, headers=self.admin)
        self.assertEqual(res.status_code, 200, res.text)
        data = res.json()
        self.assertEqual(data["count"], 2)

        # Verify tasks are in_progress and assigned to designer1
        for tid in data["ids"]:
            t = self.db.get_task(tid)
            self.assertEqual(t["status"], "in_progress")
            self.assertEqual(t["assigned_to_id"], 10001)

    def test_add_skus_to_pool_with_assignment(self):
        self.client.get("/api/me", headers=self.admin)
        self.client.get("/api/me", headers=self.designer2)

        pool_name = "Существующий пул"
        self.db.create_tasks_bulk([{"sku": "EXIST-01", "pool_name": pool_name}], 999999999, "Админ")

        payload = {
            "pool_name": pool_name,
            "skus_text": "EXTRA-01, EXTRA-02",
            "assign_to_id": 10002
        }
        res = self.client.post("/api/pools/add-skus", json=payload, headers=self.admin)
        self.assertEqual(res.status_code, 200, res.text)
        data = res.json()
        self.assertEqual(data["created_count"], 2)

        for tid in data["task_ids"]:
            t = self.db.get_task(tid)
            self.assertEqual(t["status"], "in_progress")
            self.assertEqual(t["assigned_to_id"], 10002)

    def test_users_active_tasks_count(self):
        res = self.client.get("/api/users", headers=self.admin)
        self.assertEqual(res.status_code, 200)
        users = res.json()["users"]
        self.assertTrue(len(users) > 0)
        # Check active_tasks_count field is present
        for u in users:
            self.assertIn("active_tasks_count", u)
            self.assertIsInstance(u["active_tasks_count"], int)
