"""Shims for the two Pythons this ships on.

Counters running Windows 7 get a build frozen on Python 3.8 -- the last release
that starts on that OS at all -- while every other machine gets 3.12. One
source tree, so anything newer than 3.8 has to be reachable both ways.

There is exactly one such thing, and it is here. Nothing else in the package
needs 3.9 or later: every module already postpones its annotations, so the
`list[str]` and `str | None` written throughout are never evaluated.
"""

from __future__ import annotations

import enum
import sys

if sys.version_info >= (3, 11):
    StrEnum = enum.StrEnum  # novermin
else:

    class StrEnum(str, enum.Enum):  # type: ignore[no-redef]
        """`enum.StrEnum`, which arrived in 3.11.

        The two dunders are the whole point, and they are what 3.11 defines.
        A plain `(str, Enum)` mixin still *formats* as "JobStatus.SENT", so
        without them an f-string anywhere -- a log line, a hold reason, a
        status written into the database -- would quietly change shape between
        the 3.8 build and the 3.12 one, and only on the client's machine.
        """

        __str__ = str.__str__
        __format__ = str.__format__
