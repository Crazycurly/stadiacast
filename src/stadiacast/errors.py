"""The one exception the command line reports as a single line: something the user has to install
or configure (an extra, a driver, a library, a device), as opposed to a programming error, which
keeps its traceback.

It subclasses ``RuntimeError`` so callers that only know the old type keep working.
"""

from __future__ import annotations


class SetupError(RuntimeError):
    """A missing extra, driver, library or device, with the message saying what to install."""


__all__ = ["SetupError"]
