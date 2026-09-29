"""Which addresses may be opened when following a link taken from an email."""

import pytest

from invoice_collector.destinations import DestinationPolicy


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("http://8.8.8.8/invoice", "http links are not followed"),
        ("file:///etc/passwd", "file links are not followed"),
        ("javascript:alert(1)", "javascript links are not followed"),
        ("https://127.0.0.1/invoice", "127.0.0.1 is not a public address"),
        ("https://localhost/invoice", "localhost is not a public address"),
        ("https://10.0.0.5/invoice", "10.0.0.5 is not a public address"),
        ("https://192.168.1.10/invoice", "192.168.1.10 is not a public address"),
        ("https://169.254.169.254/latest/meta-data", "169.254.169.254 is not a public address"),
        ("https://[::1]/invoice", "::1 is not a public address"),
        ("https://user:secret@8.8.8.8/invoice", "links carrying a user name or password"),
        ("https:///invoice", "the link has no host"),
    ],
)
def test_link_that_must_not_be_followed_is_refused(url: str, reason: str) -> None:
    refusal = DestinationPolicy().refusal(url)

    assert refusal is not None
    assert reason in refusal


def test_secure_link_to_a_public_address_is_allowed() -> None:
    assert DestinationPolicy().refusal("https://8.8.8.8/i/in_1PqX7fK2") is None


def test_host_that_does_not_exist_is_refused() -> None:
    refusal = DestinationPolicy().refusal("https://billing.vendor.invalid/i/1")

    assert refusal == "billing.vendor.invalid could not be found"


def test_local_sample_pages_can_be_allowed_on_purpose() -> None:
    policy = DestinationPolicy.for_local_pages()

    assert policy.refusal("http://localhost:8765/in_1PqX7fK2.html") is None
    assert policy.refusal("file:///etc/passwd") is not None
