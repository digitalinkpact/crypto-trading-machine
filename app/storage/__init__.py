"""Persistent storage layer (SQLite). Survives restarts and stays usable
when switching from paper to live trading."""
from .db import Storage, storage

__all__ = ["Storage", "storage"]
