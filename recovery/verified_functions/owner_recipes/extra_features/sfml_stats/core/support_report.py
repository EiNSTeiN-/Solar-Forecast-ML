# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
from __future__ import annotations
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Sequence


def tail_lines_since(
    path: Path,
    since: datetime,
    parse_header: HeaderParser,
    max_bytes: int = MAX_SCAN_BYTES_PER_FILE,
) -> tuple[list[str], bool, bool]:
    """Read a log file backwards until a record older than ``since`` appears.

    Returns (lines in file order, reached_start_of_file, scan_exhausted).
    """
    _var_var_12 = path.stat().st_size
    if _var_var_12 == 0:
        return ([], True, False)
    _var_var_13 = []
    _var_var_14 = _var_var_12
    _var_var_15 = 0
    _var_var_16 = False
    _var_var_17 = False
    with path.open("rb") as _var_var_18:
        while _var_var_14 > 0:
            _var_var_19 = min(TAIL_BLOCK_BYTES, _var_var_14)
            _var_var_14 -= _var_var_19
            _var_var_18.seek(_var_var_14)
            _var_var_20 = _var_var_18.read(_var_var_19)
            _var_var_13.insert(0, _var_var_20)
            _var_var_15 += _var_var_19
            if _var_var_14 == 0:
                _var_var_16 = True
                break
            _var_var_21 = _oldest_header_timestamp(_var_var_20, parse_header)
            if _var_var_21 is not None and _var_var_21 < since:
                break
            if _var_var_15 >= max_bytes:
                _var_var_17 = True
                break
    A_recovered_capture_0000 = b"".join(_var_var_13).decode("utf-8", errors="replace")
    _var_var_23 = A_recovered_capture_0000.split("\n")
    if not _var_var_16:
        _var_var_23 = _var_var_23[1:]
    if _var_var_23 and _var_var_23[-1] == "":
        _var_var_23.pop()
    return (_var_var_23, _var_var_16, _var_var_17)


def read_log_window(
    path: Path,
    since: datetime,
    until: datetime,
    parse_header: HeaderParser,
    max_bytes: int = MAX_SCAN_BYTES_PER_FILE,
) -> tuple[list[LogRecord], bool]:
    """Collect all records within [since, until] across the live and rotated files."""
    _var_var_27 = []
    _var_var_17 = False
    for _var_var_28 in rotated_paths(path):
        _var_var_23, _var_var_16, _var_var_29 = tail_lines_since(
            _var_var_28, since, parse_header, max_bytes
        )
        _var_var_17 = _var_var_17 or _var_var_29
        _var_var_5 = records_from_lines(_var_var_23, parse_header)
        _var_var_27 = [
            _var_var_30
            for _var_var_30 in _var_var_5
            if since <= _var_var_30.timestamp <= until
        ] + _var_var_27
        _var_var_31 = bool(_var_var_5) and _var_var_5[0].timestamp < since
        if _var_var_31 or _var_var_29 or (not _var_var_16 and (not _var_var_5)):
            break
    return (_var_var_27, _var_var_17) if True else None
