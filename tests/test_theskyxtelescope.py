# SPDX-FileCopyrightText: 2025-present William Schoenell <wschoenell@gmail.com>
# SPDX-License-Identifier: GPL-2.0-or-later
"""Instrument-level tests for TheSkyXTelescope against a fake TheSkyX server.

The fake server emulates the one behaviour that drove these tests: TheSkyX
turns sidereal tracking ON by itself when an RA/Dec slew finishes. That means

  * ``is_tracking()`` must report the mount's *live* state, not a cached "last
    commanded" flag (otherwise ``chimera-tel --info`` shows "disabled" right
    after a slew while the mount is actually tracking), and
  * an Alt/Az slew - whose target is fixed to the horizon - must force tracking
    back off, while an RA/Dec slew must leave the auto-started tracking on.
"""

import socketserver
import threading

import pytest
from chimera.core.site import Site

from chimera_bisque.instruments.theskyxtelescope import TheSkyXTelescope

SITE_CONFIG = {
    "name": "LNA",
    "latitude": "-22 32 03",
    "longitude": "-45 34 57",
    "altitude": "1864",
}


class _FakeSkyXHandler(socketserver.BaseRequestHandler):
    """Emulate the subset of the TheSkyX scripting interface we exercise.

    Tracking is stateful: an RA/Dec slew turns it on (as the real TheSkyX
    does), SetTracking sets it explicitly, and IsTracking reports it back.
    """

    def handle(self):
        data = self.request.recv(4096).decode("utf-8", errors="ignore")
        self.server.scripts.append(data)
        self.request.sendall(self._respond(data).encode("utf-8"))
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

        if "SlewToRaDec" in command:
            # TheSkyX starts sidereal tracking once the slew completes.
            server.tracking = True
            return "undefined"
        if "GetRaDec" in command:
            return "12.5 45.0"
        if "IsTracking" in command:
            return "1" if server.tracking else "0"
        if "IsSlewComplete" in command:
            return "1"  # slew complete
        if "IsConnected" in command and "Disconnect" in command:
            return "0"
        if "IsConnected" in command:
            return "1"  # connected
        return "undefined"


@pytest.fixture
def skyx_server():
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _FakeSkyXHandler)
    server.scripts = []
    server.tracking = False
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def telescope(manager, skyx_server):
    host, port = skyx_server.server_address
    manager.add_class(Site, "lna", config=SITE_CONFIG)
    yield manager.add_class(
        TheSkyXTelescope,
        "skyx",
        config={"skyx_host": host, "skyx_port": port, "poll_interval_sec": 0.01},
    )


def _scripts_after_slew(server):
    slew_indexes = [
        i for i, script in enumerate(server.scripts) if "SlewToRaDec" in script
    ]
    assert slew_indexes, "no slew command was sent to TheSkyX"
    return server.scripts[slew_indexes[-1] + 1 :]


def test_is_tracking_reports_live_state_after_ra_dec_slew(telescope, skyx_server):
    # The real bug: the mount auto-tracks after an RA/Dec slew, and --info must
    # reflect that rather than a stale cached flag.
    telescope.slew_to_ra_dec(12.0, 44.0)
    assert telescope.is_tracking() is True


def test_ra_dec_slew_leaves_tracking_on(telescope, skyx_server):
    telescope.slew_to_ra_dec(12.0, 44.0)
    assert not any(
        "SetTracking(0" in script for script in _scripts_after_slew(skyx_server)
    ), "tracking was disabled after an RA/Dec slew"


def test_alt_az_slew_forces_tracking_off(telescope, skyx_server):
    telescope.slew_to_alt_az(45.0, 180.0)
    assert any(
        "SetTracking(0, 1, 0, 0)" in script
        for script in _scripts_after_slew(skyx_server)
    ), "tracking was not forced off after an Alt/Az slew"
    assert telescope.is_tracking() is False


def test_offset_leaves_tracking_on(telescope, skyx_server):
    # Offsets nudge an already-tracked target, so tracking stays on.
    telescope.move_east(30.0)
    assert not any(
        "SetTracking(0" in script for script in _scripts_after_slew(skyx_server)
    ), "tracking was disabled after an offset"


def test_stop_tracking_reflected_by_is_tracking(telescope, skyx_server):
    telescope.slew_to_ra_dec(12.0, 44.0)
    assert telescope.is_tracking() is True
    telescope.stop_tracking()
    assert telescope.is_tracking() is False


def test_get_site_shim_works_on_cores_from_both_sides_of_271():
    """astroufsc/chimera#271 replaced TelescopeBase.site() with the
    manager-injected ChimeraObject.get_site(). Deploying that core broke
    every alt/az conversion on opd-40 (2026-07-28: `AttributeError:
    'TheSkyXTelescope' object has no attribute 'site'` out of get_az, which
    the dome lookup calls). The driver must run on either core."""
    from chimera_bisque.instruments.theskyxtelescope import TheSkyXTelescope

    class NewCore(TheSkyXTelescope):
        def __init__(self):
            pass

        def get_site(self):
            return "injected-site"

    class OldCore(TheSkyXTelescope):
        def __init__(self):
            pass

        def site(self):
            return "proxy-site"

    assert TheSkyXTelescope._get_site(NewCore()) == "injected-site"
    assert TheSkyXTelescope._get_site(OldCore()) == "proxy-site"
