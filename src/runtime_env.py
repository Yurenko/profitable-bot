from __future__ import annotations

import os


def is_vercel() -> bool:
    """Detect Vercel serverless runtime.

    Vercel sets `VERCEL=1` and `VERCEL_URL` in most deployments.
    """

    return os.environ.get("VERCEL") == "1" or bool(os.environ.get("VERCEL_URL"))

