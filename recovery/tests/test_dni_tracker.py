"""Rolling DNI history, persistence, and failure behavior against proved code.

Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
The integration is never imported; the class uses owned database and clock seams.
"""
import asyncio
import copy
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
import unittest

from pure_reference import classes
from test_isolated_initialization import logger_for


RELATIVE = "extra_features/solar_forecast_eai/ai/ai_dni_tracker.py"
NOW = datetime(2026, 10, 12, 12)


def result(awaitable):
    try:
        return asyncio.run(awaitable)
    except asyncio.CancelledError:
        return "cancelled"


def state(tracker):
    return copy.deepcopy((tracker.max_dni, tracker.history,
                          tracker.last_updated, tracker._loaded))


class DniTrackerTests(unittest.TestCase):
    def tracker(self, index, events, tracker_rows=(), history_rows=(),
                fetch_error=None, save_error=None):
        async def fetch(statement, *arguments):
            events.append(("fetch", statement, arguments))
            if fetch_error:
                raise fetch_error("owned fetch failure")
            return tracker_rows if "FROM ai_dni_tracker" in statement else history_rows

        async def save(snapshot):
            events.append(("save", copy.deepcopy(snapshot)))
            if save_error:
                raise save_error("owned save failure")

        cls = classes(RELATIVE, "DniTracker", {
            "_LOGGER": logger_for(events),
            "datetime": SimpleNamespace(now=lambda: NOW,
                                        fromisoformat=datetime.fromisoformat),
            "TRACKED_HOURS": range(6, 21), "HISTORY_DAYS": 7,
            "Any": Any, "Dict": Dict, "List": List, "Optional": Optional,
            "DatabaseManager": object,
        })[index]
        return cls(SimpleNamespace(fetchall=fetch, save_dni_tracker=save))

    def test_load_dates_history_self_healing_and_idempotence(self):
        for rows, history in (
            ((), ()),
            (((6, 10.0, NOW.isoformat()),), ((6, 40.0), (6, 20.0))),
            (((6, 10.0, "2026-10-11T12:00:00"),), ((6, 40.0),)),
            (((6, 10.0, "invalid date"),), ((6, 5.0),)),
            (((6, 1500.0, NOW.isoformat()),), ((6, 1400.0), (22, 12.0))),
        ):
            paired = []
            for index in (0, 1):
                events = []
                tracker = self.tracker(index, events, rows, history)
                first = result(tracker.async_load())
                loaded = state(tracker)
                event_count = len(events)
                second = result(tracker.async_load())
                self.assertEqual(len(events), event_count)
                paired.append((first, second, loaded, state(tracker), events))
            self.assertEqual(paired[1], paired[0])
            self.assertTrue(paired[1][2][-1])
            fetches = [event for event in paired[1][-1] if event[0] == "fetch"]
            self.assertEqual(len(fetches), 2)
            self.assertEqual(fetches[1][-1], ((105,),))
            if rows and rows[0][1] > 1200:
                self.assertEqual(paired[1][2][0][6], 0.0)
                self.assertEqual(paired[1][2][1][6], [])

    def test_records_boundaries_rollover_trimming_and_stats_copy(self):
        paired = []
        for index in (0, 1):
            events = []
            tracker = self.tracker(index, events)
            for hour, value in ((5, 100), (21, 100), (6, -1), (6, 200),
                                (6, 100), (6, 300), (20, 50)):
                tracker.record_dni(hour, value)
            initial = state(tracker)
            tracker.history[7] = list(range(10))
            tracker.start_new_day()
            tracker.record_dni(6, 250)
            value = result(tracker.end_of_day_update())
            stats = tracker.get_stats()
            stats["max_dni"][6] = -999
            paired.append((initial, value, state(tracker), stats,
                           tracker.get_max_dni(6), tracker.get_max_dni(5), events))
        self.assertEqual(paired[1], paired[0])
        self.assertEqual(paired[1][0][1][6], [300])
        self.assertEqual(paired[1][2][1][6], [300, 250])
        self.assertEqual(len(paired[1][2][1][7]), 7)
        self.assertEqual(paired[1][2][1][8], [0.0, 0.0])
        self.assertEqual(paired[1][4:6], (300, 0.0))

    def test_save_snapshot_clock_failure_and_cancellation(self):
        for timestamp in (None, "2026-10-11T09:00:00"):
            for failure in (None, RuntimeError, asyncio.CancelledError):
                paired = []
                for index in (0, 1):
                    events = []
                    tracker = self.tracker(index, events, save_error=failure)
                    tracker.last_updated = timestamp
                    tracker.record_dni(6, 123.0)
                    tracker.max_dni[6] = 321.0
                    value = result(tracker.async_save())
                    paired.append((value, state(tracker), events))
                self.assertEqual(paired[1], paired[0])
                self.assertEqual(paired[1][0],
                                 "cancelled" if failure is asyncio.CancelledError else None)
                snapshot = next(event[1] for event in paired[1][-1] if event[0] == "save")
                self.assertEqual(snapshot["version"], "1.0")
                self.assertEqual(snapshot["last_updated"], timestamp or NOW.isoformat())
                self.assertEqual(snapshot["max_dni"]["6"], 321.0)
                self.assertEqual(snapshot["history"]["6"], [123.0])
                self.assertEqual(set(snapshot["history"]), {str(hour) for hour in range(6, 21)})

    def test_load_failures_keep_retry_and_cancellation_semantics(self):
        for rows, fetch_failure, save_failure, expected_loaded in (
            ((), RuntimeError, None, False),
            ((), asyncio.CancelledError, None, False),
            (None, None, None, False),
            (((6, 2000.0, NOW.isoformat()),), None, RuntimeError, True),
            (((6, 2000.0, NOW.isoformat()),), None, asyncio.CancelledError, False),
        ):
            paired = []
            for index in (0, 1):
                events = []
                tracker = self.tracker(index, events, rows,
                                       fetch_error=fetch_failure, save_error=save_failure)
                value = result(tracker.async_load())
                paired.append((value, state(tracker), events))
            self.assertEqual(paired[1], paired[0])
            self.assertEqual(paired[1][1][-1], expected_loaded)
            cancelled = asyncio.CancelledError in (fetch_failure, save_failure)
            self.assertEqual(paired[1][0], "cancelled" if cancelled else None)


if __name__ == "__main__":
    unittest.main()
