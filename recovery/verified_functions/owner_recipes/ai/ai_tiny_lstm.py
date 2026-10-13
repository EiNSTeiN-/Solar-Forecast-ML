# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
import asyncio
import logging
import math
from typing import Any, Dict, List, Optional, Tuple


class TinyLSTM:

    @classmethod
    def _chronological_split_indices(
        cls,
        n_samples: int,
        validation_split: float,
        sample_groups: Optional[List[Any]] = None,
        sample_dates: Optional[List[Any]] = None,
        source_intervals: Optional[List[Tuple[Any, Any]]] = None,
        embargo: Any = 0,
    ) -> Tuple[List[int], List[int]]:
        embargo__recovered_capture_0006 = (
            sample_groups if sample_groups is not None else sample_dates
        )
        if (
            embargo__recovered_capture_0006 is not None
            and len(embargo__recovered_capture_0006) != n_samples
        ):
            raise ValueError("sample group/date labels must match sample count")
        if source_intervals is not None and len(source_intervals) != n_samples:
            raise ValueError("source intervals must match sample count")
        _var_var_20 = max(1, int(n_samples * validation_split)) if n_samples >= 2 else 0
        if embargo__recovered_capture_0006 is None:
            _var_var_21 = list(range(n_samples))
            _var_var_22 = _var_var_21[n_samples - _var_var_20 :]
            _var_var_23 = _var_var_21[: n_samples - _var_var_20]
        else:
            try:
                _var_var_21 = sorted(
                    range(n_samples),
                    key=lambda index: embargo__recovered_capture_0006[index],
                )
            except TypeError:
                _var_var_21 = sorted(
                    range(n_samples),
                    key=lambda index: str(embargo__recovered_capture_0006[index]),
                )
            _var_var_22 = []
            _var_var_24 = set()
            for embargo__recovered_capture_0005 in reversed(_var_var_21):
                _var_var_22.append(embargo__recovered_capture_0005)
                _var_var_24.add(
                    embargo__recovered_capture_0006[embargo__recovered_capture_0005]
                )
                if len(_var_var_22) >= _var_var_20:
                    break
            _var_var_23 = [
                embargo__recovered_capture_0005
                for embargo__recovered_capture_0005 in _var_var_21
                if embargo__recovered_capture_0006[embargo__recovered_capture_0005]
                not in _var_var_24
            ]
            _var_var_22.reverse()
        if source_intervals is not None and _var_var_22:
            _var_var_25 = [
                source_intervals[embargo__recovered_capture_0005]
                for embargo__recovered_capture_0005 in _var_var_22
            ]
            _var_var_23 = [
                embargo__recovered_capture_0005
                for embargo__recovered_capture_0005 in _var_var_23
                if not any(
                    (
                        cls._intervals_overlap(
                            source_intervals[embargo__recovered_capture_0005],
                            _var_var_26,
                            embargo,
                        )
                        for _var_var_26 in _var_var_25
                    )
                )
            ]
        return (_var_var_23, _var_var_22)
