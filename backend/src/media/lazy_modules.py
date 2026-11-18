"""Deferred imports for heavy optional dependencies.

The all-in-one Render image runs the API, the video worker, PostgreSQL,
Redis, the Next.js frontend and nginx inside a single 512 MB container.
Eagerly importing cv2 (~80 MB) or assemblyai in *every* Python process
leaves no headroom for renders, so modules import them through the proxies
below: the real import happens on first attribute access.

Usage::

    from .lazy_modules import cv2          # media/*
    from ..media.lazy_modules import cv2   # services/* and deeper

Attribute access works exactly as before (``cv2.VideoCapture(...)``);
nothing may rely on ``isinstance``/``dir()`` on the module object itself.
"""

from __future__ import annotations

import importlib
from typing import Any


class _LazyModule:
    """Wrap a module name and import it on first attribute access."""

    def __init__(self, name: str) -> None:
        object.__setattr__(self, "_name", name)
        object.__setattr__(self, "_module", None)

    def _load(self) -> Any:
        module = object.__getattribute__(self, "_module")
        if module is None:
            module = importlib.import_module(object.__getattribute__(self, "_name"))
            object.__setattr__(self, "_module", module)
        return module

    def __getattr__(self, attr: str) -> Any:
        return getattr(self._load(), attr)

    def __setattr__(self, attr: str, value: Any) -> None:
        setattr(self._load(), attr, value)

    def __repr__(self) -> str:
        loaded = object.__getattribute__(self, "_module") is not None
        name = object.__getattribute__(self, "_name")
        return f"<lazy module {name}{' (loaded)' if loaded else ''}>"


cv2 = _LazyModule("cv2")
assemblyai = _LazyModule("assemblyai")
