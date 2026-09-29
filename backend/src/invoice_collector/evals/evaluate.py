"""The three evals: what each candidate is asked, about which items, and how it is scored."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from invoice_collector.classifier import Classifier, text_of
from invoice_collector.domain import Email
from invoice_collector.evals.candidates import Candidate
from invoice_collector.evals.documents import Document
from invoice_collector.evals.golden import GoldenCase
from invoice_collector.evals.metering import Provider, cost_usd
from invoice_collector.evals.runner import Answer, AnswerCache, Call, Item, content_id, judge_all
from invoice_collector.evals.scoring import (
    CandidateResult,
    score_classification,
    score_extraction,
    score_matching,
)
from invoice_collector.extractor import Extractor
from invoice_collector.vendor_matcher import VendorMatcher


@dataclass(frozen=True)
class MatchQuestion:
    text: str
    expected_vendors: tuple[str, ...]


@dataclass(frozen=True)
class Settings:
    cache: AnswerCache | None
    read_cache: bool = True
    concurrency: int = 4


def email_id(email: Email) -> str:
    parts: list[str | bytes] = [
        email.sender,
        email.subject,
        email.text_body or "",
        email.html_body or "",
    ]
    for attachment in email.attachments:
        parts += [attachment.filename, attachment.content]
    return content_id(*parts)


def ask_classifier(classifier: Classifier, email: Email) -> Answer:
    answer = classifier.classify(email)
    return {
        "kind": answer.kind,
        "vendor": answer.vendor,
        "confidence": answer.confidence,
        "probability": answer.probability,
    }


def ask_extractor(extractor: Extractor, pdf: bytes) -> Answer:
    answer = extractor.extract(pdf)
    return {
        "document_type": answer.document_type,
        "vendor": answer.vendor,
        "invoice_date": answer.invoice_date.isoformat(),
        "total": str(answer.total),
        "currency": answer.currency,
        "confidence": answer.confidence,
    }


def ask_matcher(matcher: VendorMatcher, question: MatchQuestion) -> Answer:
    answer = matcher.match(question.text, question.expected_vendors)
    return {"vendor": answer.vendor, "probability": answer.probability}


def classification_items(cases: Sequence[GoldenCase]) -> list[Item[Email]]:
    return [Item(case.key, email_id(case.email), case.email) for case in cases]


def extraction_items(documents: Sequence[Document]) -> list[Item[bytes]]:
    return [Item(doc.key, doc.identity, doc.pdf) for doc in documents]


def matching_items(
    cases: Sequence[GoldenCase], documents: Sequence[Document], expected_vendors: Sequence[str]
) -> list[Item[MatchQuestion]]:
    """The document's text for each billing document; the email's text when there is no PDF."""
    texts = {doc.key: doc.text for doc in documents}
    vendors = tuple(expected_vendors)
    items: list[Item[MatchQuestion]] = []
    for case in cases:
        if not case.is_billing_document:
            continue
        text = texts.get(case.key) or text_of(case.email)
        items.append(Item(case.key, content_id(text, *vendors), MatchQuestion(text, vendors)))
    return items


def run_classification(
    candidates: Sequence[Candidate[Classifier]], cases: Sequence[GoldenCase], settings: Settings
) -> list[CandidateResult]:
    items = classification_items(cases)
    return [
        score_classification(candidate, cases, _judge(candidate, items, ask_classifier, settings))
        for candidate in candidates
    ]


def run_extraction(
    candidates: Sequence[Candidate[Extractor]],
    cases: Sequence[GoldenCase],
    documents: Sequence[Document],
    settings: Settings,
) -> list[CandidateResult]:
    produced = {doc.key for doc in documents}
    scored = [case for case in cases if case.key in produced]
    items = extraction_items(documents)
    return [
        score_extraction(candidate, scored, _judge(candidate, items, ask_extractor, settings))
        for candidate in candidates
    ]


def run_matching(
    candidates: Sequence[Candidate[VendorMatcher]],
    cases: Sequence[GoldenCase],
    documents: Sequence[Document],
    expected_vendors: Sequence[str],
    settings: Settings,
) -> list[CandidateResult]:
    items = matching_items(cases, documents, expected_vendors)
    billing = [case for case in cases if case.is_billing_document]
    return [
        score_matching(
            candidate, billing, expected_vendors, _judge(candidate, items, ask_matcher, settings)
        )
        for candidate in candidates
    ]


def _judge[J, C](
    candidate: Candidate[J],
    items: Sequence[Item[C]],
    ask: Callable[[J, C], Answer],
    settings: Settings,
) -> dict[str, Call]:
    return judge_all(
        candidate,
        items,
        ask,
        settings.cache,
        read_cache=settings.read_cache,
        concurrency=settings.concurrency,
    )


# --- Estimating the cost of the calls a run would make -------------------------------------------
# Token counts are estimates from docs/research/cost-and-latency.md: about 2,300 input tokens a page for a
# PDF, and about four characters a token for text. Larger models count up to a third more tokens.
TOKENS_PER_PDF_PAGE = 2_300
CHARACTERS_PER_TOKEN = 4
LARGER_MODEL_TOKEN_FACTOR = {"claude-sonnet-5-5": 1.33}


@dataclass(frozen=True)
class Estimate:
    provider: Provider
    calls: int
    usd: float


def estimate[J, C](
    candidate: Candidate[J],
    items: Sequence[Item[C]],
    tokens_of: Callable[[C], tuple[int, int]],
    settings: Settings,
) -> Estimate:
    """What the calls not already in the cache would cost."""
    if candidate.judge is None or not candidate.cached:
        return Estimate(candidate.provider, 0, 0.0)
    cache = settings.cache
    pending = [
        item
        for item in items
        if cache is None
        or not settings.read_cache
        or not cache.contains(AnswerCache.key(candidate, item.content_id))
    ]
    factor = LARGER_MODEL_TOKEN_FACTOR.get(candidate.model, 1.0)
    usd = 0.0
    for item in pending:
        input_tokens, output_tokens = tokens_of(item.content)
        usd += cost_usd(
            candidate.provider,
            candidate.model,
            round(input_tokens * factor),
            round(output_tokens * factor),
        )
    return Estimate(candidate.provider, len(pending), usd)


def classification_tokens(email: Email) -> tuple[int, int]:
    return len(text_of(email)) // CHARACTERS_PER_TOKEN + 700, 40


def extraction_tokens(pages_of: Callable[[bytes], int]) -> Callable[[bytes], tuple[int, int]]:
    return lambda pdf: (max(1, pages_of(pdf)) * TOKENS_PER_PDF_PAGE + 300, 100)


def matching_tokens(question: MatchQuestion) -> tuple[int, int]:
    characters = len(question.text) + sum(len(v) + 8 for v in question.expected_vendors)
    return characters // CHARACTERS_PER_TOKEN + 150, 10
