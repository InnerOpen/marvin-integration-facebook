"""Meta's Facebook Page webhook deliveries as Marvin Social inbox items (``inbox_items_from_webhook``, SDK 0.18.0, and the
``social.inbox_items`` action).

Pure: no SDK import, no HTTP. A delivery is ``{"object": "page", "entry": [...]}``; each entry carries ``changes`` (the
Page's ``feed``: comments and replies; ``mention``: the Page tagged in a post or comment) or ``messaging`` (Messenger: direct
messages and reactions). Each becomes an item in Marvin's shared shape — a comment keeps the ``id`` a ``social.comments`` read
gives it, so the inbox keeps it once whichever way it arrives. Edits, removals, likes, the Page's own messages and read
receipts give nothing.
"""

from __future__ import annotations

from datetime import UTC, datetime

from .engagement import KIND_COMMENT, KIND_MENTION, KIND_REPLY, NETWORK

KIND_MESSAGE, KIND_REACTION = "message", "reaction"


def _iso(value) -> str | None:
    """Meta's times: messaging in milliseconds, changes in seconds or ISO text."""
    if isinstance(value, str) and value:
        return value
    if not isinstance(value, int | float):
        return None
    seconds = value / 1000 if value > 10**11 else value
    return datetime.fromtimestamp(seconds, UTC).isoformat()


def _person(raw) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    name = raw.get("name") or ""
    return {"id": str(raw.get("id") or ""), "handle": name, "name": name}


def _feed_item(value: dict, when) -> dict | None:
    if value.get("item") != "comment" or value.get("verb") != "add" or not value.get("comment_id"):
        return None
    parent, post = value.get("parent_id"), value.get("post_id")
    return {
        "id": str(value["comment_id"]),
        "kind": KIND_REPLY if parent and parent != post else KIND_COMMENT,
        "post_id": str(post) if post else None,
        "author": _person(value.get("from")),
        "text": value.get("message") or "",
        "created_at": _iso(value.get("created_time")) or when,
        "url": value.get("permalink_url") or None,
        "network": NETWORK,
    }


def _mention_item(value: dict, when) -> dict | None:
    if value.get("verb") not in (None, "add"):
        return None
    item_id = value.get("comment_id") or value.get("post_id")
    if not item_id:
        return None
    return {
        "id": str(item_id),
        "kind": KIND_MENTION,
        "post_id": str(value["post_id"]) if value.get("post_id") else None,
        "author": {"id": str(value.get("sender_id") or ""), "handle": value.get("sender_name") or "", "name": value.get("sender_name") or ""},
        "text": value.get("message") or "",
        "created_at": _iso(value.get("created_time")) or when,
        "network": NETWORK,
    }


def _attachments(message: dict) -> list[dict]:
    out = []
    for a in message.get("attachments") or []:
        if isinstance(a, dict):
            payload = a.get("payload") if isinstance(a.get("payload"), dict) else {}
            kept = {k: v for k, v in (("type", a.get("type")), ("url", payload.get("url")), ("title", a.get("title") or payload.get("title"))) if v}
            if kept:
                out.append(kept)
    return out


def _messaging_item(event: dict, when) -> dict | None:
    sender = str((event.get("sender") or {}).get("id") or "")
    stamp = _iso(event.get("timestamp")) or when
    if isinstance(event.get("reaction"), dict):
        reaction = event["reaction"]
        if not reaction.get("mid") or reaction.get("action") != "react":
            return None
        return {
            "id": f"{reaction['mid']}:reaction:{sender}",
            "kind": KIND_REACTION,
            "author": {"id": sender, "handle": "", "name": ""},
            "text": reaction.get("emoji") or reaction.get("reaction") or "",
            "created_at": stamp,
            "network": NETWORK,
        }
    message = event.get("message") if isinstance(event.get("message"), dict) else {}
    if not message.get("mid") or message.get("is_echo"):
        return None
    attachments = _attachments(message)
    return {
        "id": message["mid"],
        "kind": KIND_MESSAGE,
        "author": {"id": sender, "handle": "", "name": ""},
        "text": message.get("text") or next((a["title"] for a in attachments if a.get("title")), ""),
        "created_at": stamp,
        "attachments": attachments,
        "network": NETWORK,
    }


def inbox_items(payload: dict) -> list[dict]:
    """Every inbox item in one delivery, in order."""
    if not isinstance(payload, dict) or payload.get("object") != "page":
        return []
    items: list[dict] = []
    for entry in payload.get("entry") or []:
        if not isinstance(entry, dict):
            continue
        when = _iso(entry.get("time")) or datetime.now(UTC).isoformat()
        for change in entry.get("changes") or []:
            value = change.get("value") if isinstance(change, dict) and isinstance(change.get("value"), dict) else {}
            field = change.get("field") if isinstance(change, dict) else None
            item = _feed_item(value, when) if field == "feed" else _mention_item(value, when) if field == "mention" else None
            if item:
                items.append(item)
        for event in entry.get("messaging") or []:
            if isinstance(event, dict) and (item := _messaging_item(event, when)):
                items.append(item)
    return items
