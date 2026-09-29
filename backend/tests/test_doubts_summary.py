"""The reason an email is held, made from the doubts about its documents."""

from invoice_collector.checks import summary_of
from invoice_collector.domain import Doubt

UNSURE = Doubt(None, "the reader was unsure")
TOTAL = Doubt("total", "the email says 190.00 and the document says 910.00")


def test_one_doubt_is_the_reason() -> None:
    assert summary_of([TOTAL]) == "the email says 190.00 and the document says 910.00"


def test_several_doubts_give_the_first_and_how_many_more() -> None:
    assert summary_of([UNSURE, TOTAL]) == "the reader was unsure, and 1 more"


def test_no_doubt_says_the_document_was_held_with_another() -> None:
    assert summary_of([]) == "held with another document of the email"
