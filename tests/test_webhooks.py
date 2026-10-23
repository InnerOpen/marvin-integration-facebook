"""Meta's Facebook Page webhook deliveries as Social inbox items (0.5.0), the `social.inbox_items` action, and the
Facebook webhooks incoming webhook this integration offers."""

import logging

import pytest
from marvin_integration_sdk import IntegrationContext

from marvin_integration_facebook.content import APP_SECRET_REF, VERIFY_TOKEN_REF
from marvin_integration_facebook.provider import FacebookProvider
from marvin_integration_facebook.webhooks import inbox_items

PAGE, POST, PERSON = "100000000000001", "100000000000001_200000000000002", "3000000000000003"


def _feed(**value):
    return {"object": "page", "entry": [{"id": PAGE, "time": 1792775851, "changes": [{"field": "feed", "value": value}]}]}


def _messaging(*events):
    return {"object": "page", "entry": [{"id": PAGE, "time": 1792776187033, "messaging": list(events)}]}


def test_a_new_comment_is_a_comment_with_the_id_the_poll_gives_it():
    (item,) = inbox_items(_feed(item="comment", verb="add", comment_id=f"{POST}_9", post_id=POST, parent_id=POST,
                                message="Love these jeans", **{"from": {"id": PERSON, "name": "Ada"}}, created_time=1792775850))  # fmt: skip

    assert (item["id"], item["kind"], item["text"], item["post_id"]) == (f"{POST}_9", "comment", "Love these jeans", POST)
    assert item["author"] == {"id": PERSON, "handle": "Ada", "name": "Ada"} and item["network"] == "facebook"


def test_a_comment_under_a_comment_is_a_reply():
    (item,) = inbox_items(_feed(item="comment", verb="add", comment_id=f"{POST}_10", post_id=POST, parent_id=f"{POST}_9", message="Same"))

    assert item["kind"] == "reply"


def test_edits_removals_likes_and_posts_give_nothing():
    for value in (
        {"item": "comment", "verb": "edited", "comment_id": "c", "post_id": POST},
        {"item": "comment", "verb": "remove", "comment_id": "c", "post_id": POST},
        {"item": "reaction", "verb": "add", "post_id": POST},
        {"item": "status", "verb": "add", "post_id": POST},
    ):
        assert inbox_items(_feed(**value)) == []


def test_a_mention_is_kept_even_without_its_text():
    payload = {"object": "page", "entry": [{"id": PAGE, "time": 1792775851, "changes": [
        {"field": "mention", "value": {"item": "comment", "verb": "add", "comment_id": "c-men", "post_id": "p-men", "sender_id": PERSON}},
    ]}]}  # fmt: skip

    (item,) = inbox_items(payload)

    assert (item["kind"], item["id"], item["author"]["id"]) == ("mention", "c-men", PERSON)


def test_a_messenger_message_with_a_picture():
    event = {"sender": {"id": PERSON}, "recipient": {"id": PAGE}, "timestamp": 1792776186475,
             "message": {"mid": "m.1", "text": "Do you ship?", "attachments": [{"type": "image", "payload": {"url": "https://cdn.example/a.jpg"}}]}}  # fmt: skip

    (item,) = inbox_items(_messaging(event))

    assert (item["id"], item["kind"], item["text"], item["author"]["id"]) == ("m.1", "message", "Do you ship?", PERSON)
    assert item["attachments"] == [{"type": "image", "url": "https://cdn.example/a.jpg"}]


def test_a_reaction_is_kept_and_the_pages_own_messages_are_not():
    react = {"sender": {"id": PERSON}, "timestamp": 1792776186475, "reaction": {"mid": "m.1", "action": "react", "emoji": "👍"}}
    echo = {"sender": {"id": PAGE}, "timestamp": 1792776186475, "message": {"mid": "m.2", "text": "Yes!", "is_echo": True}}

    (item,) = inbox_items(_messaging(react, echo))

    assert (item["kind"], item["text"]) == ("reaction", "👍")


def test_anything_that_isnt_a_page_delivery_gives_nothing():
    assert inbox_items({"object": "instagram", "entry": []}) == [] and inbox_items({}) == []


def test_the_provider_hands_marvin_the_items_and_the_action_does_the_same():
    payload = _messaging({"sender": {"id": PERSON}, "timestamp": 1792776186475, "message": {"mid": "m.3", "text": "Hi"}})
    ctx = IntegrationContext(config={"page_id": PAGE}, secret="tok", logger=logging.getLogger("t"), http=None)

    assert FacebookProvider().inbox_items_from_webhook(payload, ctx)[0]["id"] == "m.3"
    assert FacebookProvider().run_action("inbox_items", {"payload": payload}, ctx)["items"][0]["id"] == "m.3"
    with pytest.raises(TypeError, match="payload"):
        FacebookProvider().run_action("inbox_items", {"payload": []}, ctx)


def test_the_offered_webhook_uses_the_meta_schemes_and_is_a_suggestion():
    (hook,) = FacebookProvider().content
    assert (hook.kind, hook.slug, hook.required) == ("incoming_webhook", "facebook-webhooks", False)
    assert (hook.payload["verification_scheme"], hook.payload["signature_scheme"]) == ("meta", "meta")
    assert (hook.payload["verify_token_ref"], hook.payload["signing_secret_ref"]) == (VERIFY_TOKEN_REF, APP_SECRET_REF)
