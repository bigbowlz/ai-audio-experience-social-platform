"""Tests for payment/stub.py — mock agentic payment.

Spec: docs/specs/2026-04-17-producer-alignment-plan.md Phase 2 (decision 1d)
      Master design wanlizhou-main-design-20260413-182237.md §Agentic payment
"""
from __future__ import annotations

from payment.stub import TxResult, initiate_tx


def test_initiate_tx_returns_mock_result():
    result = initiate_tx(
        from_wallet="0xPRODUCER",
        to_wallet="0x8043AeeD92c681492B13f46e91EFb8B42D18E3b2",
        amount_usdc=0.10,
    )
    assert isinstance(result, dict)
    assert result["mode"] == "MOCK"
    assert result["amount_usdc"] == 0.10
    assert result["to_wallet"] == "0x8043AeeD92c681492B13f46e91EFb8B42D18E3b2"
    assert result["tx_hash"].startswith("0xMOCK")
    assert result["basescan_url"] == ""


def test_mock_tx_hash_is_deterministic_per_arguments():
    a = initiate_tx("0xA", "0xB", 0.10)
    b = initiate_tx("0xA", "0xB", 0.10)
    assert a["tx_hash"] == b["tx_hash"]
