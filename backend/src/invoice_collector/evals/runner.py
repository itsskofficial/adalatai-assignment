"""Asks a candidate about each item, through a cache so an unchanged pair is never paid twice."""

import hashlib
import json
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from invoice_collector.evals.candidates import Candidate
from invoice_collector.evals.metering import measuring

Answer = dict[str, Any]
"""An answer as JSON, so it can be cached and written to the scorecard."""


@dataclass(frozen=True)
class Call:
    """What a candidate answered about one item, and what that cost."""

    answer: Answer | None
    error: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    seconds: float = 0.0
    cached: bool = False
    """The answer was read from the cache; its tokens and time are those of the original call."""


@dataclass(frozen=True)
class Item[C]:
    key: str
    """The golden case this item belongs to."""
    content_id: str
    """A hash of everything the candidate is shown, which with the candidate keys the cache."""
    content: C


def content_id(*parts: str | bytes) -> str:
    digest = hashlib.sha256()
    for part in parts:
        data = part.encode() if isinstance(part, str) else part
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest()


class AnswerCache:
    """Answers kept as JSON files, one per candidate and content."""

    def __init__(self, folder: Path) -> None:
        self._folder = folder

    @staticmethod
    def key(candidate: Candidate[Any], item_content_id: str) -> str:
        return content_id(
            candidate.name, candidate.model, candidate.prompt_version, item_content_id
        )

    def get(self, key: str) -> Call | None:
        path = self._folder / f"{key}.json"
        if not path.exists():
            return None
        stored = cast(dict[str, Any], json.loads(path.read_text("utf-8")))
        return Call(
            answer=cast(Answer, stored["answer"]),
            input_tokens=int(stored["input_tokens"]),
            output_tokens=int(stored["output_tokens"]),
            seconds=float(stored["seconds"]),
            cached=True,
        )

    def contains(self, key: str) -> bool:
        return (self._folder / f"{key}.json").exists()

    def put(self, key: str, call: Call) -> None:
        self._folder.mkdir(parents=True, exist_ok=True)
        stored = {
            "answer": call.answer,
            "input_tokens": call.input_tokens,
            "output_tokens": call.output_tokens,
            "seconds": call.seconds,
        }
        (self._folder / f"{key}.json").write_text(
            json.dumps(stored, indent=2, sort_keys=True), "utf-8"
        )


def _ask_once[J, C](judge: J, ask: Callable[[J, C], Answer], content: C) -> Call:
    started = time.perf_counter()
    with measuring() as usage:
        try:
            answer, error = ask(judge, content), None
        except Exception as failure:  # one failed call must not stop the eval
            answer, error = None, str(failure) or type(failure).__name__
    return Call(
        answer=answer,
        error=error,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        seconds=time.perf_counter() - started,
    )


def judge_all[J, C](
    candidate: Candidate[J],
    items: Sequence[Item[C]],
    ask: Callable[[J, C], Answer],
    cache: AnswerCache | None,
    *,
    read_cache: bool = True,
    concurrency: int = 4,
) -> dict[str, Call]:
    """The candidate's answer for each item, by item key. Failed calls carry their error."""
    judge = candidate.judge
    if judge is None:
        return {}
    use_cache = cache if candidate.cached else None

    def one(item: Item[C]) -> Call:
        key = AnswerCache.key(candidate, item.content_id)
        if use_cache is not None and read_cache:
            stored = use_cache.get(key)
            if stored is not None:
                return stored
        call = _ask_once(judge, ask, item.content)
        if use_cache is not None and call.answer is not None:
            use_cache.put(key, call)
        return call

    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        calls = list(pool.map(one, items))
    return {item.key: call for item, call in zip(items, calls, strict=True)}
