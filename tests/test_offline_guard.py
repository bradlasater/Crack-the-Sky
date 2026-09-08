"""The conftest offline guard must block both connect flavours, and only IP.

``connect`` was the only patched entry point; a client using ``connect_ex``
(the errno-returning variant, used by e.g. ``http.client`` probes) would have
slipped past it. And the old string check read an AF_UNIX path as a host,
falsely rejecting local sockets.
"""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

OUTBOUND = ("203.0.113.1", 443)  # TEST-NET-3, unroutable by definition

# pytest loads the root conftest as top-level module ``conftest`` while a
# plain import resolves it as ``tests.conftest`` — two module objects, two
# OfflineTestViolation classes. Match the guard's message on the
# AssertionError base instead of a class identity that depends on import order.
GUARD = pytest.raises(AssertionError, match="outbound connection")


def test_connect_to_public_ip_is_blocked() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s, GUARD:
        s.connect(OUTBOUND)


def test_connect_ex_to_public_ip_is_blocked() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s, GUARD:
        s.connect_ex(OUTBOUND)


def test_localhost_connect_ex_passes_the_guard() -> None:
    """Loopback is allowed through; bind a listener so the connect succeeds.

    Pointing at port 1 assumed nothing listens there and that the OS answers
    exactly ECONNREFUSED — both vary with the machine or sandbox policy, so
    a box could fail this test without the guard being broken. Binding port
    0 gets a guaranteed-free loopback port and makes the allowed path
    deterministic: connect_ex returns 0.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            err = s.connect_ex(("127.0.0.1", port))
    assert err == 0


def test_unix_socket_path_is_not_misread_as_a_host(tmp_path: Path) -> None:
    """An AF_UNIX string address is a filesystem path, not an outbound host."""
    path = str(tmp_path / "local.sock")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(path)
        server.listen(1)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(path)  # would have raised OfflineTestViolation
            conn, _ = server.accept()
            conn.close()
