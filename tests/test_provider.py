"""The Facebook Page provider through a fake http helper: the three post shapes, link, dry run, refusals, errors, health."""

import json
import logging
from urllib.parse import parse_qs

import pytest
from marvin_integration_sdk import IntegrationContext, IntegrationError, Response, load_guide

from marvin_integration_facebook import FacebookProvider

_LOG = logging.getLogger("test")
BASE = "https://graph.facebook.com/v22.0"
PAGE = "1000000000001"
PIC = "https://assets.example.test/a.jpg"
PIC2 = "https://assets.example.test/b.jpg"
PIC3 = "https://assets.example.test/c.jpg"
POST_ID = f"{PAGE}_555"
PERMALINK = "https://www.facebook.com/example/posts/555"


class _FakeHttp:
    """POSTs are recorded (form decoded) and answered by endpoint; GETs are answered by URL substring."""

    def __init__(self, gets=None, post_status=200, post_body=None):
        self.gets = {f"/{POST_ID}?fields=permalink_url": {"permalink_url": PERMALINK}, **(gets or {})}
        self.get_log: list[dict] = []
        self.posts: list[dict] = []
        self.post_status = post_status
        self.post_body = post_body
        self._photos = 0

    def get(self, url, *, headers=None, timeout=15):
        self.get_log.append({"url": url, "headers": headers})
        for needle, payload in self.gets.items():
            if needle in url:
                status, body = payload if isinstance(payload, tuple) else (200, payload)
                return Response(status_code=status, content=json.dumps(body).encode())
        return Response(status_code=404, content=b'{"error":{"message":"no route","code":100}}')

    def post(self, url, *, json=None, data=None, headers=None, timeout=15):
        form = {k: v[0] for k, v in parse_qs((data or b"").decode()).items()}
        self.posts.append({"url": url, "form": form, "headers": headers})
        if self.post_body is not None or self.post_status != 200:
            return Response(status_code=self.post_status, content=_dump(self.post_body))
        if url.endswith("/feed"):
            return Response(status_code=200, content=_dump({"id": POST_ID}))
        self._photos += 1
        body = {"id": f"p{self._photos}"}
        if form.get("published") == "true":
            body["post_id"] = POST_ID
        return Response(status_code=200, content=_dump(body))


def _dump(body):
    return json.dumps(body).encode()


def _ctx(http=None, token="fake-token", **config):
    return IntegrationContext(config={"page_id": PAGE, **config}, secret=token, http=http or _FakeHttp(), logger=_LOG)


def _post(args, http=None, **kw):
    return FacebookProvider().run_action("post", args, _ctx(http, **kw))


# ── declaration ──────────────────────────────────────────────────────────────


def test_post_declares_the_social_capability_approval_gated():
    action = next(a for a in FacebookProvider.actions if a.key == "post")
    assert action.capability == "social.post" and action.requires_approval
    assert set(action.input_schema["properties"]) == {"text", "images", "alt", "link", "dry_run"}
    assert action.input_schema["required"] == ["text"]
    assert {"posted", "post_id", "url", "network", "would_post"} <= set(action.output_schema["properties"])
    listing = next(a for a in FacebookProvider.actions if a.key == "list_recent_posts")
    assert listing.capability is None and not listing.requires_approval


def test_the_config_and_credential_match_the_instagram_pattern():
    schema = FacebookProvider.config_schema
    assert schema["required"] == ["page_id"] and schema["properties"]["api_version"]["default"] == "v22.0"
    assert [c.key for c in FacebookProvider.credentials] == ["page_access_token"]


def test_the_guide_ships_with_the_package():
    guide = load_guide(FacebookProvider)
    assert guide.startswith("# Facebook Page") and "social.post" in guide and "attached" not in guide.split("## Actions")[0]


# ── posting ──────────────────────────────────────────────────────────────────


def test_text_only_is_a_feed_post_with_the_message():
    http = _FakeHttp()
    out = _post({"text": "Hello world"}, http)
    assert [(p["url"], p["form"]) for p in http.posts] == [(f"{BASE}/{PAGE}/feed", {"message": "Hello world"})]
    assert out == {"posted": True, "post_id": POST_ID, "url": PERMALINK, "network": "facebook", "kind": "text"}


