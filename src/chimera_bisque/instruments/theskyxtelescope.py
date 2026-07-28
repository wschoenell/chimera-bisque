# SPDX-FileCopyrightText: 2025-present William Schoenell <wschoenell@gmail.com>
# SPDX-License-Identifier: GPL-2.0-or-later
"""Chimera telescope driver for TheSkyX over its TCP/IP scripting interface."""

import threading
import time

from chimera.core.lock import lock
from chimera.instruments.telescope import TelescopeBase
from chimera.interfaces.telescope import TelescopeStatus
from chimera.util.coord import Coord
from chimera.util.position import Position

from chimera_bisque.instruments.theskyxdriver import (
    TheSkyXCommandError,
    TheSkyXConnectionError,
    TheSkyXDriver,
)


class TheSkyXTelescope(TelescopeBase):
    __config__ = {
        "skyx_host": "localhost",
        "skyx_port": 3040,
        "max_slew_time_sec": 300,
        "poll_interval_sec": 0.1,
        "min_altitude": -90,
        # A Paramount loses its pointing model reference across a power cycle,
        # so home it as part of waking up. Turn off for mounts whose driver
        # does not implement FindHome.
        "find_home_on_unpark": True,
        "max_find_home_time_sec": 300,
    }

    def __init__(self):
        super().__init__()
        self._driver: TheSkyXDriver | None = None
        self._abort = threading.Event()

    @lock
    def __start__(self):
        self.log.info("Starting TheSkyX telescope instrument")

        try:
            self._driver = TheSkyXDriver(
                self.log,
                host=self["skyx_host"],
                port=int(self["skyx_port"]),
            )
            self._driver.connect()
            self.log.info("Connected TheSkyX successfully")

        except TheSkyXConnectionError as e:
            self.log.error(f"Failed to connect to TheSkyX: {e}")
            raise

    @lock
    def __stop__(self):
        self.log.info("Stopping TheSkyX telescope instrument")
        if self._driver:
            try:
                self._driver.disconnect()
            except Exception as e:
                self.log.warning(f"Error disconnecting from TheSkyX: {e}")

    @lock
    def slew_to_ra_dec(self, ra: float, dec: float, epoch: float = 2000) -> None:
        """Slew telescope to target RA/Dec coordinates.

        Args:
            ra: Right Ascension in hours (0-24)
            dec: Declination in degrees (-90 to +90)
            epoch: Epoch of coordinates (default: J2000)

        Raises:
            ObjectTooLowException: If target is below minimum altitude
            RuntimeError: If slew fails or times out
        """
        if self._driver is None:
            raise RuntimeError("Telescope not initialized")

        if epoch != 2000.0:
            raise NotImplementedError(f"Only J2000 epoch is supported. Got: {epoch}")

        self._validate_ra_dec(ra, dec)
        self._slew(ra, dec, epoch, self._driver.slew_to_ra_dec_j2000)

    def _slew(self, ra: float, dec: float, epoch: float, slew_command) -> None:
        """Issue a slew and poll it to completion.

        ``slew_command`` selects the epoch the mount is commanded in: J2000
        targets are precessed by TheSkyX, Alt/Az targets are already in the
        epoch of date and must not be precessed again.
        """
        self.slew_begin(ra, dec, epoch)
        self._abort.clear()

        try:
            slew_command(ra, dec)

            # Poll for slew completion
            start_time = time.time()
            max_slew_time_sec = float(self["max_slew_time_sec"])
            poll_interval_sec = float(self["poll_interval_sec"])

            while True:
                if self._abort.is_set():
                    self._driver.abort_slew()
                    self.slew_complete(ra, dec, TelescopeStatus.ABORTED)
                    return

                elapsed = time.time() - start_time
                if elapsed > max_slew_time_sec:
                    self._driver.abort_slew()
                    raise RuntimeError(
                        f"Slew timeout: took longer than {max_slew_time_sec}s"
                    )

                if not self._driver.is_slewing():
                    self.slew_complete(ra, dec, TelescopeStatus.OK)
                    return

                # Block on the abort event instead of time.sleep(): an
                # abort_slew() breaks the poll at once rather than after a
                # whole tick (astroufsc/chimera#255 applies the same idiom to
                # the scheduler's pre-slew wait).
                self._abort.wait(poll_interval_sec)

        except TheSkyXCommandError as e:
            self.slew_complete(ra, dec, TelescopeStatus.ERROR)
            raise RuntimeError(f"Slew failed: {e}")

    def _get_site(self):
        """The site object, whichever accessor this core provides.

        astroufsc/chimera#271 replaced ``TelescopeBase.site()`` with the
        manager-injected ``ChimeraObject.get_site()``. Support both so the
        driver runs on cores from either side of that change.
        """
        get_site = getattr(self, "get_site", None)
        if get_site is not None:
            return get_site()
        return self.site()

    @lock
    def slew_to_alt_az(self, alt: float, az: float) -> None:
        """Slew telescope to target Alt/Az coordinates.

        This is implemented by converting Alt/Az to RA/Dec and calling slew_to_ra_dec.

        Args:
            alt: Altitude in degrees (0-90)
            az: Azimuth in degrees (0-360)

        Raises:
            ObjectTooLowException: If altitude is below minimum
            RuntimeError: If conversion or slew fails
        """
        self._validate_alt_az(alt, az)

        site = self._get_site()
        ra, dec = site.alt_az_to_ra_dec(alt, az)

        # alt_az_to_ra_dec works off the local sidereal time, so its RA/Dec is
        # already in the epoch of date -- precessing it as if it were J2000
        # would put the mount 20' off a target that is by definition where the
        # telescope is pointing.
        self._slew(ra, dec, 2000, self._driver.slew_to_ra_dec)
        # An Alt/Az target is fixed to the horizon, so sidereal tracking must be
        # off. TheSkyX turns tracking on when the (RA/Dec) slew finishes, so
        # force it back off unconditionally. RA/Dec slews deliberately leave
        # tracking on.
        self._force_tracking_off()

    def _force_tracking_off(self) -> None:
        """Turn sidereal tracking off after an Alt/Az slew.

        Best effort: a failure here (e.g. a mount that rejects SetTracking)
        must not fail the slew itself.
        """
        try:
            self._driver.stop_tracking()
            self.tracking_stopped(TelescopeStatus.OK)
        except TheSkyXCommandError as e:
            self.log.warning(f"Could not disable tracking after Alt/Az slew: {e}")

    def abort_slew(self) -> None:
        """Abort any in-progress slew."""
        self._abort.set()

    def is_slewing(self) -> bool:
        """Check if telescope is currently slewing."""
        if self._driver is None:
            return False
        try:
            return self._driver.is_slewing()
        except TheSkyXConnectionError:
            return False

    def get_position_ra_dec(self) -> tuple[float, float]:
        """Get current telescope RA/Dec position, in J2000.

        chimera writes this straight into the RA/DEC header cards under
        EQUINOX = 2000.0, so it must be J2000 and not the epoch-of-date
        coordinates TheSkyX reports.

        Returns:
            (ra_hours, dec_degrees): Current position
        """
        if self._driver is None:
            raise RuntimeError("Telescope not initialized")
        try:
            return self._driver.get_ra_dec_j2000()
        except TheSkyXCommandError as e:
            raise RuntimeError(f"Failed to get position: {e}")

    def get_position_alt_az(self) -> tuple[float, float]:
        """Get current telescope Alt/Az position.

        Returns:
            (alt_degrees, az_degrees): Current altitude and azimuth
        """
        if self._driver is None:
            raise RuntimeError("Telescope not initialized")
        # The hour-angle conversion needs coordinates in the epoch of date, so
        # read the mount's own numbers rather than the J2000 ones.
        try:
            ra, dec = self._driver.get_ra_dec()
        except TheSkyXCommandError as e:
            raise RuntimeError(f"Failed to get position: {e}")
        site = self._get_site()
        alt, az = site.ra_dec_to_alt_az(ra, dec)
        return alt, az

    def get_ra(self) -> float:
        return self.get_position_ra_dec()[0]

    def get_dec(self) -> float:
        return self.get_position_ra_dec()[1]

    def get_alt(self) -> float:
        return self.get_position_alt_az()[0]

    def get_az(self) -> float:
        return self.get_position_alt_az()[1]

    @lock
    def sync_ra_dec(self, ra: float, dec: float, epoch: float = 2000) -> None:
        """Sync telescope to current position (calibration).

        This tells the telescope "I'm at this RA/Dec right now" to correct
        tracking or alignment errors.

        Args:
            ra: Right Ascension in hours (0-24)
            dec: Declination in degrees (-90 to +90)
            epoch: Epoch of coordinates (default: J2000)
        """
        if self._driver is None:
            raise RuntimeError("Telescope not initialized")

        if epoch != 2000.0:
            raise NotImplementedError(f"Only J2000 epoch is supported. Got: {epoch}")

        try:
            self._driver.sync_ra_dec_j2000(ra, dec)
            self.sync_complete(ra, dec)
        except TheSkyXCommandError as e:
            raise RuntimeError(f"Sync failed: {e}")

    @lock
    def move_east(self, offset: float, rate=None) -> None:
        # offset is in arcseconds; float() is required because "float + Coord"
        # does not add hours correctly.
        ra, dec = self.get_position_ra_dec()
        new_ra = ra + float(Coord.from_as(offset).to_h())
        self.slew_to_ra_dec(new_ra, dec)

    @lock
    def move_west(self, offset: float, rate=None) -> None:
        ra, dec = self.get_position_ra_dec()
        new_ra = ra - float(Coord.from_as(offset).to_h())
        self.slew_to_ra_dec(new_ra, dec)

    @lock
    def move_north(self, offset: float, rate=None) -> None:
        ra, dec = self.get_position_ra_dec()
        new_dec = dec + float(Coord.from_as(offset).to_d())
        self.slew_to_ra_dec(ra, new_dec)

    @lock
    def move_south(self, offset: float, rate=None) -> None:
        ra, dec = self.get_position_ra_dec()
        new_dec = dec - float(Coord.from_as(offset).to_d())
        self.slew_to_ra_dec(ra, new_dec)

    @lock
    def start_tracking(self) -> None:
        """Start telescope tracking."""
        if self._driver is None:
            raise RuntimeError("Telescope not initialized")

        try:
            self._driver.start_tracking()
            self.tracking_started()
        except TheSkyXCommandError as e:
            raise RuntimeError(f"Failed to start tracking: {e}")

    @lock
    def stop_tracking(self) -> None:
        """Stop telescope tracking."""
        if self._driver is None:
            raise RuntimeError("Telescope not initialized")

        try:
            self._driver.stop_tracking()
            self.tracking_stopped(TelescopeStatus.OK)
        except TheSkyXCommandError as e:
            raise RuntimeError(f"Failed to stop tracking: {e}")

    def is_tracking(self) -> bool:
        """Check if telescope is tracking.

        Returns:
            True if tracking, False otherwise
        """
        if self._driver is None:
            return False

        return self._driver.is_tracking()

    @lock
    def set_park_position(self, position: Position):
        raise NotImplementedError(
            "Parking position is to be set manually in TheSkyX GUI"
        )

    @lock
    def park(self) -> None:
        """Park the telescope."""
        if self._driver is None:
            raise RuntimeError("Telescope not initialized")

        try:
            self._driver.park()
            self.park_complete()
        except TheSkyXCommandError as e:
            raise RuntimeError(f"Failed to park telescope: {e}")

    @lock
    def unpark(self) -> None:
        """Unpark the telescope, homing it first when configured to."""
        if self._driver is None:
            raise RuntimeError("Telescope not initialized")

        try:
            self._driver.unpark()
        except TheSkyXCommandError as e:
            raise RuntimeError(f"Failed to unpark telescope: {e}")

        if self["find_home_on_unpark"]:
            self._find_home()

        self.unpark_complete()

    @lock
    def find_home(self) -> None:
        """Send the mount to its mechanical home position and wait for it."""
        if self._driver is None:
            raise RuntimeError("Telescope not initialized")

        self._find_home()

    def _find_home(self) -> None:
        """Home the mount, blocking until it gets there.

        Homing is the same kind of motion as a slew, so it is polled the same
        way and honours abort_slew().
        """
        max_find_home_time_sec = float(self["max_find_home_time_sec"])
        poll_interval_sec = float(self["poll_interval_sec"])

        self._abort.clear()
        self.log.info("Homing telescope")

        try:
            # The mount may hold the script for the whole homing run, so let
            # the socket wait as long as the homing itself is allowed to take.
            self._driver.find_home(timeout=max_find_home_time_sec)

            start_time = time.time()
            while self._driver.is_slewing():
                # Wait on the abort event rather than sleeping through the
                # tick, so abort_slew() releases the poll immediately.
                if self._abort.wait(poll_interval_sec):
                    self._driver.abort_slew()
                    self.log.warning("Homing aborted")
                    return

                if time.time() - start_time > max_find_home_time_sec:
                    self._driver.abort_slew()
                    raise RuntimeError(
                        f"Homing timeout: took longer than {max_find_home_time_sec}s"
                    )

        except TheSkyXCommandError as e:
            raise RuntimeError(f"Failed to home telescope: {e}")

        self.log.info("Telescope homed")

    def is_parked(self) -> bool:
        """Check if telescope is parked.

        Returns:
            True if parked, False otherwise
        """
        if self._driver is None:
            return False

        return self._driver.is_parked()
