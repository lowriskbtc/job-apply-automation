"""Tests exercise only pure functions. The network is blocked so nothing can reach a real site."""
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _blocked(*args, **kwargs):
    raise RuntimeError("Network access is blocked in tests")


socket.socket.connect = _blocked
socket.create_connection = _blocked
