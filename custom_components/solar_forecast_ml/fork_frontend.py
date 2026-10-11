"""Publish fork card assets. Powered by Solar Forecast ML by Zara-Toorox."""

from __future__ import annotations

import logging
from pathlib import Path
import shutil

from homeassistant.const import EVENT_HOMEASSISTANT_STARTED

_LOGGER = logging.getLogger(__name__)
FRONTEND_VERSION = "50.0.1-einstein.1"
CARD_FILES = ("sfml-card.js", "sfml-weather-card.js", "sfml-warning-card.js")
PUBLIC_PATH = "/local/solar-forecast-ml-fork"


def _copy_assets(config_dir: str) -> None:
    root = Path(__file__).parent
    source = root / "extra_features/sfml_stats/frontend"
    target = Path(config_dir) / "www" / "solar-forecast-ml-fork"
    target.mkdir(parents=True, exist_ok=True)
    for filename in CARD_FILES:
        shutil.copy2(source / filename, target / filename)
    shutil.copy2(root / "frontend/sfml-ha-data.mjs", target / "sfml-ha-data.mjs")


async def async_setup_frontend(hass) -> None:
    """Use HA's static hosting for assets and its authenticated state API for data."""
    await hass.async_add_executor_job(_copy_assets, hass.config.config_dir)

    async def register_resources(_event=None):
        try:
            lovelace = hass.data.get("lovelace")
            if lovelace is None or lovelace.resources is None:
                return
            resources = lovelace.resources
            await resources.async_load()
            for filename in CARD_FILES:
                legacy = f"/api/sfml_stats/static/{filename}"
                new = f"{PUBLIC_PATH}/{filename}?v={FRONTEND_VERSION}"
                matches = [item for item in resources.async_items()
                           if item.get("url", "").split("?", 1)[0]
                           in (legacy, f"{PUBLIC_PATH}/{filename}")]
                if matches:
                    await resources.async_update_item(matches[0]["id"], {"url": new, "res_type": "module"})
                    for duplicate in matches[1:]:
                        await resources.async_delete_item(duplicate["id"])
                else:
                    await resources.async_create_item({"url": new, "res_type": "module"})
        except Exception:
            _LOGGER.exception("Could not register fork dashboard resources")

    if hass.is_running:
        await register_resources()
    else:
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, register_resources)
