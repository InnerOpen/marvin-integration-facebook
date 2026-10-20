"""Pure helpers for Marvin's shared social-engagement shape: normalised comment items and the poll cursor.

No SDK import and no HTTP here. The provider does the Graph calls and hands rows in. The cursor and paging behave exactly
like the Instagram and Mastodon plugins', so the shared auto-reply engine treats every network the same.
"""

from __future__ import annotations

import base64
import binascii
import json
from datetime import UTC, datetime

NETWORK = "facebook"
KIND_COMMENT, KIND_REPLY, KIND_MENTION = "comment", "reply", "mention"
KINDS = (KIND_COMMENT, KIND_REPLY, KIND_MENTION)
DEFAULT_LIMIT = 50


def _iso(ts: datetime) -> str:
    return ts.astimezone(UTC).isoformat()


def to_item(row: dict) -> dict | None:
    """One comment row (see ``FacebookProvider._comment_row``) as a ``social.comments`` item; None when it has no usable
    timestamp, since the cursor and the oldest→newest order both hang on it."""
    ts = row.get("timestamp")
    if ts is None:
        return None
    name = row.get("name") or ""
    return {
        "id": row["comment_id"],
        "kind": KIND_REPLY if row.get("parent_id") else KIND_COMMENT,
        "post_id": row["post_id"],
        "post_url": row.get("post_url"),
        # The Pages API gives no username, so the commenter's name stands in for the handle. `id` and `name` may be empty
        # when Facebook withholds them (no pages_read_user_content, or a commenter who restricts it).
        "author": {"id": row.get("author_id") or "", "handle": name, "name": name},
        "text": row.get("text") or "",
        "created_at": _iso(ts),
        "url": row.get("url"),
        "network": NETWORK,
    }


def encode_cursor(newest: str | None, ids_at_newest: list[str]) -> str:
    """The cursor is the newest created_at handed out plus the ids sharing that exact second, so a comment that
    lands in the same second as the last one returned is neither repeated nor lost. Opaque to callers."""
    raw = json.dumps({"t": newest, "ids": sorted(ids_at_newest)}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[str | None, set[str]]:
    try:
        data = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        newest, ids = data["t"], data["ids"]
        if newest is not None:
            datetime.fromisoformat(newest)
        return newest, {str(i) for i in ids}
    except (binascii.Error, ValueError, KeyError, TypeError, UnicodeDecodeError) as e:
        raise ValueError("The 'since' cursor is not one this plugin returned.") from e


def page_after(items: list[dict], since: str | None, limit: int) -> tuple[list[dict], str]:
    """The items newer than the cursor, oldest→newest, at most `limit`, and the cursor to ask with next time.

    With a cursor the *oldest* `limit` new items are returned, so a backlog drains in order without gaps. Without one
    (the first poll) the *newest* `limit` are, since older history is not what a poller starting now wants.
    """
    items = sorted(items, key=lambda i: (i["created_at"], i["id"]))
    seen_at, seen_ids = decode_cursor(since) if since else (None, set())
    if since and seen_at:
        items = [i for i in items if i["created_at"] > seen_at or (i["created_at"] == seen_at and i["id"] not in seen_ids)]
    page = items[:limit] if since else items[-limit:]
    if not page:
        return [], since or encode_cursor(None, [])
    newest = page[-1]["created_at"]
    ids = [i["id"] for i in page if i["created_at"] == newest]
    if newest == seen_at:
        ids += list(seen_ids)
    return page, encode_cursor(newest, ids)
