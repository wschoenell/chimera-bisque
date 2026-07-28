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
import time

import pytest
from chimera.core.site import Site

from chimera_bisque.instruments.theskyxtelescope import TheSkyXTelescope

SITE_CONFIG = {
    "name": "LNA",
    "latitude": "-22 32 03",
    "longitude": "-45 34 57",
    "altitude": "1864",
}

# The same sky position in both epochs, offset by roughly the 2026 precession
# so a test can tell which one a code path used.
MOUNT_RA_DEC_NOW = (12.52, 45.15)
MOUNT_RA_DEC_J2000 = (12.5, 45.0)


class _FakeSkyXHandler(socketserver.BaseRequestHandler):
    """Emulate the subset of the TheSkyX scripting interface we exercise.

    Tracking is stateful: an RA/Dec slew turns it on (as the real TheSkyX
    does), SetTracking sets it explicitly, and IsTracking reports it back.

    The mount sits at MOUNT_RA_DEC_NOW, in the epoch of date as the real
    sky6RASCOMTele reports it; a read that precesses back to J2000 gets
    MOUNT_RA_DEC_J2000 instead, so the tests can tell the two apart.
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
            ra, dec = (
                MOUNT_RA_DEC_J2000
                if "PrecessNowTo2000" in command
                else MOUNT_RA_DEC_NOW
            )
            return f"{ra} {dec}"
        if "IsTracking" in command:
            return "1" if server.tracking else "0"
        if "IsSlewComplete" in command:
            return "1" if server.slew_complete else "0"
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
    server.slew_complete = True
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


def test_unpark_homes_the_mount(telescope, skyx_server):
    # A Paramount coming out of park has no valid pointing reference until it
    # has found home, so waking it up must include the homing run.
    telescope.unpark()
    assert any("FindHome" in script for script in skyx_server.scripts)


def test_unpark_can_skip_homing(manager, skyx_server):
    host, port = skyx_server.server_address
    telescope = manager.add_class(
        TheSkyXTelescope,
        "skyx-nohome",
        config={
            "skyx_host": host,
            "skyx_port": port,
            "poll_interval_sec": 0.01,
            "find_home_on_unpark": False,
        },
    )
    telescope.unpark()
    assert not any("FindHome" in script for script in skyx_server.scripts)


def test_find_home_can_be_called_directly(telescope, skyx_server):
    telescope.find_home()
    assert any("FindHome" in script for script in skyx_server.scripts)


def test_homing_abort_does_not_wait_out_the_poll_interval(manager, skyx_server):
    # The poll blocks on the abort event, so abort_slew() lands at once. With
    # a time.sleep() in the loop it would only be seen a whole tick later --
    # 30 s here (astroufsc/chimera#255 hit the same thing in the scheduler).
    skyx_server.slew_complete = False  # homing never finishes on its own
    host, port = skyx_server.server_address
    telescope = manager.add_class(
        TheSkyXTelescope,
        "skyx-abort",
        config={"skyx_host": host, "skyx_port": port, "poll_interval_sec": 30},
    )
    threading.Timer(0.2, telescope.abort_slew).start()

    start = time.time()
    telescope.find_home()
    assert time.time() - start < 10


def _last_script_with(server, needle):
    matches = [script for script in server.scripts if needle in script]
    assert matches, f"no script containing {needle!r} was sent to TheSkyX"
    return matches[-1]


def test_ra_dec_slew_precesses_j2000_target_to_epoch_of_date(telescope, skyx_server):
    # SlewToRaDec takes coordinates "for the current epoch"; handing it J2000
    # left every target 10-30' off (lna40 PENDING_ISSUES #51). TheSkyX does the
    # conversion, so the slew must be commanded from the precessed values and
    # not from the J2000 literals.
    telescope.slew_to_ra_dec(12.0, -30.0)

    script = _last_script_with(skyx_server, "SlewToRaDec")
    assert "sky6Utils.Precess2000ToNow(12.0, -30.0)" in script
    assert "SlewToRaDec(raNow, decNow" in script
    assert "SlewToRaDec(12.0, -30.0" not in script


def test_alt_az_slew_is_not_precessed(telescope, skyx_server):
    # alt_az_to_ra_dec already returns epoch-of-date coordinates (it works off
    # the local sidereal time), so precessing them again would reintroduce the
    # very error #51 is about, pointing 20' from the requested horizon spot.
    telescope.slew_to_alt_az(45.0, 180.0)

    script = _last_script_with(skyx_server, "SlewToRaDec")
    assert "Precess2000ToNow" not in script


def test_position_is_reported_in_j2000(telescope, skyx_server):
    # chimera writes this into RA/DEC under EQUINOX = 2000.0.
    assert telescope.get_position_ra_dec() == MOUNT_RA_DEC_J2000
    assert "PrecessNowTo2000" in _last_script_with(skyx_server, "GetRaDec")


def test_alt_az_position_uses_epoch_of_date(telescope, skyx_server):
    # The hour-angle conversion is only valid in the mount's own epoch, so this
    # read must NOT be precessed back to J2000.
    telescope.get_position_alt_az()
    assert "PrecessNowTo2000" not in _last_script_with(skyx_server, "GetRaDec")


def test_sync_precesses_j2000_target(telescope, skyx_server):
    # Sync writes the mount model: an un-precessed sync bakes the error in.
    telescope.sync_ra_dec(12.0, -30.0)

    script = _last_script_with(skyx_server, "Sync")
    assert "sky6Utils.Precess2000ToNow(12.0, -30.0)" in script
    assert "Sync(sky6Utils.dOut0, sky6Utils.dOut1" in script


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
