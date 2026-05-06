"""Indicator pipelines. Prefer pandas-ta, fall back to ta when needed."""
from .indicators import add_indicators

__all__ = ["add_indicators"]
