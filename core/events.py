"""Typed event bus for decoupling core from UI."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, TypeVar, Generic
from collections import defaultdict

log = logging.getLogger(__name__)

T = TypeVar("T")


@dataclass
class ItemFetched:
    media_id: int


@dataclass
class ScanFinished:
    counts: dict


@dataclass
class MetadataChanged:
    media_ids: list[int]


@dataclass
class ItemWatched:
    media_id: int
    watched: bool


@dataclass
class ItemRating:
    media_id: int
    rating: int | None


@dataclass
class PlayRequested:
    path: str


@dataclass
class FilterRequested:
    kind: str
    value: str


class EventBus:
    """Simple typed event bus with synchronous dispatch."""
    
    def __init__(self) -> None:
        self._subscribers: dict[type, list[Callable]] = defaultdict(list)
    
    def subscribe(self, event_type: type[T], handler: Callable[[T], None]) -> Callable[[], None]:
        """Subscribe to an event type. Returns unsubscribe function."""
        self._subscribers[event_type].append(handler)
        def unsubscribe():
            try:
                self._subscribers[event_type].remove(handler)
            except ValueError:
                pass
        return unsubscribe
    
    def emit(self, event: T) -> None:
        """Emit an event to all subscribers."""
        event_type = type(event)
        for handler in self._subscribers.get(event_type, []):
            try:
                handler(event)
            except Exception:
                log.exception("Event handler failed for %s", event_type.__name__)
    
    def clear(self) -> None:
        """Clear all subscribers (for testing/shutdown)."""
        self._subscribers.clear()


# Global event bus instance
event_bus = EventBus()
