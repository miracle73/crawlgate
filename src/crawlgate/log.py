"""Structured JSON logging to stderr."""

from __future__ import annotations

import json
import logging
import sys

_STD = set(vars(logging.makeLogRecord({})))


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {"level": record.levelname.lower(), "logger": record.name, "msg": record.getMessage()}
        out.update({k: v for k, v in vars(record).items() if k not in _STD and k != "message"})
        return json.dumps(out, default=str, sort_keys=True)


def setup(level: str = "INFO") -> None:
    h = logging.StreamHandler(sys.stderr)
    h.setFormatter(JsonFormatter())
    root = logging.getLogger("crawlgate")
    root.handlers[:] = [h]
    root.setLevel(level.upper())
    root.propagate = False
