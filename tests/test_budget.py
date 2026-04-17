"""Tests for persistent budget tracking.

Spec: audio/docs/DESIGN.md §Cost tracking
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from audio import budget


@pytest.fixture
def budget_path(tmp_path: Path) -> str:
    return str(tmp_path / "budget.json")


class TestLoad:
    def test_missing_file_returns_default(self, budget_path):
        state = budget.load(budget_path)
        assert state["billed_characters"] == 0
        assert state["total_cost_usd"] == 0.0

    def test_reads_existing_file(self, budget_path):
        Path(budget_path).write_text(json.dumps({
            "billed_characters": 5000,
            "total_cost_usd": 1.50,
            "last_updated": "2026-04-16T00:00:00+00:00",
        }))
        state = budget.load(budget_path)
        assert state["billed_characters"] == 5000
        assert state["total_cost_usd"] == 1.50

    def test_corrupt_file_resets(self, budget_path):
        Path(budget_path).write_text("not json{{{")
        state = budget.load(budget_path)
        assert state["billed_characters"] == 0


class TestRecord:
    def test_accumulates_characters(self, budget_path):
        budget.record(1000, path=budget_path)
        budget.record(2000, path=budget_path)
        state = budget.load(budget_path)
        assert state["billed_characters"] == 3000

    def test_computes_cost(self, budget_path):
        budget.record(10_000, path=budget_path)
        state = budget.load(budget_path)
        # $0.30 / 1K chars * 10K = $3.00
        assert state["total_cost_usd"] == 3.00

    def test_warns_at_threshold(self, budget_path, caplog):
        # 80% of 66,667 = 53,334
        budget.record(53_334, path=budget_path)
        assert "Budget warning" in caplog.text

    def test_no_warning_below_threshold(self, budget_path, caplog):
        budget.record(1000, path=budget_path)
        assert "Budget warning" not in caplog.text
        assert "Budget EXCEEDED" not in caplog.text

    def test_warns_when_exceeded(self, budget_path, caplog):
        budget.record(70_000, path=budget_path)
        assert "EXCEEDED" in caplog.text

    def test_persists_across_calls(self, budget_path):
        budget.record(500, path=budget_path)
        state1 = budget.load(budget_path)
        budget.record(300, path=budget_path)
        state2 = budget.load(budget_path)
        assert state2["billed_characters"] == state1["billed_characters"] + 300