def test_the_token_is_sent_as_a_bearer_header_and_never_in_a_url():
    http = _FakeHttp()
    _post({"text": "Hi"}, http, token="fake-token")
    assert http.posts[0]["headers"]["Authorization"] == "Bearer fake-token"
    assert http.get_log[0]["headers"]["Authorization"] == "Bearer fake-token"
    assert "fake-token" not in http.posts[0]["url"] + http.get_log[0]["url"] and "access_token" not in http.posts[0]["form"]


def test_a_link_is_attached_to_a_text_post():
    http = _FakeHttp()
    _post({"text": "New piece", "link": "https://example.test/p"}, http)
    assert http.posts[0]["form"] == {"message": "New piece", "link": "https://example.test/p"}


def test_one_image_is_a_published_photo_with_the_text_as_caption():
    http = _FakeHttp()
    out = _post({"text": "Studio day", "images": [PIC], "alt": ["A cat on a desk"]}, http)
    assert [(p["url"], p["form"]) for p in http.posts] == [
        (f"{BASE}/{PAGE}/photos", {"url": PIC, "caption": "Studio day", "published": "true", "alt_text_custom": "A cat on a desk"})
    ]
    assert out["post_id"] == POST_ID and out["url"] == PERMALINK and out["kind"] == "photo" and out["images"] == 1
    assert any(f"/{POST_ID}?fields=permalink_url" in g["url"] for g in http.get_log)  # the permalink of the feed post, not the photo


def test_a_photo_post_cannot_carry_a_link_preview_so_the_link_joins_the_caption():
    http = _FakeHttp()
    _post({"text": "Studio day", "images": [PIC], "link": "https://example.test/p"}, http)
    assert http.posts[0]["form"]["caption"] == "Studio day\n\nhttps://example.test/p" and "link" not in http.posts[0]["form"]


def test_several_images_are_unpublished_photos_attached_to_one_feed_post():
    http = _FakeHttp()
    out = _post({"text": "Three", "images": [PIC, PIC2, PIC3], "alt": ["one", "", "three"], "link": "https://example.test/p"}, http)
    forms = [p["form"] for p in http.posts]
    assert [p["url"].rsplit("/", 1)[1] for p in http.posts] == ["photos", "photos", "photos", "feed"]
    assert forms[0] == {"url": PIC, "published": "false", "alt_text_custom": "one"}
    assert forms[1] == {"url": PIC2, "published": "false"}
    assert forms[2] == {"url": PIC3, "published": "false", "alt_text_custom": "three"}
    assert forms[3] == {
        "message": "Three\n\nhttps://example.test/p",
        "attached_media[0]": '{"media_fbid": "p1"}',
        "attached_media[1]": '{"media_fbid": "p2"}',
        "attached_media[2]": '{"media_fbid": "p3"}',
    }
    assert out["kind"] == "photos" and out["images"] == 3 and out["post_id"] == POST_ID and out["url"] == PERMALINK


def test_a_failure_while_uploading_stops_before_the_feed_post():
    class _Http(_FakeHttp):
        def post(self, url, *, json=None, data=None, headers=None, timeout=15):
            if len([p for p in self.posts if p["url"].endswith("/photos")]) == 1:
                self.posts.append({"url": url, "form": {}, "headers": headers})
                return Response(status_code=400, content=_dump({"error": {"message": "Invalid image", "code": 100}}))
            return super().post(url, json=json, data=data, headers=headers, timeout=timeout)

    http = _Http()
    with pytest.raises(IntegrationError, match="Invalid image") as exc:
        _post({"text": "x", "images": [PIC, PIC2]}, http)
    assert exc.value.code == "invalid_request"
    assert not any(p["url"].endswith("/feed") for p in http.posts)


def test_a_missing_permalink_does_not_fail_a_post_that_is_out():
    out = _post({"text": "Hi"}, _FakeHttp(gets={f"/{POST_ID}?fields=permalink_url": (400, {"error": {"message": "nope", "code": 100}})}))
    assert out["posted"] is True and out["url"] is None and out["post_id"] == POST_ID


