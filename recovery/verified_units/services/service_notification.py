# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""
Bridge communication relay for Warp Core Simulation.
Handles persistent notifications to bridge crew via Holodeck Assistant.
Supports priority levels: routine, yellow alert, red alert.
"""
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

_LOGGER = logging.getLogger(__name__)
NOTIFICATION_ID_DEPENDENCIES = "solar_forecast_ml_dependencies"
NOTIFICATION_ID_INSTALLATION = "solar_forecast_ml_installation"
NOTIFICATION_ID_SUCCESS = "solar_forecast_ml_success"
NOTIFICATION_ID_ERROR = "solar_forecast_ml_error"
NOTIFICATION_ID_ML_ACTIVE = "solar_forecast_ml_ml_active"
NOTIFICATION_ID_STARTUP = "solar_forecast_ml_startup"
NOTIFICATION_ID_FORECAST = "solar_forecast_ml_forecast"
NOTIFICATION_ID_LEARNING = "solar_forecast_ml_learning"
NOTIFICATION_ID_RETRAINING = "solar_forecast_ml_retraining"
NOTIFICATION_ID_FROST = "solar_forecast_ml_frost"
NOTIFICATION_ID_FOG = "solar_forecast_ml_fog"
NOTIFICATION_ID_WEATHER_ALERT = "solar_forecast_ml_weather_alert"
NOTIFICATION_ID_SNOW_COVERED = "solar_forecast_ml_snow_covered"
NOTIFICATION_ID_ADAPTIVE_CORRECTION = "solar_forecast_ml_adaptive_correction"


class NotificationService:
    """Service for Persistent Notifications in Home Assistant. @zara"""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry):
        """Initialize Notification Service. @zara"""
        self.hass = hass
        self.entry = entry
        self._initialized = False
        self._notification_lock = asyncio.Lock()
        _LOGGER.debug("NotificationService instance created")

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
        except Exception as error:
            _LOGGER.error(
                f"[X] Error during NotificationService initialization: {error}",
                exc_info=True,
            )
            return False
        return None if False else None

    def _should_notify(self, notification_type: str) -> bool:
        """Centralized check if notification should be displayed. @zara"""
        if not self._initialized:
            return False
        notification_enabled = self.entry.options.get(notification_type, True)
        if not notification_enabled:
            _LOGGER.debug(f"Notification {notification_type} disabled by option")
        return notification_enabled

    async def _safe_create_notification(
        self, message: str, title: str, notification_id: str
    ) -> bool:
        """Create notification with error handling. @zara"""
        if not self._initialized:
            _LOGGER.warning(
                f"[!] NotificationService not initialized - Notification {notification_id} will not be displayed"
            )
            return False
        try:
            await self.hass.services.async_call(
                domain="persistent_notification",
                service="create",
                service_data={
                    "message": message,
                    "title": title,
                    "notification_id": notification_id,
                },
                blocking=True,
            )
            _LOGGER.debug(f"[OK] Notification {notification_id} created")
        except Exception as error:
            _LOGGER.error(
                f"[X] Error creating notification {notification_id}: {error}",
                exc_info=True,
            )
            return False
        return True

    async def _safe_dismiss_notification(self, notification_id: str) -> bool:
        """Remove notification with error handling. @zara"""
        if not self._initialized:
            return False
        try:
            await self.hass.services.async_call(
                domain="persistent_notification",
                service="dismiss",
                service_data={"notification_id": notification_id},
                blocking=True,
            )
            _LOGGER.debug(f"[OK] Notification {notification_id} dismissed")
        except Exception as error:
            _LOGGER.warning(
                f"[!] Error dismissing notification {notification_id}: {error}"
            )
            return False
        return True

    async def create_notification(
        self, title: str, message: str, notification_id: str
    ) -> bool:
        """Public method to create a custom notification. @zara"""
        return await self._safe_create_notification(message, title, notification_id)

    async def dismiss_notification(self, notification_id: str) -> bool:
        """Public method to dismiss a notification. @zara"""
        return await self._safe_dismiss_notification(notification_id)

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
            installed_dependencies_section = ""
            if installed_packages:
                installed_packages_list = "\n".join(
                    [f"* {package_name}" for package_name in installed_packages]
                )
                installed_dependencies_section = (
                    f"\n\n**Installed Dependencies:**\n{installed_packages_list}"
                )
            missing_packages_section = ""
            if missing_packages:
                missing_packages_list = "\n".join(
                    [f"x {package_name}" for package_name in missing_packages]
                )
                missing_packages_section = (
                    f"\n\n**Missing Packages:**\n{missing_packages_list}"
                )
            if ml_mode:
                notification_message = f'The Hubble AI Stack — a custom-built ensemble of 4 proprietary AI models, a 20.5M-parameter Transformer (TFS), a local Machine Learning engine, and a full solar physics backbone — work in perfect synergy to deliver **3-day hourly forecasts with up to 97% accuracy** after calibration.\n\n🔒 **100% local AI** — no cloud, no subscriptions, no data leaves your network. No external AI (ChatGPT, Grok, Gemini) needed. Everything runs on your hardware.\n\n| | |\n|---|---|\n| 🧠 **AI Stack** | Hubble AI 3.0 |\n| ⚡ **Engine** | AI (Attention) + Ridge + Physics Backbone |\n| 🤖 **Ensemble** | Hybrid-AI · Miss Ridge · Frau Holle · Kalman Tracker |\n| 🔬 **Transformer** | Toorox ForeSight — Adaptive Blend (10–55%) |\n| 📊 **Features** | Multi-Panel · Weather · Self-Learning · Shadow Detection |\n| ✅ **Status** | All systems operational |\n{installed_dependencies_section}\n> *"Logic is the beginning of wisdom, not the end."* — Spock\n\n🖖 by **Zara-Toorox** — Live long and prosper!'
            else:
                notification_message = f'The solar physics backbone delivers reliable forecasts — even without ML dependencies. Install the missing packages to unlock the full **Hubble AI Stack** with 4 proprietary models, Transformer AI, and up to **97% accuracy**.\n\n🔒 **100% local** — no cloud, no subscriptions, no data leaves your network.\n\n| | |\n|---|---|\n| 📐 **Mode** | Rule-Based (Physics Only) |\n| 📊 **Features** | Solar Forecasting · Production Statistics |\n| ✅ **Status** | Operational |\n{missing_packages_section}{installed_dependencies_section}\n⚠️ Install missing Python packages to enable the Hubble AI Stack.\n\n> *"Logic is the beginning of wisdom, not the end."* — Spock\n\n🖖 by **Zara-Toorox**'
            await self._safe_create_notification(
                message=notification_message,
                title=f'☀️ Solar Forecast ML V{VERSION} "Transformer" — Hubble AI Stack',
                notification_id=NOTIFICATION_ID_STARTUP,
            )
            return True
        except Exception as error:
            _LOGGER.error(
                f"[X] Error showing startup notification: {error}", exc_info=True
            )
            return False

    async def show_forecast_update(
        self, forecast_energy: float, confidence: Optional[float] = None
    ) -> bool:
        """Show forecast update notification. @zara"""
        if not self._should_notify(CONF_NOTIFY_FORECAST):
            return False
        try:
            notification_message = (
                f"Solar forecast updated: {forecast_energy:.2f} kWh today"
            )
            if confidence is not None:
                notification_message += f"\nConfidence: {confidence:.0f}%"
            await self._safe_create_notification(
                message=notification_message,
                title="Forecast Updated",
                notification_id=NOTIFICATION_ID_FORECAST,
            )
        except Exception as error:
            _LOGGER.error(
                f"[X] Error showing forecast notification: {error}", exc_info=True
            )
            return False
        return True

    async def show_training_start(self, sample_count: Optional[int] = None) -> bool:
        """Show notification when AI training starts. @zara"""
        if not self._should_notify(CONF_NOTIFY_LEARNING):
            return False
        try:
            notification_message = "AI training started"
            if sample_count is not None:
                notification_message += f" with {sample_count} samples"
            await self._safe_create_notification(
                message=notification_message,
                title="Training Started",
                notification_id=NOTIFICATION_ID_LEARNING,
            )
        except Exception as error:
            _LOGGER.error(
                f"[X] Error showing training start notification: {error}", exc_info=True
            )
            return False
        return True

    async def show_training_complete(
        self,
        success: bool,
        accuracy: Optional[float] = None,
        sample_count: Optional[int] = None,
    ) -> bool:
        """Show notification when AI training completes. @zara"""
        if not self._should_notify(CONF_NOTIFY_SUCCESSFUL_LEARNING):
            return False
        try:
            if success:
                accuracy_section = ""
                if accuracy is not None:
                    accuracy_section = f"\n**Accuracy:** {accuracy:.1f}%"
                sample_count_section = ""
                if sample_count is not None:
                    sample_count_section = f"\n**Samples Used:** {sample_count}"
                notification_message = (
                    f"AI Training Complete{accuracy_section}{sample_count_section}"
                )
            else:
                notification_message = "AI Training Failed"
            await self._safe_dismiss_notification(NOTIFICATION_ID_LEARNING)
            await self._safe_create_notification(
                message=notification_message,
                title="Training Complete",
                notification_id=NOTIFICATION_ID_LEARNING,
            )
        except Exception as error:
            _LOGGER.error(
                f"[X] Error showing training complete notification: {error}",
                exc_info=True,
            )
            return False
        return True

    async def dismiss_startup_notification(self) -> bool:
        """Remove startup notification. @zara"""
        return await self._safe_dismiss_notification(NOTIFICATION_ID_STARTUP)

    async def dismiss_forecast_notification(self) -> bool:
        """Remove forecast notification. @zara"""
        return await self._safe_dismiss_notification(NOTIFICATION_ID_FORECAST)

    async def dismiss_training_notification(self) -> bool:
        """Remove training notification. @zara"""
        return await self._safe_dismiss_notification(NOTIFICATION_ID_LEARNING)

    async def show_model_retraining_required(
        self,
        reason: str = "unknown",
        old_features: Optional[int] = None,
        new_features: Optional[int] = None,
    ) -> bool:
        """Show notification when AI model needs retraining. @zara"""
        try:
            if reason == "feature_mismatch":
                retraining_details = f"**Reason:** Sensor change detected\n\n**Details:**\n- Old Features: {old_features}\n- New Features: {new_features}\n\nThe AI model will be automatically retrained to account for the changed sensor configuration."
            else:
                retraining_details = "The AI model needs to be retrained."
            notification_message = f'**Solar Forecast ML - Model Retraining Required**\n\n{retraining_details}\n\n**Next Steps:**\n- Training will be performed automatically\n- If needed, manually start: Service `solar_forecast_ml.force_retrain`\n\n**Status:** Automatic training running...\n\n*"Adaptation is the key to survival."* - Inspired by Star Trek\n\n**Personal Note from Zara:**\nNo worries! The integration adapts automatically.'
            await self._safe_create_notification(
                message=notification_message,
                title="AI Model Retraining",
                notification_id=NOTIFICATION_ID_RETRAINING,
            )
        except Exception as error:
            _LOGGER.error(
                f"[X] Error showing retraining notification: {error}", exc_info=True
            )
            return False
        return True

    async def dismiss_retraining_notification(self) -> bool:
        """Remove retraining notification. @zara"""
        return await self._safe_dismiss_notification(NOTIFICATION_ID_RETRAINING)

    async def show_frost_warning(
        self,
        frost_score: int,
        temperature_c: float,
        dewpoint_c: Optional[float],
        frost_margin_c: float,
        hour: int,
        confidence: float = 0.0,
    ) -> bool:
        """Show frost warning notification when heavy frost is detected. @zara"""
        if not self._should_notify(CONF_NOTIFY_FROST):
            return False
        try:
            confidence_percent = int(confidence * 100)
            dewpoint_text = f"{dewpoint_c:.1f}°C" if dewpoint_c is not None else "N/A"
            frost_margin_text = (
                f"{frost_margin_c:.1f}" if frost_margin_c is not None else "N/A"
            )
            notification_message = f'**Heavy Frost on Solar Panels Detected!**\n\n**Time:** {hour:02d}:00\n**Frost Score:** {frost_score}/10\n**Confidence:** {confidence_percent}%\n\n**Weather Conditions:**\n- Temperature: {temperature_c:.1f}°C\n- Dew Point: {dewpoint_text}\n- Frost Margin: {frost_margin_text}°C\n\n**Effects:**\n- Solar production is likely reduced\n- This hour will be excluded from AI training\n- Forecast accuracy may be affected\n\n**Note:** Frost typically dissipates once the sun warms the panels.\n\n*"Even the coldest winter holds the promise of spring."* - Inspired by Star Trek'
            await self._safe_create_notification(
                message=notification_message,
                title="Frost on Solar Panels",
                notification_id=NOTIFICATION_ID_FROST,
            )
        except Exception as error:
            _LOGGER.error(
                f"[X] Error showing frost notification: {error}", exc_info=True
            )
            return False
        return True

    async def dismiss_frost_notification(self) -> bool:
        """Remove frost notification. @zara"""
        return await self._safe_dismiss_notification(NOTIFICATION_ID_FROST)

    async def show_fog_warning(
        self,
        visibility_m: Optional[float],
        temperature_c: float,
        humidity: float,
        hour: int,
        fog_type: str = "dense",
    ) -> bool:
        """Show fog warning notification when dense fog is detected. @zara"""
        if not self._should_notify(CONF_NOTIFY_FOG):
            return False
        try:
            fog_description = "Dense Fog" if fog_type == "dense" else "Light Fog"
            visibility_section = ""
            if visibility_m is not None:
                visibility_section = (
                    f"**Visibility:** {float(visibility_m) / 1000.0:.1f} km\n"
                )
            measurement_basis_section = f"**Basis:** local measurement (temperature {temperature_c:.1f}C, humidity {humidity:.0f}%)\n"
            notification_message = f'**{fog_description} Detected!**\n\n**Time:** {hour:02d}:00\n{visibility_section}**Humidity:** {humidity:.0f}%\n**Temperature:** {temperature_c:.1f}C\n{measurement_basis_section}\n**Effects on Solar Production:**\n- Fog blocks less light than real clouds\n- Diffuse radiation passes through fog\n- The forecast is automatically adjusted\n\n**Note:** Fog often dissipates once the sun gets stronger and temperature rises.\n\n*"Through the fog, the sun still shines."* - Inspired by Star Trek'
            await self._safe_create_notification(
                message=notification_message,
                title="Dense Fog Detected",
                notification_id=NOTIFICATION_ID_FOG,
            )
        except Exception as error:
            _LOGGER.error(f"[X] Error showing fog notification: {error}", exc_info=True)
            return False
        return True

    async def dismiss_fog_notification(self) -> bool:
        """Remove fog notification. @zara"""
        return await self._safe_dismiss_notification(NOTIFICATION_ID_FOG)

    async def show_weather_alert(
        self,
        alert_type: str,
        reason: str,
        hour: int,
        date_str: str,
        weather_actual: dict = None,
        weather_forecast: dict = None,
    ) -> bool:
        """Show weather alert notification when unexpected weather is detected. @zara"""
        if not self._should_notify(CONF_NOTIFY_WEATHER_ALERT):
            return False
        try:
            alert_descriptions = {
                "unexpected_rain": "Unexpected Rain",
                "unexpected_snow": "Unexpected Snow",
                "unexpected_clouds": "Unexpected Clouds",
                "unexpected_sun": "Unexpected Clearing",
                "sudden_storm": "Sudden Storm",
                "unexpected_fog": "Unexpected Fog",
                "snow_covered_panels": "Snow Covered Panels",
            }
            alert_description = alert_descriptions.get(alert_type, alert_type)
            weather_details = ""
            if weather_actual:
                actual_precipitation_mm = weather_actual.get("precipitation_mm", 0)
                actual_cloud_cover = weather_actual.get(
                    "clouds", weather_actual.get("cloud_cover_percent", 0)
                )
                actual_temperature = weather_actual.get(
                    "temperature", weather_actual.get("temperature_c", "N/A")
                )
                weather_details += f"\n**Current Weather Data:**\n- Precipitation: {actual_precipitation_mm:.1f} mm\n- Cloud Cover: {actual_cloud_cover}%\n- Temperature: {actual_temperature}C"
            if weather_forecast:
                forecast_precipitation = weather_forecast.get(
                    "precipitation_probability",
                    weather_forecast.get("precipitation_mm", 0),
                )
                forecast_cloud_cover = weather_forecast.get(
                    "clouds", weather_forecast.get("cloud_cover_percent", 0)
                )
                weather_details += f"\n\n**Forecast Was:**\n- Precipitation: {forecast_precipitation}\n- Cloud Cover: {forecast_cloud_cover}%"
            notification_message = f'**Unexpected Weather Event Detected!**\n\n**Time:** {date_str} {hour:02d}:00\n**Event:** {alert_description}\n**Reason:** {reason}\n{weather_details}\n\n**Effects:**\n- Solar production deviates from forecast\n- This hour is marked for clean forecast evaluation\n- Learnable forecast misses remain available to weather learning\n\n**Note:** The system learns from this deviation for future weather forecasts.\n\n*"Space may be cold, but our algorithms learn warm."* - Inspired by Star Trek'
            await self._safe_create_notification(
                message=notification_message,
                title=f"Weather Alert: {alert_description}",
                notification_id=NOTIFICATION_ID_WEATHER_ALERT,
            )
        except Exception as error:
            _LOGGER.error(
                f"[X] Error showing weather alert notification: {error}", exc_info=True
            )
            return False
        return True

    async def dismiss_weather_alert_notification(self) -> bool:
        """Remove weather alert notification. @zara"""
        return await self._safe_dismiss_notification(NOTIFICATION_ID_WEATHER_ALERT)

    async def show_snow_covered_warning(
        self,
        temperature_c: float,
        precipitation_mm: float,
        hour: int,
        message: str = None,
    ) -> bool:
        """Show warning when snow coverage on panels is possible. @zara V16.1"""
        if not self._should_notify(CONF_NOTIFY_SNOW_COVERED):
            return False
        try:
            estimated_snow_depth_mm = precipitation_mm * 8
            if message:
                notification_message = f'❄️ **Schnee auf Solarmodulen erkannt**\n\n**Zeit:** {hour:02d}:00 Uhr\n**Temperatur:** {temperature_c:.1f}°C\n\n{message}\n\n**Hinweis:** Diese Stunden werden vom ML-Training ausgeschlossen.\n\n*"Auch im kältesten Winter geht die Sonne auf."*'
            else:
                notification_message = f'❄️ **Schnee auf Solarmodulen möglich**\n\n**Zeit:** {hour:02d}:00 Uhr\n**Temperatur:** {temperature_c:.1f}°C\n**Niederschlag:** {precipitation_mm:.1f} mm\n**Geschätzte Schneehöhe:** ~{estimated_snow_depth_mm:.0f} mm\n\n**Mögliche Auswirkungen:**\n- Solarproduktion kann reduziert sein\n- Diese Stunde wird vorsorglich vom ML-Training ausgeschlossen\n\n**Hinweis:** Diese Warnung basiert auf Wetterdaten und ist eine Schätzung.\n\n*"Auch im kältesten Winter geht die Sonne auf."*'
            await self._safe_create_notification(
                message=notification_message,
                title="❄️ Schnee erkannt",
                notification_id=NOTIFICATION_ID_SNOW_COVERED,
            )
        except Exception as error:
            _LOGGER.error(
                f"[X] Error showing snow covered notification: {error}", exc_info=True
            )
            return False
        return True

    async def show_snow_melting_info(self, temperature_c: float, hour: int) -> bool:
        """Show info when snow may be melting from panels. @zara V16.1"""
        if not self._should_notify(CONF_NOTIFY_SNOW_COVERED):
            return False
        try:
            notification_message = f'☀️ **Schnee schmilzt vermutlich**\n\n**Zeit:** {hour:02d}:00 Uhr\n**Temperatur:** {temperature_c:.1f}°C\n\n**Status:**\n- Temperatur ist gestiegen\n- Schnee beginnt zu schmelzen\n- Solarproduktion normalisiert sich\n\n**Hinweis:** Es kann noch einige Stunden dauern, bis die Module schneefrei sind.\n\n*"Nach jedem Sturm kommt die Ruhe."*'
            await self._safe_create_notification(
                message=notification_message,
                title="☀️ Schnee schmilzt",
                notification_id=NOTIFICATION_ID_SNOW_COVERED,
            )
        except Exception as error:
            _LOGGER.error(
                f"[X] Error showing snow melting notification: {error}", exc_info=True
            )
            return False
        return True

    async def dismiss_snow_covered_notification(self) -> bool:
        """Remove snow covered notification. @zara"""
        return await self._safe_dismiss_notification(NOTIFICATION_ID_SNOW_COVERED)

    async def show_adaptive_correction(
        self,
        original_kwh: float,
        corrected_kwh: float,
        reason: str,
        hours_corrected: int,
        am_actual: float = 0.0,
        am_predicted: float = 0.0,
    ) -> bool:
        """Show notification when adaptive forecast correction was applied. @zara"""
        try:
            if original_kwh > 0.1:
                daily_correction_percent = (
                    (corrected_kwh - original_kwh) / original_kwh * 100
                )
                positive_sign = "+" if daily_correction_percent > 0 else ""
                daily_correction_text = (
                    f"{positive_sign}{daily_correction_percent:.0f}%"
                )
            else:
                daily_correction_text = "N/A"
            if am_predicted > 0.1:
                morning_deviation_percent = (
                    (am_actual - am_predicted) / am_predicted * 100
                )
                morning_deviation_text = f"{morning_deviation_percent:+.0f}%"
            else:
                morning_deviation_text = "N/A"
            notification_message = f'**Adaptive Forecast Correction Applied**\n\n**Reason:** {reason}\n\n**Morning Analysis:**\n- Actual Production: {am_actual:.2f} kWh\n- Forecast Was: {am_predicted:.2f} kWh\n- Deviation: {morning_deviation_text}\n\n**Correction:**\n- Original Daily Forecast: {original_kwh:.2f} kWh\n- Corrected Daily Forecast: {corrected_kwh:.2f} kWh ({daily_correction_text})\n- Recalculated Hours: {hours_corrected}\n\n**Note:**\nThe afternoon forecast was recalculated based on more recent weather data.\nMorning values remain unchanged.\n\n*"Adaptation is the key to survival."* - Inspired by Star Trek'
            await self._safe_create_notification(
                message=notification_message,
                title="Forecast Automatically Adjusted",
                notification_id=NOTIFICATION_ID_ADAPTIVE_CORRECTION,
            )
        except Exception as error:
            _LOGGER.error(
                f"[X] Error showing adaptive correction notification: {error}",
                exc_info=True,
            )
            return False
        return True

    async def dismiss_adaptive_correction_notification(self) -> bool:
        """Remove adaptive correction notification. @zara"""
        return await self._safe_dismiss_notification(
            NOTIFICATION_ID_ADAPTIVE_CORRECTION
        )


async def create_notification_service(
    hass: HomeAssistant, entry: ConfigEntry
) -> Optional[NotificationService]:
    """Factory function to create and initialize NotificationService. @zara"""
    try:
        notification_service = NotificationService(hass, entry)
        if await notification_service.initialize():
            _LOGGER.debug("[OK] NotificationService created successfully")
            return notification_service
        _LOGGER.warning("[!] NotificationService created but not initialized")
        return notification_service
    except Exception as error:
        _LOGGER.error(
            f"[X] Failed to create NotificationService: {error}", exc_info=True
        )
        return
