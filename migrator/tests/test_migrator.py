import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import discord_fc_migrator as migrator  # noqa: E402


class PermissionTests(unittest.TestCase):
    def test_merge_preserves_other_overwrites_and_resolves_conflicts(self):
        source = [
            {"id": "guild", "type": 0, "allow": str(migrator.SEND_MESSAGES), "deny": "0"},
            {"id": "moderator", "type": 0, "allow": "123", "deny": "456"},
        ]
        result = migrator.merge_permission_overwrite(
            source,
            "guild",
            0,
            migrator.VIEW_CHANNEL,
            migrator.SEND_MESSAGES,
        )
        everyone = next(item for item in result if item["id"] == "guild")
        self.assertTrue(int(everyone["allow"]) & migrator.VIEW_CHANNEL)
        self.assertFalse(int(everyone["allow"]) & migrator.SEND_MESSAGES)
        self.assertTrue(int(everyone["deny"]) & migrator.SEND_MESSAGES)
        self.assertEqual(next(item for item in result if item["id"] == "moderator"), source[1])

    def test_archive_role_can_view_but_not_write_or_connect(self):
        result = migrator.archive_overwrites([], "guild", "archive")
        everyone = next(item for item in result if item["id"] == "guild")
        archive = next(item for item in result if item["id"] == "archive")
        self.assertTrue(int(everyone["deny"]) & migrator.VIEW_CHANNEL)
        self.assertTrue(int(archive["allow"]) & migrator.VIEW_CHANNEL)
        self.assertTrue(int(archive["deny"]) & migrator.SEND_MESSAGES)
        self.assertTrue(int(archive["deny"]) & migrator.CONNECT)


class SafetyTests(unittest.TestCase):
    def test_client_rejects_delete_before_network(self):
        client = migrator.DiscordClient("not-a-real-token")
        with self.assertRaises(ValueError):
            client.request("DELETE", "/channels/123")

    def test_plan_is_safe_and_valid(self):
        with (ROOT / "migration_plan.example.json").open(encoding="utf-8") as handle:
            plan = json.load(handle)
        migrator.validate_plan_shape(plan)
        self.assertFalse(any(plan["safety"].values()))

    def test_category_alias_resolves_pre_migration_name(self):
        channels = [{"id": "10", "name": "Общие", "type": 4}]
        plan = {
            "category_renames": [{"source": "Общие", "target": "FC // START"}]
        }
        found = migrator.category_by_logical_name(channels, plan, "FC // START")
        self.assertEqual(found["id"], "10")

    def test_whitespace_normalization_matches_existing_voice_name(self):
        channels = [
            {"id": "1", "name": "Общие", "type": 4},
            {
                "id": "2",
                "name": "🔥  Стрим ON🌱",
                "type": 2,
                "parent_id": "1",
            },
        ]
        found = migrator.find_channel_in_category(channels, "1", "🔥 Стрим ON🌱")
        self.assertEqual(found["id"], "2")


if __name__ == "__main__":
    unittest.main()
