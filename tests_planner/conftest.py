"""Offline guard for v1 planner tests (R12): no non-localhost sockets, no hub downloads."""

import os
import socket

import pytest

os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["HF_DATASETS_OFFLINE"] = "1"
_ALLOWED = {"127.0.0.1", "::1", "localhost"}
_real_connect = socket.socket.connect


def _guarded_connect(self: socket.socket, address: object) -> object:
    host = address[0] if isinstance(address, tuple) else address
    if isinstance(host, str) and host not in _ALLOWED and self.family != socket.AF_UNIX:
        raise RuntimeError(f"network access blocked in tests: {host}")
    return _real_connect(self, address)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket.socket, "connect", _guarded_connect)


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "models: needs locally downloaded model files")
