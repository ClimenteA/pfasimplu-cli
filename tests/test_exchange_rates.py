from __future__ import annotations

import datetime

import pytest
from tenacity import wait_none

from pfasimplu_cli import exchange_rates


def test_ron_exchange_rate_retries_transient_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        exchange_rates._ron_exchange_rate_with_retry.retry, "wait", wait_none()
    )
    calls = {"count": 0}

    def fake_ron_exchange_rate(amount, currency, date):
        calls["count"] += 1
        if calls["count"] < 3:
            raise ConnectionError("BNR is unreachable")
        return 4.97

    monkeypatch.setattr(exchange_rates, "_ron_exchange_rate", fake_ron_exchange_rate)

    result = exchange_rates.ron_exchange_rate(
        amount=10, currency="EUR", date=datetime.date(2026, 1, 5)
    )

    assert result == 4.97
    assert calls["count"] == 3


def test_ron_exchange_rate_raises_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        exchange_rates._ron_exchange_rate_with_retry.retry, "wait", wait_none()
    )

    def fake_ron_exchange_rate(amount, currency, date):
        raise ValueError("no rate")

    monkeypatch.setattr(exchange_rates, "_ron_exchange_rate", fake_ron_exchange_rate)

    with pytest.raises(exchange_rates.ExchangeRateError, match="cursul BNR"):
        exchange_rates.ron_exchange_rate(
            amount=10, currency="EUR", date=datetime.date(2026, 1, 5)
        )
