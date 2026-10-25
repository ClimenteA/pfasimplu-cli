"""Currency conversion helpers with retries and clearer error messages."""

from __future__ import annotations

import datetime
from decimal import Decimal

from cursvalutarbnr import Currency
from cursvalutarbnr import ron_exchange_rate as _ron_exchange_rate
from tenacity import retry, stop_after_attempt, wait_exponential_jitter


class ExchangeRateError(RuntimeError):
    """Raised when the BNR exchange rate cannot be retrieved."""


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential_jitter(initial=1, max=8),
    reraise=True,
)
def _ron_exchange_rate_with_retry(
    amount: float | Decimal,
    currency: Currency | str,
    date: str | datetime.date | None,
) -> float:
    return _ron_exchange_rate(amount=amount, currency=currency, date=date)


def ron_exchange_rate(
    amount: float | Decimal,
    currency: Currency | str,
    date: str | datetime.date | None = None,
) -> float:
    """Convert ``amount`` from ``currency`` to RON using the BNR rate for ``date``.

    The BNR endpoint is flaky, so the call is retried a few times. If all
    attempts fail an ``ExchangeRateError`` with the original cause is raised.
    """
    try:
        return _ron_exchange_rate_with_retry(
            amount=amount, currency=currency, date=date
        )
    except Exception as exc:
        raise ExchangeRateError(
            f"Nu am putut obtine cursul BNR pentru {amount} {currency} "
            f"la data {date}: {exc}"
        ) from exc
