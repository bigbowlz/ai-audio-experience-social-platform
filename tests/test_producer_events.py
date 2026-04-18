"""Tests for producer/events.py — in-process bus + sinks.

Spec: docs/specs/2026-04-17-producer-alignment-plan.md Phase 1
      producer/docs/DESIGN.md §SSE
"""
from __future__ import annotations

import io
import json

import pytest

from producer.events import (
    EventBus,
    JsonlSink,
    emit,
    set_default_bus,
    subscribe,
)


def test_emit_with_no_subscribers_is_silent():
    bus = EventBus()
    bus.emit("producer.test", {"k": 1})  # no exception


def test_subscribe_receives_emissions():
    bus = EventBus()
    received = []
    bus.subscribe(lambda name, payload: received.append((name, payload)))
    bus.emit("producer.test", {"k": 1})
    assert received == [("producer.test", {"k": 1})]


def test_emission_order_preserved_across_multiple_emits():
    bus = EventBus()
    received = []
    bus.subscribe(lambda name, payload: received.append(name))
    bus.emit("producer.memory.applied", {})
    bus.emit("producer.selecting.started", {})
    bus.emit("producer.pick", {})
    bus.emit("producer.selecting.done", {})
    assert received == [
        "producer.memory.applied",
        "producer.selecting.started",
        "producer.pick",
        "producer.selecting.done",
    ]


def test_jsonl_sink_writes_one_line_per_event():
    buf = io.StringIO()
    sink = JsonlSink(buf)
    sink("producer.test", {"k": 1})
    sink("producer.test", {"k": 2})
    lines = buf.getvalue().strip().split("\n")
    assert len(lines) == 2
    assert json.loads(lines[0]) == {"event": "producer.test", "payload": {"k": 1}}
    assert json.loads(lines[1]) == {"event": "producer.test", "payload": {"k": 2}}


def test_module_level_emit_routes_to_default_bus():
    bus = EventBus()
    received = []
    bus.subscribe(lambda name, payload: received.append((name, payload)))
    set_default_bus(bus)
    emit("producer.test", {"k": 1})
    assert received == [("producer.test", {"k": 1})]


def test_subscribe_module_level_adds_to_default_bus():
    bus = EventBus()
    received = []
    set_default_bus(bus)
    subscribe(lambda n, p: received.append(n))
    emit("producer.test", {})
    assert received == ["producer.test"]
