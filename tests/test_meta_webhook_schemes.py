"""Meta's webhook schemes, for an incoming webhook in Marvin: the URL check (WebSub's hub.* GET) and the delivery
signature (X-Hub-Signature-256), declared here rather than in Marvin's core."""

from marvin_integration_facebook.provider import FacebookProvider


def test_meta_verification_names_the_hub_parameters():
    meta = FacebookProvider.verification_schemes["meta"]
    assert (meta["challenge_param"], meta["token_param"], meta["mode_param"], meta["mode_values"]) == (
        "hub.challenge",
        "hub.verify_token",
        "hub.mode",
        ["subscribe"],
    )


def test_meta_signs_deliveries_with_sha256_in_x_hub_signature_256():
    meta = FacebookProvider.signature_schemes["meta"]
    assert (meta["header"], meta["prefix"], meta["sender_issues_key"]) == ("X-Hub-Signature-256", "sha256=", True)
