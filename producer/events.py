"""In-process event bus for Producer SSE-bound events.

Sinks consume `(event_name, payload)` tuples. Default sink is JSONL to stdout
during dev/CLI runs; the api-storage component can replace the sink with one
that ships events over HTTP/SSE.

The bus is intentionally thread-safe-by-stupid (uses a list, not a lock):
producer events are emitted from the main thread between phases, never from
inside the parallel pitch round. If that changes, wrap subscribers in a lock.

Spec: producer/docs/DESIGN.md §SSE
      docs/specs/2026-04-17-producer-alignment-plan.md Phase 1 (decision 3d)
"""
from __future__ import annotations

import json
import sys
from collections.abc import Callable
from typing import IO, TextIO

EventSink = Callable[[str, dict], None]


class EventBus:
    """Tiny pub/sub. Subscribers are functions of (event_name, payload)."""

    def __init__(self) -> None:
        self._subscribers: list[EventSink] = []

    def subscribe(self, sink: EventSink) -> None:
        self._subscribers.append(sink)

    def emit(self, name: str, payload: dict) -> None:
        for sink in self._subscribers:
            sink(name, payload)


class JsonlSink:
    """Writes one JSON line per event to a file-like (default: stdout).

    Wire format: {"event": "<name>", "payload": <payload>}
    """

    def __init__(self, stream: IO[str] | TextIO | None = None) -> None:
        self._stream = stream if stream is not None else sys.stdout

    def __call__(self, name: str, payload: dict) -> None:
        self._stream.write(json.dumps({"event": name, "payload": payload}) + "\n")
        self._stream.flush()


# ── Module-level convenience ──────────────────────────────────────────

_default_bus = EventBus()


def set_default_bus(bus: EventBus) -> None:
    """Replace the module-level default bus (test seam)."""
    global _default_bus
    _default_bus = bus


def emit(name: str, payload: dict) -> None:
    """Emit on the module-level default bus."""
    _default_bus.emit(name, payload)


def subscribe(sink: EventSink) -> None:
    """Subscribe a sink to the module-level default bus."""
    _default_bus.subscribe(sink)
