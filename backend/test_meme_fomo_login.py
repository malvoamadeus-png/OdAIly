from __future__ import annotations

import socket
from pathlib import Path

import pytest

from packages.meme_scanner import fomo_login


def test_fomo_login_command_binds_devtools_only_to_loopback(tmp_path):
    command = fomo_login.chromium_command(
        executable="/opt/chromium",
        profile_dir=Path(tmp_path) / "profile",
        cdp_port=9224,
    )

    assert "--remote-debugging-address=127.0.0.1" in command
    assert "--remote-debugging-port=9224" in command
    assert all("0.0.0.0" not in argument for argument in command)
    assert f"--user-data-dir={Path(tmp_path) / 'profile'}" in command


def test_fomo_login_rejects_privileged_or_invalid_cdp_ports():
    for port in (0, 1023, 65536):
        try:
            fomo_login._validated_port(port)
        except ValueError:
            continue
        raise AssertionError(f"port {port} should be rejected")


def test_fomo_login_refuses_an_already_bound_loopback_devtools_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        occupied_port = int(listener.getsockname()[1])
        with pytest.raises(RuntimeError, match="already in use"):
            fomo_login._assert_loopback_port_available(occupied_port)
