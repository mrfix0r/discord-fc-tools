#!/usr/bin/env python3
"""Safe, idempotent Discord server migrator for FC.

The program intentionally supports only GET, POST and PATCH Discord API calls.
There is no delete operation and it never sends Discord messages.
"""

from __future__ import annotations

import argparse
import datetime as dt
import getpass
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Iterable


API_BASE = "https://discord.com/api/v10"
ALLOWED_HTTP_METHODS = frozenset({"GET", "POST", "PATCH"})

CHANNEL_TYPES = {
    "text": 0,
    "voice": 2,
    "category": 4,
    "announcement": 5,
    "forum": 15,
}

# Discord permission flags used by this migration.
VIEW_CHANNEL = 1 << 10
SEND_MESSAGES = 1 << 11
READ_MESSAGE_HISTORY = 1 << 16
CONNECT = 1 << 20
SPEAK = 1 << 21
CREATE_PUBLIC_THREADS = 1 << 35
SEND_MESSAGES_IN_THREADS = 1 << 38

PRESETS = {
    "public_category": (VIEW_CHANNEL | READ_MESSAGE_HISTORY, 0),
    "public_text": (VIEW_CHANNEL | SEND_MESSAGES | READ_MESSAGE_HISTORY, 0),
    "readonly_text": (VIEW_CHANNEL | READ_MESSAGE_HISTORY, SEND_MESSAGES),
    "public_forum": (
        VIEW_CHANNEL
        | SEND_MESSAGES
        | READ_MESSAGE_HISTORY
        | CREATE_PUBLIC_THREADS
        | SEND_MESSAGES_IN_THREADS,
        0,
    ),
    "public_voice": (VIEW_CHANNEL | CONNECT | SPEAK, 0),
}


class DiscordApiError(RuntimeError):
    pass


