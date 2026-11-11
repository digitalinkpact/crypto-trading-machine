"""Enforce temporary runtime storage and offline tests before app imports."""
import os
import socket
import tempfile
import sys

import pytest


_runtime = tempfile.TemporaryDirectory(prefix="profitstream-tests-")
os.environ.update({
    "APP_ENV_FILE": "/dev/null",
    "DATA_CACHE_DIR": _runtime.name,
    "LIVE_BUYS_ENABLED": "false",
    "BINANCE_API_KEY": "",
    "BINANCE_API_SECRET": "",
    "LLM_PROVIDER": "none",
    "LLM_IN_TRADING_LOOP": "false",
})
for _mode_key in ("TRADING_MODE", "LIVE_MODE", "PAPER_TRADING", "DRY_RUN"):
    os.environ.pop(_mode_key, None)


def pytest_unconfigure(config):
    _runtime.cleanup()


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    attempts = []
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex

    def connect(connection, address):
        if connection.family in (socket.AF_INET, socket.AF_INET6):
            attempts.append("unmocked network connection")
            connection.close()
            raise AssertionError("Tests must mock the exchange/network boundary")
        return original_connect(connection, address)

    def connect_ex(connection, address):
        if connection.family in (socket.AF_INET, socket.AF_INET6):
            attempts.append("unmocked network connection")
            connection.close()
            raise AssertionError("Tests must mock the exchange/network boundary")
        return original_connect_ex(connection, address)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    yield
    assert not attempts, "A test attempted network access, even if its exception was caught"


@pytest.fixture(autouse=True)
def isolate_runtime_storage(tmp_path_factory, monkeypatch, forbid_network):
    from app.storage.db import Storage

    isolated = Storage(tmp_path_factory.mktemp("runtime") / "runtime.db")
    for name, module in list(sys.modules.items()):
        if name.startswith("app.") and isinstance(getattr(module, "storage", None), Storage):
            monkeypatch.setattr(module, "storage", isolated)