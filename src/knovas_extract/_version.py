"""Package + spec version constants.

Lives in its own module so internal code can import the values without
re-entering the public `knovas_extract` package (which would create a
circular import via dispatch.py's lazy registry).
"""

from __future__ import annotations

__version__ = "0.1.3"
SPEC_VERSION = "1.2.0"
