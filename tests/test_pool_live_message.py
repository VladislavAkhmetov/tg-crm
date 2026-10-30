import sys
import unittest
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from support import isolated_app
import database as db

# Load the real render_pool_message function directly from bot file
import importlib.util
spec = importlib.util.spec_from_file_location("real_bot", str(Path(__file__).resolve().parents[1] / "bot.py"))
real_bot = importlib.util.module_from_spec(spec)
# inject db into real_bot namespace
real_bot.__dict__["db"] = db
real_bot.__dict__["get_group_keyboard"] = lambda: {}
spec.loader.exec_module(real_bot)
render_pool_message = real_bot.render_pool_message

class PoolLiveMessageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix="ugc-pool-msg-test-")
        cls.db_path = Path(cls.directory.name) / "crm.db"
        cls.app, cls.db = isolated_app(cls.db_path)

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def test_pool_messages_db_operations(self):
        pool_name = "Тестовый пул сообщений"
        chat_id = -1001234567890
        msg_id = 45678
        thread_id = 99

        # 1. Save
        self.db.save_pool_message(pool_name, chat_id, msg_id, thread_id)

        # 2. Get
        info = self.db.get_pool_message(pool_name)
        self.assertIsNotNone(info)
        self.assertEqual(info["pool_name"], pool_name)
        self.assertEqual(info["chat_id"], chat_id)
        self.assertEqual(info["message_id"], msg_id)
        self.assertEqual(info["thread_id"], thread_id)

        # 3. Update existing
        new_msg_id = 45679
        self.db.save_pool_message(pool_name, chat_id, new_msg_id, thread_id)
        info2 = self.db.get_pool_message(pool_name)
        self.assertEqual(info2["message_id"], new_msg_id)

        # 4. Delete
        self.db.delete_pool_message(pool_name)
        self.assertIsNone(self.db.get_pool_message(pool_name))

    def test_render_pool_message_formatting(self):
        pool_name = "Пул Рендер Тест"
        
        # Create tasks in various states
        tasks = [
            {"sku": "SKU-001", "pool_name": pool_name},
            {"sku": "SKU-002", "pool_name": pool_name},
            {"sku": "SKU-003", "pool_name": pool_name},
        ]
        t_ids = self.db.create_tasks_bulk(tasks, 999999999, "Админ")

        # Initial render: all 3 available
        text = render_pool_message(pool_name)
        self.assertIn("Пул задач:", text)
        self.assertIn("SKU-001", text)
        self.assertIn("⚪ Свободен", text)
        self.assertIn("⚪ 3", text)

        # Designer takes SKU-001
        self.db.update_task_status(t_ids[0], "in_progress", 10001, "Иван", username="ivan_design")
        text = render_pool_message(pool_name)
        self.assertIn("🟡 В работе (@ivan_design)", text)
        self.assertIn("🟡 1", text)
        self.assertIn("⚪ 2", text)

        # Complete SKU-001
        self.db.update_task_status(t_ids[0], "completed", 10001, "Иван")
        text = render_pool_message(pool_name)
        self.assertIn("🟢 Готово", text)
        self.assertIn("🟢 1", text)

        # All completed
        self.db.update_task_status(t_ids[1], "completed", 10001, "Иван")
        self.db.update_task_status(t_ids[2], "completed", 10001, "Иван")
        text = render_pool_message(pool_name)
        self.assertIn("Все артикулы пула успешно согласованы и готовы", text)

    def test_render_pool_message_over_35_items(self):
        pool_name = "Большой Пул"
        tasks = [{"sku": f"BIG-{i:03d}", "pool_name": pool_name} for i in range(40)]
        self.db.create_tasks_bulk(tasks, 999999999, "Админ")

        text = render_pool_message(pool_name)
        self.assertIn("BIG-000", text)
        self.assertIn("BIG-034", text)
        self.assertNotIn("BIG-035", text)
        self.assertIn("и еще 5 артикулов в приложении", text)

    def test_render_pool_message_with_bitrix_url(self):
        pool_name = "Пул с Битриксом"
        bitrix_url = "https://company.bitrix24.ru/workgroups/group/5/tasks/task/view/888/"
        tasks = [
            {"sku": "BTX-1", "pool_name": pool_name, "bitrix_url": bitrix_url},
            {"sku": "BTX-2", "pool_name": pool_name, "bitrix_url": bitrix_url}
        ]
        self.db.create_tasks_bulk(tasks, 999999999, "Админ")

        text = render_pool_message(pool_name)
        self.assertIn("Пул задач:", text)
        self.assertIn("Битрикс24:", text)
        self.assertIn(bitrix_url, text)
        self.assertIn("Учет времени пула", text)

if __name__ == "__main__":
    unittest.main()

