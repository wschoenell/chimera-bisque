# SPDX-FileCopyrightText: 2025-present William Schoenell <wschoenell@gmail.com>
# SPDX-License-Identifier: GPL-2.0-or-later
"""Tests for the pure-socket TheSkyX driver against a fake TCP server.

These exercise the real request/response parsing without any hardware or COM,
so they run on every platform.
"""

import logging
import socketserver
import threading

import pytest

from chimera_bisque.instruments.theskyxdriver import (
    TheSkyXConnectionError,
    TheSkyXDriver,
)


class _FakeSkyXHandler(socketserver.BaseRequestHandler):
    """Emulate the TheSkyX scripting interface for the commands we send.

    Tracking is stateful on the server so IsTracking reflects SetTracking.
    """

    def handle(self):
        data = self.request.recv(4096).decode("utf-8", errors="ignore")
        self.request.sendall(self._respond(data).encode("utf-8"))
        # Keep the connection open until the client shuts down its side, so the
        # driver's shutdown()/close() do not race with the server closing first.
        try:
            self.request.recv(1)
        except OSError:
            pass

    def _respond(self, command: str) -> str:
        server = self.server
        if "SetTracking(1" in command:
            server.tracking = True
        elif "SetTracking(0" in command:
            server.tracking = False

        if "ParkAndDoNotDisconnect" in command:
            # What the real TheSkyX answers when the mount is already parked.
            if server.parked:
                return (
                    "ScriptError: TypeError: the device is parked and must be "
                    "unparked before this operation. Error = 216."
                )
            server.parked = True
        elif "Unpark" in command:
            server.parked = False

        if "GetRaDec" in command:
            # the mount reports the epoch of date; precessing back gives J2000
            return "12.5 45.0" if "PrecessNowTo2000" in command else "12.52 45.15"
        if "IsParked" in command:
            return "true" if server.parked else "false"
        if "IsTracking" in command:
            return "1" if server.tracking else "0"
        if "IsSlewComplete" in command and "SlewToRaDec" not in command:
            return "1"  # slew complete
        if "IsConnected" in command and "Disconnect" in command:
            return "0"
        if "IsConnected" in command:
            return "1"  # connected
        return "undefined"


@pytest.fixture
def skyx_server():
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _FakeSkyXHandler)
    server.tracking = False
    server.parked = False
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def driver(skyx_server):
    host, port = skyx_server.server_address
    return TheSkyXDriver(logging.getLogger("test"), host=host, port=port)


def test_connect_and_disconnect(driver):
    driver.connect()
    assert driver._is_connected is True
    driver.disconnect()
    assert driver._is_connected is False


def test_get_ra_dec(driver):
    driver.connect()
    ra, dec = driver.get_ra_dec()
    # the raw read is the mount's own epoch of date
    assert (ra, dec) == (12.52, 45.15)


def test_get_ra_dec_j2000(driver):
    driver.connect()
    # one round trip: the precession rides along in the same script, because
    # this read is polled
    assert driver.get_ra_dec_j2000() == (12.5, 45.0)


def test_slew_and_completion(driver):
    driver.connect()
    driver.slew_to_ra_dec(10.0, 20.0)
    # fake server reports IsSlewComplete == 1 -> not slewing
    assert driver.is_slewing() is False


def test_slew_j2000_marks_the_mount_as_moving(driver):
    driver.connect()
    driver.slew_to_ra_dec_j2000(10.0, 20.0)
    assert driver._is_slewing is True


def test_sync_and_tracking(driver):
    driver.connect()
    driver.sync_ra_dec(1.0, 2.0)  # should not raise
    driver.sync_ra_dec_j2000(1.0, 2.0)  # should not raise
    driver.start_tracking()
    assert driver.is_tracking() is True
    driver.stop_tracking()
    assert driver.is_tracking() is False


def test_park_unpark(driver):
    driver.connect()
    driver.set_park_position()
    driver.park()
    assert driver.is_parked() is True
    driver.unpark()
    assert driver.is_parked() is False


def test_find_home_requires_connection(driver):
    with pytest.raises(TheSkyXConnectionError):
        driver.find_home()


def test_find_home_marks_the_mount_as_moving(driver):
    driver.connect()
    driver.find_home()
    assert driver._is_slewing is True


def test_park_is_idempotent(driver, skyx_server):
    """Parking an already-parked mount must succeed, not raise Error 216."""
    driver.connect()
    driver.park()

    driver.park()

    assert driver.is_parked() is True


def test_park_survives_losing_the_race_to_another_parker(driver, skyx_server):
    """Error 216 between the state check and the command still means parked."""
    driver.connect()
    # The mount parks after is_parked() has already answered "no", so the
    # check above cannot catch it and the command hits Error 216.
    skyx_server.parked = True
    driver.is_parked = lambda: False

    driver.park()

    assert driver._is_parked is True


def test_is_parked_reads_the_mount_not_a_cached_flag(driver, skyx_server):
    """A mount parked before chimera started must not report itself unparked."""
    skyx_server.parked = True
    driver.connect()

    assert driver._is_parked is False  # nothing has commanded a park
    assert driver.is_parked() is True


def test_commands_require_connection(driver):
    with pytest.raises(TheSkyXConnectionError):
        driver.get_ra_dec()


def test_connection_error_on_dead_port():
    # nothing is listening on this port
    driver = TheSkyXDriver(logging.getLogger("test"), host="127.0.0.1", port=1)
    with pytest.raises(TheSkyXConnectionError):
        driver.connect()
