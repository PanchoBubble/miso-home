from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import unittest

from miso.identity import VOICE_ACTOR, web_actor
from miso.memory import MemoryStore
from miso.tools import (
    HouseholdStore,
    InMemoryAuditLog,
    ScheduledItemWorker,
    ToolRegistry,
    ToolStatus,
    register_household_tools,
)


class HouseholdToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.path = Path(self.temporary.name) / "miso.sqlite3"
        self.clock = [datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)]
        self.registry = self.new_registry()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def new_registry(self) -> ToolRegistry:
        registry = ToolRegistry()
        register_household_tools(registry, self.path, now=lambda: self.clock[0])
        return registry

    def invoke(self, registry: ToolRegistry, name: str, arguments: dict):
        result = registry.invoke(name, arguments)
        self.assertEqual(result.status, ToolStatus.SUCCESS, result.error)
        return result.output

    def test_schema_migration_is_repeatable(self) -> None:
        MemoryStore(self.path).migrate()
        with MemoryStore(self.path).connect() as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        self.assertIn("scheduled_items", tables)
        self.assertIn("shopping_items", tables)

    def test_timers_create_update_cancel_and_survive_restart(self) -> None:
        created = self.invoke(
            self.registry,
            "timer_create",
            {"duration_seconds": 300, "title": "Tea"},
        )["timer"]
        timer_id = created["id"]
        self.assertEqual(created["status"], "pending")
        self.assertEqual(created["due_at"], "2026-08-22T12:05:00.000000+00:00")

        restarted = self.new_registry()
        listed = self.invoke(restarted, "timer_list", {})["timers"]
        self.assertEqual([item["id"] for item in listed], [timer_id])
        updated = self.invoke(
            restarted,
            "timer_update",
            {"id": timer_id, "duration_seconds": 600, "title": "Green tea"},
        )["timer"]
        self.assertEqual(updated["revision"], 2)
        self.assertEqual(updated["due_at"], "2026-08-22T12:10:00.000000+00:00")
        cancelled = self.invoke(
            restarted, "timer_cancel", {"id": timer_id}
        )["timer"]
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(cancelled["revision"], 3)
        duplicate = restarted.invoke("timer_cancel", {"id": timer_id})
        self.assertEqual(duplicate.status, ToolStatus.REJECTED)

    def test_overdue_timer_and_reminder_recover_after_restart(self) -> None:
        timer = self.invoke(
            self.registry, "timer_create", {"duration_seconds": 10}
        )["timer"]
        reminder = self.invoke(
            self.registry,
            "reminder_create",
            {"title": "Put the bins out", "due_at": "2026-08-22T13:00:10+01:00"},
        )["reminder"]
        self.clock[0] += timedelta(seconds=11)

        restarted = self.new_registry()
        timers = self.invoke(restarted, "timer_list", {"status": "all"})["timers"]
        reminders = self.invoke(
            restarted, "reminder_list", {"status": "all"}
        )["reminders"]
        self.assertEqual(timers[0]["id"], timer["id"])
        self.assertEqual(timers[0]["status"], "completed")
        self.assertEqual(reminders[0]["id"], reminder["id"])
        self.assertEqual(reminders[0]["status"], "completed")
        self.assertEqual(timers[0]["revision"], 2)

    def test_reminder_requires_timezone_and_future_time(self) -> None:
        missing_timezone = self.registry.invoke(
            "reminder_create",
            {"title": "Invalid", "due_at": "2026-08-22T13:00:00"},
        )
        self.assertEqual(missing_timezone.status, ToolStatus.REJECTED)
        past = self.registry.invoke(
            "reminder_create",
            {"title": "Past", "due_at": "2026-08-22T10:00:00Z"},
        )
        self.assertEqual(past.status, ToolStatus.REJECTED)

    def test_shopping_items_update_remove_and_survive_restart(self) -> None:
        created = self.invoke(
            self.registry,
            "shopping_add",
            {
                "list_name": "Groceries",
                "name": "Coffee",
                "quantity": 2,
            },
        )["item"]
        self.assertTrue(created["shared"])
        self.assertEqual(created["added_by"], VOICE_ACTOR.actor_id)
        self.assertEqual(created["actor_id"], VOICE_ACTOR.actor_id)
        self.assertEqual(created["revision"], 1)

        restarted = self.new_registry()
        listed = self.invoke(
            restarted, "shopping_list", {"list_name": "groceries"}
        )["items"]
        self.assertEqual([item["id"] for item in listed], [created["id"]])
        updated = self.invoke(
            restarted,
            "shopping_update",
            {"id": created["id"], "quantity": 3, "completed": True},
        )["item"]
        self.assertTrue(updated["completed"])
        self.assertEqual(updated["quantity"], 3)
        self.assertEqual(updated["revision"], 2)
        self.assertEqual(
            self.invoke(restarted, "shopping_list", {"list_name": "groceries"})[
                "items"
            ],
            [],
        )
        removed = self.invoke(
            restarted, "shopping_remove", {"id": created["id"]}
        )["item"]
        self.assertEqual(removed["status"], "removed")
        history = self.invoke(
            self.new_registry(),
            "shopping_list",
            {
                "list_name": "groceries",
                "include_completed": True,
                "include_removed": True,
            },
        )["items"]
        self.assertEqual(history[0]["status"], "removed")
        self.assertEqual(history[0]["revision"], 3)

    def test_invalid_inputs_never_write(self) -> None:
        result = self.registry.invoke(
            "shopping_add", {"name": "Milk", "quantity": 0}
        )
        self.assertEqual(result.status, ToolStatus.REJECTED)
        self.assertEqual(
            self.invoke(self.registry, "shopping_list", {})["items"], []
        )

    def test_background_worker_fires_and_audits_due_items(self) -> None:
        timer = self.invoke(
            self.registry, "timer_create", {"duration_seconds": 10}
        )["timer"]
        self.clock[0] += timedelta(seconds=11)
        audit = InMemoryAuditLog()
        worker = ScheduledItemWorker(
            HouseholdStore(self.path, now=lambda: self.clock[0]),
            audit,
            poll_interval_seconds=0.01,
        )
        fired = Event()
        worker.start()
        try:
            for _attempt in range(100):
                if audit.events():
                    fired.set()
                    break
                fired.wait(0.01)
            self.assertTrue(fired.is_set())
            event = audit.events()[0]
            self.assertEqual(event["event"], "scheduled_item_due")
            self.assertEqual(event["scheduled_item_id"], timer["id"])
        finally:
            worker.stop()

    def test_private_scheduled_items_and_lists_enforce_owner_in_store(self) -> None:
        store = HouseholdStore(self.path, now=lambda: self.clock[0])
        juan = web_actor("juan@example.com")
        ana = web_actor("ana@example.com")
        MemoryStore(self.path).provision_household_members((juan.email, ana.email))
        timer = store.create_scheduled(
            "timer",
            "Juan only",
            self.clock[0] + timedelta(minutes=5),
            actor=juan,
        )
        self.assertEqual(timer["visibility"], "private")
        self.assertEqual(store.list_scheduled("timer", actor=ana), [])
        with self.assertRaisesRegex(Exception, "not found"):
            store.cancel_scheduled(timer["id"], "timer", actor=ana)

        item = store.add_shopping_item(
            "Juan private", "Coffee", 1, actor=juan, shared=False
        )
        self.assertFalse(item["shared"])
        self.assertEqual(store.list_shopping_items("Juan private", actor=ana), [])
        with self.assertRaisesRegex(Exception, "not found"):
            store.remove_shopping_item(item["id"], actor=ana)


if __name__ == "__main__":
    unittest.main()
