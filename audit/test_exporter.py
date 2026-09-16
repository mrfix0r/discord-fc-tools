import importlib.util
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parent / "discord_audit_export.py"
SPEC = importlib.util.spec_from_file_location("discord_audit_export", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class FakeAPI:
    def __init__(self):
        self.calls = 0

    def get(self, path, params=None):
        self.calls += 1
        if self.calls > 1:
            return []
        return [
            {
                "id": "3",
                "timestamp": "2026-09-03T12:00:00+00:00",
                "author": {"id": "10", "bot": False},
                "content": "hello https://example.com",
                "attachments": [{"url": "secret"}],
                "reactions": [{"count": 2}],
            },
            {
                "id": "2",
                "timestamp": "2026-09-02T12:00:00+00:00",
                "author": {"id": "20", "bot": True},
                "content": "bot",
                "attachments": [],
                "reactions": [],
            },
        ]


class ExporterTests(unittest.TestCase):
    def test_snowflake_timestamp_round_trip(self):
        target_ms = 1_700_000_000_000
        snowflake = str((target_ms - MODULE.DISCORD_EPOCH_MS) << 22)
        actual = MODULE.snowflake_timestamp(snowflake)
        self.assertEqual(actual, datetime.fromtimestamp(target_ms / 1000, tz=timezone.utc))

    def test_redact_text_removes_sensitive_patterns(self):
        text = (
            "mail me at test@example.com or +49 123 456 789; "
            "see https://example.com/path and ping <@123> <@&456> <#789>"
        )
        actual = MODULE.redact_text(
            text,
            role_names={"456": "Raid"},
            channel_names={"789": "general"},
        )
        self.assertNotIn("test@example.com", actual)
        self.assertNotIn("+49 123 456 789", actual)
        self.assertNotIn("https://example.com", actual)
        self.assertIn("@member", actual)
        self.assertIn("@Raid", actual)
        self.assertIn("#general", actual)

    def test_activity_aggregates_without_exporting_user_ids(self):
        result = MODULE.collect_channel_activity(
            api=FakeAPI(),
            channel_id="1",
            cutoff=datetime(2026, 1, 1, tzinfo=timezone.utc),
            max_messages=100,
            include_content=False,
            sample_limit=20,
            pseudonyms={},
            role_names={},
            channel_names={},
        )
        self.assertEqual(result["messages_in_window"], 2)
        self.assertEqual(result["human_messages"], 1)
        self.assertEqual(result["bot_messages"], 1)
        self.assertEqual(result["unique_human_authors"], 1)
        self.assertEqual(result["attachments"], 1)
        self.assertEqual(result["reactions"], 2)
        self.assertNotIn("redacted_content_samples", result)


if __name__ == "__main__":
    unittest.main()
