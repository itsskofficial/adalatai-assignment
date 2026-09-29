"""The exchange rate service, checked against recorded responses without calling it."""

import json
import threading
from collections.abc import Callable, Iterator
from datetime import date
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from invoice_collector.exchange_rates import ExchangeRateUnavailable, FrankfurterExchangeRates

# Recorded from https://api.frankfurter.dev/v1/2026-08-03?base=USD&symbols=INR
RECORDED: dict[str, Any] = {
    "amount": 1.0,
    "base": "USD",
    "date": "2026-08-03",
    "rates": {"INR": 95.34},
}

Replay = Callable[[int, Any], tuple[FrankfurterExchangeRates, list[str]]]


@pytest.fixture
def replay() -> Iterator[Replay]:
    servers: list[ThreadingHTTPServer] = []

    def start(status: int, body: Any) -> tuple[FrankfurterExchangeRates, list[str]]:
        requested: list[str] = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                pass

            def do_GET(self) -> None:
                requested.append(self.path)
                payload = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        servers.append(server)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        rates = FrankfurterExchangeRates(f"http://127.0.0.1:{server.server_port}/v1", timeout=5)
        return rates, requested

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


def test_rate_to_rupees_is_read_for_the_invoice_date(replay: Replay) -> None:
    rates, requested = replay(200, RECORDED)

    assert rates.to_rupees("USD", date(2026, 8, 3)) == Decimal("95.34")
    assert requested == ["/v1/2026-08-03?base=USD&symbols=INR"]


def test_rupees_need_no_request(replay: Replay) -> None:
    rates, requested = replay(200, RECORDED)

    assert rates.to_rupees("INR", date(2026, 8, 3)) == Decimal(1)
    assert requested == []


def test_currency_the_service_does_not_know_is_unavailable(replay: Replay) -> None:
    rates, _ = replay(404, {"message": "not found"})

    with pytest.raises(ExchangeRateUnavailable, match="HTTP 404"):
        rates.to_rupees("XXX", date(2026, 8, 3))


def test_text_that_is_not_a_currency_code_makes_no_request(replay: Replay) -> None:
    rates, requested = replay(200, RECORDED)

    with pytest.raises(ExchangeRateUnavailable, match="not a currency code"):
        rates.to_rupees("US$&symbols=EUR", date(2026, 8, 3))
    assert requested == []


def test_answer_without_a_rate_is_unavailable(replay: Replay) -> None:
    rates, _ = replay(200, {"amount": 1.0, "base": "USD", "rates": {}})

    with pytest.raises(ExchangeRateUnavailable, match="unusable answer"):
        rates.to_rupees("USD", date(2026, 8, 3))


def test_answer_that_is_not_json_is_unavailable(replay: Replay) -> None:
    rates, _ = replay(200, b"<html>maintenance</html>")

    with pytest.raises(ExchangeRateUnavailable, match="unusable answer"):
        rates.to_rupees("USD", date(2026, 8, 3))


def test_service_that_cannot_be_reached_is_unavailable() -> None:
    rates = FrankfurterExchangeRates("http://127.0.0.1:9/v1", timeout=2)

    with pytest.raises(ExchangeRateUnavailable, match="could not be reached"):
        rates.to_rupees("USD", date(2026, 8, 3))