def test_a_host_less_permalink_is_made_absolute():
    out = _post({"text": "Hi"}, _FakeHttp(gets={f"/{POST_ID}?fields=permalink_url": {"permalink_url": "/example/posts/555"}}))
    assert out["url"] == "https://www.facebook.com/example/posts/555"


def test_the_api_version_comes_from_the_config():
    http = _FakeHttp(gets={"/v21.0/": {}})
    _post({"text": "Hi", "dry_run": False}, http, api_version="v21.0")
    assert http.posts[0]["url"] == f"https://graph.facebook.com/v21.0/{PAGE}/feed"


# ── dry run ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("args", "would_post"),
    [
        ({"text": "Hi"}, {"kind": "text", "endpoint": "feed", "message": "Hi"}),
        ({"text": "Hi", "link": "https://example.test/p"}, {"kind": "text", "endpoint": "feed", "message": "Hi", "link": "https://example.test/p"}),
        ({"text": "Hi", "images": [PIC], "alt": ["Cat"]}, {"kind": "photo", "endpoint": "photos", "caption": "Hi", "images": [PIC], "alt": ["Cat"]}),
        ({"text": "Hi", "images": [PIC, PIC2]}, {"kind": "photos", "endpoint": "feed", "message": "Hi", "images": [PIC, PIC2]}),
    ],
)
def test_dry_run_reports_would_post_and_calls_nothing(args, would_post):
    http = _FakeHttp()
    out = _post({**args, "dry_run": True}, http)
    assert out == {"posted": False, "dry_run": True, "post_id": None, "url": None, "network": "facebook", "would_post": would_post}
    assert http.posts == [] and http.get_log == []


# ── refusals: before any call ────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ({}, "needs text"),
        ({"text": "  "}, "needs text"),
        ({"text": "x", "images": [f"https://x.test/{i}.jpg" for i in range(11)]}, "at most 10"),
        ({"text": "x", "images": ["http://plain.test/a.jpg"]}, "public https"),
        ({"text": "x", "images": "https://x.test/a.jpg"}, "list of public image URLs"),
        ({"text": "x", "images": [PIC], "alt": ["a", "b"]}, "one text per image"),
        ({"text": "x", "images": [PIC], "alt": ["a" * 1001]}, "Alt text is limited"),
        ({"text": "x", "link": "ftp://x.test"}, "must be a URL"),
    ],
)
def test_unsupported_requests_are_refused_with_a_clear_error(args, message):
    http = _FakeHttp()
    with pytest.raises(IntegrationError, match=message) as exc:
        _post(args, http)
    assert exc.value.code == "unsupported" and isinstance(exc.value, ValueError)
    assert http.posts == [] and http.get_log == []


def test_a_dry_run_refuses_what_a_real_post_would_refuse():
    with pytest.raises(IntegrationError, match="public https"):
        _post({"text": "x", "images": ["http://plain.test/a.jpg"], "dry_run": True})


def test_no_token_and_no_page_are_refused():
    with pytest.raises(ValueError, match="No Facebook Page access token"):
        FacebookProvider().run_action("post", {"text": "x"}, _ctx(token=None))
    with pytest.raises(ValueError, match="Missing Facebook Page ID"):
        FacebookProvider().run_action("post", {"text": "x"}, IntegrationContext(config={}, secret="t", http=_FakeHttp(), logger=_LOG))


def test_an_unknown_action_is_not_implemented():
    with pytest.raises(NotImplementedError):
        FacebookProvider().run_action("nope", {}, _ctx())


# ── Graph errors ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("status", "graph_code", "code"),
    [(400, 190, "auth_expired"), (403, 200, "permission_denied"), (403, 10, "permission_denied"), (400, 368, "blocked"), (400, 613, "rate_limited"),
     (400, 100, "invalid_request"), (500, 2, "unknown")],
)  # fmt: skip
def test_a_graph_error_becomes_an_integration_error_with_a_code(status, graph_code, code):
    http = _FakeHttp(post_status=status, post_body={"error": {"message": "Boom happened", "type": "OAuthException", "code": graph_code}})
    with pytest.raises(IntegrationError) as exc:
        _post({"text": "Hi"}, http)
    assert exc.value.code == code
    assert f"HTTP {status}: Boom happened (code {graph_code})" in str(exc.value)
    assert "fake-token" not in str(exc.value)


