from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
import unittest

from miso.conversation import ConversationState, StateTransition
from miso.identity import SYSTEM_ACTOR, VOICE_ACTOR, web_actor
from miso.live_events import (
    LiveAuditSink,
    LiveEventStore,
    LiveToolResultPublisher,
    MAX_CAPTION_CHARACTERS,
    conversation_caption_publisher,
    conversation_event_publisher,
)
from miso.memory import MemoryStore
from miso.tools import InMemoryAuditLog, ToolResult, ToolStatus


class LiveEventStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.path = Path(self.temporary.name) / "miso.sqlite3"
        memory = MemoryStore(self.path)
        memory.migrate()
        self.juan = web_actor("juan@example.com")
        self.ana = web_actor("ana@example.com")
        memory.provision_household_members((self.juan.email, self.ana.email))
        self.store = LiveEventStore(self.path, capacity=3)

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def test_shared_and_private_replay_enforces_record_visibility(self) -> None:
        shared = self.store.publish(
            "assistant_state", {"state": "listening"}, actor=VOICE_ACTOR
        )
        private_juan = self.store.publish(
            "tool_outcome", {"tool": "calendar_event_list"}, actor=self.juan
        )
        private_ana = self.store.publish(
            "tool_outcome", {"tool": "memory_search"}, actor=self.ana
        )

        self.assertEqual(
            [event.event_id for event in self.store.after(0, actor=self.juan)],
            [shared.event_id, private_juan.event_id],
        )
        self.assertEqual(
            [event.event_id for event in self.store.after(0, actor=self.ana)],
            [shared.event_id, private_ana.event_id],
        )
        self.assertEqual(
            [event.event_id for event in self.store.after(0, actor=VOICE_ACTOR)],
            [shared.event_id],
        )

    def test_capacity_retains_only_the_newest_events(self) -> None:
        created = [
            self.store.publish("assistant_state", {"index": index})
            for index in range(5)
        ]

        self.assertEqual(
            [event.event_id for event in self.store.recent(actor=self.juan)],
            [event.event_id for event in created[-3:]],
        )

    def test_wait_after_wakes_when_an_accessible_event_commits(self) -> None:
        received = []

        def wait() -> None:
            received.extend(self.store.wait_after(0, actor=self.juan, timeout=2))

        thread = threading.Thread(target=wait)
        thread.start()
        time.sleep(0.02)
        published = self.store.publish("household_changed", {"kind": "timer"})
        thread.join(timeout=2)

        self.assertFalse(thread.is_alive())
        self.assertEqual([event.event_id for event in received], [published.event_id])

    def test_conversation_projection_excludes_internal_reason(self) -> None:
        conversation_event_publisher(self.store)(
            StateTransition(
                ConversationState.IDLE,
                ConversationState.ACKNOWLEDGING,
                "sensitive internal reason",
                123.456,
            )
        )

        event = self.store.after(0, actor=self.juan)[0]
        self.assertEqual(event.event_type, "assistant_state")
        self.assertEqual(event.payload["state"], "acknowledging")
        self.assertNotIn("reason", event.payload)

    def test_voice_caption_is_shared_bounded_and_contains_no_web_chat(self) -> None:
        conversation_caption_publisher(self.store)(
            "  This reply is spoken aloud.  ", "en"
        )
        self.store.publish(
            "assistant_caption",
            {"text": "private dashboard reply", "language": "en"},
            actor=self.juan,
        )

        juan_events = self.store.after(0, actor=self.juan)
        ana_events = self.store.after(0, actor=self.ana)
        self.assertEqual(juan_events[0].payload["text"], "This reply is spoken aloud.")
        self.assertEqual(juan_events[0].payload["language"], "en")
        self.assertFalse(juan_events[0].payload["truncated"])
        self.assertEqual(len(ana_events), 1)
        self.assertNotIn("private dashboard reply", str(ana_events))

        conversation_caption_publisher(self.store)("x" * 4_000, "es")
        bounded = self.store.recent(actor=self.ana)[-1]
        self.assertEqual(len(bounded.payload["text"]), MAX_CAPTION_CHARACTERS)
        self.assertTrue(bounded.payload["truncated"])

    def test_scheduled_audit_and_tool_results_are_projected_safely(self) -> None:
        audit = InMemoryAuditLog()
        sink = LiveAuditSink(audit, self.store)
        sink.record(
            {
                "event": "scheduled_item_due",
                "scheduled_item_id": "timer-1",
                "kind": "timer",
                "title": "Tea",
                "due_at": "2026-08-26T08:00:00+00:00",
                "revision": 2,
                "visibility": "private",
                "owner_email": self.juan.email,
                "actor": SYSTEM_ACTOR.actor_id,
            }
        )
        result = ToolResult(
            invocation_id="invocation",
            tool="timer_create",
            status=ToolStatus.SUCCESS,
            output={
                "timer": {
                    "id": "timer-2",
                    "kind": "timer",
                    "visibility": "private",
                    "owner_email": self.juan.email,
                }
            },
            error=None,
            duration_ms=12,
        )
        LiveToolResultPublisher(self.store)(result, self.juan)

        events = self.store.after(0, actor=self.juan)
        self.assertEqual(
            [event.event_type for event in events],
            ["scheduled_item_due", "tool_outcome", "household_changed"],
        )
        self.assertNotIn("output", events[1].payload)
        self.assertFalse(self.store.after(0, actor=self.ana))
        self.assertEqual(audit.events()[0]["title"], "Tea")


if __name__ == "__main__":
    unittest.main()
