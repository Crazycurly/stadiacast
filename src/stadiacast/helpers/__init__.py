"""Helper processes the receiver spawns next to itself: a redirecting DNS server and an SNTP server.

Both write JSON Lines logs into the run directory.  They are separate processes because they
bind privileged-looking ports (53, 123) and must keep answering while the receiver's event loop
is busy.  This module is their launcher: :func:`helper_command` builds the command line the
receiver spawns, normally ``python -m stadiacast.helpers.<name>``; a frozen executable has no
interpreter to run ``-m`` with, so it re-launches itself with ``--helper <name>`` and
:func:`run_helper` runs the helper in that process.
"""

from __future__ import annotations

import importlib
import sys
from collections.abc import Sequence

DNS_SINK = "dns_sink"
SNTP_SERVER = "sntp_server"
#: Every helper module of this package, by the name :func:`helper_command` and ``--helper`` take.
HELPERS = (DNS_SINK, SNTP_SERVER)
#: The first argument that makes the ``stadiacast`` command run a helper instead of the receiver.
HELPER_FLAG = "--helper"


def helper_command(name: str, args: Sequence[str]) -> list[str]:
    """The command line that runs the helper ``name`` (one of :data:`HELPERS`) with ``args``."""
    if getattr(sys, "frozen", False):
        return [sys.executable, HELPER_FLAG, name, *args]
    return [sys.executable, "-m", f"{__name__}.{name}", *args]


def run_helper(argv: Sequence[str]) -> int:
    """``stadiacast --helper NAME ARGS...``: run the helper ``NAME`` in this process; its exit status."""
    if not argv or argv[0] not in HELPERS:
        print(f"stadiacast {HELPER_FLAG} needs one of {', '.join(HELPERS)}", file=sys.stderr)
        return 2
    module = importlib.import_module(f"{__name__}.{argv[0]}")
    return int(module.main(list(argv[1:])))
