"""The workspace content this integration offers, declared for the core (0.5.0).

An incoming webhook ready for Meta's Page webhooks — Page comments and replies, @mentions, Messenger messages — set to the
`meta` verification and signature schemes this provider declares, so Meta can check the URL and Marvin can verify each
delivery. Its deliveries reach the Social inbox on their own (``inbox_items_from_webhook``).

A provider cannot create content itself (it never touches the database), so it declares it here and the workspace applies
it after review. It starts disabled, with no token: mint one once it is set up.
"""

from marvin_integration_sdk import ContentBlueprint

WEBHOOK_SLUG = "facebook-webhooks"
# A Facebook-login app's webhooks are signed with its App secret (App settings → Basic).
APP_SECRET_REF = "FACEBOOK_APP_SECRET"
VERIFY_TOKEN_REF = "FACEBOOK_WEBHOOK_VERIFY_TOKEN"  # the phrase typed into Meta's webhook settings

CONTENT = (
    ContentBlueprint(
        kind="incoming_webhook",
        slug=WEBHOOK_SLUG,
        name="Facebook webhooks",
        description=(
            "Where Meta sends the Page's webhooks (a comment, a mention, a Messenger message) for the Social inbox and for "
            f"workflows. After applying: mint its token; under Verification, Generate phrase (stored as {VERIFY_TOKEN_REF}); under "
            f"Signing, store the app's App secret (App settings → Basic) as {APP_SECRET_REF}. Then, in the Meta app, add the URL "
            "and phrase to the Page webhooks, subscribe the Page to feed, mention and messages, and publish the app."
        ),
        payload={
            "name": "Facebook webhooks",
            "description": "Meta's Page webhooks: checked with Meta's verification request, deliveries signed (X-Hub-Signature-256).",
            "verification_scheme": "meta",
            "verify_token_ref": VERIFY_TOKEN_REF,
            "signature_scheme": "meta",
            "signing_secret_ref": APP_SECRET_REF,
        },
    ),
)
