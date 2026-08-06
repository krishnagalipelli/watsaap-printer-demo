"""Decide whether a captured job may be sent without a human looking at it."""

from .gate import Decision, GateOutcome, evaluate

__all__ = ["Decision", "GateOutcome", "evaluate"]