def test_a_non_json_error_body_is_still_readable():
    http = _FakeHttp(post_status=502, post_body=None)
    http.post = lambda url, **kw: Response(status_code=502, content=b"<html>Bad gateway</html>")
    with pytest.raises(IntegrationError, match="HTTP 502: <html>Bad gateway") as exc:
        _post({"text": "Hi"}, http)
    assert exc.value.code == "unknown"


def test_a_response_without_an_id_is_an_error():
    with pytest.raises(IntegrationError, match="returned no id"):
        _post({"text": "Hi"}, _FakeHttp(post_body={"ok": True}))


# ── list_recent_posts ────────────────────────────────────────────────────────

RECENT = {
    "data": [
        {"id": POST_ID, "message": "Hello", "created_time": "2026-10-19T12:00:00+0000", "permalink_url": "/example/posts/555"},
        {"id": f"{PAGE}_556", "created_time": "2026-10-18T12:00:00+0000", "permalink_url": "https://www.facebook.com/example/posts/556"},
    ]
}


def test_list_recent_posts_reads_the_page_feed():
    http = _FakeHttp(gets={f"/{PAGE}/posts": RECENT})
    out = FacebookProvider().run_action("list_recent_posts", {"limit": 2}, _ctx(http))
    assert http.get_log[0]["url"] == f"{BASE}/{PAGE}/posts?fields=id,message,created_time,permalink_url&limit=2"
    assert out["posts"][0] == {"post_id": POST_ID, "message": "Hello", "created_time": "2026-10-19T12:00:00+0000", "url": PERMALINK}
    assert out["posts"][1]["message"] == "" and out["posts"][1]["url"].endswith("/posts/556")


@pytest.mark.parametrize(("limit", "expected"), [(None, 10), (500, 100), (0, 10), ("x", 10)])
def test_list_recent_posts_clamps_the_limit(limit, expected):
    http = _FakeHttp(gets={f"/{PAGE}/posts": {"data": []}})
    out = FacebookProvider().run_action("list_recent_posts", {"limit": limit}, _ctx(http))
    assert http.get_log[0]["url"].endswith(f"limit={expected}") and out == {"posts": []}


def test_list_recent_posts_raises_the_graph_error():
    http = _FakeHttp(gets={f"/{PAGE}/posts": (403, {"error": {"message": "Missing permission", "code": 200}})})
    with pytest.raises(IntegrationError, match="Missing permission") as exc:
        FacebookProvider().run_action("list_recent_posts", {}, _ctx(http))
    assert exc.value.code == "permission_denied"


# ── health ───────────────────────────────────────────────────────────────────


def test_check_ok_reads_the_page_with_the_token():
    http = _FakeHttp(gets={f"/{PAGE}?fields=id,name": {"id": PAGE, "name": "Example"}})
    assert FacebookProvider().check(_ctx(http)) == ("ok", None)
    assert http.get_log[0]["url"] == f"{BASE}/{PAGE}?fields=id,name" and http.get_log[0]["headers"]["Authorization"] == "Bearer fake-token"


def test_check_reports_a_refused_token():
    http = _FakeHttp(gets={f"/{PAGE}?fields=id,name": (400, {"error": {"message": "Error validating access token", "code": 190}})})
    status, message = FacebookProvider().check(_ctx(http))
    assert status == "error" and "HTTP 400" in message and "Error validating access token" in message and "fake-token" not in message


def test_check_reports_a_token_for_another_page():
    http = _FakeHttp(gets={f"/{PAGE}?fields=id,name": {"id": "999", "name": "Other"}})
    status, message = FacebookProvider().check(_ctx(http))
    assert status == "error" and "999" in message


def test_check_reports_an_unreachable_graph():
    class _Down(_FakeHttp):
        def get(self, url, *, headers=None, timeout=15):
            raise ConnectionError("graph unreachable")

    assert FacebookProvider().check(_ctx(_Down())) == ("error", "graph unreachable")


def test_check_unconfigured_without_token_or_page():
    assert FacebookProvider().check(_ctx(token=None))[0] == "unconfigured"
    assert FacebookProvider().check(IntegrationContext(config={}, secret="t", http=_FakeHttp(), logger=_LOG))[0] == "unconfigured"
