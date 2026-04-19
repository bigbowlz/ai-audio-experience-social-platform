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


class PrettySink:
    """Writes events as indented, key-per-line tree text (default: stdout).

    Each event renders as a header line plus one line per (possibly nested)
    payload field. Nested dicts/lists indent further; list items use
    ``[i]`` keys. Designed for human-readable CLI runs — JsonlSink remains
    the wire format for SSE/api-storage consumers.

    Example::

        ▸ producer.marketplace.queried
          candidates:
            [0]:
              handle: alices
              price_usdc: 0.1
          reasoning_summary: 1 candidate available
    """

    INDENT = "  "
    EVENT_GLYPH = "▸"

    def __init__(self, stream: IO[str] | TextIO | None = None) -> None:
        self._stream = stream if stream is not None else sys.stdout

    def __call__(self, name: str, payload: dict) -> None:
        lines = [f"{self.EVENT_GLYPH} {name}"]
        lines.extend(self._render(payload, depth=1))
        self._stream.write("\n".join(lines) + "\n")
        self._stream.flush()

    def _render(self, value: object, depth: int) -> list[str]:
        prefix = self.INDENT * depth
        lines: list[str] = []
        if isinstance(value, dict):
            if not value:
                lines.append(f"{prefix}{{}}")
                return lines
            for k, v in value.items():
                if isinstance(v, (dict, list)) and v:
                    lines.append(f"{prefix}{k}:")
                    lines.extend(self._render(v, depth + 1))
                else:
                    lines.append(f"{prefix}{k}: {self._scalar(v)}")
        elif isinstance(value, list):
            if not value:
                lines.append(f"{prefix}[]")
                return lines
            for i, item in enumerate(value):
                if isinstance(item, (dict, list)) and item:
                    lines.append(f"{prefix}[{i}]:")
                    lines.extend(self._render(item, depth + 1))
                else:
                    lines.append(f"{prefix}[{i}]: {self._scalar(item)}")
        else:
            lines.append(f"{prefix}{self._scalar(value)}")
        return lines

    @staticmethod
    def _scalar(v: object) -> str:
        if v is None:
            return "null"
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, str):
            return v
        return str(v)


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
