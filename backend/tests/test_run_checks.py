"""Tests at the run seam: doubts, reading again with a stronger model, and holding for review."""

from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal

from support import AUGUST, JULY, OPS, Collection, invoice_email, usd

from invoice_collector.domain import Doubt, Email, EmailState
from invoice_collector.exchange_rates import FakeExchangeRates

SLACK_PDF = b"%PDF-1.7 slack august"
SLACK = usd("Slack", date(2026, 8, 3), "652.50")


def august(day: int) -> datetime:
    return datetime(2026, 8, day, 9, 0, tzinfo=UTC)


def slack_email(**changes: object) -> Email:
    return replace(invoice_email("Slack", SLACK_PDF, received=august(3)), **changes)  # pyright: ignore[reportArgumentType]


def state_of(collection: Collection, email: Email) -> tuple[EmailState, str | None]:
    [examined] = [
        e for e in collection.ledger.examined_emails(AUGUST) if e.message_id == email.message_id
    ]
    return examined.state, examined.reason


# Holding for review


def test_document_the_reader_was_unsure_of_is_held_for_review(collection: Collection) -> None:
    unsure = replace(SLACK, confidence="low", doubts="the total is partly covered by a stamp")
    collection.answers[SLACK_PDF] = unsure

    result = collection.run([slack_email()])

    assert result.summary == []
    [pending] = result.pending
    assert pending.extraction.vendor == "Slack"
    assert pending.doubts == (
        Doubt(None, "the reader was unsure: the total is partly covered by a stamp"),
    )
    assert state_of(collection, slack_email()) == (
        EmailState.NEEDS_REVIEW,
        "the reader was unsure: the total is partly covered by a stamp",
    )


def test_held_document_is_kept_in_the_pending_folder(collection: Collection) -> None:
    collection.answers[SLACK_PDF] = replace(SLACK, confidence="low")

    result = collection.run([slack_email()])

    [pending] = result.pending
    assert pending.file_link == "archive/2026-08/pending/2026-08_Slack_652.50-USD.pdf"
    assert (collection.tmp_path / pending.file_link).read_bytes() == SLACK_PDF
    assert collection.saved_files() == ["pending"]


def test_document_that_passes_every_check_is_collected(collection: Collection) -> None:
    collection.answers[SLACK_PDF] = SLACK
    collection.expect("Slack", usual="640.00")

    result = collection.run([slack_email()])

    assert [row.vendor for row in result.summary] == ["Slack"]
    assert result.pending == []


def test_held_when_the_email_states_a_different_total(collection: Collection) -> None:
    collection.answers[SLACK_PDF] = SLACK
    email = slack_email(text_body="Your invoice is attached. Total due: $625.50")

    result = collection.run([email])

    assert result.summary == []
    assert result.pending[0].doubts == (
        Doubt("total", "the email says 625.50 and the document says 652.50"),
    )


def test_not_held_when_the_email_states_the_same_total(collection: Collection) -> None:
    collection.answers[SLACK_PDF] = SLACK
    email = slack_email(text_body="Subtotal: $600.00\nTotal due: $652.50")

    result = collection.run([email])

    assert [row.vendor for row in result.summary] == ["Slack"]


def test_held_when_subtotal_and_tax_do_not_add_up_to_the_total(collection: Collection) -> None:
    collection.answers[SLACK_PDF] = replace(SLACK, subtotal=Decimal("600.00"), tax=Decimal("25.50"))

    result = collection.run([slack_email()])

    assert result.pending[0].doubts == (
        Doubt("total", "subtotal 600.00 and tax 25.50 do not add up to 652.50"),
    )


def test_not_held_when_subtotal_and_tax_add_up(collection: Collection) -> None:
    collection.answers[SLACK_PDF] = replace(SLACK, subtotal=Decimal("600.00"), tax=Decimal("52.50"))

    result = collection.run([slack_email()])

    assert [row.vendor for row in result.summary] == ["Slack"]


# What the vendor has billed before


def test_held_when_the_total_is_far_above_the_vendors_usual(collection: Collection) -> None:
    collection.answers[SLACK_PDF] = SLACK
    collection.expect("Slack", usual="472.50")

    result = collection.run([slack_email()])

    assert result.pending[0].doubts == (
        Doubt("total", "the total is 38% above the usual 472.50 USD for Slack"),
    )


def test_how_far_from_usual_counts_can_be_set(collection: Collection) -> None:
    collection.answers[SLACK_PDF] = SLACK
    collection.expect("Slack", usual="472.50")
    collection.anomaly_threshold = Decimal("0.50")

    result = collection.run([slack_email()])

    assert [row.vendor for row in result.summary] == ["Slack"]


def test_usual_amount_comes_from_earlier_months_for_a_vendor_on_no_list(
    collection: Collection,
) -> None:
    july_pdf = b"%PDF-1.7 slack july"
    collection.answers[july_pdf] = usd("Slack", date(2026, 7, 3), "470.00")
    collection.answers[SLACK_PDF] = SLACK
    collection.run(
        [invoice_email("Slack", july_pdf, received=datetime(2026, 7, 3, 9, 0, tzinfo=UTC))], JULY
    )

    result = collection.run([slack_email()])

    assert result.pending[0].doubts == (
        Doubt("total", "the total is 39% above the usual 470.00 USD for Slack"),
    )


