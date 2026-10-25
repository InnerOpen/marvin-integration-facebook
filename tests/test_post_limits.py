"""post_limits: the numbers Marvin checks a caption against, matching what this plugin enforces when it posts."""

from marvin_integration_sdk import check_post_limits

from marvin_integration_facebook import FacebookProvider


def test_post_limits_are_declared_and_usable():
    assert FacebookProvider.post_limits and check_post_limits(FacebookProvider.post_limits) == []
