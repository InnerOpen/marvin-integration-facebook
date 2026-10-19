"""Facebook Page provider: post text, a link or photos to a Page through the Graph API.

Uses ``graph.facebook.com`` with a **Page access token** (sent as a Bearer header, never in a URL). The provider is pure
with respect to Marvin: it gets ``config``, the resolved ``secret`` (the token), a ``logger`` and a safe ``http`` client via
``ctx``, and returns dicts.

The ``post`` action is Marvin's shared ``social.post`` capability:

- no images: a feed post (``POST /{page}/feed``) with ``message`` and, if given, ``link``;
- one image: a photo post (``POST /{page}/photos``) with the text as ``caption``;
- several images: each uploaded unpublished (``published=false``), then one feed post whose ``attached_media`` lists them.
"""

from __future__ import annotations

import json
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
    register_provider,
)

NETWORK = "facebook"
DEFAULT_API_VERSION = "v22.0"
MAX_IMAGES = 10  # the social.post contract's cap; Facebook itself allows more photos in one post
ALT_MAX = 1000
DEFAULT_RECENT = 10
MAX_RECENT = 100
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


def _graph_error(resp: Response) -> IntegrationError:
    """A readable IntegrationError from a failed Graph response. Graph answers ``{"error": {"message", "code", ...}}``."""
    message, code = resp.text[:ERROR_TEXT_LIMIT], None
    try:
        err = (resp.json() or {}).get("error") or {}
        message, code = str(err.get("message") or message), err.get("code")
    except (ValueError, AttributeError):
        pass
    kind = CODE_UNKNOWN
    if isinstance(code, int):
        if code in _RATE_LIMIT_CODES:
            kind = CODE_RATE_LIMIT
        elif 200 <= code <= 299:  # 200–299: permissions
            kind = CODE_PERMISSION
        else:
            kind = _GRAPH_CODES.get(code, CODE_UNKNOWN)
    suffix = f" (code {code})" if code is not None else ""
    return IntegrationError(f"Facebook API error: HTTP {resp.status_code}: {message}{suffix}", code=kind)


def _refuse(message: str) -> IntegrationError:
    return IntegrationError(message, code=CODE_UNSUPPORTED)


def _with_link(text: str, link: str) -> str:
    """The text with the link on its own line (Facebook turns a URL in a message or caption into a clickable link),
    unless the text already has it."""
    return f"{text}\n\n{link}" if link and link not in text else text


@register_provider
class FacebookProvider(IntegrationProvider):
    slug = "facebook"
    name = "Facebook Page"
    description = "Post text, links and photos to a Facebook Page, after approval."
    category = CATEGORY_DESTINATION
    icon = "📘"
    # No packaged logo on purpose: Facebook's mark is Meta's to supply (see the README, "Logo"). The emoji is the fallback.
    guide = "guide.md"

    credentials = (
        CredentialField(
            key="page_access_token",
            label="Page access token",
            help="A long-lived Page access token (it does not expire when made from a long-lived user token) with pages_manage_posts.",
        ),
    )
    config_schema: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "page_id": {"type": "string", "title": "Page ID", "description": "The numeric id of the Facebook Page the token belongs to."},
            "api_version": {"type": "string", "title": "Graph API version", "default": DEFAULT_API_VERSION},
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

    def _post_form(self, ctx: IntegrationContext, path: str, form: dict) -> dict:
        headers = {**self._auth(ctx), "Content-Type": "application/x-www-form-urlencoded"}
        resp = ctx.http.post(f"{self._base(ctx)}/{path}", data=urlencode(form).encode(), headers=headers)
        if not resp.ok:
            raise _graph_error(resp)
        return resp.json()

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
        handler = {"post": self._action_post, "list_recent_posts": self._action_list_recent_posts}.get(key)
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
