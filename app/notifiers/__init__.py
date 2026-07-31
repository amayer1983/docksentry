#!/usr/bin/env python3
"""Notifier plugin package — registry + auto-discovery.

Every ``*.py`` module in this package (other than ``base``) is imported and
scanned for :class:`BaseNotifier` subclasses. Drop a new channel file in here
and it's picked up automatically — no registration list to edit. Channels run
in ``order`` (then ``name``) so the existing Discord → webhook → SMTP sequence
stays stable and new channels append after.
"""

import importlib
import pkgutil

from .base import BaseNotifier, post_json_with_retry, version_str

__all__ = ["BaseNotifier", "post_json_with_retry", "version_str",
           "discover", "build_all", "build_configured"]


def discover():
    """Return the registered channel classes, sorted by (order, name)."""
    classes = []
    for mod in pkgutil.iter_modules(__path__):
        if mod.name == "base":
            continue
        module = importlib.import_module(f"{__name__}.{mod.name}")
        for obj in vars(module).values():
            if (isinstance(obj, type) and issubclass(obj, BaseNotifier)
                    and obj is not BaseNotifier
                    and obj.__module__ == module.__name__):
                classes.append(obj)
    classes.sort(key=lambda c: (getattr(c, "order", 100), c.name))
    return classes


def build_all(config):
    """Instantiate every registered channel with ``config`` (unfiltered)."""
    return [cls(config) for cls in discover()]


def build_configured(config):
    """The channels that are actually active for this config, in run order."""
    return [p for p in build_all(config) if p.configured()]
