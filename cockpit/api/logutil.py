"""
logutil.py - structured, dependency-free logging for cockpit-api.

Emits one line per event in a compact ``key=value`` structured format that is
easy to grep on the device and easy to pipe into a log collector later. All
output goes to stderr by default (so it survives being launched from
``boot-hook.sh``); a file target can be configured.

Only the Python standard library is used.
"""

import logging
import sys
import time


class _StructuredFormatter(logging.Formatter):
    """Render records as ``ts=... level=... logger=... msg="..." k=v ...``.

    Extra structured fields are passed via ``logger.info("msg", extra={"kv": {...}})``.
    """

    def format(self, record):
        ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record.created))
        ts = "%s.%03dZ" % (ts, record.msecs)
        parts = [
            "ts=%s" % ts,
            "level=%s" % record.levelname,
            "logger=%s" % record.name,
            'msg=%s' % _quote(record.getMessage()),
        ]
        kv = getattr(record, "kv", None)
        if isinstance(kv, dict):
            for key, value in kv.items():
                parts.append("%s=%s" % (key, _quote(value)))
        if record.exc_info:
            parts.append("exc=%s" % _quote(self.formatException(record.exc_info)))
        return " ".join(parts)


def _quote(value):
    """Quote a value only when it contains whitespace or quotes."""
    text = str(value)
    if text == "" or any(c in text for c in ' "\t\n'):
        return '"%s"' % text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return text


def setup(level="INFO", file_path=None):
    """Configure the root logger once. Safe to call multiple times."""
    root = logging.getLogger()
    # Drop any handlers a previous call (or the stdlib default) installed.
    for handler in list(root.handlers):
        root.removeHandler(handler)

    if file_path:
        handler = logging.FileHandler(file_path)
    else:
        handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_StructuredFormatter())
    root.addHandler(handler)
    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    return root


def get(name):
    """Return a named logger with a small ``.event()`` convenience helper."""
    logger = logging.getLogger(name)

    def event(level, msg, **kv):
        logger.log(level, msg, extra={"kv": kv})

    # Attach the helper without clobbering the real logger API.
    logger.event = event  # type: ignore[attr-defined]
    return logger
