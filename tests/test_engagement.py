"""The shared engagement capabilities (social.comments / reply / like / hide / delete_comment / message), through a fake
http. Nothing here talks to Meta."""

import json
import logging
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs

import pytest
from marvin_integration_sdk import IntegrationContext, IntegrationError, Response

from marvin_integration_facebook import FacebookProvider
from marvin_integration_facebook.engagement import decode_cursor, encode_cursor, page_after

_LOG = logging.getLogger("test")
BASE = "https://graph.facebook.com/v22.0"
PAGE = "1000000000001"
NOW = datetime.now(UTC).replace(microsecond=0)


def _ts(days_ago=0, minutes_ago=0):
    return (NOW - timedelta(days=days_ago, minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%S%z")


def _iso(days_ago=0, minutes_ago=0):
    return (NOW - timedelta(days=days_ago, minutes=minutes_ago)).isoformat()


POSTS = {
    "data": [
        {"id": f"{PAGE}_1", "created_time": _ts(), "permalink_url": "/example/posts/1"},
        {"id": f"{PAGE}_2", "created_time": _ts(1), "permalink_url": "https://www.facebook.com/example/posts/2"},
    ]
}
COMMENTS = {
    "1": {
        "data": [
            # newest first, as `order=reverse_chronological` returns them; the reply carries its parent
            {
                "id": "1_r1",
                "message": "thanks!",
                "created_time": _ts(minutes_ago=10),
                "from": {"id": PAGE, "name": "Example Page"},
                "parent": {"id": "1_c1"},
                "permalink_url": "https://www.facebook.com/example/posts/1?comment_id=r1",
            },
            {
                "id": "1_c1",
                "message": "What SIZE is this?",
                "created_time": _ts(minutes_ago=30),
                "from": {"id": "u1", "name": "Ada Fan"},
                "permalink_url": "https://www.facebook.com/example/posts/1?comment_id=c1",
            },
        ]
    },
    "2": {"data": [{"id": "2_c2", "message": "love it", "created_time": _ts(minutes_ago=20)}]},  # `from` withheld
}
ROUTES = {f"/{PAGE}/posts": POSTS, f"/{PAGE}_1/comments": COMMENTS["1"], f"/{PAGE}_2/comments": COMMENTS["2"]}


class _Http:
    """GETs are answered by URL substring (first match wins); POSTs and DELETEs are recorded and answered by `status`."""

    def __init__(self, routes=None, status=200, post_body=None):
        self.routes = routes if routes is not None else dict(ROUTES)
        self.status = status
        self.post_body = post_body if post_body is not None else {"id": "new1", "success": True}
        self.gets: list[str] = []
        self.posts: list[dict] = []
        self.deletes: list[dict] = []

    def get(self, url, *, headers=None, timeout=15):
        self.gets.append(url)
        for needle, payload in self.routes.items():
            if needle in url:
                status, body = payload if isinstance(payload, tuple) else (200, payload)
                return Response(status_code=status, content=json.dumps(body).encode())
        return Response(status_code=404, content=b'{"error":{"message":"no route","code":100}}')

    def post(self, url, *, json=None, data=None, headers=None, timeout=15):
        form = {k: v[0] for k, v in parse_qs((data or b"").decode()).items()}
        self.posts.append({"url": url, "json": json, "form": form, "headers": headers})
        return Response(status_code=self.status, content=_dump(self.post_body))

    def delete(self, url, *, headers=None, timeout=15):
        self.deletes.append({"url": url, "headers": headers})
        return Response(status_code=self.status, content=_dump(self.post_body))


def _dump(body):
    return json.dumps(body).encode()


def _ctx(http=None, token="fake-token", **config):
    return IntegrationContext(config={"page_id": PAGE, **config}, secret=token, http=http or _Http(), logger=_LOG)


def _run(key, args=None, *, http=None, **config):
    return FacebookProvider().run_action(key, args or {}, _ctx(http, **config))


# ── declaration ──────────────────────────────────────────────────────────────

# action key -> (capability, gated). `post` and `list_recent_posts` stay as they were.
DECLARED = {
    "post": ("social.post", True),
    "list_recent_posts": (None, False),
    "comments": ("social.comments", False),
    "reply": ("social.reply", True),
    "like": ("social.like", False),
    "hide": ("social.hide", False),
    "delete_comment": ("social.delete_comment", True),
    "message": ("social.message", True),
}


def test_the_capabilities_are_declared_with_the_right_gates_and_there_is_no_repost():
    assert {a.key: (a.capability, a.requires_approval) for a in FacebookProvider.actions} == DECLARED
    assert "social.repost" not in {a.capability for a in FacebookProvider.actions}


def test_the_schemas_match_the_shared_contract():
    by_key = {a.key: a for a in FacebookProvider.actions}
    assert set(by_key["comments"].input_schema["properties"]) == {"since", "limit", "kinds"}
    assert set(by_key["reply"].input_schema["properties"]) == {"comment_id", "text", "private", "dry_run"}
    assert by_key["reply"].input_schema["properties"]["private"]["default"] is True
    assert by_key["reply"].input_schema["required"] == ["comment_id", "text"]
    assert by_key["hide"].input_schema["properties"]["hidden"]["default"] is True
    assert by_key["message"].input_schema["required"] == ["to", "text"]
    assert {"replied", "reply_id", "private", "url", "network", "would_reply"} <= set(by_key["reply"].output_schema["properties"])
    assert {"sent", "message_id", "network"} <= set(by_key["message"].output_schema["properties"])


# ── social.comments ──────────────────────────────────────────────────────────


def test_comments_returns_normalised_items_oldest_first_with_replies():
    out = _run("comments", http=_Http())
    assert [i["id"] for i in out["items"]] == ["1_c1", "2_c2", "1_r1"]
    c1, c2, r1 = out["items"]
    assert c1 == {
        "id": "1_c1",
        "kind": "comment",
        "post_id": f"{PAGE}_1",
        "post_url": "https://www.facebook.com/example/posts/1",  # Graph's host-less permalink, made absolute
        "author": {"id": "u1", "handle": "Ada Fan", "name": "Ada Fan"},
        "text": "What SIZE is this?",
        "created_at": _iso(minutes_ago=30),
        "url": "https://www.facebook.com/example/posts/1?comment_id=c1",
        "network": "facebook",
    }
    assert r1["kind"] == "reply" and r1["post_id"] == f"{PAGE}_1" and r1["author"]["id"] == PAGE
    assert c2["kind"] == "comment" and c2["post_url"] == "https://www.facebook.com/example/posts/2"
    assert c2["author"] == {"id": "", "handle": "", "name": ""} and c2["url"] is None  # Facebook withheld `from`
    assert out["cursor"] and decode_cursor(out["cursor"])[0] == r1["created_at"]


def test_comments_reads_the_posts_then_each_posts_stream_with_the_bearer_token():
    http = _Http()
    _run("comments", http=http)
    assert http.gets[0] == f"{BASE}/{PAGE}/posts?fields=id,created_time,permalink_url&limit=10"
    assert http.gets[1].startswith(f"{BASE}/{PAGE}_1/comments?filter=stream&order=reverse_chronological&fields=id,message,created_time,from")
    assert "parent{id}" in http.gets[1] and http.gets[1].endswith("limit=50") and f"{PAGE}_2/comments" in http.gets[2]
    assert "fake-token" not in "".join(http.gets)


def test_comments_scans_the_configured_number_of_posts():
    http = _Http()
    _run("comments", http=http, lookback_posts="3")
    assert http.gets[0].endswith("limit=3")
    http = _Http()
    _run("comments", http=http, lookback_posts="junk")
    assert http.gets[0].endswith("limit=10")


def test_comments_since_the_cursor_returns_only_what_is_new():
    first = _run("comments", http=_Http())
    again = _run("comments", {"since": first["cursor"]}, http=_Http())
    assert again["items"] == [] and again["cursor"] == first["cursor"]
    # A newer comment appears; only it comes back.
    newer = {"id": "1_c9", "message": "one more", "created_time": _ts(minutes_ago=1), "from": {"id": "u9", "name": "Bo"}}
    routes = {**ROUTES, f"/{PAGE}_1/comments": {"data": [newer, *COMMENTS["1"]["data"]]}}
    out = _run("comments", {"since": first["cursor"]}, http=_Http(routes))
    assert [i["id"] for i in out["items"]] == ["1_c9"]


def test_comments_limit_drains_a_backlog_oldest_first_without_gaps():
    full, since = [], encode_cursor(None, [])
    for _ in range(4):
        out = _run("comments", {"since": since, "limit": 1}, http=_Http())
        full += [i["id"] for i in out["items"]]
        since = out["cursor"]
    assert full == ["1_c1", "2_c2", "1_r1"]


def test_comments_first_poll_with_a_limit_takes_the_newest():
    out = _run("comments", {"limit": 2}, http=_Http())
    assert [i["id"] for i in out["items"]] == ["2_c2", "1_r1"]


def test_comments_kinds_filter_and_mentions():
    assert [i["kind"] for i in _run("comments", {"kinds": ["reply"]}, http=_Http())["items"]] == ["reply"]
    assert {i["kind"] for i in _run("comments", {"kinds": ["comment"]}, http=_Http())["items"]} == {"comment"}
    both = _run("comments", {"kinds": ["comment", "mention"]}, http=_Http())  # mentions are skipped, the rest returned
    assert {i["kind"] for i in both["items"]} == {"comment"}
    assert {i["kind"] for i in _run("comments", {}, http=_Http())["items"]} == {"comment", "reply"}  # the default
    http = _Http()
    with pytest.raises(IntegrationError, match="mentions") as exc:
        _run("comments", {"kinds": ["mention"]}, http=http)
    assert exc.value.code == "unsupported" and http.gets == []
    with pytest.raises(IntegrationError) as exc:
        _run("comments", {"kinds": ["like"]}, http=http)
    assert exc.value.code == "unsupported"


def test_comments_a_bad_cursor_or_limit_is_refused_before_any_call():
    http = _Http()
    with pytest.raises(IntegrationError) as exc:
        _run("comments", {"since": "not-a-cursor"}, http=http)
    assert exc.value.code == "invalid_cursor"
    with pytest.raises(ValueError, match="limit"):
        _run("comments", {"limit": -1}, http=http)
    assert http.gets == []


def test_comments_graph_failure_raises_with_a_code():
    http = _Http({f"/{PAGE}/posts": (403, {"error": {"message": "Missing permission", "code": 200}})})
    with pytest.raises(IntegrationError, match="Missing permission") as exc:
        _run("comments", http=http)
    assert exc.value.code == "permission_denied"
    routes = {**ROUTES, f"/{PAGE}_1/comments": (400, {"error": {"message": "Expired", "code": 190}})}
    with pytest.raises(IntegrationError) as exc:
        _run("comments", http=_Http(routes))
    assert exc.value.code == "auth_expired"


def test_comments_skips_a_row_without_a_usable_timestamp():
    routes = {**ROUTES, f"/{PAGE}_2/comments": {"data": [{"id": "2_x", "message": "no time"}, {"id": "2_y", "created_time": "garbage"}]}}
    out = _run("comments", http=_Http(routes))
    assert [i["id"] for i in out["items"]] == ["1_c1", "1_r1"]


def test_the_cursor_keeps_comments_in_the_same_second_as_the_last_one():
    def item(i, t):
        return {"id": i, "created_at": t}

    a, b, c = item("a", "2026-10-19T12:00:00+00:00"), item("b", "2026-10-19T12:00:00+00:00"), item("c", "2026-10-19T12:00:00+00:00")
    page, cursor = page_after([a, b], None, 10)
    assert [i["id"] for i in page] == ["a", "b"]
    page, cursor2 = page_after([a, b, c], cursor, 10)  # c lands in the same second after the poll
    assert [i["id"] for i in page] == ["c"] and decode_cursor(cursor2)[1] == {"a", "b", "c"}
    assert page_after([a, b, c], cursor2, 10)[0] == []


# ── social.reply ─────────────────────────────────────────────────────────────


def _comment_node(age_days=1, author="u1"):
    return {"/c1?fields=id,created_time,from": {"id": "c1", "created_time": _ts(age_days), "from": {"id": author, "name": "Ada"}}}


def _reply_http(age_days=1, **kw):
    return _Http(routes=_comment_node(age_days), **kw)


def test_reply_is_private_by_default_and_is_a_messenger_private_reply():
    http = _reply_http(post_body={"recipient_id": "psid1", "message_id": "mid.1"})
    out = _run("reply", {"comment_id": "c1", "text": "Sizes S-XL"}, http=http)
    assert out == {"replied": True, "reply_id": "mid.1", "private": True, "url": None, "network": "facebook"}
    (post,) = http.posts
    assert post["url"] == f"{BASE}/{PAGE}/messages"
    assert post["json"] == {"recipient": {"comment_id": "c1"}, "message": {"text": "Sizes S-XL"}}
    assert post["headers"] == {"Authorization": "Bearer fake-token"}


def test_reply_public_posts_to_the_comments_edge_and_returns_the_permalink():
    routes = {"/new1?fields=permalink_url": {"permalink_url": "/example/posts/1?comment_id=new1"}}
    http = _Http(routes=routes)
    out = _run("reply", {"comment_id": "c1", "text": "Thank you!", "private": False}, http=http)
    assert out == {
        "replied": True,
        "reply_id": "new1",
        "private": False,
        "url": "https://www.facebook.com/example/posts/1?comment_id=new1",
        "network": "facebook",
    }
    (post,) = http.posts
    assert post["url"] == f"{BASE}/c1/comments" and post["form"] == {"message": "Thank you!"} and post["json"] is None
    assert post["headers"]["Content-Type"] == "application/x-www-form-urlencoded"


def test_reply_public_succeeds_when_the_permalink_cannot_be_read():
    out = _run("reply", {"comment_id": "c1", "text": "Thanks", "private": False}, http=_Http(routes={}))
    assert out["replied"] is True and out["url"] is None


def test_reply_private_dry_run_checks_the_comment_and_sends_nothing():
    http = _reply_http()
    out = _run("reply", {"comment_id": "c1", "text": "Hi", "dry_run": True}, http=http)
    assert out == {
        "replied": False,
        "dry_run": True,
        "reply_id": None,
        "private": True,
        "url": None,
        "network": "facebook",
        "would_reply": {"comment_id": "c1", "text": "Hi", "private": True},
    }
    assert http.posts == [] and len(http.gets) == 1


def test_reply_public_dry_run_sends_and_reads_nothing():
    http = _Http()
    out = _run("reply", {"comment_id": "c1", "text": "Hi", "private": False, "dry_run": True}, http=http)
    assert out["replied"] is False and out["would_reply"]["private"] is False
    assert http.posts == [] and http.gets == []


def test_reply_private_refuses_a_comment_past_the_seven_day_window_before_sending():
    http = _reply_http(age_days=8)
    with pytest.raises(IntegrationError, match="7 days") as exc:
        _run("reply", {"comment_id": "c1", "text": "Hi"}, http=http)
    assert exc.value.code == "unsupported" and http.posts == []
    with pytest.raises(IntegrationError, match="7 days"):  # a dry run says so too
        _run("reply", {"comment_id": "c1", "text": "Hi", "dry_run": True}, http=_reply_http(age_days=8))


def test_reply_private_at_six_days_still_goes_out_and_public_ignores_the_window():
    http = _reply_http(age_days=6, post_body={"message_id": "mid.2"})
    assert _run("reply", {"comment_id": "c1", "text": "Hi"}, http=http)["replied"] is True
    http = _reply_http(age_days=30)
    assert _run("reply", {"comment_id": "c1", "text": "Hi", "private": False}, http=http)["replied"] is True
    assert not any("created_time" in g for g in http.gets)  # no comment read for a public reply


def test_reply_private_refuses_the_pages_own_comment():
    http = _Http(routes=_comment_node(author=PAGE))
    with pytest.raises(IntegrationError, match="own Page") as exc:
        _run("reply", {"comment_id": "c1", "text": "Hi"}, http=http)
    assert exc.value.code == "unsupported" and http.posts == []


def test_reply_private_goes_out_when_the_comment_has_no_time_or_author():
    http = _Http(routes={"/c1?fields=": {"id": "c1"}}, post_body={"message_id": "mid.3"})
    assert _run("reply", {"comment_id": "c1", "text": "Hi"}, http=http)["reply_id"] == "mid.3"


def test_reply_flags_given_as_text_are_read_as_text():
    http = _Http()
    out = _run("reply", {"comment_id": "c1", "text": "Hi", "private": "false", "dry_run": "true"}, http=http)
    assert out["private"] is False and out["replied"] is False and http.posts == []


def test_reply_needs_comment_id_and_text_and_a_short_enough_message():
    http = _reply_http()
    for args in ({}, {"comment_id": "c1"}, {"text": "x"}, {"comment_id": " ", "text": "x"}):
        with pytest.raises(ValueError, match="reply needs"):
            _run("reply", args, http=http)
    with pytest.raises(IntegrationError, match="2000") as exc:
        _run("reply", {"comment_id": "c1", "text": "x" * 2001}, http=http)
    assert exc.value.code == "unsupported" and http.gets == [] and http.posts == []
    assert _run("reply", {"comment_id": "c1", "text": "x" * 2001, "private": False, "dry_run": True}, http=http)["replied"] is False


def test_reply_private_needs_the_page_id():
    ctx = IntegrationContext(config={}, secret="t", http=_reply_http(), logger=_LOG)
    with pytest.raises(ValueError, match="Page ID"):
        FacebookProvider().run_action("reply", {"comment_id": "c1", "text": "Hi"}, ctx)


@pytest.mark.parametrize("private", [True, False])
def test_reply_graph_failures_raise_with_the_mapped_code(private):
    body = {"error": {"message": "(#200) Requires pages_messaging permission", "code": 200}}
    http = _reply_http(status=403, post_body=body)
    with pytest.raises(IntegrationError, match="pages_messaging") as exc:
        _run("reply", {"comment_id": "c1", "text": "Hi", "private": private}, http=http)
    assert exc.value.code == "permission_denied"


def test_reply_a_public_reply_without_an_id_is_not_reported_as_sent():
    with pytest.raises(IntegrationError, match="no id"):
        _run("reply", {"comment_id": "c1", "text": "Hi", "private": False}, http=_Http(post_body={"success": True}))


# ── social.like / hide / delete_comment ──────────────────────────────────────


def test_like_posts_to_the_likes_edge():
    http = _Http(post_body={"success": True})
    assert _run("like", {"comment_id": "1_c1"}, http=http) == {"liked": True, "network": "facebook"}
    assert http.posts[0]["url"] == f"{BASE}/1_c1/likes" and http.posts[0]["headers"]["Authorization"] == "Bearer fake-token"


def test_hide_hides_by_default_and_can_unhide():
    http = _Http(post_body={"success": True})
    assert _run("hide", {"comment_id": "c1"}, http=http) == {"hidden": True, "network": "facebook"}
    assert http.posts[0]["url"] == f"{BASE}/c1?is_hidden=true"
    assert _run("hide", {"comment_id": "c1", "hidden": False}, http=http) == {"hidden": False, "network": "facebook"}
    assert http.posts[1]["url"] == f"{BASE}/c1?is_hidden=false"
    assert _run("hide", {"comment_id": "c1", "hidden": "false"}, http=http)["hidden"] is False


def test_delete_comment_deletes_the_comment():
    http = _Http(post_body={"success": True})
    assert _run("delete_comment", {"comment_id": "c1"}, http=http) == {"deleted": True, "network": "facebook"}
    assert http.deletes == [{"url": f"{BASE}/c1", "headers": {"Authorization": "Bearer fake-token"}}] and http.posts == []


@pytest.mark.parametrize("key", ["like", "hide", "delete_comment"])
def test_moderation_actions_need_a_comment_id_and_raise_graph_errors(key):
    with pytest.raises(ValueError, match=f"{key} needs"):
        _run(key, {}, http=_Http())
    http = _Http(status=400, post_body={"error": {"message": "Unsupported delete request", "code": 100}})
    with pytest.raises(IntegrationError, match="Unsupported delete request") as exc:
        _run(key, {"comment_id": "c1"}, http=http)
    assert exc.value.code == "invalid_request"


@pytest.mark.parametrize("key", ["like", "hide", "delete_comment"])
def test_moderation_actions_do_not_claim_success_when_facebook_says_no(key):
    with pytest.raises(IntegrationError) as exc:
        _run(key, {"comment_id": "c1"}, http=_Http(post_body={"success": False}))
    assert exc.value.code == "unknown"


def test_the_comment_id_is_quoted_into_the_path():
    http = _Http(post_body={"success": True})
    _run("like", {"comment_id": "../x?y"}, http=http)
    assert http.posts[0]["url"] == f"{BASE}/..%2Fx%3Fy/likes"


# ── social.message ───────────────────────────────────────────────────────────


def test_message_sends_a_messenger_response_to_the_psid():
    http = _Http(post_body={"recipient_id": "psid1", "message_id": "mid.9"})
    out = _run("message", {"to": {"id": "psid1"}, "text": "Hello"}, http=http)
    assert out == {"sent": True, "message_id": "mid.9", "network": "facebook"}
    (post,) = http.posts
    assert post["url"] == f"{BASE}/{PAGE}/messages"
    assert post["json"] == {"recipient": {"id": "psid1"}, "messaging_type": "RESPONSE", "message": {"text": "Hello"}}
    assert post["headers"] == {"Authorization": "Bearer fake-token"}


def test_message_with_a_handle_alone_is_unsupported_and_an_id_wins_over_a_handle():
    http = _Http()
    with pytest.raises(IntegrationError, match="page-scoped id") as exc:
        _run("message", {"to": {"handle": "Ada Fan"}, "text": "Hi"}, http=http)
    assert exc.value.code == "unsupported" and http.posts == []
    _run("message", {"to": {"id": "psid1", "handle": "Ada Fan"}, "text": "Hi"}, http=http)
    assert http.posts[0]["json"]["recipient"] == {"id": "psid1"}


def test_message_dry_run_sends_nothing():
    http = _Http()
    out = _run("message", {"to": {"id": "psid1"}, "text": "Hello", "dry_run": True}, http=http)
    assert out == {
        "sent": False,
        "dry_run": True,
        "message_id": None,
        "network": "facebook",
        "would_send": {"to": {"id": "psid1"}, "text": "Hello"},
    }
    assert http.posts == [] and http.gets == []


def test_message_bad_arguments():
    for args in ({}, {"text": "x"}, {"to": "psid1", "text": "x"}, {"to": {}, "text": "x"}, {"to": {"id": "psid1"}}, {"to": {"id": "p"}, "text": " "}):
        with pytest.raises(ValueError, match="message needs"):
            _run("message", args, http=_Http())
    with pytest.raises(IntegrationError, match="2000") as exc:
        _run("message", {"to": {"id": "p"}, "text": "x" * 2001}, http=_Http())
    assert exc.value.code == "unsupported"


def test_message_outside_the_24_hour_window_is_meta_s_error_mapped_to_unsupported():
    # The plugin can't see when the person last wrote, so Meta decides: its "outside the allowed window" error (subcode
    # 2018278, under code 10) is a permanent refusal, not a missing permission.
    body = {"error": {"message": "(#10) This message is sent outside of allowed window.", "code": 10, "error_subcode": 2018278}}
    with pytest.raises(IntegrationError, match="outside of allowed window") as exc:
        _run("message", {"to": {"id": "psid1"}, "text": "Hi"}, http=_Http(status=400, post_body=body))
    assert exc.value.code == "unsupported" and "subcode 2018278" in str(exc.value)


def test_message_without_pages_messaging_is_permission_denied_and_other_errors_map_as_before():
    body = {"error": {"message": "(#200) Requires pages_messaging", "code": 200}}
    with pytest.raises(IntegrationError) as exc:
        _run("message", {"to": {"id": "psid1"}, "text": "Hi"}, http=_Http(status=403, post_body=body))
    assert exc.value.code == "permission_denied"
    body = {"error": {"message": "slow down", "code": 613}}
    with pytest.raises(IntegrationError) as exc:
        _run("message", {"to": {"id": "psid1"}, "text": "Hi"}, http=_Http(status=429, post_body=body))
    assert exc.value.code == "rate_limited"


def test_a_code_10_error_without_the_window_subcode_stays_permission_denied():
    body = {"error": {"message": "(#10) no permission", "code": 10, "error_subcode": 1234}}
    with pytest.raises(IntegrationError) as exc:
        _run("message", {"to": {"id": "psid1"}, "text": "Hi"}, http=_Http(status=400, post_body=body))
    assert exc.value.code == "permission_denied"


# ── all of them ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("key", ["comments", "reply", "like", "hide", "delete_comment", "message"])
def test_every_new_action_needs_the_token(key):
    with pytest.raises(ValueError, match="No Facebook Page access token"):
        FacebookProvider().run_action(key, {"comment_id": "c1", "text": "x", "to": {"id": "p"}}, _ctx(token=None))


def test_the_guide_covers_every_new_capability():
    from marvin_integration_sdk import load_guide

    guide = load_guide(FacebookProvider)
    for capability in (a.capability for a in FacebookProvider.actions if a.capability and a.capability != "social.post"):
        assert capability in guide
    assert "pages_messaging" in guide and "24 hours" in guide and "7 days" in guide
