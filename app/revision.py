"""Git revision of the code loaded into THIS process.

Resolved once at first use and cached: a later `git pull` on the droplet must
not make a still-running old process report the new commit, otherwise
"journalctl shows the expected commit" would prove nothing. Set GIT_REVISION to
override (e.g. in a container without .git).
"""
from __future__ import annotations

import os
import subprocess
from functools import lru_cache


@lru_cache(maxsize=1)
def get_revision() -> str:
    configured = os.getenv("GIT_REVISION")
    if configured:
        return configured
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"
