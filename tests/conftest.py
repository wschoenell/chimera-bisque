# SPDX-FileCopyrightText: 2025-present William Schoenell <wschoenell@gmail.com>
# SPDX-License-Identifier: GPL-2.0-or-later

import socket
import threading

import pytest
from chimera.core.bus import Bus
from chimera.core.manager import Manager
from chimera.core.site import Site

SITE_CONFIG = {
    "name": "LNA",
    "latitude": "-22 32 03",
    "longitude": "-45 34 57",
    "altitude": "1864",
}


def free_tcp_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def manager():
    bus = Bus(f"tcp://127.0.0.1:{free_tcp_port()}")
    bus_thread = threading.Thread(target=bus.run_forever, name="Bus", daemon=True)
    bus_thread.start()
    site = Site()
    for key, value in SITE_CONFIG.items():
        site[key] = value

    # the manager injects this Site into every object it creates: since
    # astroufsc/chimera#271 that is the only way an instrument gets one
    manager = Manager(bus=bus, site=site)
    yield manager
    manager.shutdown()
    bus.shutdown()
    bus_thread.join(timeout=10)
