"""Repair malformed Unicode at text boundaries without changing valid text."""
from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping
from typing import Any


_SURROGATES = re.compile("[\ud800-\udfff]+")
INPUT_REPAIR_NOTICE = (
    "Se repararon caracteres dañados recibidos de la terminal. "
    "Los que no pudieron recuperarse aparecen como �; revisa que la pregunta conserve su sentido."
)


def repair_text(text: str) -> str:
    """Recover UTF-8 surrogateescape bytes/pairs; mark unrecoverable text with �."""
    def repair_run(match: re.Match[str]) -> str:
        run = match.group()
        pieces: list[str] = []
        position = 0
        while position < len(run):
            code = ord(run[position])
            if 0xD800 <= code <= 0xDBFF and position + 1 < len(run) and 0xDC00 <= ord(run[position + 1]) <= 0xDFFF:
                pieces.append(chr(0x10000 + ((code - 0xD800) << 10) + ord(run[position + 1]) - 0xDC00))
                position += 2
            elif 0xDC80 <= code <= 0xDCFF:
                escaped = bytearray()
                while position < len(run) and 0xDC80 <= ord(run[position]) <= 0xDCFF:
                    escaped.append(ord(run[position]) - 0xDC00)
                    position += 1
                pieces.append(escaped.decode("utf-8", errors="replace"))
            else:
                pieces.append("�")
                position += 1
        return "".join(pieces)

    return _SURROGATES.sub(repair_run, text)


def repair_payload(value: Any) -> Any:
    """Repair nested strings and mapping keys, preserving numeric/binary values."""
    if isinstance(value, str):
        return repair_text(value)
    if isinstance(value, Mapping):
        return {repair_payload(key): repair_payload(item) for key, item in value.items()}
    if isinstance(value, list):
        return [repair_payload(item) for item in value]
    if isinstance(value, tuple):
        return tuple(repair_payload(item) for item in value)
    return value


def dumps_json(value: Any, **kwargs: Any) -> str:
    """Serialize repaired text, including strings produced by a default encoder."""
    if kwargs.get("ensure_ascii", True) is False:
        # UTF-8 recovery only produces non-ASCII characters, so it cannot
        # introduce JSON quotes or backslashes. Repair the serialized text to
        # avoid copying large result tables just to serialize them.
        return repair_text(json.dumps(value, **kwargs))
    default = kwargs.get("default")
    if default is not None:
        kwargs["default"] = lambda item: repair_payload(default(item))
    return json.dumps(repair_payload(value), **kwargs)


class TextConnection(sqlite3.Connection):
    """Repair bound text in local state, including scalar fields outside JSON."""
    def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
        return super().execute(sql, repair_payload(parameters))

    def executemany(self, sql: str, parameters: Any, /) -> sqlite3.Cursor:
        return super().executemany(sql, (repair_payload(row) for row in parameters))
