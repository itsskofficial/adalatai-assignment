"""Which addresses may be opened when following a link taken from an email."""

import pytest

from invoice_collector.cli import destination_policy
from invoice_collector.destinations import DestinationPolicy, NotAnOrigin


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
        ("https://8.8.8.8:notaport/invoice", "the link is malformed"),
        ("https://[::1/invoice", "the link is malformed"),
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


PORTAL = DestinationPolicy.with_sample_portal("http://portal:8765")


@pytest.mark.parametrize(
    "url",
    ["http://portal:8765/in_1PqX7fK2.html", "http://PORTAL:8765/", "http://portal:8765"],
)
def test_the_named_sample_portal_is_allowed(url: str) -> None:
    assert PORTAL.refusal(url) is None


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("http://portal:8766/in_1PqX7fK2.html", "http links are not followed"),
        ("http://portal2:8765/in_1PqX7fK2.html", "http links are not followed"),
        ("http://portal.attacker.example:8765/", "http links are not followed"),
        ("http://localhost:8765/in_1PqX7fK2.html", "http links are not followed"),
        ("http://10.0.0.5:8765/", "http links are not followed"),
        ("https://127.0.0.1/invoice", "127.0.0.1 is not a public address"),
        ("https://169.254.169.254/latest/meta-data", "169.254.169.254 is not a public address"),
        ("http://user:secret@portal:8765/", "http links are not followed"),
    ],
)
def test_nothing_else_local_is_allowed_beside_the_sample_portal(url: str, reason: str) -> None:
    refusal = PORTAL.refusal(url)

    assert refusal is not None
    assert reason in refusal


def test_public_secure_links_are_still_allowed_beside_the_sample_portal() -> None:
    assert PORTAL.refusal("https://8.8.8.8/i/in_1PqX7fK2") is None


@pytest.mark.parametrize(
    "address",
    ["portal:8765", "http://portal:8765/pages", "ftp://portal:21", "http://portal:port", "http://"],
)
def test_a_sample_portal_address_must_be_a_scheme_a_host_and_a_port(address: str) -> None:
    with pytest.raises(NotAnOrigin, match="INVOICE_COLLECTOR_SAMPLE_PORTAL_URL must be"):
        DestinationPolicy.with_sample_portal(address)


def test_the_collect_command_opens_only_the_sample_portal_it_is_given() -> None:
    policy = destination_policy(
        False, {"INVOICE_COLLECTOR_SAMPLE_PORTAL_URL": "http://portal:8765"}
    )

    assert policy.refusal("http://portal:8765/in_1PqX7fK2.html") is None
    assert policy.refusal("http://runner:8001/health") is not None
    assert destination_policy(False, {}) == DestinationPolicy()
