#!/usr/bin/env python3
"""Read-only Discord server audit exporter.

The script performs GET requests only. By default it exports server structure,
roles, and aggregate activity metrics without message bodies or user IDs.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


API_BASE = "https://discord.com/api/v10"
DISCORD_EPOCH_MS = 1420070400000

CHANNEL_TYPES = {
    0: "text",
    1: "dm",
    2: "voice",
    3: "group_dm",
    4: "category",
    5: "announcement",
    10: "announcement_thread",
    11: "public_thread",
    12: "private_thread",
    13: "stage",
    14: "directory",
    15: "forum",
    16: "media",
}

MESSAGE_CHANNEL_TYPES = {0, 5, 10, 11, 12}

PERMISSION_BITS = {
    0: "CREATE_INSTANT_INVITE",
    1: "KICK_MEMBERS",
    2: "BAN_MEMBERS",
    3: "ADMINISTRATOR",
    4: "MANAGE_CHANNELS",
    5: "MANAGE_GUILD",
    6: "ADD_REACTIONS",
    7: "VIEW_AUDIT_LOG",
    9: "STREAM",
    10: "VIEW_CHANNEL",
    11: "SEND_MESSAGES",
    13: "MANAGE_MESSAGES",
    14: "EMBED_LINKS",
    15: "ATTACH_FILES",
    16: "READ_MESSAGE_HISTORY",
    17: "MENTION_EVERYONE",
    20: "CONNECT",
    21: "SPEAK",
    22: "MUTE_MEMBERS",
    23: "DEAFEN_MEMBERS",
    24: "MOVE_MEMBERS",
    28: "MANAGE_ROLES",
    29: "MANAGE_WEBHOOKS",
    31: "USE_APPLICATION_COMMANDS",
    32: "REQUEST_TO_SPEAK",
    33: "MANAGE_EVENTS",
    34: "MANAGE_THREADS",
    35: "CREATE_PUBLIC_THREADS",
    36: "CREATE_PRIVATE_THREADS",
    38: "SEND_MESSAGES_IN_THREADS",
    40: "MODERATE_MEMBERS",
    44: "CREATE_EVENTS",
    46: "SEND_VOICE_MESSAGES",
    49: "SEND_POLLS",
}

URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
PHONE_RE = re.compile(r"(?<!\w)(?:\+?\d[\d\s().-]{7,}\d)(?!\w)")
TOKEN_RE = re.compile(r"\b(?:mfa\.[\w-]{20,}|[\w-]{20,}\.[\w-]{5,}\.[\w-]{20,})\b")
USER_MENTION_RE = re.compile(r"<@!?\d+>")
ROLE_MENTION_RE = re.compile(r"<@&(\d+)>")
CHANNEL_MENTION_RE = re.compile(r"<#(\d+)>")


class DiscordAPIError(RuntimeError):
    def __init__(self, status: Optional[int], path: str, detail: str):
        self.status = status
        self.path = path
        self.detail = detail
        super().__init__(f"Discord API error {status or 'network'} for {path}: {detail}")


class DiscordReadOnlyAPI:
    """Minimal Discord REST client that intentionally exposes GET only."""

    def __init__(self, token: str, timeout_seconds: int = 30):
        self.token = token
        self.timeout_seconds = timeout_seconds

    def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        query = f"?{urlencode(params)}" if params else ""
        url = f"{API_BASE}{path}{query}"
        headers = {
            "Authorization": f"Bot {self.token}",
            "User-Agent": "DiscordReadOnlyAudit/1.0 (server-structure-audit)",
            "Accept": "application/json",
        }

        for attempt in range(8):
            request = Request(url, headers=headers, method="GET")
            try:
                with urlopen(request, timeout=self.timeout_seconds) as response:
                    payload = response.read().decode("utf-8")
                    remaining = response.headers.get("X-RateLimit-Remaining")
                    reset_after = response.headers.get("X-RateLimit-Reset-After")
                    data = json.loads(payload) if payload else None
                    if remaining == "0" and reset_after:
                        time.sleep(min(float(reset_after) + 0.05, 60.0))
                    return data
            except HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                try:
                    error_data = json.loads(body)
                except json.JSONDecodeError:
                    error_data = {"message": body or exc.reason}

                if exc.code == 429 and attempt < 7:
                    retry_after = float(error_data.get("retry_after", 1.0))
                    time.sleep(min(retry_after + 0.1, 60.0))
                    continue

                detail = str(error_data.get("message", body or exc.reason))
                raise DiscordAPIError(exc.code, path, detail) from exc
            except URLError as exc:
                if attempt < 2:
                    time.sleep(1.0 + attempt)
                    continue
                raise DiscordAPIError(None, path, str(exc.reason)) from exc

        raise DiscordAPIError(None, path, "retry limit exceeded")


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export a privacy-conscious, read-only audit of a Discord server."
    )
    parser.add_argument(
        "--guild-id",
        default=os.environ.get("DISCORD_GUILD_ID"),
        help="Discord server ID; may also be set as DISCORD_GUILD_ID.",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=365,
        help="Activity window in days (default: 365).",
    )
    parser.add_argument(
        "--max-messages-per-channel",
        type=int,
        default=1000,
        help="Maximum messages inspected per channel (default: 1000).",
    )
    parser.add_argument(
        "--include-content",
        action="store_true",
        help="Include a small, redacted sample of message text. Off by default.",
    )
    parser.add_argument(
        "--samples-per-channel",
        type=int,
        default=20,
        help="Maximum content samples per channel when --include-content is used.",
    )
    parser.add_argument(
        "--output",
        default="discord-audit-report.json",
        help="Output JSON path (default: discord-audit-report.json).",
    )
    args = parser.parse_args(argv)

    if not args.guild_id or not str(args.guild_id).isdigit():
        parser.error("--guild-id must be a numeric Discord server ID")
    if args.days < 1 or args.days > 3650:
        parser.error("--days must be between 1 and 3650")
    if args.max_messages_per_channel < 0 or args.max_messages_per_channel > 100000:
        parser.error("--max-messages-per-channel must be between 0 and 100000")
    if args.samples_per_channel < 0 or args.samples_per_channel > 100:
        parser.error("--samples-per-channel must be between 0 and 100")
    return args


def iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_discord_timestamp(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def snowflake_timestamp(snowflake: Optional[str]) -> Optional[datetime]:
    if not snowflake:
        return None
    try:
        milliseconds = (int(snowflake) >> 22) + DISCORD_EPOCH_MS
        return datetime.fromtimestamp(milliseconds / 1000.0, tz=timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def decode_permissions(value: Any) -> List[str]:
    try:
        bits = int(value)
    except (TypeError, ValueError):
        return []
    return [name for bit, name in PERMISSION_BITS.items() if bits & (1 << bit)]


def redact_text(
    text: str,
    role_names: Optional[Dict[str, str]] = None,
    channel_names: Optional[Dict[str, str]] = None,
) -> str:
    role_names = role_names or {}
    channel_names = channel_names or {}
    text = TOKEN_RE.sub("[SECRET]", text)
    text = URL_RE.sub("[URL]", text)
    text = EMAIL_RE.sub("[EMAIL]", text)
    text = PHONE_RE.sub("[PHONE]", text)
    text = USER_MENTION_RE.sub("@member", text)
    text = ROLE_MENTION_RE.sub(lambda match: "@" + role_names.get(match.group(1), "role"), text)
    text = CHANNEL_MENTION_RE.sub(
        lambda match: "#" + channel_names.get(match.group(1), "channel"), text
    )
    return re.sub(r"\s+", " ", text).strip()[:1000]


def channel_type_name(type_id: Any) -> str:
    try:
        numeric = int(type_id)
    except (TypeError, ValueError):
        return "unknown"
    return CHANNEL_TYPES.get(numeric, f"unknown_{numeric}")


def safe_get(
    api: DiscordReadOnlyAPI,
    path: str,
    warnings: List[str],
    label: str,
    params: Optional[Dict[str, Any]] = None,
) -> Any:
    try:
        return api.get(path, params=params)
    except DiscordAPIError as exc:
        warnings.append(f"{label}: {exc}")
        return None


def permission_overwrites(
    raw_overwrites: Iterable[Dict[str, Any]], role_names: Dict[str, str]
) -> Dict[str, Any]:
    role_entries: List[Dict[str, Any]] = []
    member_count = 0
    for overwrite in raw_overwrites or []:
        if int(overwrite.get("type", -1)) == 0:
            role_entries.append(
                {
                    "role": role_names.get(str(overwrite.get("id")), "unknown_role"),
                    "allow": decode_permissions(overwrite.get("allow", "0")),
                    "deny": decode_permissions(overwrite.get("deny", "0")),
                }
            )
        else:
            member_count += 1
    return {
        "roles": role_entries,
        "member_specific_overwrite_count": member_count,
    }


def pseudonym_for(author_id: str, pseudonyms: Dict[str, str]) -> str:
    if author_id not in pseudonyms:
        pseudonyms[author_id] = f"member_{len(pseudonyms) + 1:03d}"
    return pseudonyms[author_id]


def collect_channel_activity(
    api: DiscordReadOnlyAPI,
    channel_id: str,
    cutoff: datetime,
    max_messages: int,
    include_content: bool,
    sample_limit: int,
    pseudonyms: Dict[str, str],
    role_names: Dict[str, str],
    channel_names: Dict[str, str],
) -> Dict[str, Any]:
    if max_messages == 0:
        return {
            "scan_status": "disabled",
            "messages_in_window": None,
            "unique_human_authors": None,
        }

    before: Optional[str] = None
    inspected = 0
    messages_in_window = 0
    human_messages = 0
    bot_messages = 0
    attachments = 0
    reactions = 0
    human_authors = set()
    active_days = set()
    month_counts: Counter[str] = Counter()
    latest_at: Optional[datetime] = None
    oldest_at: Optional[datetime] = None
    reached_cutoff = False
    samples: List[Dict[str, Any]] = []

    while inspected < max_messages:
        page_limit = min(100, max_messages - inspected)
        params: Dict[str, Any] = {"limit": page_limit}
        if before:
            params["before"] = before

        page = api.get(f"/channels/{channel_id}/messages", params=params)
        if not page:
            break

        for message in page:
            inspected += 1
            timestamp = parse_discord_timestamp(message.get("timestamp"))
            if timestamp is None:
                continue
            if timestamp < cutoff:
                reached_cutoff = True
                break

            messages_in_window += 1
            latest_at = timestamp if latest_at is None or timestamp > latest_at else latest_at
            oldest_at = timestamp if oldest_at is None or timestamp < oldest_at else oldest_at
            active_days.add(timestamp.date().isoformat())
            month_counts[timestamp.strftime("%Y-%m")] += 1
            attachments += len(message.get("attachments") or [])
            reactions += sum(int(reaction.get("count", 0)) for reaction in message.get("reactions") or [])

            author = message.get("author") or {}
            if author.get("bot"):
                bot_messages += 1
            else:
                human_messages += 1
                author_id = str(author.get("id", "unknown"))
                human_authors.add(author_id)

                if include_content and len(samples) < sample_limit:
                    content = redact_text(
                        str(message.get("content") or ""),
                        role_names=role_names,
                        channel_names=channel_names,
                    )
                    if content:
                        samples.append(
                            {
                                "timestamp": iso_utc(timestamp),
                                "author": pseudonym_for(author_id, pseudonyms),
                                "content": content,
                                "attachment_count": len(message.get("attachments") or []),
                            }
                        )

        if reached_cutoff or len(page) < page_limit:
            break
        before = str(page[-1].get("id"))
        if not before:
            break

    result: Dict[str, Any] = {
        "scan_status": "ok",
        "window_start": iso_utc(cutoff),
        "messages_in_window": messages_in_window,
        "human_messages": human_messages,
        "bot_messages": bot_messages,
        "unique_human_authors": len(human_authors),
        "active_days": len(active_days),
        "attachments": attachments,
        "reactions": reactions,
        "latest_message_at": iso_utc(latest_at) if latest_at else None,
        "oldest_scanned_message_at": iso_utc(oldest_at) if oldest_at else None,
        "messages_by_month": dict(sorted(month_counts.items())),
        "inspected_messages": inspected,
        "reached_window_start": reached_cutoff,
        "scan_limit_reached": inspected >= max_messages and not reached_cutoff,
    }
    if include_content:
        result["redacted_content_samples"] = list(reversed(samples))
    return result


def collect_report(
    api: DiscordReadOnlyAPI,
    guild_id: str,
    days: int,
    max_messages: int,
    include_content: bool,
    sample_limit: int,
) -> Dict[str, Any]:
    warnings: List[str] = []
    guild = api.get(f"/guilds/{guild_id}", params={"with_counts": "true"})
    channels = api.get(f"/guilds/{guild_id}/channels")
    roles = api.get(f"/guilds/{guild_id}/roles")

    active_threads_data = safe_get(
        api,
        f"/guilds/{guild_id}/threads/active",
        warnings,
        "active threads",
    )
    active_threads = (active_threads_data or {}).get("threads", [])
    all_channels = list(channels or []) + list(active_threads or [])

    role_counts = safe_get(
        api,
        f"/guilds/{guild_id}/roles/member-counts",
        warnings,
        "role member counts",
    ) or {}
    scheduled_events = safe_get(
        api,
        f"/guilds/{guild_id}/scheduled-events",
        warnings,
        "scheduled events",
        params={"with_user_count": "true"},
    ) or []
    welcome_screen = safe_get(
        api,
        f"/guilds/{guild_id}/welcome-screen",
        warnings,
        "welcome screen",
    )

    role_names = {str(role.get("id")): str(role.get("name", "unnamed")) for role in roles}
    channel_names = {
        str(channel.get("id")): str(channel.get("name", "unnamed")) for channel in all_channels
    }
    channel_by_id = {str(channel.get("id")): channel for channel in all_channels}
    category_names = {
        str(channel.get("id")): str(channel.get("name", "unnamed"))
        for channel in channels
        if int(channel.get("type", -1)) == 4
    }

    exported_roles = []
    for role in sorted(roles, key=lambda item: int(item.get("position", 0)), reverse=True):
        color = int(role.get("color", 0))
        exported_roles.append(
            {
                "name": role.get("name"),
                "position": role.get("position"),
                "member_count": role_counts.get(str(role.get("id"))),
                "color": f"#{color:06X}",
                "hoisted": bool(role.get("hoist")),
                "mentionable": bool(role.get("mentionable")),
                "managed": bool(role.get("managed")),
                "permissions": decode_permissions(role.get("permissions", "0")),
            }
        )

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    pseudonyms: Dict[str, str] = {}
    exported_channels = []

    for index, channel in enumerate(
        sorted(
            all_channels,
            key=lambda item: (
                int(channel_by_id.get(str(item.get("parent_id")), {}).get("position", -1)),
                int(item.get("position", 0)),
                str(item.get("name", "")),
            ),
        ),
        start=1,
    ):
        channel_id = str(channel.get("id"))
        type_id = int(channel.get("type", -1))
        parent_id = str(channel.get("parent_id")) if channel.get("parent_id") else None
        last_at = snowflake_timestamp(channel.get("last_message_id"))
        entry: Dict[str, Any] = {
            "audit_key": f"channel_{index:03d}",
            "name": channel.get("name"),
            "type": channel_type_name(type_id),
            "category": category_names.get(parent_id) if parent_id else None,
            "position": channel.get("position"),
            "topic": redact_text(str(channel.get("topic") or ""), role_names, channel_names)
            or None,
            "nsfw": bool(channel.get("nsfw")),
            "slowmode_seconds": channel.get("rate_limit_per_user", 0),
            "last_message_at_from_channel": iso_utc(last_at) if last_at else None,
            "permission_overwrites": permission_overwrites(
                channel.get("permission_overwrites") or [], role_names
            ),
        }

        if type_id in MESSAGE_CHANNEL_TYPES:
            try:
                entry["activity"] = collect_channel_activity(
                    api=api,
                    channel_id=channel_id,
                    cutoff=cutoff,
                    max_messages=max_messages,
                    include_content=include_content,
                    sample_limit=sample_limit,
                    pseudonyms=pseudonyms,
                    role_names=role_names,
                    channel_names=channel_names,
                )
            except DiscordAPIError as exc:
                entry["activity"] = {
                    "scan_status": "unavailable",
                    "reason": f"HTTP {exc.status}: {exc.detail}" if exc.status else exc.detail,
                }
                warnings.append(f"channel #{channel.get('name')}: message scan unavailable")
        exported_channels.append(entry)

    welcome_export = None
    if welcome_screen:
        welcome_export = {
            "description": redact_text(str(welcome_screen.get("description") or "")) or None,
            "channels": [
                {
                    "channel": channel_names.get(str(item.get("channel_id")), "unknown_channel"),
                    "description": redact_text(str(item.get("description") or "")) or None,
                    "emoji_name": item.get("emoji_name"),
                }
                for item in welcome_screen.get("welcome_channels") or []
            ],
        }

    events_export = [
        {
            "name": event.get("name"),
            "description": redact_text(str(event.get("description") or "")) or None,
            "status": event.get("status"),
            "entity_type": event.get("entity_type"),
            "scheduled_start_time": event.get("scheduled_start_time"),
            "scheduled_end_time": event.get("scheduled_end_time"),
            "interested_user_count": event.get("user_count"),
        }
        for event in scheduled_events
    ]

    return {
        "report": {
            "format": "discord-readonly-audit",
            "version": 1,
            "generated_at": iso_utc(datetime.now(timezone.utc)),
            "activity_window_days": days,
            "max_messages_inspected_per_channel": max_messages,
        },
        "privacy": {
            "message_content_included": include_content,
            "raw_user_ids_included": False,
            "usernames_included": False,
            "attachment_urls_included": False,
            "bot_token_included": False,
            "content_redaction_enabled": include_content,
        },
        "server": {
            "name": guild.get("name"),
            "description": redact_text(str(guild.get("description") or "")) or None,
            "approximate_member_count": guild.get("approximate_member_count"),
            "approximate_online_count": guild.get("approximate_presence_count"),
            "features": sorted(guild.get("features") or []),
            "verification_level": guild.get("verification_level"),
            "default_notification_level": guild.get("default_message_notifications"),
            "boost_level": guild.get("premium_tier"),
            "emoji_names": sorted(
                emoji.get("name") for emoji in guild.get("emojis") or [] if emoji.get("name")
            ),
        },
        "roles": exported_roles,
        "channels": exported_channels,
        "scheduled_events": events_export,
        "welcome_screen": welcome_export,
        "limitations": [
            "Archived threads are not enumerated; active threads are included when accessible.",
            "Channels the bot cannot view may be absent or have activity marked unavailable.",
            "Activity can be truncated by max_messages_inspected_per_channel.",
            "Voice participation history is not available through this audit.",
        ],
        "warnings": warnings,
    }


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        print("DISCORD_BOT_TOKEN is not set.", file=sys.stderr)
        return 2

    output_path = Path(args.output).expanduser().resolve()
    api = DiscordReadOnlyAPI(token)

    try:
        print("Reading Discord server structure and activity (GET requests only)...")
        report = collect_report(
            api=api,
            guild_id=str(args.guild_id),
            days=args.days,
            max_messages=args.max_messages_per_channel,
            include_content=args.include_content,
            sample_limit=args.samples_per_channel,
        )
        output_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except DiscordAPIError as exc:
        if exc.status == 401:
            hint = "Check the bot token. Never paste the token into chat."
        elif exc.status == 403:
            hint = "The bot lacks access to the server or required read permissions."
        elif exc.status == 404:
            hint = "Check the server ID and make sure the bot was invited to that server."
        else:
            hint = "Check the network connection and Discord API availability."
        print(f"Export failed: {exc}\n{hint}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"Could not write the report: {exc}", file=sys.stderr)
        return 1

    print(f"Done: {output_path}")
    if args.include_content:
        print("Message samples were included. Review the JSON before sharing it.")
    else:
        print("Message bodies and usernames were not included.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
