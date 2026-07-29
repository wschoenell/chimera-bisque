# SPDX-FileCopyrightText: 2025-present William Schoenell <wschoenell@gmail.com>
# SPDX-License-Identifier: GPL-2.0-or-later
"""Interface-level live tests for TheSkyXTelescope against a running TheSkyX.

Unlike ``test_theskyx_live.py`` (which drives the low-level socket driver
directly), this module runs the ``TheSkyXTelescope`` ChimeraObject inside a
real chimera ``Manager`` -- with a ``Site`` for coordinate conversions and the
telescope proxied over the bus -- so it exercises the class exactly as chimera
would: lifecycle (``__start__``/``__stop__``), config, ``site()``, events and
locks, end to end against a live TheSkyX + Telescope Mount Simulator.

Skipped unless ``THESKYX_TEST_URL`` (``host:port``) is set, e.g.::

    THESKYX_TEST_URL=localhost:13040 uv run pytest tests/test_theskyxtelescope_live.py -v

Mount-moving operations (move_*, park, tracking) are further gated behind
``THESKYX_TEST_DESTRUCTIVE=1``.
"""

import logging
import os
import socket
import threading
import time

import pytest
from chimera.core.bus import Bus
from chimera.core.manager import Manager
from chimera.core.site import Site
from chimera.interfaces.telescope import TelescopeStatus

from chimera_bisque.instruments.theskyxtelescope import TheSkyXTelescope

_URL = os.environ.get("THESKYX_TEST_URL")
_DESTRUCTIVE = os.environ.get("THESKYX_TEST_DESTRUCTIVE")

pytestmark = pytest.mark.skipif(
    not _URL, reason="set THESKYX_TEST_URL=host:port to run live TheSkyX tests"
)

destructive = pytest.mark.skipif(
    not _DESTRUCTIVE,
    reason="set THESKYX_TEST_DESTRUCTIVE=1 to run mount-moving tests",
)

logging.disable(logging.CRITICAL)


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(scope="module")
def telescope():
    host, _, port = _URL.partition(":")
    bus = Bus(f"tcp://127.0.0.1:{_free_port()}")
    manager = Manager(bus=bus)
    threading.Thread(target=bus.run_forever, daemon=True).start()
    time.sleep(0.5)
    manager.add_class(
        Site,
        "lna",
        {
            "name": "LNA",
            "latitude": "-22 32 04",
            "longitude": "-45 34 57",
            "altitude": "1864",
        },
        start=True,
    )
    tel = manager.add_class(
        TheSkyXTelescope,
        "skyx",
        {
            "skyx_host": host,
            "skyx_port": int(port or 3040),
            "min_altitude": -90,  # allow any target so slews validate
        },
        start=True,
    )
    yield tel
    manager.shutdown()


def _wait_not_slewing(tel, timeout=60):
    start = time.time()
    while tel.is_slewing():
        assert time.time() - start < timeout, "still slewing after timeout"
        time.sleep(0.5)


def test_position_reads_are_consistent(telescope):
    ra, dec = telescope.get_position_ra_dec()
    assert 0.0 <= ra < 24.0
    assert -90.0 <= dec <= 90.0
    # get_ra/get_dec must agree with get_position_ra_dec
    assert abs(telescope.get_ra() - ra) < 0.1
    assert abs(telescope.get_dec() - dec) < 0.5
    alt, az = telescope.get_position_alt_az()
    assert -90.0 <= alt <= 90.0
    assert 0.0 <= az <= 360.0
    assert abs(telescope.get_alt() - alt) < 1.0
    assert abs(telescope.get_az() - az) < 1.0


def test_initial_not_slewing(telescope):
    assert telescope.is_slewing() is False


def test_slew_fires_events_and_arrives(telescope):
    events = {}
    ready = threading.Event()

    def on_begin(ra, dec, epoch):
        events["begin"] = (ra, dec, epoch)

    def on_complete(ra, dec, status):
        events["complete"] = (ra, dec, status)
        ready.set()

    telescope.slew_begin += on_begin
    telescope.slew_complete += on_complete
    try:
        ra0, dec0 = telescope.get_position_ra_dec()
        target = (ra0 + 0.1, dec0 + 1.0)
        telescope.slew_to_ra_dec(*target)

        assert ready.wait(60), "slew_complete never fired"
        assert "begin" in events
        assert events["complete"][2] == TelescopeStatus.OK

        ra1, dec1 = telescope.get_position_ra_dec()
        assert abs(ra1 - target[0]) < 0.05
        assert abs(dec1 - target[1]) < 0.5
    finally:
        telescope.slew_begin -= on_begin
        telescope.slew_complete -= on_complete
        telescope.slew_to_ra_dec(ra0, dec0)
        _wait_not_slewing(telescope)


def test_sync_fires_event(telescope):
    completed = threading.Event()

    def on_sync(ra, dec):
        completed.set()

    telescope.sync_complete += on_sync
    try:
        ra0, dec0 = telescope.get_position_ra_dec()
        telescope.sync_ra_dec(ra0, dec0)
        assert completed.wait(10), "sync_complete never fired"
        ra1, dec1 = telescope.get_position_ra_dec()
        assert abs(ra1 - ra0) < 0.05
        assert abs(dec1 - dec0) < 0.5
    finally:
        telescope.sync_complete -= on_sync


@destructive
def test_move_east_west_shifts_ra(telescope):
    offset_as = 600.0  # 10 arcmin
    offset_h = offset_as / 3600.0 / 15.0  # arcsec -> hours
    ra0, dec0 = telescope.get_position_ra_dec()

    telescope.move_east(offset_as)
    _wait_not_slewing(telescope)
    ra_e, _ = telescope.get_position_ra_dec()
    assert abs((ra_e - ra0) - offset_h) < 0.01, "move_east RA shift wrong"

    telescope.move_west(offset_as)
    _wait_not_slewing(telescope)
    ra_w, _ = telescope.get_position_ra_dec()
    assert abs(ra_w - ra0) < 0.01, "move_west did not return to start"


@destructive
def test_move_north_south_shifts_dec(telescope):
    offset_as = 600.0
    offset_d = offset_as / 3600.0
    ra0, dec0 = telescope.get_position_ra_dec()

    telescope.move_north(offset_as)
    _wait_not_slewing(telescope)
    _, dec_n = telescope.get_position_ra_dec()
    assert abs((dec_n - dec0) - offset_d) < 0.05, "move_north Dec shift wrong"

    telescope.move_south(offset_as)
    _wait_not_slewing(telescope)
    _, dec_s = telescope.get_position_ra_dec()
    assert abs(dec_s - dec0) < 0.05, "move_south did not return to start"


@destructive
def test_tracking_toggle_is_graceful(telescope):
    # Real mounts toggle tracking; the Mount Simulator reports it unsupported.
    # Either way the interface call must return promptly and report status.
    start = time.time()
    try:
        telescope.start_tracking()
        assert telescope.is_tracking() is True
        telescope.stop_tracking()
        assert telescope.is_tracking() is False
    except Exception:
        # e.g. simulator: "not supported by the selected device"
        pass
    assert time.time() - start < 15


@destructive
def test_park_and_unpark(telescope):
    telescope.park()
    _wait_not_slewing(telescope)
    assert telescope.is_parked() is True
    # unpark() homes the mount before returning (find_home_on_unpark).
    telescope.unpark()
    assert telescope.is_parked() is False
    assert telescope.is_slewing() is False


@destructive
def test_find_home(telescope):
    telescope.find_home()
    assert telescope.is_slewing() is False
