"""Typed data-engine errors."""

from __future__ import annotations


class DataEngineError(Exception):
    """Base class for data-engine failures."""


class NoData(DataEngineError):
    """A source returned no data for the requested window."""


class SourceError(DataEngineError):
    """A source failed unexpectedly."""
