"""The rate from a currency to rupees on a date."""

import json
import urllib.error
import urllib.request
from collections.abc import Mapping
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Protocol

RUPEE = "INR"


class ExchangeRateUnavailable(Exception):
    pass


class ExchangeRates(Protocol):
    def to_rupees(self, currency: str, on: date) -> Decimal:
        """How many rupees one unit of the currency was worth. Raises ExchangeRateUnavailable."""
        ...


class FakeExchangeRates:
    """Returns prepared rates by currency, and records what it was asked for."""

    def __init__(self, rates: Mapping[str, Decimal]) -> None:
        self._rates = dict(rates)
        self.asked: list[tuple[str, date]] = []

    def to_rupees(self, currency: str, on: date) -> Decimal:
        if currency == RUPEE:
            return Decimal(1)
        self.asked.append((currency, on))
        try:
            return self._rates[currency]
        except KeyError:
            raise ExchangeRateUnavailable(f"no rate for {currency} on {on}") from None


class FrankfurterExchangeRates:
    """Reference rates published by the European Central Bank, served by frankfurter.dev.

    On a day with no published rate, such as a weekend, the service answers with the
    most recent earlier rate.
    """

    def __init__(
        self, base_url: str = "https://api.frankfurter.dev/v1", timeout: float = 10
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    def to_rupees(self, currency: str, on: date) -> Decimal:
        if currency == RUPEE:
            return Decimal(1)
        if not (len(currency) == 3 and currency.isalpha()):
            raise ExchangeRateUnavailable(f"{currency!r} is not a currency code")

        url = f"{self._base_url}/{on.isoformat()}?base={currency.upper()}&symbols={RUPEE}"
        # The service refuses requests that do not say what is calling.
        request = urllib.request.Request(
            url, headers={"User-Agent": "invoice-collector", "Accept": "application/json"}
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                body = json.load(response, parse_float=Decimal)
            return Decimal(body["rates"][RUPEE])
        except urllib.error.HTTPError as error:
            raise ExchangeRateUnavailable(
                f"no rate for {currency} on {on}: HTTP {error.code}"
            ) from error
        except (urllib.error.URLError, TimeoutError) as error:
            raise ExchangeRateUnavailable(
                f"no rate for {currency} on {on}: the rate service could not be reached"
            ) from error
        except (KeyError, TypeError, ValueError, InvalidOperation) as error:
            raise ExchangeRateUnavailable(
                f"no rate for {currency} on {on}: the rate service gave an unusable answer"
            ) from error
