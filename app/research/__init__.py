"""Read-only research / evidence layer.

Everything in this package is ADDITIVE and READ-ONLY with respect to live
trading: it reads the existing SQLite audit tables and public market data, and
writes ONLY to a separate research database (never the live trading tables). No
module here imports an order-placement code path or shares the live tick loop.
"""
