"""Project package.

Windows consoles default to a legacy code page (cp1252 here), so printing the
arrows and box characters used in progress output and finding summaries raises
UnicodeEncodeError -- after the work is done and the report is already written,
which makes a successful run look like a failure.

Reconfiguring stdout/stderr to UTF-8 at import time fixes it for every entry
point. `errors="replace"` ensures a console that genuinely cannot render a
glyph degrades to '?' rather than taking the process down.
"""
from __future__ import annotations

import sys

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        # Not a reconfigurable text stream (e.g. redirected/captured) -- fine.
        pass
