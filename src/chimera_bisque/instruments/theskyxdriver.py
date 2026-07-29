# SPDX-FileCopyrightText: 2025-present William Schoenell <wschoenell@gmail.com>
# SPDX-License-Identifier: GPL-2.0-or-later
"""Pure-socket driver for the TheSkyX TCP/IP JavaScript scripting interface."""

import logging
import time
from socket import AF_INET, SHUT_RDWR, SOCK_STREAM, socket


class TheSkyXConnectionError(Exception):
    pass


class TheSkyXCommandError(Exception):
    pass


class TheSkyXDriver:
    def __init__(
        self,
        logger: logging.Logger,
        host: str = "localhost",
        port: int = 3040,
        timeout: float = 15.0,
    ):
        self.log = logger
        self.host = host
        self.port = port
        self.timeout = timeout
        # How long to keep retrying a command that TheSkyX rejects with
        # "Another script is running" (it serialises socket scripts and can
        # stay busy for a second or two after a slew).
        self._busy_timeout = 30.0
        self._busy_retry_delay = 0.3
        self._is_connected = False
        self._is_tracking = False
        self._is_parked = False
        self._is_slewing = False

    def _send_command(self, javascript: str, timeout: float | None = None) -> str:
        #  The try/catch is essential: an *uncaught* script exception (e.g.
        #  "TypeError: Process aborted. Error = 212." from IsSlewComplete right
        #  after Abort()) drops TheSkyX into its interactive Qt Script debugger,
        #  which blocks the script engine until someone dismisses it in the GUI.
        #  Catching it turns the exception into a normal response instead.
        #  The packet markers are used to not break between network packets:
        #  https://www.bisque.com/wp-content/scriptthesky/script_over_socket.html
        command = (
            "/* Java Script */\n"
            "/* Socket Start Packet */\n"
            "var Out;\n"
            "try {\n"
            f"{javascript}\n"
            '} catch (e) { Out = "ScriptError: " + e; }\n'
            "/* Socket End Packet */\n"
        )

        # TheSkyX runs one socket script at a time. A command sent while the
        # previous one is still finalizing is rejected with "Another script is
        # running"; keep retrying (bounded) until the engine frees up.
        deadline = time.monotonic() + self._busy_timeout
        while True:
            result = self._send_once(command, timeout=timeout)
            if self._is_busy(result) and time.monotonic() < deadline:
                self.log.debug("TheSkyX busy, retrying...")
                time.sleep(self._busy_retry_delay)
                continue
            break

        if result.startswith("ScriptError:") or "error" in result.lower():
            raise TheSkyXCommandError(f"TheSkyX error response: {result}")

        return result

    @staticmethod
    def _is_busy(result: str) -> bool:
        low = result.lower()
        return result == "NG" or "another script is running" in low

    def _send_once(self, command: str, timeout: float | None = None) -> str:
        # Per-command override for the few scripts (homing) that legitimately
        # take longer to answer than the default round-trip timeout.
        timeout = self.timeout if timeout is None else timeout
        try:
            with socket(AF_INET, SOCK_STREAM) as sock:
                # A timeout is essential: without it an unresponsive or
                # restarted TheSkyX would make recv() block forever.
                sock.settimeout(timeout)
                sock.connect((self.host, self.port))
                sock.sendall(command.encode("utf-8"))
                response = sock.recv(2048).decode("utf-8", errors="ignore")
                # Best-effort: TheSkyX often closes its side as soon as it has
                # replied, so shutdown() may raise ENOTCONN -- harmless here.
                try:
                    sock.shutdown(SHUT_RDWR)
                except OSError:
                    pass
        except TimeoutError as e:
            raise TheSkyXConnectionError(
                f"Timed out talking to TheSkyX at {self.host}:{self.port} "
                f"after {timeout}s: {e}"
            )
        except OSError as e:
            raise TheSkyXConnectionError(
                f"Failed to connect to TheSkyX at {self.host}:{self.port}: {e}"
            )

        result = response.split("|")[0].strip()
        # One concise line per round-trip (repr keeps it single-line). The
        # position/slew/tracking pollers hit this constantly, so avoid dumping
        # the raw framed response and a second parsed line on top of it.
        self.log.debug(f"TheSkyX -> {result!r}")
        return result

    def connect(self) -> None:
        """Connect to TheSkyX telescope.

        Raises:
            TheSkyXConnectionError: If connection fails
            TheSkyXCommandError: If telescope is not available
        """
        self.log.info(f"Connecting to TheSkyX at {self.host}:{self.port}")
        try:
            # Asynchronous = 1 is required so SlewToRaDec returns immediately and
            # we can poll IsSlewComplete; a synchronous slew would block the
            # socket command for the whole slew (and hang on a bad target).
            command = """
                var Out;
                sky6RASCOMTele.Connect();
                sky6RASCOMTele.Asynchronous = 1;
                Out = sky6RASCOMTele.IsConnected;
            """
            result = self._send_command(command)

            if int(result) != 1:
                raise TheSkyXCommandError(
                    f"Telescope connection failed. IsConnected={result}"
                )

            self._is_connected = True
            self.log.info("Connected to TheSkyX telescope")
        except TheSkyXConnectionError:
            raise
        except (ValueError, TheSkyXCommandError) as e:
            self._is_connected = False
            raise TheSkyXCommandError(f"Failed to connect to telescope: {e}")

    def disconnect(self) -> None:
        """Disconnect from TheSkyX telescope."""
        if not self._is_connected:
            return

        try:
            command = """
                var Out;
                sky6RASCOMTele.Disconnect();
                Out = sky6RASCOMTele.IsConnected;
            """
            self._send_command(command)
            self._is_connected = False
            self.log.info("Disconnected from TheSkyX telescope")
        except Exception as e:
            self.log.error(f"Error disconnecting from TheSkyX: {e}")

    def get_ra_dec(self) -> tuple[float, float]:
        """Get current telescope RA and Dec"""
        if not self._is_connected:
            raise TheSkyXConnectionError("Not connected to TheSkyX")

        try:
            command = """
                var Out;
                sky6RASCOMTele.GetRaDec();
                Out = String(sky6RASCOMTele.dRa) + " " + String(sky6RASCOMTele.dDec);
            """
            result = self._send_command(command)
            parts = result.split()

            if len(parts) < 2:
                raise TheSkyXCommandError(f"Invalid RA/Dec response: {result}")

            ra_hours = float(parts[0])
            dec_degrees = float(parts[1])

            # No per-call debug line here: get_ra_dec is polled and the wire
            # log in _send_once already carries the result.
            return ra_hours, dec_degrees

        except (ValueError, TheSkyXConnectionError) as e:
            raise TheSkyXCommandError(f"Failed to get RA/Dec: {e}")

    def slew_to_ra_dec(self, ra_hours: float, dec_degrees: float) -> None:
        """Slew telescope to target RA/Dec.

        This command initiates an asynchronous slew. Use is_slewing() to poll.

        Raises:
            TheSkyXConnectionError: If not connected
            TheSkyXCommandError: If slew fails
        """
        if not self._is_connected:
            raise TheSkyXConnectionError("Not connected to TheSkyX")

        try:
            command = f"""
                var Out;
                sky6RASCOMTele.RightAscension = {ra_hours};
                sky6RASCOMTele.Declination = {dec_degrees};
                sky6RASCOMTele.SlewToRaDec({ra_hours}, {dec_degrees}, "Chimera");
                Out = "undefined";
            """
            self._send_command(command)
            self._is_slewing = True
            self.log.info(f"Slewing to RA={ra_hours}h, Dec={dec_degrees}°")

        except TheSkyXConnectionError:
            raise
        except Exception as e:
            self._is_slewing = False
            raise TheSkyXCommandError(f"Failed to slew to RA/Dec: {e}")

    def is_slewing(self) -> bool:
        """Check if telescope is currently slewing."""
        if not self._is_connected:
            return False

        try:
            command = """
                var Out;
                Out = sky6RASCOMTele.IsSlewComplete;
            """
            result = self._send_command(command)

            # IsSlewComplete returns 1 when slew is done, 0 when still slewing
            is_complete = int(result) == 1
            self._is_slewing = not is_complete
            return self._is_slewing

        except TheSkyXCommandError as e:
            # After Abort(), IsSlewComplete raises "Process aborted. Error =
            # 212." until a new slew is commanded: the mount is stopped.
            if "process aborted" in str(e).lower():
                self._is_slewing = False
                return False
            self.log.debug(f"Error checking slew status: {e}")
            return False
        except Exception as e:
            self.log.debug(f"Error checking slew status: {e}")
            return False

    def abort_slew(self) -> None:
        """Abort any in-progress slew"""
        if not self._is_connected:
            raise TheSkyXConnectionError("Not connected to TheSkyX")

        try:
            command = """
                var Out;
                sky6RASCOMTele.Abort();
                Out = "undefined";
            """
            self._send_command(command)
            self._is_slewing = False
            self.log.info("Slew aborted")

        except TheSkyXConnectionError:
            raise
        except Exception as e:
            raise TheSkyXCommandError(f"Failed to abort slew: {e}")

    def sync_ra_dec(self, ra_hours: float, dec_degrees: float) -> None:
        """Sync telescope to current position (calibration)"""
        if not self._is_connected:
            raise TheSkyXConnectionError("Not connected to TheSkyX")

        try:
            command = f"""
                var Out;
                sky6RASCOMTele.Sync({ra_hours}, {dec_degrees}, "Chimera");
                Out = "undefined";
            """
            self._send_command(command)
            self.log.info(f"Synced to RA={ra_hours}h, Dec={dec_degrees}°")

        except TheSkyXConnectionError:
            raise
        except Exception as e:
            raise TheSkyXCommandError(f"Failed to sync RA/Dec: {e}")

    def start_tracking(self) -> None:
        """Start telescope tracking"""
        if not self._is_connected:
            raise TheSkyXConnectionError("Not connected to TheSkyX")

        try:
            # https://www.bisque.com/wp-content/scriptthesky/classsky6_r_a_s_c_o_m_tele.html#a6df8aa451ca5a9986436e017e98a8b17
            command = """
                var Out;
                sky6RASCOMTele.SetTracking(1, 1, 0, 0);
                Out = "undefined";
            """
            self._send_command(command)
            self._is_tracking = True
            self.log.info("Tracking started")

        except TheSkyXConnectionError:
            raise
        except Exception as e:
            raise TheSkyXCommandError(f"Failed to start tracking: {e}")

    def stop_tracking(self) -> None:
        """Stop telescope tracking"""
        if not self._is_connected:
            raise TheSkyXConnectionError("Not connected to TheSkyX")

        try:
            # https://www.bisque.com/wp-content/scriptthesky/classsky6_r_a_s_c_o_m_tele.html#a6df8aa451ca5a9986436e017e98a8b17
            command = """
                var Out;
                sky6RASCOMTele.SetTracking(0, 1, 0, 0);
                Out = "undefined";
            """
            self._send_command(command)
            self._is_tracking = False
            self.log.info("Tracking stopped")

        except TheSkyXConnectionError:
            raise
        except Exception as e:
            raise TheSkyXCommandError(f"Failed to stop tracking: {e}")

    def is_tracking(self) -> bool:
        """Query the mount's live tracking state.

        Returns the real ``sky6RASCOMTele.IsTracking`` value rather than a
        cached "last commanded" flag: TheSkyX turns tracking on by itself when
        an RA/Dec slew finishes, so a cached flag drifts out of sync with the
        mount and reports "disabled" while it is actually tracking.
        """
        if not self._is_connected:
            return False

        try:
            command = """
                var Out;
                Out = sky6RASCOMTele.IsTracking;
            """
            result = self._send_command(command)
            self._is_tracking = int(result) == 1
            return self._is_tracking
        except (TheSkyXCommandError, ValueError) as e:
            # e.g. the Mount Simulator: "not supported by the selected device".
            # Fall back to the last known state instead of hanging or raising.
            self.log.debug(f"IsTracking query failed, using cached state: {e}")
            return self._is_tracking

    def set_park_position(self) -> None:
        """Set the parking position for the telescope to the current position"""
        if not self._is_connected:
            raise TheSkyXConnectionError("Not connected to TheSkyX")

        try:
            command = """
                var Out;
                sky6RASCOMTele.SetParkPosition();
                Out = "undefined";
            """
            self._send_command(command)
            self.log.info("Parking position set")
        except TheSkyXConnectionError:
            raise
        except Exception as e:
            raise TheSkyXCommandError(f"Failed to set parking position: {e}")

    def park(self) -> None:
        """Park the telescope. Must have set a parking position. Does not disconnect TheSkyX from the mount"""
        if not self._is_connected:
            raise TheSkyXConnectionError("Not connected to TheSkyX")

        try:
            # https://www.bisque.com/wp-content/scriptthesky/classsky6_r_a_s_c_o_m_tele.html#ad08e329bb8844fa4d0a0d59a9ce10d07
            command = """
                var Out;
                sky6RASCOMTele.ParkAndDoNotDisconnect();
                Out = "undefined";
            """
            self._send_command(command)
            self._is_parked = True
            self.log.info("Telescope parked")

        except TheSkyXConnectionError:
            raise
        except Exception as e:
            raise TheSkyXCommandError(f"Failed to park telescope: {e}")

    def unpark(self) -> None:
        """Unpark the telescope. Happend automatically on connect"""
        if not self._is_connected:
            raise TheSkyXConnectionError("Not connected to TheSkyX")

        try:
            command = """
                var Out;
                sky6RASCOMTele.Unpark();
                Out = "undefined";
            """
            self._send_command(command)
            self._is_parked = False
            self.log.info("Telescope unparked")

        except TheSkyXConnectionError:
            raise
        except Exception as e:
            raise TheSkyXCommandError(f"Failed to unpark telescope: {e}")

    def is_parked(self) -> bool:
        """Check if telescope is parked"""
        return self._is_parked

    def find_home(self, timeout: float | None = None) -> None:
        """Send the mount to its mechanical home position.

        Behaves like a slew: with Asynchronous = 1 the script normally returns
        at once and completion is polled with :meth:`is_slewing`. Some mount
        drivers block the script for the whole homing run anyway, hence the
        ``timeout`` override -- the default 15 s round-trip timeout would abort
        a homing that is still perfectly healthy.
        """
        if not self._is_connected:
            raise TheSkyXConnectionError("Not connected to TheSkyX")

        try:
            # https://www.bisque.com/wp-content/scriptthesky/classsky6_r_a_s_c_o_m_tele.html
            command = """
                var Out;
                sky6RASCOMTele.FindHome();
                Out = "undefined";
            """
            self._send_command(command, timeout=timeout)
            self._is_slewing = True
            self.log.info("Finding home")

        except TheSkyXConnectionError:
            raise
        except Exception as e:
            self._is_slewing = False
            raise TheSkyXCommandError(f"Failed to find home: {e}")
