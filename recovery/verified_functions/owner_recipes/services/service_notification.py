# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
import asyncio
import logging
from typing import List, Optional
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from ..const import (
    CONF_NOTIFY_FORECAST,
    CONF_NOTIFY_FOG,
    CONF_NOTIFY_FROST,
    CONF_NOTIFY_LEARNING,
    CONF_NOTIFY_STARTUP,
    CONF_NOTIFY_SUCCESSFUL_LEARNING,
    CONF_NOTIFY_WEATHER_ALERT,
    CONF_NOTIFY_SNOW_COVERED,
    VERSION,
)


class NotificationService:

    async def initialize(self) -> bool:
        """Initialize the Notification Service. @zara"""
        try:
            async with self._notification_lock:
                if self._initialized:
                    _LOGGER.debug("[OK] NotificationService already initialized")
                    return True
                if "persistent_notification" not in self.hass.config.components:
                    _LOGGER.warning(
                        "[!] persistent_notification not available - Notifications will not be displayed"
                    )
                    self._initialized = True
                    return False
                self._initialized = True
                _LOGGER.debug("[OK] NotificationService successfully initialized")
                return True
        except Exception as _var_var_0:
            _LOGGER.error(
                f"[X] Error during NotificationService initialization: {_var_var_0}",
                exc_info=True,
            )
            return False
        return None if False else None

    async def show_startup_success(
        self,
        ml_mode: bool = True,
        installed_packages: Optional[List[str]] = None,
        missing_packages: Optional[List[str]] = None,
        use_attention: bool = False,
    ) -> bool:
        """Show startup notification with integration status. @zara"""
        if not self._should_notify(CONF_NOTIFY_STARTUP):
            return False
        try:
            _var_var_2 = ""
            if installed_packages:
                _var_var_4 = "\n".join(
                    [f"* {_var_var_3}" for _var_var_3 in installed_packages]
                )
                _var_var_2 = f"\n\n**Installed Dependencies:**\n{_var_var_4}"
            _var_var_5 = ""
            if missing_packages:
                _var_var_6 = "\n".join(
                    [f"x {_var_var_3}" for _var_var_3 in missing_packages]
                )
                _var_var_5 = f"\n\n**Missing Packages:**\n{_var_var_6}"
            if ml_mode:
                _var_var_7 = f'The Hubble AI Stack — a custom-built ensemble of 4 proprietary AI models, a 20.5M-parameter Transformer (TFS), a local Machine Learning engine, and a full solar physics backbone — work in perfect synergy to deliver **3-day hourly forecasts with up to 97% accuracy** after calibration.\n\n🔒 **100% local AI** — no cloud, no subscriptions, no data leaves your network. No external AI (ChatGPT, Grok, Gemini) needed. Everything runs on your hardware.\n\n| | |\n|---|---|\n| 🧠 **AI Stack** | Hubble AI 3.0 |\n| ⚡ **Engine** | AI (Attention) + Ridge + Physics Backbone |\n| 🤖 **Ensemble** | Hybrid-AI · Miss Ridge · Frau Holle · Kalman Tracker |\n| 🔬 **Transformer** | Toorox ForeSight — Adaptive Blend (10–55%) |\n| 📊 **Features** | Multi-Panel · Weather · Self-Learning · Shadow Detection |\n| ✅ **Status** | All systems operational |\n{_var_var_2}\n> *"Logic is the beginning of wisdom, not the end."* — Spock\n\n🖖 by **Zara-Toorox** — Live long and prosper!'
            else:
                _var_var_7 = f'The solar physics backbone delivers reliable forecasts — even without ML dependencies. Install the missing packages to unlock the full **Hubble AI Stack** with 4 proprietary models, Transformer AI, and up to **97% accuracy**.\n\n🔒 **100% local** — no cloud, no subscriptions, no data leaves your network.\n\n| | |\n|---|---|\n| 📐 **Mode** | Rule-Based (Physics Only) |\n| 📊 **Features** | Solar Forecasting · Production Statistics |\n| ✅ **Status** | Operational |\n{_var_var_5}{_var_var_2}\n⚠️ Install missing Python packages to enable the Hubble AI Stack.\n\n> *"Logic is the beginning of wisdom, not the end."* — Spock\n\n🖖 by **Zara-Toorox**'
            await self._safe_create_notification(
                message=_var_var_7,
                title=f'☀️ Solar Forecast ML V{VERSION} "Transformer" — Hubble AI Stack',
                notification_id=NOTIFICATION_ID_STARTUP,
            )
            return True
        except Exception as _var_var_0:
            _LOGGER.error(
                f"[X] Error showing startup notification: {_var_var_0}", exc_info=True
            )
            return False