class PlanError(RuntimeError):
    pass


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def normalized(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().casefold()


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


class DiscordClient:
    def __init__(self, token: str, reason: str = "FC safe migration") -> None:
        token = token.strip()
        if not token:
            raise ValueError("Bot token is empty")
        self.token = token
        self.reason = reason

    def request(self, method: str, path: str, body: Any | None = None) -> Any:
        method = method.upper()
        if method not in ALLOWED_HTTP_METHODS:
            raise ValueError(f"Unsafe or unsupported HTTP method: {method}")

        data = None
        headers = {
            "Authorization": f"Bot {self.token}",
            "User-Agent": "DiscordBot (FCMigrator, 1.0)",
            "Accept": "application/json",
            "X-Audit-Log-Reason": urllib.parse.quote(self.reason),
        }
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"

        url = f"{API_BASE}{path}"
        for attempt in range(6):
            request = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with urllib.request.urlopen(request, timeout=45) as response:
                    raw = response.read()
                    return json.loads(raw.decode("utf-8")) if raw else None
            except urllib.error.HTTPError as error:
                raw = error.read().decode("utf-8", errors="replace")
                if error.code == 429 and attempt < 5:
                    try:
                        retry_after = float(json.loads(raw).get("retry_after", 1.0))
                    except (ValueError, TypeError, json.JSONDecodeError):
                        retry_after = 1.0
                    time.sleep(min(max(retry_after, 0.25), 30.0))
                    continue
                raise DiscordApiError(
                    f"Discord API {method} {path} returned HTTP {error.code}: {raw[:1000]}"
                ) from error
            except urllib.error.URLError as error:
                if attempt < 2:
                    time.sleep(1.0 + attempt)
                    continue
                raise DiscordApiError(f"Discord API connection failed: {error}") from error
        raise DiscordApiError(f"Discord API request failed after retries: {method} {path}")

    def get_guild(self, guild_id: str) -> dict[str, Any]:
        return self.request("GET", f"/guilds/{guild_id}?with_counts=true")

    def get_channels(self, guild_id: str) -> list[dict[str, Any]]:
        return self.request("GET", f"/guilds/{guild_id}/channels")

    def get_roles(self, guild_id: str) -> list[dict[str, Any]]:
        return self.request("GET", f"/guilds/{guild_id}/roles")

    def create_channel(self, guild_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self.request("POST", f"/guilds/{guild_id}/channels", payload)

    def modify_channel(self, channel_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self.request("PATCH", f"/channels/{channel_id}", payload)

    def modify_channel_positions(
        self, guild_id: str, payload: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        return self.request("PATCH", f"/guilds/{guild_id}/channels", payload)

    def create_role(self, guild_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self.request("POST", f"/guilds/{guild_id}/roles", payload)

    def modify_role(
        self, guild_id: str, role_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        return self.request("PATCH", f"/guilds/{guild_id}/roles/{role_id}", payload)


def only_match(
    items: Iterable[dict[str, Any]], predicate: Any, description: str
) -> dict[str, Any] | None:
    matches = [item for item in items if predicate(item)]
    if len(matches) > 1:
        ids = ", ".join(str(item.get("id")) for item in matches)
        raise PlanError(f"Ambiguous {description}; matching IDs: {ids}")
    return matches[0] if matches else None


def find_named(
    items: Iterable[dict[str, Any]], name: str, *, item_type: int | None = None
) -> dict[str, Any] | None:
    return only_match(
        items,
        lambda item: normalized(str(item.get("name", ""))) == normalized(name)
        and (item_type is None or int(item.get("type", -1)) == item_type),
        repr(name),
    )


def category_by_logical_name(
    channels: list[dict[str, Any]], plan: dict[str, Any], name: str
) -> dict[str, Any] | None:
    found = find_named(channels, name, item_type=CHANNEL_TYPES["category"])
    if found:
        return found
    for rename in plan.get("category_renames", []):
        if normalized(rename["target"]) == normalized(name):
            return find_named(
                channels, rename["source"], item_type=CHANNEL_TYPES["category"]
            )
    return None


def find_channel_in_category(
    channels: list[dict[str, Any]], category_id: str, name: str
) -> dict[str, Any] | None:
    return only_match(
        channels,
        lambda item: str(item.get("parent_id")) == str(category_id)
        and int(item.get("type", -1)) != CHANNEL_TYPES["category"]
        and normalized(str(item.get("name", ""))) == normalized(name),
        f"channel {name!r} in category {category_id}",
    )


def merge_permission_overwrite(
    overwrites: list[dict[str, Any]] | None,
    target_id: str,
    overwrite_type: int,
    allow_bits: int,
    deny_bits: int,
) -> list[dict[str, Any]]:
    result = [dict(item) for item in (overwrites or [])]
    target = None
    for item in result:
        if str(item.get("id")) == str(target_id) and int(item.get("type", -1)) == overwrite_type:
            target = item
            break
    if target is None:
        target = {"id": str(target_id), "type": overwrite_type, "allow": "0", "deny": "0"}
        result.append(target)

    current_allow = int(target.get("allow", "0"))
    current_deny = int(target.get("deny", "0"))
    current_allow = (current_allow | allow_bits) & ~deny_bits
    current_deny = (current_deny | deny_bits) & ~allow_bits
    target["allow"] = str(current_allow)
    target["deny"] = str(current_deny)
    return result


def apply_preset(
    overwrites: list[dict[str, Any]] | None, guild_id: str, preset_name: str
) -> list[dict[str, Any]]:
    if preset_name not in PRESETS:
        raise PlanError(f"Unknown permission preset: {preset_name}")
    allow_bits, deny_bits = PRESETS[preset_name]
    return merge_permission_overwrite(overwrites, guild_id, 0, allow_bits, deny_bits)


def archive_overwrites(
    overwrites: list[dict[str, Any]] | None, guild_id: str, access_role_id: str
) -> list[dict[str, Any]]:
    result = merge_permission_overwrite(
        overwrites, guild_id, 0, 0, VIEW_CHANNEL | CONNECT
    )
    result = merge_permission_overwrite(
        result,
        access_role_id,
        0,
        VIEW_CHANNEL | READ_MESSAGE_HISTORY,
        SEND_MESSAGES | CONNECT,
    )
    return result


def validate_plan_shape(plan: dict[str, Any]) -> None:
    if plan.get("plan_version") != 1:
        raise PlanError("Unsupported or missing plan_version")
    safety = plan.get("safety", {})
    forbidden = [key for key in ("delete_channels", "delete_roles", "send_messages") if safety.get(key)]
    if forbidden:
        raise PlanError(f"Unsafe plan flags are enabled: {', '.join(forbidden)}")

    new_category_names = [item["name"] for item in plan.get("categories_to_create", [])]
    if len({normalized(x) for x in new_category_names}) != len(new_category_names):
        raise PlanError("Duplicate category names in categories_to_create")
    for item in plan.get("channels_to_create", []):
        if item["type"] not in CHANNEL_TYPES or item["type"] == "category":
            raise PlanError(f"Unsupported channel type: {item['type']}")
        if item["preset"] not in PRESETS:
            raise PlanError(f"Unknown preset: {item['preset']}")


def validate_guild(guild_id: str, guild: dict[str, Any], plan: dict[str, Any]) -> None:
    expected_id = str(plan.get("guild", {}).get("expected_id", ""))
    if expected_id and guild_id != expected_id:
        raise PlanError(f"Plan is for guild {expected_id}, not {guild_id}")
    expected_name = plan.get("guild", {}).get("expected_name")
    if expected_name and normalized(guild.get("name", "")) != normalized(expected_name):
        raise PlanError(
            f"Expected guild name {expected_name!r}, API returned {guild.get('name')!r}"
        )


def preview_actions(
    guild: dict[str, Any],
    channels: list[dict[str, Any]],
    roles: list[dict[str, Any]],
    plan: dict[str, Any],
) -> tuple[list[str], list[str]]:
    actions: list[str] = []
    warnings: list[str] = []

    for item in plan.get("role_renames", []):
        source = find_named(roles, item["source"])
        target = find_named(roles, item["target"])
        if source and target and source["id"] != target["id"]:
            raise PlanError(f"Both source and target roles exist: {item}")
        if source and not target:
            actions.append(f"rename role: {item['source']} -> {item['target']}")
        elif not source and not target:
            warnings.append(f"role not found, rename will be skipped: {item['source']}")

    for item in plan.get("roles_to_create", []):
        if not find_named(roles, item["name"]):
            actions.append(f"create role: {item['name']}")

    for item in plan.get("category_renames", []):
        source = find_named(channels, item["source"], item_type=4)
        target = find_named(channels, item["target"], item_type=4)
        if source and target and source["id"] != target["id"]:
            raise PlanError(f"Both source and target categories exist: {item}")
        if source and not target:
            actions.append(f"rename category: {item['source']} -> {item['target']}")
        elif not source and not target:
            warnings.append(f"category not found, rename will be skipped: {item['source']}")

    for item in plan.get("categories_to_create", []):
        if not category_by_logical_name(channels, plan, item["name"]):
            actions.append(f"create category: {item['name']}")

    for item in plan.get("channels_to_update", []):
        source_category = category_by_logical_name(channels, plan, item["source_category"])
        if not source_category:
            warnings.append(f"source category not found: {item['source_category']}")
            continue
        source = find_channel_in_category(channels, source_category["id"], item["source_name"])
        target_category = category_by_logical_name(channels, plan, item["target_category"])
        target = None
        if target_category:
            target = find_channel_in_category(channels, target_category["id"], item["target_name"])
        if source and target and source["id"] != target["id"]:
            raise PlanError(f"Target channel already exists separately: {item['target_name']}")
        if source:
            actions.append(
                f"update channel: {item['source_name']} -> {item['target_name']} "
                f"[{item['target_category']}]"
            )
        elif not target:
            warnings.append(
                f"source channel not found, update will be skipped: {item['source_name']}"
            )

    for item in plan.get("channels_to_create", []):
        category = category_by_logical_name(channels, plan, item["category"])
        existing = (
            find_channel_in_category(channels, category["id"], item["name"])
            if category
            else None
        )
        if not existing:
            actions.append(f"create {item['type']} channel: {item['category']} / {item['name']}")

    for item in plan.get("archive_categories", []):
        if category_by_logical_name(channels, plan, item["name"]):
            actions.append(f"restrict archive visibility: {item['name']}")
        else:
            warnings.append(f"archive category not found: {item['name']}")

    actions.append("reorder categories and migrated channels")
    return actions, warnings


def make_backup(
    backup_dir: Path,
    guild: dict[str, Any],
    channels: list[dict[str, Any]],
    roles: list[dict[str, Any]],
    plan_path: Path,
) -> Path:
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = backup_dir / f"fc-discord-backup-{stamp}.json"
    write_json(
        path,
        {
            "backup_version": 1,
            "created_at": utc_now(),
            "guild": guild,
            "channels": channels,
            "roles": roles,
            "plan_file": plan_path.name,
            "warning": "Contains Discord object IDs and permission overwrites. Keep private.",
        },
    )
    return path


def rename_roles(client: DiscordClient, guild_id: str, roles: list[dict[str, Any]], plan: dict[str, Any]) -> None:
    for item in plan.get("role_renames", []):
        source = find_named(roles, item["source"])
        target = find_named(roles, item["target"])
        if source and target and source["id"] != target["id"]:
            raise PlanError(f"Both source and target roles exist: {item}")
        if source and not target:
            print(f"APPLY  rename role: {item['source']} -> {item['target']}")
            updated = client.modify_role(guild_id, source["id"], {"name": item["target"]})
            roles[roles.index(source)] = updated


def create_roles(client: DiscordClient, guild_id: str, roles: list[dict[str, Any]], plan: dict[str, Any]) -> dict[str, str]:
    created: dict[str, str] = {}
    for item in plan.get("roles_to_create", []):
        existing = find_named(roles, item["name"])
        if existing:
            continue
        print(f"APPLY  create role: {item['name']}")
        role = client.create_role(
            guild_id,
            {
                "name": item["name"],
                "permissions": "0",
                "color": int(item.get("color", 0)),
                "hoist": False,
                "mentionable": False,
            },
        )
        roles.append(role)
        created[item["name"]] = role["id"]
    return created


def rename_categories(client: DiscordClient, channels: list[dict[str, Any]], plan: dict[str, Any]) -> None:
    for item in plan.get("category_renames", []):
        source = find_named(channels, item["source"], item_type=4)
        target = find_named(channels, item["target"], item_type=4)
        if source and target and source["id"] != target["id"]:
            raise PlanError(f"Both source and target categories exist: {item}")
        if source and not target:
            print(f"APPLY  rename category: {item['source']} -> {item['target']}")
            updated = client.modify_channel(source["id"], {"name": item["target"]})
            channels[channels.index(source)] = updated


def create_categories(client: DiscordClient, guild_id: str, channels: list[dict[str, Any]], plan: dict[str, Any]) -> dict[str, str]:
    created: dict[str, str] = {}
    for item in plan.get("categories_to_create", []):
        existing = find_named(channels, item["name"], item_type=4)
        if existing:
            continue
        print(f"APPLY  create category: {item['name']}")
        payload = {
            "name": item["name"],
            "type": CHANNEL_TYPES["category"],
            "permission_overwrites": apply_preset([], guild_id, item["preset"]),
        }
        category = client.create_channel(guild_id, payload)
        channels.append(category)
        created[item["name"]] = category["id"]
    return created


def update_channels(client: DiscordClient, guild_id: str, channels: list[dict[str, Any]], plan: dict[str, Any]) -> None:
    for item in plan.get("channels_to_update", []):
        source_category = find_named(channels, item["source_category"], item_type=4)
        target_category = find_named(channels, item["target_category"], item_type=4)
        if not source_category or not target_category:
            raise PlanError(f"Missing source or target category for {item['source_name']}")
        source = find_channel_in_category(channels, source_category["id"], item["source_name"])
        target = find_channel_in_category(channels, target_category["id"], item["target_name"])
        if source and target and source["id"] != target["id"]:
            raise PlanError(f"Target channel already exists separately: {item['target_name']}")
        channel = source or target
        if not channel:
            print(f"SKIP   source channel not found: {item['source_name']}")
            continue
        payload: dict[str, Any] = {
            "name": item["target_name"],
            "parent_id": target_category["id"],
            "position": int(item.get("position", channel.get("position", 0))),
            "permission_overwrites": apply_preset(
                channel.get("permission_overwrites"), guild_id, item["preset"]
            ),
        }
        if "topic" in item and int(channel.get("type", -1)) in (0, 5, 15):
            payload["topic"] = item["topic"]
        print(
            f"APPLY  update channel: {channel.get('name')} -> {item['target_name']} "
            f"[{item['target_category']}]"
        )
        updated = client.modify_channel(channel["id"], payload)
        channels[channels.index(channel)] = updated


def create_channels(client: DiscordClient, guild_id: str, channels: list[dict[str, Any]], plan: dict[str, Any]) -> dict[str, str]:
    created: dict[str, str] = {}
    for item in plan.get("channels_to_create", []):
        category = find_named(channels, item["category"], item_type=4)
        if not category:
            raise PlanError(f"Target category does not exist: {item['category']}")
        existing = find_channel_in_category(channels, category["id"], item["name"])
        if existing:
            continue
        payload: dict[str, Any] = {
            "name": item["name"],
            "type": CHANNEL_TYPES[item["type"]],
            "parent_id": category["id"],
            "position": int(item.get("position", 0)),
            "permission_overwrites": apply_preset([], guild_id, item["preset"]),
        }
        if "topic" in item and item["type"] in ("text", "announcement", "forum"):
            payload["topic"] = item["topic"]
        if item["type"] == "forum" and item.get("tags"):
            payload["available_tags"] = [
                {"name": tag, "moderated": False} for tag in item["tags"]
            ]
        print(f"APPLY  create {item['type']} channel: {item['category']} / {item['name']}")
        channel = client.create_channel(guild_id, payload)
        channels.append(channel)
        created[f"{item['category']} / {item['name']}"] = channel["id"]
    return created


def restrict_archives(
    client: DiscordClient,
    guild_id: str,
    channels: list[dict[str, Any]],
    roles: list[dict[str, Any]],
    plan: dict[str, Any],
) -> None:
    for item in plan.get("archive_categories", []):
        category = find_named(channels, item["name"], item_type=4)
        role = find_named(roles, item["access_role"])
        if not category or not role:
            raise PlanError(f"Archive category or access role missing: {item}")
        targets = [category] + [
            channel for channel in channels if str(channel.get("parent_id")) == str(category["id"])
        ]
        print(f"APPLY  restrict archive visibility: {item['name']} ({len(targets) - 1} children)")
        for channel in targets:
            payload = {
                "permission_overwrites": archive_overwrites(
                    channel.get("permission_overwrites"), guild_id, role["id"]
                )
            }
            updated = client.modify_channel(channel["id"], payload)
            channels[channels.index(channel)] = updated


def reorder_categories(client: DiscordClient, guild_id: str, channels: list[dict[str, Any]], plan: dict[str, Any]) -> None:
    payload: list[dict[str, Any]] = []
    for position, name in enumerate(plan.get("category_order", [])):
        category = find_named(channels, name, item_type=4)
        if category:
            payload.append({"id": category["id"], "position": position})
    if payload:
        print(f"APPLY  reorder categories: {len(payload)}")
        client.modify_channel_positions(guild_id, payload)


def run_migration(
    client: DiscordClient,
    guild_id: str,
    plan: dict[str, Any],
    plan_path: Path,
    backup_dir: Path,
) -> tuple[Path, Path]:
    guild = client.get_guild(guild_id)
    channels = client.get_channels(guild_id)
    roles = client.get_roles(guild_id)
    validate_guild(guild_id, guild, plan)

    backup_path = make_backup(backup_dir, guild, channels, roles, plan_path)
    print(f"BACKUP {backup_path.resolve()}")

    created_roles = create_roles(client, guild_id, roles, plan)
    rename_roles(client, guild_id, roles, plan)
    rename_categories(client, channels, plan)
    created_categories = create_categories(client, guild_id, channels, plan)
    update_channels(client, guild_id, channels, plan)
    created_channels = create_channels(client, guild_id, channels, plan)
    restrict_archives(client, guild_id, channels, roles, plan)
    reorder_categories(client, guild_id, channels, plan)

    state_path = backup_path.with_name(backup_path.stem.replace("backup", "result") + ".json")
    write_json(
        state_path,
        {
            "result_version": 1,
            "completed_at": utc_now(),
            "guild_id": guild_id,
            "backup_path": str(backup_path.resolve()),
            "created_roles": created_roles,
            "created_categories": created_categories,
            "created_channels": created_channels,
            "note": "Rollback restores prior objects but deliberately does not delete newly created objects.",
        },
    )
    return backup_path, state_path


def channel_restore_payload(channel: dict[str, Any]) -> dict[str, Any]:
    channel_type = int(channel.get("type", -1))
    payload: dict[str, Any] = {
        "name": channel["name"],
        "position": int(channel.get("position", 0)),
        "parent_id": channel.get("parent_id"),
        "permission_overwrites": channel.get("permission_overwrites", []),
    }
    if channel_type in (0, 5, 15):
        payload["topic"] = channel.get("topic")
        payload["nsfw"] = bool(channel.get("nsfw", False))
        payload["rate_limit_per_user"] = int(channel.get("rate_limit_per_user", 0))
    if channel_type == 2:
        payload["bitrate"] = int(channel.get("bitrate", 64000))
        payload["user_limit"] = int(channel.get("user_limit", 0))
        payload["rtc_region"] = channel.get("rtc_region")
        if channel.get("video_quality_mode") is not None:
            payload["video_quality_mode"] = int(channel["video_quality_mode"])
    return payload


def role_restore_payload(role: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": role["name"],
        "permissions": str(role.get("permissions", "0")),
        "color": int(role.get("color", 0)),
        "hoist": bool(role.get("hoist", False)),
        "mentionable": bool(role.get("mentionable", False)),
    }


def rollback_preview(
    backup: dict[str, Any],
    live_channels: list[dict[str, Any]],
    live_roles: list[dict[str, Any]],
) -> tuple[list[str], list[str]]:
    backup_channels = {str(item["id"]): item for item in backup["channels"]}
    live_channel_ids = {str(item["id"]) for item in live_channels}
    backup_roles = {str(item["id"]): item for item in backup["roles"]}
    live_role_ids = {str(item["id"]) for item in live_roles}
    actions = [
        f"restore existing channel: {item['name']}"
        for channel_id, item in backup_channels.items()
        if channel_id in live_channel_ids
    ]
    actions += [
        f"restore existing role: {item['name']}"
        for role_id, item in backup_roles.items()
        if role_id in live_role_ids and item.get("name") != "@everyone"
    ]
    warnings = []
    new_channels = [item for item in live_channels if str(item["id"]) not in backup_channels]
    new_roles = [item for item in live_roles if str(item["id"]) not in backup_roles]
    if new_channels:
        warnings.append(f"{len(new_channels)} newly created channels/categories will remain (no deletion policy)")
    if new_roles:
        warnings.append(f"{len(new_roles)} newly created roles will remain (no deletion policy)")
    return actions, warnings


def run_rollback(client: DiscordClient, guild_id: str, backup: dict[str, Any]) -> None:
    if str(backup.get("guild", {}).get("id")) != guild_id:
        raise PlanError("Backup belongs to a different guild")
    live_channels = client.get_channels(guild_id)
    live_roles = client.get_roles(guild_id)
    live_channels_by_id = {str(item["id"]): item for item in live_channels}
    live_roles_by_id = {str(item["id"]): item for item in live_roles}

    for saved in sorted(backup["channels"], key=lambda item: int(item.get("position", 0))):
        if str(saved["id"]) not in live_channels_by_id:
            print(f"SKIP   missing channel cannot be restored: {saved['name']} ({saved['id']})")
            continue
        print(f"ROLLBACK channel: {saved['name']}")
        client.modify_channel(saved["id"], channel_restore_payload(saved))

    for saved in backup["roles"]:
        if saved.get("name") == "@everyone" or str(saved["id"]) not in live_roles_by_id:
            continue
        if saved.get("managed"):
            continue
        print(f"ROLLBACK role: {saved['name']}")
        client.modify_role(guild_id, saved["id"], role_restore_payload(saved))

    print("Rollback finished. Newly created objects were intentionally left in place.")


def print_preview(title: str, actions: list[str], warnings: list[str]) -> None:
    print(title)
    print(f"Planned actions: {len(actions)}")
    for index, action in enumerate(actions, 1):
        print(f"  {index:02d}. {action}")
    if warnings:
        print(f"Warnings: {len(warnings)}")
        for warning in warnings:
            print(f"  - {warning}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    script_dir = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description="Safe Discord FC server migrator")
    parser.add_argument("--guild-id", required=True, help="Discord server (guild) ID")
    parser.add_argument("--plan", type=Path, default=script_dir / "migration_plan.json")
    parser.add_argument("--backup-dir", type=Path, default=script_dir / "backups")
    parser.add_argument("--apply", action="store_true", help="Apply changes; otherwise preview only")
    parser.add_argument("--confirm", default="", help="Required control phrase for --apply")
    parser.add_argument("--rollback", type=Path, help="Backup JSON to preview or restore")
    return parser.parse_args(argv)


def get_token() -> str:
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    if token:
        return token
    return getpass.getpass("Discord bot token (hidden): ").strip()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        plan = load_json(args.plan)
        validate_plan_shape(plan)
        token = get_token()
        client = DiscordClient(token)

        if args.rollback:
            backup = load_json(args.rollback)
            guild = client.get_guild(args.guild_id)
            if str(backup.get("guild", {}).get("id")) != args.guild_id:
                raise PlanError("Backup belongs to a different guild")
            live_channels = client.get_channels(args.guild_id)
            live_roles = client.get_roles(args.guild_id)
            actions, warnings = rollback_preview(backup, live_channels, live_roles)
            print_preview(f"ROLLBACK PREVIEW for {guild.get('name')} ({args.guild_id})", actions, warnings)
            if not args.apply:
                print("No changes made. Add --apply --confirm ROLLBACK_FC to restore.")
                return 0
            if args.confirm != "ROLLBACK_FC":
                raise PlanError("Rollback requires --confirm ROLLBACK_FC")
            run_rollback(client, args.guild_id, backup)
            return 0

        guild = client.get_guild(args.guild_id)
        channels = client.get_channels(args.guild_id)
        roles = client.get_roles(args.guild_id)
        validate_guild(args.guild_id, guild, plan)
        actions, warnings = preview_actions(guild, channels, roles, plan)
        print_preview(f"MIGRATION PREVIEW for {guild.get('name')} ({args.guild_id})", actions, warnings)
        if not args.apply:
            print("No changes made. Add --apply --confirm MIGRATE_FC to execute.")
            return 0
        if args.confirm != "MIGRATE_FC":
            raise PlanError("Migration requires --confirm MIGRATE_FC")
        backup_path, state_path = run_migration(
            client, args.guild_id, plan, args.plan, args.backup_dir
        )
        print("Migration completed.")
        print(f"Backup: {backup_path.resolve()}")
        print(f"Result: {state_path.resolve()}")
        return 0
    except (DiscordApiError, PlanError, ValueError, OSError, json.JSONDecodeError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
