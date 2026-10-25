"""Facebook Page provider: post text, a link or photos to a Page, and answer its comments, through the Graph API.

Uses ``graph.facebook.com`` with a **Page access token** (sent as a Bearer header, never in a URL). The provider is pure
with respect to Marvin: it gets ``config``, the resolved ``secret`` (the token), a ``logger`` and a safe ``http`` client via
``ctx``, and returns dicts.

The ``post`` action is Marvin's shared ``social.post`` capability:

- no images: a feed post (``POST /{page}/feed``) with ``message`` and, if given, ``link``;
- one image: a photo post (``POST /{page}/photos``) with the text as ``caption``;
- several images: each uploaded unpublished (``published=false``), then one feed post whose ``attached_media`` lists them.

The other actions are Marvin's shared social-engagement capabilities (``social.comments`` / ``reply`` / ``like`` / ``hide`` /
``delete_comment`` / ``message``), which the auto-reply engine and workflows use. A *private reply* is a Messenger message
to the commenter (``POST /{page}/messages`` with ``recipient.comment_id``): one per comment, within 7 days of it. A direct
message is allowed only within 24 hours of the person's last message to the Page (Meta's messaging policy).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import ClassVar
from urllib.parse import quote, urlencode

from marvin_integration_sdk import (
    CATEGORY_DESTINATION,
    CredentialField,
    IntegrationContext,
    IntegrationError,
    IntegrationProvider,
    ProviderAction,
    Response,
    WorkflowRecipe,
    register_provider,
)

from .content import CONTENT
from .engagement import DEFAULT_LIMIT, KIND_COMMENT, KIND_MENTION, KIND_REPLY, KINDS, NETWORK, decode_cursor, page_after, to_item
from .webhooks import inbox_items

DEFAULT_API_VERSION = "v22.0"
MAX_IMAGES = 10  # the social.post contract's cap; Facebook itself allows more photos in one post
ALT_MAX = 1000
DEFAULT_RECENT = 10
MAX_RECENT = 100
DEFAULT_LOOKBACK_POSTS = 10
COMMENTS_PAGE_SIZE = 50
PRIVATE_REPLY_DAYS = 7  # Meta: a private reply goes out within 7 days of the comment (verify)
MESSENGER_TEXT_MAX = 2000  # Messenger's limit for a message's text (verify)
POST_FIELDS = "id,created_time,permalink_url"
COMMENT_FIELDS = "id,message,created_time,from{id,name},permalink_url,parent{id}"
ERROR_TEXT_LIMIT = 200

# Error codes this provider raises (the SDK's IntegrationError.code). No error policy is declared: nothing is retried.
CODE_UNSUPPORTED = "unsupported"  # what Facebook (or the contract) can't do: refused before any call
CODE_AUTH = "auth_expired"  # the Page token is invalid or expired
CODE_PERMISSION = "permission_denied"  # the token lacks a permission, or the app/Page is restricted
CODE_RATE_LIMIT = "rate_limited"
CODE_BLOCKED = "blocked"  # Facebook blocked the action (spam / policy)
CODE_REQUEST = "invalid_request"
CODE_UNKNOWN = "unknown"

# Graph error `code` → ours (https://developers.facebook.com/docs/graph-api/guides/error-handling — verify for new versions).
_GRAPH_CODES = {190: CODE_AUTH, 102: CODE_AUTH, 10: CODE_PERMISSION, 368: CODE_BLOCKED, 100: CODE_REQUEST}
_RATE_LIMIT_CODES = {4, 17, 32, 341, 613}
# Graph `error_subcode`s that are a permanent "not allowed now", not a missing permission (verify against Meta's Messenger docs):
# 2018278 is a message sent outside the 24-hour window. The engine skips `unsupported` without retrying.
_SUBCODES = {2018278: CODE_UNSUPPORTED}


def _graph_error(resp: Response) -> IntegrationError:
    """A readable IntegrationError from a failed Graph response. Graph answers ``{"error": {"message", "code", ...}}``."""
    message, code, subcode = resp.text[:ERROR_TEXT_LIMIT], None, None
    try:
        err = (resp.json() or {}).get("error") or {}
        message, code, subcode = str(err.get("message") or message), err.get("code"), err.get("error_subcode")
    except (ValueError, AttributeError):
        pass
    kind = CODE_UNKNOWN
    if isinstance(subcode, int) and subcode in _SUBCODES:
        kind = _SUBCODES[subcode]
    elif isinstance(code, int):
        if code in _RATE_LIMIT_CODES:
            kind = CODE_RATE_LIMIT
        elif 200 <= code <= 299:  # 200–299: permissions
            kind = CODE_PERMISSION
        else:
            kind = _GRAPH_CODES.get(code, CODE_UNKNOWN)
    suffix = f" (code {code})" if code is not None else ""
    if subcode is not None:
        suffix = f"{suffix[:-1]}, subcode {subcode})" if suffix else f" (subcode {subcode})"
    return IntegrationError(f"Facebook API error: HTTP {resp.status_code}: {message}{suffix}", code=kind)


def _refuse(message: str) -> IntegrationError:
    return IntegrationError(message, code=CODE_UNSUPPORTED)


def _parse_ts(value: str | None) -> datetime | None:
    # Graph timestamps look like "2026-09-24T18:03:11+0000": not quite ISO 8601 for fromisoformat.
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S%z")
    except ValueError:
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None


def _flag(value, default: bool) -> bool:
    """A boolean argument. Workflow templates can hand over "false" as text, which bool() would read as true."""
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() not in ("", "false", "0", "no", "off")
    return bool(value)


def _with_link(text: str, link: str) -> str:
    """The text with the link on its own line (Facebook turns a URL in a message or caption into a clickable link),
    unless the text already has it."""
    return f"{text}\n\n{link}" if link and link not in text else text


@register_provider
class FacebookProvider(IntegrationProvider):
    slug = "facebook"
    name = "Facebook Page"
    description = "Post text, links and photos to a Facebook Page, and read and answer its comments, after approval."
    category = CATEGORY_DESTINATION
    icon = "📘"
    logo = "logo.png"
    post_limits: ClassVar[dict] = {"max": 63206, "unit": "characters", "visible": 125, "links": "clickable"}
    """A Page post: Facebook's 63,206 characters, about 125 shown before "See more" on a phone; a link in it is clickable."""
    # Meta's webhooks, for an incoming webhook in Marvin: it checks the URL with a GET (hub.mode / hub.challenge /
    # hub.verify_token — the WebSub handshake) before it delivers, then signs each delivery with the app secret.
    verification_schemes: ClassVar[dict[str, dict]] = {
        "meta": {
            "challenge_param": "hub.challenge",
            "token_param": "hub.verify_token",
            "mode_param": "hub.mode",
            "mode_values": ["subscribe"],
            "notes": "Meta (Instagram, Facebook, WhatsApp): the verify token you type into the app's webhook settings",
        }
    }
    signature_schemes: ClassVar[dict[str, dict]] = {
        "meta": {
            "header": "X-Hub-Signature-256",
            "prefix": "sha256=",
            # The key is the secret of the login setup the app uses: Instagram login signs with the Instagram app secret
            # (Use cases → Instagram API → API setup with Instagram login), Facebook login with App settings → Basic.
            "notes": "Meta: sha256=<hex> of the body, keyed with your app's secret — the Instagram app secret for Instagram login",
            "sender_issues_key": True,
        }
    }
    # No packaged logo on purpose: Facebook's mark is Meta's to supply (see the README, "Logo"). The emoji is the fallback.
    guide = "guide.md"
    content = CONTENT
    # One ready-made workflow in the Workflow Library, offered once the Page is connected: post on publish, after an editor
    # approves. The title and summary as the message, with the entry's link (Facebook shows its preview).
    recipes = (
        WorkflowRecipe(
            id="post-on-publish",
            title="Post to Facebook when published",
            outcome=(
                "When an entry is published, an editor is asked whether to post it; once approved, its title, summary and link go "
                "to your Facebook Page, and the post's link is kept on the entry."
            ),
            category="Social and community",
            tags=("facebook", "social", "publishing", "approval"),
            definition={
                "trigger": {"type": "event", "event": "entry_published"},
                "conditions": [{"field": "entry.entry_type", "op": "eq", "value": "{{entry_type}}"}],
                "actions": [
                    {
                        "kind": "approval",
                        "id": "ok",
                        "message": "Post “${entry.title}” to the Facebook Page? Text: ${entry.summary}",
                        "approvers": "editor",
                        "expires_in_hours": 48,
                    },
                    {
                        "kind": "integration",
                        "id": "post",
                        "integration": "{{facebook_integration}}",
                        "action": "post",
                        "args": {"text": "${entry.title}\n\n${entry.summary}", "link": "${entry.url}", "dry_run": False},
                        "when": {"field": "steps.ok.output.decision", "op": "eq", "value": "approved"},
                    },
                    {
                        "kind": "entry",
                        "op": "set_metadata",
                        "metadata": {"facebook_post_id": "${steps.post.output.post_id}", "facebook_url": "${steps.post.output.url}"},
                        "when": {"field": "steps.post.output.posted", "op": "eq", "value": True},
                    },
                ],
            },
            variables={
                "entry_type": {
                    "type": "entry_type_slug",
                    "description": "Entries of this type are offered to the Facebook Page when published.",
                    "example": "post",
                },
                "facebook_integration": {
                    "type": "integration_slug",
                    "description": "The Facebook Page connection. Its Page token needs the `pages_manage_posts` permission.",
                    "example": "facebook",
                },
            },
            side_effects=(
                {"kind": "external_post", "description": "One public Facebook Page post per approved run."},
                {"kind": "content_mutation", "description": "Saves the post's link on the entry."},
            ),
        ),
    )

    credentials = (
        CredentialField(
            key="page_access_token",
            label="Page access token",
            help="A long-lived Page access token (it does not expire when made from a long-lived user token) with pages_manage_posts; comments, replies and messages also need "
            "pages_read_engagement, pages_read_user_content, pages_manage_engagement and pages_messaging.",
        ),
    )
    config_schema: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "page_id": {"type": "string", "title": "Page ID", "description": "The numeric id of the Facebook Page the token belongs to."},
            "api_version": {"type": "string", "title": "Graph API version", "default": DEFAULT_API_VERSION},
            "lookback_posts": {
                "type": "string",
                "title": "Posts to scan",
                "default": str(DEFAULT_LOOKBACK_POSTS),
                "description": "How many of your most recent posts to check for comments.",
            },
        },
        "required": ["page_id"],
        "additionalProperties": False,
    }

    actions = (
        ProviderAction(
            key="post",
            label="Post to the Facebook Page",
            description=(
                "Marvin's shared social post: text (and a link) as a feed post, one image as a photo post with the text as its "
                "caption, several images as one feed post with all the photos attached."
            ),
            capability="social.post",
            input_schema={
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "The post's message (or the photo's caption)."},
                    "images": {
                        "type": "array",
                        "items": {"type": "string"},
                        "maxItems": MAX_IMAGES,
                        "description": "0–10 public https image URLs; Facebook fetches them. None makes a text post.",
                    },
                    "alt": {"type": "array", "items": {"type": "string"}, "description": "Alt text, one per image, in order."},
                    "link": {
                        "type": "string",
                        "description": "A URL to attach to a text post (Facebook shows a link preview); with images it is added to the text.",
                    },
                    "dry_run": {"type": "boolean", "default": False, "description": "Report what would be posted without calling Facebook."},
                },
                "required": ["text"],
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {
                    "posted": {"type": "boolean"},
                    "post_id": {"type": ["string", "null"]},
                    "url": {"type": ["string", "null"], "description": "The post's permalink."},
                    "network": {"type": "string", "const": NETWORK},
                    "would_post": {"type": "object", "description": "A dry run only: what would be sent."},
                },
            },
            cost_hint="free",
            requires_approval=True,
        ),
        ProviderAction(
            key="list_recent_posts",
            label="List recent posts",
            description="The Page's most recent published posts: id, message, time and permalink.",
            input_schema={
                "type": "object",
                "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": MAX_RECENT, "default": DEFAULT_RECENT}},
                "additionalProperties": False,
            },
            cost_hint="free",
        ),
        ProviderAction(
            key="comments",
            label="Read comments and replies",
            description=(
                "Marvin's shared social comments read: new comments and replies on your Page's recent posts since a cursor, oldest first. "
                "Pass back the cursor from the last call as `since`."
            ),
            capability="social.comments",
            input_schema={
                "type": "object",
                "properties": {
                    "since": {"type": "string", "description": "The cursor this action returned last time (opaque); omit on the first call."},
                    "limit": {"type": "integer", "default": DEFAULT_LIMIT, "minimum": 1},
                    "kinds": {
                        "type": "array",
                        "items": {"type": "string", "enum": list(KINDS)},
                        "description": "comment, reply, mention. Facebook reports Page tags only by webhook, so that kind returns nothing.",
                    },
                },
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {
                    "items": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string"},
                                "kind": {"type": "string", "enum": [KIND_COMMENT, KIND_REPLY, KIND_MENTION]},
                                "post_id": {"type": "string"},
                                "post_url": {"type": ["string", "null"]},
                                "author": {
                                    "type": "object",
                                    "properties": {"id": {"type": "string"}, "handle": {"type": "string"}, "name": {"type": "string"}},
                                },
                                "text": {"type": "string"},
                                "created_at": {"type": "string", "description": "ISO 8601, UTC."},
                                "url": {"type": ["string", "null"]},
                                "network": {"type": "string", "const": NETWORK},
                            },
                        },
                    },
                    "cursor": {"type": "string"},
                },
            },
            cost_hint="free",
        ),
        ProviderAction(
            key="reply",
            label="Reply to a comment",
            description=(
                "Marvin's shared social reply: a private reply (a Messenger message to the commenter, once per comment, within 7 days) "
                "by default, or a public reply under the comment."
            ),
            capability="social.reply",
            input_schema={
                "type": "object",
                "properties": {
                    "comment_id": {"type": "string"},
                    "text": {"type": "string"},
                    "private": {"type": "boolean", "default": True, "description": "true: Messenger the commenter; false: reply in the thread."},
                    "dry_run": {
                        "type": "boolean",
                        "default": False,
                        "description": "Check the reply and report what would be sent, sending nothing.",
                    },
                },
                "required": ["comment_id", "text"],
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {
                    "replied": {"type": "boolean"},
                    "reply_id": {"type": ["string", "null"]},
                    "private": {"type": "boolean"},
                    "url": {"type": ["string", "null"]},
                    "network": {"type": "string", "const": NETWORK},
                    "would_reply": {"type": "object", "description": "A dry run only: what would be sent."},
                },
            },
            cost_hint="free",
            requires_approval=True,
        ),
        ProviderAction(
            key="like",
            label="Like a comment",
            description="Marvin's shared social like: like a comment under your Page's post, as the Page.",
            capability="social.like",
            input_schema={
                "type": "object",
                "properties": {"comment_id": {"type": "string"}},
                "required": ["comment_id"],
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {"liked": {"type": "boolean"}, "network": {"type": "string", "const": NETWORK}},
            },
            cost_hint="free",
        ),
        ProviderAction(
            key="hide",
            label="Hide or unhide a comment",
            description="Marvin's shared social hide: hide a comment under your Page's post (or show it again).",
            capability="social.hide",
            input_schema={
                "type": "object",
                "properties": {"comment_id": {"type": "string"}, "hidden": {"type": "boolean", "default": True}},
                "required": ["comment_id"],
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {"hidden": {"type": "boolean"}, "network": {"type": "string", "const": NETWORK}},
            },
            cost_hint="free",
        ),
        ProviderAction(
            key="delete_comment",
            label="Delete a comment",
            description="Marvin's shared social delete: permanently delete a comment under your Page's post, or one of the Page's own replies.",
            capability="social.delete_comment",
            input_schema={
                "type": "object",
                "properties": {"comment_id": {"type": "string"}},
                "required": ["comment_id"],
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {"deleted": {"type": "boolean"}, "network": {"type": "string", "const": NETWORK}},
            },
            cost_hint="free",
            requires_approval=True,
        ),
        ProviderAction(
            key="message",
            label="Send a Messenger message",
            description=(
                "Marvin's shared social message: a Messenger message from the Page to a person, by their page-scoped id. Meta allows it "
                "only within 24 hours of the person's last message to the Page. A handle alone can't be resolved, so it is refused."
            ),
            capability="social.message",
            input_schema={
                "type": "object",
                "properties": {
                    "to": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string", "description": "The person's page-scoped id (PSID), e.g. a commenter's author id."},
                            "handle": {"type": "string", "description": "Not enough on its own: Facebook can't resolve a name to a PSID."},
                        },
                        "additionalProperties": False,
                    },
                    "text": {"type": "string"},
                    "dry_run": {"type": "boolean", "default": False, "description": "Check the message and report it, sending nothing."},
                },
                "required": ["to", "text"],
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {
                    "sent": {"type": "boolean"},
                    "message_id": {"type": ["string", "null"]},
                    "network": {"type": "string", "const": NETWORK},
                    "would_send": {"type": "object", "description": "A dry run only: what would be sent."},
                },
            },
            cost_hint="free",
            requires_approval=True,
        ),
        ProviderAction(
            key="inbox_items",
            label="Turn a webhook delivery into inbox items",
            description=(
                "Marvin's shared social.inbox_items: Meta's webhook delivery (its body, as a workflow received it) turned into "
                "Social inbox items — Marvin keeps them in this connection's inbox. For a webhook this integration didn't create; "
                "its own webhook's deliveries reach the inbox without a step. Reads only the payload: nothing is sent."
            ),
            capability="social.inbox_items",
            input_schema={
                "type": "object",
                "properties": {"payload": {"type": "object", "description": "The delivery's body, e.g. ${event.payload}."}},
                "required": ["payload"],
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {"items": {"type": "array", "description": "Inbox items in Marvin's shared shape."}},
            },
            cost_hint="free",
        ),
    )

    # ---- plumbing ---------------------------------------------------------------------------

    @staticmethod
    def _cfg(ctx: IntegrationContext) -> dict:
        return ctx.config or {}

    def _base(self, ctx: IntegrationContext) -> str:
        return f"https://graph.facebook.com/{self._cfg(ctx).get('api_version') or DEFAULT_API_VERSION}"

    def _page(self, ctx: IntegrationContext) -> str:
        page_id = str(self._cfg(ctx).get("page_id") or "").strip()
        if not page_id:
            raise ValueError("Missing Facebook Page ID.")
        return quote(page_id, safe="")

    @staticmethod
    def _auth(ctx: IntegrationContext) -> dict[str, str]:
        return {"Authorization": f"Bearer {ctx.secret}"}

    def _get_json(self, ctx: IntegrationContext, url: str) -> dict:
        resp = ctx.http.get(url, headers=self._auth(ctx))
        if not resp.ok:
            raise _graph_error(resp)
        return resp.json()

    @staticmethod
    def _json_or_raise(resp: Response) -> dict:
        if not resp.ok:
            raise _graph_error(resp)
        try:
            return resp.json() or {}
        except ValueError:  # an empty or non-JSON body on a success status
            return {}

    def _post_form(self, ctx: IntegrationContext, path: str, form: dict) -> dict:
        headers = {**self._auth(ctx), "Content-Type": "application/x-www-form-urlencoded"}
        return self._json_or_raise(ctx.http.post(f"{self._base(ctx)}/{path}", data=urlencode(form).encode(), headers=headers))

    def _send_message(self, ctx: IntegrationContext, body: dict) -> dict:
        """Messenger's Send API: ``POST /{page}/messages`` (JSON), needs pages_messaging."""
        return self._json_or_raise(ctx.http.post(f"{self._base(ctx)}/{self._page(ctx)}/messages", json=body, headers=self._auth(ctx)))

    # ---- lifecycle --------------------------------------------------------------------------

    def check(self, ctx: IntegrationContext) -> tuple[str, str | None]:
        if not ctx.secret:
            return ("unconfigured", "Missing Page access token.")
        page_id = str(self._cfg(ctx).get("page_id") or "").strip()
        if not page_id:
            return ("unconfigured", "Missing Page ID.")
        try:
            resp = ctx.http.get(f"{self._base(ctx)}/{quote(page_id, safe='')}?fields=id,name", headers=self._auth(ctx))
        except Exception as e:  # noqa: BLE001
            return ("error", str(e))
        if not resp.ok:
            return ("error", str(_graph_error(resp)))
        try:
            found = str((resp.json() or {}).get("id") or "")
        except ValueError:
            found = ""
        # A numeric id must come back unchanged: a token for another Page would otherwise pass as healthy.
        if page_id.isdigit() and found and found != page_id:
            return ("error", f"The token belongs to Page {found}, not {page_id}.")
        return ("ok", None)

    def run_action(self, key: str, args: dict, ctx: IntegrationContext) -> dict:
        handler = {
            "post": self._action_post,
            "list_recent_posts": self._action_list_recent_posts,
            "comments": self._action_comments,
            "reply": self._action_reply,
            "like": self._action_like,
            "hide": self._action_hide,
            "delete_comment": self._action_delete_comment,
            "message": self._action_message,
            "inbox_items": self._action_inbox_items,
        }.get(key)
        if handler is None:
            raise NotImplementedError(f"facebook has no action '{key}'")
        if not ctx.secret:
            raise ValueError("No Facebook Page access token configured.")
        return handler(args or {}, ctx)

    # ---- actions ----------------------------------------------------------------------------

    def _action_list_recent_posts(self, args: dict, ctx: IntegrationContext) -> dict:
        try:
            limit = min(MAX_RECENT, max(1, int(args.get("limit") or DEFAULT_RECENT)))
        except (TypeError, ValueError):
            limit = DEFAULT_RECENT
        data = self._get_json(ctx, f"{self._base(ctx)}/{self._page(ctx)}/posts?fields=id,message,created_time,permalink_url&limit={limit}")
        return {
            "posts": [
                {
                    "post_id": str(p.get("id") or ""),
                    "message": p.get("message") or "",
                    "created_time": p.get("created_time"),
                    "url": _absolute(p.get("permalink_url")),
                }
                for p in data.get("data") or []
            ]
        }

    def _plan(self, args: dict) -> dict:
        """Validate a `social.post` request and say what would be sent. Every refusal happens here, before any call."""
        text = str(args.get("text") or "").strip()
        if not text:
            raise _refuse("Facebook needs text for the post.")
        raw = args.get("images") or []
        if not isinstance(raw, list) or not all(isinstance(u, str) for u in raw):
            raise _refuse("'images' must be a list of public image URLs.")
        urls = [u.strip() for u in raw if u.strip()]
        if len(urls) > MAX_IMAGES:
            raise _refuse(f"A post takes at most {MAX_IMAGES} images ({len(urls)} given).")
        if any(not u.startswith("https://") for u in urls):
            raise _refuse("Facebook fetches the image itself, so every image URL must be public https.")
        alt = args.get("alt") or []
        if not isinstance(alt, list) or len(alt) > len(urls):
            raise _refuse(f"'alt' is one text per image ({len(urls)} image(s) given).")
        alts = [str(a or "").strip() for a in alt] + [""] * (len(urls) - len(alt))
        if any(len(a) > ALT_MAX for a in alts):
            raise _refuse(f"Alt text is limited to {ALT_MAX} characters.")
        link = str(args.get("link") or "").strip()
        if link and not link.startswith(("https://", "http://")):
            raise _refuse("'link' must be a URL starting with http:// or https://.")

        if not urls:
            plan = {"kind": "text", "endpoint": "feed", "message": text}
            if link:
                plan["link"] = link
            return plan
        # A photo post can't carry a link attachment, so the link goes into the text (Facebook linkifies it).
        message = _with_link(text, link)
        if len(urls) == 1:
            plan = {"kind": "photo", "endpoint": "photos", "caption": message, "images": urls}
        else:
            plan = {"kind": "photos", "endpoint": "feed", "message": message, "images": urls}
        if any(alts):
            plan["alt"] = alts
        return plan

    def _action_post(self, args: dict, ctx: IntegrationContext) -> dict:
        plan = self._plan(args)
        if bool(args.get("dry_run", False)):
            return {"posted": False, "dry_run": True, "post_id": None, "url": None, "network": NETWORK, "would_post": plan}
        page = self._page(ctx)

        if plan["kind"] == "text":
            form = {"message": plan["message"], **({"link": plan["link"]} if plan.get("link") else {})}
            post_id = self._require_id(self._post_form(ctx, f"{page}/feed", form))
        elif plan["kind"] == "photo":
            alt = (plan.get("alt") or [""])[0]
            form = {"url": plan["images"][0], "caption": plan["caption"], "published": "true", **({"alt_text_custom": alt} if alt else {})}
            data = self._post_form(ctx, f"{page}/photos", form)
            # A published photo answers with the photo's id and the id of the feed post it created.
            post_id = str(data.get("post_id") or self._require_id(data))
        else:
            photo_ids = []
            for url, alt in zip(plan["images"], plan.get("alt") or [""] * len(plan["images"]), strict=True):
                form = {"url": url, "published": "false", **({"alt_text_custom": alt} if alt else {})}
                photo_ids.append(self._require_id(self._post_form(ctx, f"{page}/photos", form)))
            # attached_media is an array parameter: one JSON object per photo, keyed attached_media[n].
            form = {"message": plan["message"], **{f"attached_media[{i}]": json.dumps({"media_fbid": pid}) for i, pid in enumerate(photo_ids)}}
            post_id = self._require_id(self._post_form(ctx, f"{page}/feed", form))

        out = {"posted": True, "post_id": post_id, "url": self._permalink(ctx, post_id), "network": NETWORK, "kind": plan["kind"]}
        if plan.get("images"):
            out["images"] = len(plan["images"])
        return out

    # ---- shared engagement (social.comments / reply / like / hide / delete_comment / message) ------------------------
    # Marvin's shared auto-reply engine reads and answers through these. Facebook has no repost of a comment or a
    # post-for-others, so it declares no social.repost.

    def _lookback(self, ctx: IntegrationContext) -> int:
        # The settings form renders flat text inputs, so this arrives as a string.
        try:
            return max(1, int(self._cfg(ctx).get("lookback_posts") or DEFAULT_LOOKBACK_POSTS))
        except (TypeError, ValueError):
            return DEFAULT_LOOKBACK_POSTS

    @staticmethod
    def _node(comment_id: str) -> str:
        return quote(comment_id, safe="")

    @staticmethod
    def _id_arg(args: dict, name: str, action: str) -> str:
        value = str(args.get(name) or "").strip()
        if not value:
            raise ValueError(f"{action} needs '{name}'.")
        return value

    @staticmethod
    def _comment_row(c: dict, post_id: str, post_url: str | None) -> dict:
        # `from` is withheld without pages_read_user_content (or for a commenter who limits it); `parent` is only on a reply.
        author = c.get("from") or {}
        return {
            "comment_id": str(c.get("id")),
            "post_id": post_id,
            "post_url": post_url,
            "author_id": str(author.get("id") or ""),
            "name": author.get("name") or "",
            "text": c.get("message") or "",
            "timestamp": _parse_ts(c.get("created_time")),
            "parent_id": str((c.get("parent") or {}).get("id") or "") or None,
            "url": _absolute(c.get("permalink_url")),
        }

    def _recent_comments(self, ctx: IntegrationContext) -> list[dict]:
        """Comments and replies on the Page's latest posts. ``filter=stream`` flattens a post's comments and their replies into
        one list (a reply carries its ``parent``), newest first so a busy post's first page still holds what is new."""
        base, rows = self._base(ctx), []
        data = self._get_json(ctx, f"{base}/{self._page(ctx)}/posts?fields={POST_FIELDS}&limit={self._lookback(ctx)}")
        for post in data.get("data") or []:
            post_id, post_url = str(post.get("id")), _absolute(post.get("permalink_url"))
            url = (
                f"{base}/{self._node(post_id)}/comments?filter=stream&order=reverse_chronological&fields={COMMENT_FIELDS}&limit={COMMENTS_PAGE_SIZE}"
            )
            rows.extend(self._comment_row(c, post_id, post_url) for c in self._get_json(ctx, url).get("data") or [])
        return rows

    def _action_comments(self, args: dict, ctx: IntegrationContext) -> dict:
        """The `social.comments` capability: the Page's comments and their replies as a cursor feed."""
        limit = int(args.get("limit") or DEFAULT_LIMIT)
        if limit < 1:
            raise ValueError("'limit' must be at least 1.")
        kinds = args.get("kinds") or [KIND_COMMENT, KIND_REPLY]
        if unknown := [k for k in kinds if k not in KINDS]:
            raise _refuse(f"Unknown comment kind {unknown[0]!r}; use comment, reply or mention.")
        # Facebook tells a Page about tags and mentions only by webhook, never to a poll (verify), so there is nothing to
        # return for that kind. Asking for nothing else is an error rather than a quiet empty feed.
        wanted = set(kinds) - {KIND_MENTION}
        if not wanted:
            raise _refuse("Facebook can't list mentions by polling; ask for comment or reply.")
        since = args.get("since") or None
        if since:
            try:
                decode_cursor(str(since))
            except ValueError as e:
                raise IntegrationError(str(e), code="invalid_cursor") from e

        items = [i for row in self._recent_comments(ctx) if (i := to_item(row)) and i["kind"] in wanted]
        page, cursor = page_after(items, str(since) if since else None, limit)
        return {"items": page, "cursor": cursor}

    def _check_private_window(self, ctx: IntegrationContext, comment_id: str) -> None:
        """A private reply needs a comment under 7 days old that isn't the Page's own: refuse before sending."""
        comment = self._get_json(ctx, f"{self._base(ctx)}/{self._node(comment_id)}?fields=id,created_time,from")
        ts = _parse_ts(comment.get("created_time"))
        if ts and datetime.now(UTC) - ts > timedelta(days=PRIVATE_REPLY_DAYS):
            raise _refuse(f"Facebook allows a private reply only within {PRIVATE_REPLY_DAYS} days of the comment; this one is older.")
        if str((comment.get("from") or {}).get("id") or "") == str(self._cfg(ctx).get("page_id") or "").strip():
            raise _refuse("That comment is from your own Page; there is nobody to send a private reply to.")

    def _action_reply(self, args: dict, ctx: IntegrationContext) -> dict:
        """The `social.reply` capability: a private reply (a Messenger message to the commenter) or, with private=false, a
        public reply under the comment. The approval step before it is the gate, so a real send is the default."""
        comment_id, text = self._id_arg(args, "comment_id", "reply"), self._id_arg(args, "text", "reply")
        private, dry_run = _flag(args.get("private"), True), _flag(args.get("dry_run"), False)
        if private and len(text) > MESSENGER_TEXT_MAX:
            raise _refuse(f"A Messenger reply is limited to {MESSENGER_TEXT_MAX} characters ({len(text)} given).")
        if private:
            self._page(ctx)  # the Page id is needed for the send; fail before reading the comment
            self._check_private_window(ctx, comment_id)
        result = {"replied": False, "reply_id": None, "private": private, "url": None, "network": NETWORK}
        if dry_run:
            return {**result, "dry_run": True, "would_reply": {"comment_id": comment_id, "text": text, "private": private}}
        if private:
            data = self._send_message(ctx, {"recipient": {"comment_id": comment_id}, "message": {"text": text}})
            return {**result, "replied": True, "reply_id": str(data["message_id"]) if data.get("message_id") else None}
        data = self._post_form(ctx, f"{self._node(comment_id)}/comments", {"message": text})
        reply_id = self._require_id(data)
        return {**result, "replied": True, "reply_id": reply_id, "url": self._permalink(ctx, reply_id)}

    def _action_like(self, args: dict, ctx: IntegrationContext) -> dict:
        """The `social.like` capability: like a comment as the Page."""
        comment_id = self._id_arg(args, "comment_id", "like")
        data = self._post_form(ctx, f"{self._node(comment_id)}/likes", {})
        if data.get("success") is False:
            raise IntegrationError("Facebook did not like the comment.", code=CODE_UNKNOWN)
        return {"liked": True, "network": NETWORK}

    def _action_hide(self, args: dict, ctx: IntegrationContext) -> dict:
        """The `social.hide` capability: hide a comment under the Page's post, or show it again."""
        comment_id = self._id_arg(args, "comment_id", "hide")
        hidden = _flag(args.get("hidden"), True)
        data = self._post_form(ctx, f"{self._node(comment_id)}?is_hidden={str(hidden).lower()}", {})
        if data.get("success") is False:
            raise IntegrationError("Facebook did not change the comment's visibility.", code=CODE_UNKNOWN)
        return {"hidden": hidden, "network": NETWORK}

    def _action_delete_comment(self, args: dict, ctx: IntegrationContext) -> dict:
        """The `social.delete_comment` capability: delete a comment under the Page's post (or the Page's own reply)."""
        comment_id = self._id_arg(args, "comment_id", "delete_comment")
        data = self._json_or_raise(ctx.http.delete(f"{self._base(ctx)}/{self._node(comment_id)}", headers=self._auth(ctx)))
        if data.get("success") is False:
            raise IntegrationError("Facebook did not delete the comment.", code=CODE_UNKNOWN)
        return {"deleted": True, "network": NETWORK}

    def _action_inbox_items(self, args: dict, ctx: IntegrationContext) -> dict:
        """The `social.inbox_items` capability: a webhook delivery's inbox items (webhooks.py) — Marvin keeps them."""
        payload = args.get("payload")
        if not isinstance(payload, dict):
            raise TypeError("inbox_items needs 'payload': the webhook delivery's body (an object).")
        return {"items": inbox_items(payload), "network": NETWORK}

    def inbox_items_from_webhook(self, payload: dict, ctx: IntegrationContext) -> list[dict]:
        """Meta's webhook deliveries to the Facebook webhooks incoming webhook, as Social inbox items (webhooks.py)."""
        return inbox_items(payload)

    def _action_message(self, args: dict, ctx: IntegrationContext) -> dict:
        """The `social.message` capability: a Messenger message to a person by page-scoped id. Meta only allows it within 24
        hours of their last message to the Page; the plugin can't see that time, so a send outside it fails at Facebook
        and surfaces as ``unsupported`` (subcode 2018278) or ``permission_denied``."""
        to = args.get("to") if isinstance(args.get("to"), dict) else {}
        psid, handle = str(to.get("id") or "").strip(), str(to.get("handle") or "").strip()
        text = self._id_arg(args, "text", "message")
        if not psid and not handle:
            raise ValueError("message needs 'to.id' (or 'to.handle').")
        if not psid:
            raise _refuse("Facebook can message a person only by page-scoped id (to.id); a name or handle can't be resolved.")
        if len(text) > MESSENGER_TEXT_MAX:
            raise _refuse(f"A Messenger message is limited to {MESSENGER_TEXT_MAX} characters ({len(text)} given).")
        body = {"recipient": {"id": psid}, "messaging_type": "RESPONSE", "message": {"text": text}}
        if _flag(args.get("dry_run"), False):
            return {"sent": False, "dry_run": True, "message_id": None, "network": NETWORK, "would_send": {"to": {"id": psid}, "text": text}}
        data = self._send_message(ctx, body)
        return {"sent": True, "message_id": str(data["message_id"]) if data.get("message_id") else None, "network": NETWORK}

    @staticmethod
    def _require_id(data: dict) -> str:
        if not data.get("id"):
            raise IntegrationError("Facebook accepted the request but returned no id.", code=CODE_UNKNOWN)
        return str(data["id"])

    def _permalink(self, ctx: IntegrationContext, post_id: str) -> str | None:
        try:
            return _absolute(self._get_json(ctx, f"{self._base(ctx)}/{quote(post_id, safe='')}?fields=permalink_url").get("permalink_url"))
        except Exception as e:  # noqa: BLE001 — the post is out; a missing link is not a failure
            ctx.logger.info("facebook: permalink not readable for %s: %s", post_id, e)
            return None


def _absolute(permalink: str | None) -> str | None:
    """Graph has returned Page permalinks without the host (``/123/posts/456``); make them absolute."""
    if not permalink:
        return None
    return f"https://www.facebook.com{permalink}" if permalink.startswith("/") else permalink
