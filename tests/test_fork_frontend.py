"""Test asset publication and resource migration without importing protected code."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

MODULE_PATH = Path(__file__).parents[1] / "custom_components/solar_forecast_ml/fork_frontend.py"
const = ModuleType("homeassistant.const")
const.EVENT_HOMEASSISTANT_STARTED = "homeassistant_started"
with patch.dict("sys.modules", {"homeassistant": ModuleType("homeassistant"), "homeassistant.const": const}):
    spec = spec_from_file_location("fork_frontend_under_test", MODULE_PATH)
    frontend = module_from_spec(spec)
    spec.loader.exec_module(frontend)


class Resources:
    def __init__(self, items):
        self.items = items

    async def async_load(self):
        pass

    def async_items(self):
        return list(self.items)

    async def async_update_item(self, identifier, data):
        next(item for item in self.items if item["id"] == identifier).update(data)

    async def async_delete_item(self, identifier):
        self.items = [item for item in self.items if item["id"] != identifier]

    async def async_create_item(self, data):
        self.items.append({"id": f"new-{len(self.items)}", **data})


class FakeHass:
    def __init__(self, config_dir, resources, running=True):
        self.config = SimpleNamespace(config_dir=config_dir)
        self.data = {"lovelace": SimpleNamespace(resources=resources)}
        self.is_running = running
        self.events = []
        self.bus = SimpleNamespace(async_listen_once=lambda event, callback: self.events.append((event, callback)))

    async def async_add_executor_job(self, function, *args):
        return function(*args)


class FrontendTests(unittest.IsolatedAsyncioTestCase):
    async def test_old_resources_are_replaced_and_unrelated_resources_are_preserved(self):
        resources = Resources([
            {"id": "old", "url": "/api/sfml_stats/static/sfml-card.js", "res_type": "module"},
            {"id": "duplicate", "url": "/api/sfml_stats/static/sfml-card.js?v=old", "res_type": "module"},
            {"id": "unrelated", "url": "/hacsfiles/example.js", "res_type": "module"},
        ])
        with tempfile.TemporaryDirectory() as directory:
            await frontend.async_setup_frontend(FakeHass(directory, resources))
            public = Path(directory) / "www/solar-forecast-ml-fork"
            self.assertEqual({p.name for p in public.iterdir()}, {*frontend.CARD_FILES, "sfml-ha-data.mjs"})
            for path in public.iterdir():
                source = MODULE_PATH.parent / ("frontend" if path.suffix == ".mjs" else "extra_features/sfml_stats/frontend") / path.name
                self.assertEqual(path.read_bytes(), source.read_bytes())
        self.assertEqual(len(resources.items), 4)
        self.assertEqual(next(item for item in resources.items if item["id"] == "unrelated")["url"], "/hacsfiles/example.js")
        self.assertFalse(any(item["url"].startswith("/api/sfml_stats/static/") for item in resources.items))
        first = [dict(item) for item in resources.items]
        with tempfile.TemporaryDirectory() as directory:
            await frontend.async_setup_frontend(FakeHass(directory, resources))
        self.assertEqual(resources.items, first)

    async def test_startup_registration_waits_until_other_integrations_are_ready(self):
        resources = Resources([])
        with tempfile.TemporaryDirectory() as directory:
            hass = FakeHass(directory, resources, running=False)
            await frontend.async_setup_frontend(hass)
            self.assertEqual(resources.items, [])
            self.assertEqual(hass.events[0][0], "homeassistant_started")
            await hass.events[0][1]()
            self.assertEqual(len(resources.items), 3)


if __name__ == "__main__":
    unittest.main()