def test_held_when_the_currency_is_not_the_vendors_usual(collection: Collection) -> None:
    collection.answers[SLACK_PDF] = replace(SLACK, currency="EUR")
    collection.rates = FakeExchangeRates({"EUR": Decimal("110.38")})
    collection.expect("Slack", usual="652.50")

    result = collection.run([slack_email()])

    assert result.pending[0].doubts == (
        Doubt("currency", "the currency is EUR; Slack usually bills in USD"),
    )


def test_held_when_it_is_the_second_invoice_from_the_vendor_this_month(
    collection: Collection,
) -> None:
    second_pdf = b"%PDF-1.7 slack august again"
    collection.answers[SLACK_PDF] = SLACK
    collection.answers[second_pdf] = usd("Slack", date(2026, 8, 20), "652.50")
    second = invoice_email("Slack", second_pdf, received=august(20))

    result = collection.run([slack_email(), second])

    assert [row.invoice_date for row in result.summary] == [date(2026, 8, 3)]
    assert result.pending[0].doubts == (
        Doubt("invoice_date", "second invoice from Slack this month"),
    )


def test_receipt_for_an_invoice_is_not_a_second_invoice(collection: Collection) -> None:
    receipt_pdf = b"%PDF-1.7 slack receipt"
    collection.answers[SLACK_PDF] = SLACK
    collection.answers[receipt_pdf] = usd("Slack", date(2026, 8, 5), "652.50", "receipt")
    receipt = invoice_email("Slack", receipt_pdf, received=august(5), subject="Your Slack receipt")

    result = collection.run([slack_email(), receipt])

    assert len(result.summary) == 1
    assert result.pending == []


def test_same_document_in_a_second_source_account_is_not_a_second_invoice(
    collection: Collection,
) -> None:
    collection.answers[SLACK_PDF] = SLACK
    copy = invoice_email("Slack", SLACK_PDF, received=august(3), account=OPS)

    result = collection.run([slack_email(), copy])

    assert len(result.summary) == 1
    assert result.pending == []


def test_credit_note_is_not_compared_with_the_usual_charge(collection: Collection) -> None:
    credit_pdf = b"%PDF-1.7 slack credit"
    collection.answers[credit_pdf] = usd("Slack", date(2026, 8, 18), "45.00", "credit_note")
    collection.expect("Slack", usual="652.50")
    credit = invoice_email(
        "Slack", credit_pdf, received=august(18), subject="Your Slack credit note"
    )

    result = collection.run([credit])

    assert [row.total for row in result.summary] == [Decimal("-45.00")]


# Reading again with a stronger model


def test_doubtful_reading_is_read_again_by_a_stronger_model(collection: Collection) -> None:
    collection.answers[SLACK_PDF] = replace(SLACK, total=Decimal("625.50"), confidence="low")
    collection.stronger_answers[SLACK_PDF] = SLACK

    result = collection.run([slack_email()])

    [row] = result.summary
    assert row.total == Decimal("652.50")
    assert result.pending == []


def test_document_the_stronger_model_also_doubts_is_held_with_its_reading(
    collection: Collection,
) -> None:
    collection.answers[SLACK_PDF] = replace(SLACK, total=Decimal("625.50"), confidence="low")
    collection.stronger_answers[SLACK_PDF] = replace(
        SLACK, confidence="medium", doubts="the total is smudged"
    )

    result = collection.run([slack_email()])

    [pending] = result.pending
    assert pending.extraction.total == Decimal("652.50")
    assert pending.doubts == (Doubt(None, "the reader was unsure: the total is smudged"),)
    assert pending.read_again is True


def test_stronger_model_that_cannot_read_leaves_the_first_reading_held(
    collection: Collection,
) -> None:
    collection.answers[SLACK_PDF] = replace(SLACK, confidence="low")

    result = collection.run([slack_email()])

    [pending] = result.pending
    assert pending.extraction.total == Decimal("652.50")
    assert pending.read_again is False


def test_document_that_only_differs_from_usual_is_not_read_again(
    collection: Collection,
) -> None:
    collection.answers[SLACK_PDF] = SLACK
    collection.stronger_answers[SLACK_PDF] = replace(SLACK, total=Decimal("1.00"))
    collection.expect("Slack", usual="472.50")

    result = collection.run([slack_email()])

    assert result.pending[0].extraction.total == Decimal("652.50")
    assert result.pending[0].read_again is False


# Running again


def test_held_document_is_not_read_again_on_the_next_run(collection: Collection) -> None:
    collection.answers[SLACK_PDF] = replace(SLACK, confidence="low")
    collection.run([slack_email()])
    collection.answers.clear()

    result = collection.run([slack_email()])

    assert [p.extraction.vendor for p in result.pending] == ["Slack"]
    assert state_of(collection, slack_email())[0] is EmailState.NEEDS_REVIEW


def test_collected_document_is_not_held_by_a_later_run(collection: Collection) -> None:
    collection.answers[SLACK_PDF] = SLACK
    collection.run([slack_email()])
    collection.expect("Slack", usual="100.00")

    result = collection.run([slack_email()])

    assert [row.vendor for row in result.summary] == ["Slack"]
    assert result.pending == []
