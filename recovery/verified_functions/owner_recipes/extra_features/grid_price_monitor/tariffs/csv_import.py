# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
from __future__ import annotations
import csv
import hashlib
import io
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any, Mapping
from zoneinfo import ZoneInfo
from ..const import PRICE_SOURCE_CSV


def _sniff(text: str) -> type[csv.Dialect]:
    _var_var_87 = text[:4096]
    try:
        _var_var_7 = csv.Sniffer().sniff(_var_var_87, delimiters=";\t,")
        _var_var_88 = _var_var_7.delimiter
    except csv.Error:
        _var_var_89 = _var_var_87.count(";")
        _var_var_90 = _var_var_87.count(",")
        _var_var_91 = _var_var_87.count("\t")
        if _var_var_91 > _var_var_89 and _var_var_91 > _var_var_90:
            _var_var_88 = "\t"
        elif _var_var_89 >= _var_var_90:
            _var_var_88 = ";"
        else:
            _var_var_88 = ","

    class _Dialect(csv.excel):
        pass

    _Dialect.delimiter = _var_var_88
    return _Dialect
