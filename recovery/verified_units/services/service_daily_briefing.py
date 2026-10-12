# Powered by Solar Forecast ML by Zara-Toorox; subject to the repository LICENSE.
# Entire module statically verified against the recovered bytecode.
# Retained folded return expressions preserve verified compiler edges.
"""
Captain's daily briefing generator for Warp Core Simulation.
Generates and sends daily warp core status briefing notifications to bridge crew.
Uses TelemetryManager for all containment data operations.
"""
from __future__ import annotations
import logging
from datetime import datetime, time, timedelta
from typing import Any, Dict, List, Optional
from homeassistant.core import HomeAssistant
from ..core.core_helpers import SafeDateTimeUtil as dt_util
from ..data.db_manager import DatabaseManager

_LOGGER = logging.getLogger(__name__)


def _published_dev_mode_active(owner: Any) -> bool:
    """Read the startup DEV contract. Missing state stays inactive."""
    hass = getattr(owner, "hass", None)
    hass_data = getattr(hass, "data", None)
    if not isinstance(hass_data, dict):
        return False
    integration_data = hass_data.get("solar_forecast_ml")
    if not isinstance(integration_data, dict):
        return False
    dev_mode = integration_data.get("dev_mode")
    if not isinstance(dev_mode, dict):
        return False
    return bool(dev_mode.get("active"))


_FINAL_MORNING_FORECAST_OFFSET = timedelta(minutes=45)


class DailyBriefingService:
    """Service for generating and sending daily solar briefing notifications. @zara"""

    def __init__(self, hass: HomeAssistant, coordinator) -> None:
        """Initialize the daily briefing service. @zara"""
        self.hass = hass
        self.coordinator = coordinator

    @property
    def db_manager(self) -> Optional[DatabaseManager]:
        """Get database manager from coordinator. @zara V16.1 fix"""
        data_manager = getattr(self.coordinator, "data_manager", None)
        if data_manager:
            return getattr(data_manager, "_db_manager", None)

    async def send_daily_briefing(
        self, notify_service: str = "persistent_notification", language: str = "de"
    ) -> Dict[str, Any]:
        """Generate and send daily solar briefing notification. @zara

        Args:
            notify_service: Name of the notify service (e.g., "mobile_app_iphone")
            language: Language code ("de" or "en")

        Returns:
            Dictionary with result status and message preview
        """
        try:
            if (
                notify_service
                and notify_service != "persistent_notification"
                and ("mobile_app" in notify_service)
            ):
                notify_name = notify_service.replace("notify.", "")
                if not self.hass.services.has_service("notify", notify_name):
                    error_message = f"Notify service not found: notify.{notify_name}"
                    _LOGGER.error(error_message)
                    return {"success": False, "error": error_message}
            forecast = await self._get_today_forecast_data()
            if not forecast:
                return {
                    "success": False,
                    "error": "Final morning forecast not ready for daily briefing",
                    "skipped": True,
                }
            yesterday = await self._get_yesterday_actual_data()
            astronomy = await self._get_astronomy_data()
            weather = await self._get_today_weather_data()
            briefing = await self._generate_briefing_message(
                forecast, yesterday, astronomy, weather, language
            )
            persistent_payload = {
                "title": briefing["title"],
                "message": briefing["message"],
                "data": {"notification_id": "solar_briefing_daily", "tag": "solar_briefing"},
            }
            await self.hass.services.async_call(
                "notify", "persistent_notification", persistent_payload, blocking=True
            )
            _LOGGER.info("Full briefing sent to persistent_notification (HA UI)")
            if (
                notify_service
                and notify_service != "persistent_notification"
                and ("mobile_app" in notify_service)
            ):
                prediction_kwh = forecast["prediction_kwh"]
                clouds = weather.get("clouds") if weather else None
                weather_icon, unused_value = self._interpret_weather(
                    prediction_kwh, clouds, language
                )
                weather_description = self._get_weather_description(clouds, language)
                temperature_suffix = ""
                if weather and weather.get("temperature") is not None:
                    temperature = weather["temperature"]
                    temperature_suffix = f", {temperature:.0f}C"
                mobile_message = f"{weather_icon} {prediction_kwh:.2f} kWh | {weather_description}{temperature_suffix}"
                mobile_payload = {
                    "title": briefing["title"],
                    "message": mobile_message,
                    "data": {
                        "push": {"interruption-level": "time-sensitive"},
                        "presentation_options": ["alert", "sound"],
                    },
                }
                await self.hass.services.async_call(
                    "notify", notify_service.replace("notify.", ""), mobile_payload, blocking=True
                )
                _LOGGER.info(f"Additional mobile push notification sent to {notify_service}")
            _LOGGER.info(f"Daily solar briefing sent via {notify_service} (language: {language})")
            return {
                "success": True,
                "title": briefing["title"],
                "message_preview": briefing["message"][:100] + "...",
            }
        except Exception as error:
            _LOGGER.error(f"Failed to send daily briefing: {error}", exc_info=True)
            return {"success": False, "error": str(error)}

    async def _get_today_forecast_data(self) -> Optional[Dict[str, Any]]:
        """Get today's forecast data from database. @zara"""
        try:
            database = self.db_manager
            if not database:
                _LOGGER.warning("Database manager not available for forecast data")
                return
            today = dt_util.now().date().isoformat()
            final_due_at = await self._get_final_morning_forecast_due_at(today)
            row = await database.fetchone(
                "SELECT prediction_kwh, source, locked, locked_at\n                   FROM daily_forecasts\n                   WHERE forecast_date = ? AND forecast_type = 'today'",
                (today,),
            )
            if row:
                forecast = {
                    "date": today,
                    "prediction_kwh": row[0] or 0.0,
                    "source": row[1] or "unknown",
                    "locked": bool(row[2]) if row[2] is not None else False,
                    "locked_at": row[3],
                    "final_due_at": final_due_at.isoformat(),
                }
                if self._is_final_morning_forecast_ready(forecast, final_due_at):
                    return forecast
                _LOGGER.warning(
                    "Daily briefing skipped: the final morning forecast is not ready yet (due %s, today's forecast source=%s) - schedule the briefing after the final morning forecast",
                    final_due_at.strftime("%H:%M"),
                    forecast["source"],
                )
                return
            _LOGGER.warning(
                "Daily briefing skipped: no forecast stored for %s yet (final morning forecast due %s)",
                today,
                final_due_at.strftime("%H:%M"),
            )
        except Exception as error:
            _LOGGER.error(f"Error loading today forecast from DB: {error}")
            return

    async def _get_final_morning_forecast_due_at(self, today: str) -> datetime:
        """Return the earliest time when the final morning forecast is valid."""
        now = dt_util.now()
        default_sunrise = datetime.combine(now.date(), time(8, 0))
        if now.tzinfo:
            default_sunrise = default_sunrise.replace(tzinfo=now.tzinfo)
        sunrise = default_sunrise
        database = self.db_manager
        if database:
            row = await database.fetchone(
                "SELECT sunrise FROM astronomy_cache\n                   WHERE cache_date = ? AND hour = 12 LIMIT 1",
                (today,),
            )
            if row and row[0]:
                cached_sunrise = self._parse_datetime(row[0])
                if cached_sunrise:
                    sunrise = cached_sunrise
        return sunrise - _FINAL_MORNING_FORECAST_OFFSET

    def _is_final_morning_forecast_ready(
        self, forecast_data: Dict[str, Any], final_due_at: datetime
    ) -> bool:
        """Validate that the briefing uses the finalized morning forecast."""
        if not forecast_data.get("locked"):
            return False
        locked_at = self._parse_datetime(forecast_data.get("locked_at"))
        if locked_at is None:
            return False
        if locked_at.tzinfo is None and final_due_at.tzinfo is not None:
            locked_at = locked_at.replace(tzinfo=final_due_at.tzinfo)
        elif locked_at.tzinfo is not None and final_due_at.tzinfo is not None:
            locked_at = locked_at.astimezone(final_due_at.tzinfo)
        return locked_at >= final_due_at if True else None

    def _parse_datetime(self, value: Any) -> Optional[datetime]:
        """Parse database datetime values into local-aware datetimes when possible."""
        if value is None:
            return
        if isinstance(value, datetime):
            parsed = value
        else:
            raw_timestamp = str(value).strip()
            if not raw_timestamp:
                return
            try:
                parsed = datetime.fromisoformat(raw_timestamp.replace("Z", "+00:00"))
            except ValueError:
                return
        local_timezone = dt_util.now().tzinfo
        if parsed.tzinfo is None and local_timezone is not None:
            parsed = parsed.replace(tzinfo=local_timezone)
        elif parsed.tzinfo is not None and local_timezone is not None:
            parsed = parsed.astimezone(local_timezone)
        return parsed if True else None

    async def _get_yesterday_actual_data(self) -> Optional[Dict[str, Any]]:
        """Get yesterday's actual production from database. @zara"""
        try:
            database = self.db_manager
            if not database:
                return
            yesterday_date = (dt_util.now().date() - timedelta(days=1)).isoformat()
            row = await database.fetchone(
                "SELECT actual_total_kwh, predicted_total_kwh, accuracy_percent\n                   FROM daily_summaries\n                   WHERE date = ?",
                (yesterday_date,),
            )
            if row and row[0]:
                return {
                    "date": yesterday_date,
                    "actual_kwh": row[0] or 0.0,
                    "forecast_kwh": row[1] or 0.0,
                    "accuracy": row[2] or 0.0,
                }
        except Exception as error:
            _LOGGER.error(f"Error loading yesterday data from DB: {error}")
            return
        return None

    async def _get_astronomy_data(self) -> Optional[Dict[str, Any]]:
        """Get today's astronomy data from cache. @zara"""
        try:
            from ..astronomy.astronomy_cache_manager import get_cache_manager

            cache_manager = get_cache_manager()
            today = dt_util.now().date()
            astronomy = cache_manager.get_day_data(today)
            if astronomy:
                return {
                    "sunrise": astronomy.get("sunrise_time"),
                    "sunset": astronomy.get("sunset_time"),
                    "solar_noon": astronomy.get("solar_noon"),
                    "daylight_hours": astronomy.get("day_length_hours", 0.0),
                }
        except Exception as error:
            _LOGGER.error(f"Error loading astronomy data: {error}")
            return

    async def _get_today_weather_data(self) -> Optional[Dict[str, Any]]:
        """Get today's weather data from coordinator or DB fallback. @zara"""
        try:
            if self.coordinator:
                pipeline_manager = getattr(self.coordinator, "weather_pipeline_manager", None)
                if pipeline_manager:
                    weather_corrector = getattr(pipeline_manager, "weather_corrector", None)
                    if weather_corrector:
                        cached_weather = getattr(weather_corrector, "_current_weather_cache", None)
                        if cached_weather:
                            return {
                                "temperature": cached_weather.get("temperature"),
                                "clouds": cached_weather.get("clouds"),
                                "wind": cached_weather.get("wind"),
                                "humidity": cached_weather.get("humidity"),
                            }
            database = self.db_manager
            if database:
                today = dt_util.now().date().isoformat()
                row = await database.fetchone(
                    "SELECT AVG(pw.clouds), AVG(pw.temperature)\n                       FROM prediction_weather pw\n                       JOIN hourly_predictions hp ON pw.prediction_id = hp.prediction_id\n                       WHERE hp.target_date = ? AND hp.is_production_hour = 1",
                    (today,),
                )
                if row and row[0] is not None:
                    return {
                        "temperature": round(row[1], 1) if row[1] is not None else None,
                        "clouds": round(row[0], 0),
                        "wind": None,
                        "humidity": None,
                    }
        except Exception as error:
            _LOGGER.warning(f"Error loading weather data: {error}")
            return
        return None

    async def _get_peak_hour(self) -> Optional[Dict[str, Any]]:
        """Get the hour with highest predicted production today. @zara"""
        try:
            database = self.db_manager
            if not database:
                return
            today = dt_util.now().date().isoformat()
            row = await database.fetchone(
                "SELECT target_hour, prediction_kwh, prediction_method\n                   FROM hourly_predictions\n                   WHERE target_date = ? AND prediction_kwh > 0\n                   ORDER BY prediction_kwh DESC LIMIT 1",
                (today,),
            )
            if row:
                return {"hour": row[0], "kwh": row[1], "method": row[2]}
        except Exception:
            return

    async def _get_system_status(self, language: str) -> List[str]:
        """Get system status info for briefing. @zara"""
        status_lines = []
        try:
            database = self.db_manager
            if not database:
                return status_lines
            yesterday_date = (dt_util.now().date() - timedelta(days=1)).isoformat()
            daily_summary = await database.fetchone(
                "SELECT actual_total_kwh, accuracy_percent FROM daily_summaries WHERE date = ?",
                (yesterday_date,),
            )
            if daily_summary and daily_summary[0] is not None:
                accuracy = daily_summary[1]
                if accuracy is not None:
                    if language == "de":
                        status_lines.append(f"   Gestern: Forecast war {accuracy:.0f}% genau")
                    else:
                        status_lines.append(f"   Yesterday: Forecast was {accuracy:.0f}% accurate")
            learning_state = await database.fetchone(
                "SELECT COUNT(*), MAX(sample_count)\n                   FROM method_performance_learning\n                   WHERE last_updated >= ?",
                (yesterday_date,),
            )
            if (
                _published_dev_mode_active(self)
                and learning_state
                and learning_state[0]
                and (learning_state[0] > 0)
            ):
                active_buckets = learning_state[0]
                samples = learning_state[1] or 0
                if language == "de":
                    status_lines.append(
                        f"   KI-Lernstatus: {active_buckets} Wetter-Buckets aktiv ({samples} Samples)"
                    )
                else:
                    status_lines.append(
                        f"   AI learning: {active_buckets} weather buckets active ({samples} samples)"
                    )
            predictor = getattr(self.coordinator, "ai_predictor", None)
            if _published_dev_mode_active(self) and predictor:
                model_info = getattr(predictor, "_model_info", None)
                if model_info:
                    r2_score = model_info.get("r2_score")
                    if r2_score is not None:
                        if language == "de":
                            status_lines.append(f"   KI-Modell: R²={r2_score:.3f}")
                        else:
                            status_lines.append(f"   AI model: R²={r2_score:.3f}")
            exclusion_summary = None
            if _published_dev_mode_active(self):
                exclusion_summary = await self._get_learning_exclusion_summary(
                    yesterday_date, language
                )
            if exclusion_summary:
                status_lines.append(exclusion_summary)
        except Exception as error:
            _LOGGER.debug(f"System status info error: {error}")
        return status_lines if True else None

    async def _get_learning_exclusion_summary(self, date_str: str, language: str) -> Optional[str]:
        """Return a human-readable learning exclusion summary for one day."""
        database = self.db_manager
        if not database:
            return
        rows = await database.fetchall(
            "SELECT weather_alert_type, mppt_throttled, mppt_throttle_reason,\n                      inverter_clipped\n               FROM hourly_predictions\n               WHERE target_date = ?\n                 AND COALESCE(exclude_from_learning, 0) = 1\n               ORDER BY target_hour",
            (date_str,),
        )
        if not rows:
            return
        reason_counts = {}
        for row in rows:
            reason = self._learning_exclusion_reason(row)
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
        excluded_hours = sum(reason_counts.values())
        if excluded_hours <= 0:
            return
        reason_summary = self._format_learning_exclusion_reasons(reason_counts, language)
        if language == "de":
            return f"   Gestern: {excluded_hours}h vom Lernen ausgeschlossen ({reason_summary})"
        return f"   Yesterday: {excluded_hours}h excluded from learning ({reason_summary})"

    def _learning_exclusion_reason(self, row: Any) -> str:
        """Map an excluded hourly row to its primary exclusion reason."""
        if self._row_value(row, "inverter_clipped", 3):
            return "inverter_clipped"
        if self._row_value(row, "mppt_throttled", 1):
            return self._row_value(row, "mppt_throttle_reason", 2) or "mppt_throttled"
        return self._row_value(row, "weather_alert_type", 0) or "exclude_from_learning"

    def _format_learning_exclusion_reasons(
        self, reason_counts: Dict[str, int], language: str
    ) -> str:
        """Format compact exclusion reason text for the briefing."""
        german_labels = {
            "snow_covered_panels": "Schneebedeckung",
            "unexpected_snow": "unerwarteter Schnee",
            "unexpected_clouds": "Wetterabweichung",
            "unexpected_rain": "unerwarteter Regen",
            "unexpected_sun": "unerwartete Aufklarung",
            "inverter_clipped": "Inverter-Clipping",
            "mppt_throttled": "MPPT-Drosselung",
            "battery_full": "Akku voll / MPPT-Drosselung",
            "zero_export": "Nulleinspeisung / MPPT-Drosselung",
            "full_battery_zero_export": "Akku voll / Nulleinspeisung",
            "zero_export_limited": "Nulleinspeisung / MPPT-Drosselung",
            "manual_pause": "Lernpause",
            "manually_excluded": "manuelle Exklusion",
            "exclude_from_learning": "Schutzfilter",
            "weather_alert": "Wetterwarnung",
        }
        english_labels = {
            "snow_covered_panels": "snow-covered panels",
            "unexpected_snow": "unexpected snow",
            "unexpected_clouds": "weather deviation",
            "unexpected_rain": "unexpected rain",
            "unexpected_sun": "unexpected clearing",
            "inverter_clipped": "inverter clipping",
            "mppt_throttled": "MPPT throttling",
            "battery_full": "battery full / MPPT throttling",
            "zero_export": "zero export / MPPT throttling",
            "full_battery_zero_export": "battery full / zero export",
            "zero_export_limited": "zero export / MPPT throttling",
            "manual_pause": "learning paused",
            "manually_excluded": "manual exclusion",
            "exclude_from_learning": "safeguard filter",
            "weather_alert": "weather alert",
        }
        labels = german_labels if language == "de" else english_labels
        ordered_reasons = sorted(reason_counts.items(), key=lambda item: (-item[1], item[0]))
        if len(ordered_reasons) == 1:
            return labels.get(ordered_reasons[0][0], ordered_reasons[0][0])
        parts = [f"{labels.get(reason, reason)}: {hours}h" for reason, hours in ordered_reasons[:3]]
        other_hours = sum((hours for unused_value, hours in ordered_reasons[3:]))
        if other_hours:
            parts.append(f"{('weitere' if language == 'de' else 'other')}: {other_hours}h")
        return ", ".join(parts)

    @staticmethod
    def _row_value(row: Any, key: str, index: int) -> Any:
        """Read a DB row value by key with tuple-index fallback."""
        try:
            return row[key]
        except (KeyError, TypeError, IndexError):
            try:
                return row[index]
            except (TypeError, IndexError):
                return

    def _get_daily_quote(
        self, clouds: Optional[float], prediction_kwh: float, day_of_year: int, language: str
    ) -> str:
        """Get a rotating weather-appropriate daily quote. @zara"""
        if language == "de":
            sunny_quotes = [
                "Die Sonne meint es gut mit uns heute!",
                "Perfekter Tag fuer die Panels!",
                "Heute ernten wir Sonnenstrahlen.",
                "Die Solarzellen freuen sich schon!",
                "Ein guter Tag fuer saubere Energie.",
                "Sonne satt - die Panels laufen heiss!",
                "Heute tanken die Module ordentlich auf.",
            ]
            mixed_quotes = [
                "Auch hinter Wolken scheint die Sonne.",
                "Jedes bisschen Licht zaehlt!",
                "Die Panels machen auch bei Wolken nicht frei.",
                "Grau ist auch eine Farbe - die Panels arbeiten trotzdem.",
                "Diffuses Licht ist besser als kein Licht!",
                "Die Module trotzen den Wolken tapfer.",
                "Nicht jeder Tag kann ein Sonnentag sein.",
            ]
            cloudy_quotes = [
                "Heute ist ein Ruhetag fuer die Panels.",
                "Selbst an trueben Tagen - das System lernt weiter!",
                "Die KI nutzt auch wolkige Tage zum Lernen.",
                "Morgen scheint die Sonne bestimmt wieder!",
                "Geduld - bessere Tage kommen.",
                "Auch ein kleiner Ertrag ist ein Ertrag.",
                "Die Technik ruht sich aus fuer den naechsten Sonnentag.",
            ]
        else:
            sunny_quotes = [
                "The sun is smiling on us today!",
                "Perfect day for the panels!",
                "Today we harvest sunbeams.",
                "The solar cells are ready and waiting!",
                "A great day for clean energy.",
                "Sunshine galore - panels running hot!",
                "The modules are charging up nicely today.",
            ]
            mixed_quotes = [
                "The sun shines behind the clouds too.",
                "Every bit of light counts!",
                "Panels don't take days off for clouds.",
                "Diffuse light is better than no light!",
                "The modules bravely face the clouds.",
                "Not every day can be a sunny day.",
                "Even cloudy days have their moments.",
            ]
            cloudy_quotes = [
                "A rest day for the panels.",
                "Even on grey days, the system keeps learning!",
                "The AI uses cloudy days to learn too.",
                "Tomorrow the sun will shine again!",
                "Patience - better days are coming.",
                "A small yield is still a yield.",
                "The tech rests up for the next sunny day.",
            ]
        if clouds is not None:
            if clouds < 40:
                quotes = sunny_quotes
            elif clouds < 75:
                quotes = mixed_quotes
            else:
                quotes = cloudy_quotes
        elif prediction_kwh > 5:
            quotes = sunny_quotes
        elif prediction_kwh > 1:
            quotes = mixed_quotes
        else:
            quotes = cloudy_quotes
        return quotes[day_of_year % len(quotes)]

    async def _generate_briefing_message(
        self,
        forecast_data: Dict[str, Any],
        yesterday_data: Optional[Dict[str, Any]],
        astro_data: Optional[Dict[str, Any]],
        weather_data: Optional[Dict[str, Any]],
        language: str,
    ) -> Dict[str, str]:
        """Generate formatted briefing message. @zara

        Args:
            forecast_data: Today's forecast data
            yesterday_data: Yesterday's actual data (optional)
            astro_data: Today's astronomy data (optional)
            weather_data: Today's weather data (optional)
            language: Language code ("de" or "en")

        Returns:
            Dictionary with "title" and "message" keys
        """
        try:
            forecast_date = datetime.strptime(forecast_data["date"], "%Y-%m-%d")
        except (ValueError, TypeError, KeyError):
            _LOGGER.error("Invalid date format in forecast_data")
            forecast_date = dt_util.now()
        day_of_year = forecast_date.timetuple().tm_yday
        if language == "de":
            weekday = forecast_date.strftime("%A")
            translated_weekday = {
                "Monday": "Montag",
                "Tuesday": "Dienstag",
                "Wednesday": "Mittwoch",
                "Thursday": "Donnerstag",
                "Friday": "Freitag",
                "Saturday": "Samstag",
                "Sunday": "Sonntag",
            }.get(weekday, weekday)
            title = f"☀️ Solar Forecast - {translated_weekday}, {forecast_date.strftime('%d. %b')}"
        else:
            title = f"☀️ Solar Forecast - {forecast_date.strftime('%A, %b %d')}"
        lines = []
        prediction_kwh = forecast_data["prediction_kwh"]
        clouds = weather_data.get("clouds") if weather_data else None
        quote = self._get_daily_quote(clouds, prediction_kwh, day_of_year, language)
        lines.append(f"💬 {quote}")
        lines.append("")
        weather_icon, interpretation = self._interpret_weather(prediction_kwh, clouds, language)
        lines.append(f"{weather_icon} {interpretation}")
        lines.append("")
        lines.append(f"⚡ Forecast: {prediction_kwh:.2f} kWh")
        if yesterday_data:
            yesterday_kwh = yesterday_data["actual_kwh"]
            if yesterday_kwh > 0 and prediction_kwh > 0:
                production_ratio = prediction_kwh / yesterday_kwh
                if production_ratio > 1.5:
                    if language == "de":
                        lines.append(
                            f"   ↗️ {production_ratio:.1f}x besser als gestern ({yesterday_kwh:.2f} kWh)"
                        )
                    else:
                        lines.append(
                            f"   ↗️ {production_ratio:.1f}x better than yesterday ({yesterday_kwh:.2f} kWh)"
                        )
                elif production_ratio < 0.67:
                    if language == "de":
                        lines.append(
                            f"   ↘️ {1 / production_ratio:.1f}x weniger als gestern ({yesterday_kwh:.2f} kWh)"
                        )
                    else:
                        lines.append(
                            f"   ↘️ {1 / production_ratio:.1f}x less than yesterday ({yesterday_kwh:.2f} kWh)"
                        )
                elif language == "de":
                    lines.append(f"   ↔️ Aehnlich wie gestern ({yesterday_kwh:.2f} kWh)")
                else:
                    lines.append(f"   ↔️ Similar to yesterday ({yesterday_kwh:.2f} kWh)")
        lines.append("")
        peak = await self._get_peak_hour()
        if peak:
            peak_hour = peak["hour"]
            if language == "de":
                lines.append(
                    f"🌟 Beste Stunde: {peak_hour:02d}:00-{peak_hour + 1:02d}:00 Uhr ({peak['kwh']:.3f} kWh)"
                )
            else:
                lines.append(
                    f"🌟 Best hour: {peak_hour:02d}:00-{peak_hour + 1:02d}:00 ({peak['kwh']:.3f} kWh)"
                )
        weather_description = self._get_weather_description(clouds, language)
        temperature_suffix = ""
        if weather_data and weather_data.get("temperature") is not None:
            temperature_suffix = f" ({weather_data['temperature']:.0f}°C)"
        if language == "de":
            lines.append(f"🌤️ Wetter: {weather_description}{temperature_suffix}")
        else:
            lines.append(f"🌤️ Weather: {weather_description}{temperature_suffix}")
        if astro_data:
            daylight_hours = astro_data.get("daylight_hours", 0.0)
            daylight_whole_hours = int(daylight_hours)
            daylight_minutes = int((daylight_hours - daylight_whole_hours) * 60)
            sunrise = astro_data.get("sunrise")
            sunset = astro_data.get("sunset")
            daylight_times = ""
            if sunrise and sunset:
                try:
                    raw_sunrise = str(sunrise)
                    sunrise_time = (
                        raw_sunrise.split("T")[1][:5]
                        if "T" in raw_sunrise
                        else (
                            raw_sunrise.split(" ")[1][:5] if " " in raw_sunrise else raw_sunrise[:5]
                        )
                    )
                    raw_sunset = str(sunset)
                    sunset_time = (
                        raw_sunset.split("T")[1][:5]
                        if "T" in raw_sunset
                        else raw_sunset.split(" ")[1][:5] if " " in raw_sunset else raw_sunset[:5]
                    )
                    daylight_times = f" ({sunrise_time} - {sunset_time})"
                except Exception:
                    pass
            if language == "de":
                lines.append(
                    f"🌅 Tageslicht: {daylight_whole_hours}h {daylight_minutes}min{daylight_times}"
                )
            else:
                lines.append(
                    f"🌅 Daylight: {daylight_whole_hours}h {daylight_minutes}min{daylight_times}"
                )
        lines.append("")
        status_lines = await self._get_system_status(language)
        if status_lines:
            if language == "de":
                lines.append("🤖 System-Status:")
            else:
                lines.append("🤖 System Status:")
            lines.extend(status_lines)
            lines.append("")
        shadow_summary = None
        if _published_dev_mode_active(self):
            shadow_summary = await self._get_yesterday_shadow_summary(language)
        if shadow_summary:
            lines.append(shadow_summary)
            lines.append("")
        closing_message = self._get_closing_message(prediction_kwh, clouds, language)
        lines.append(closing_message)
        message = "\n".join(lines)
        return {"title": title, "message": message}

    def _interpret_weather(
        self, prediction_kwh: float, clouds: Optional[float], language: str
    ) -> tuple[str, str]:
        """Interpret weather from cloud cover and prediction value. @zara"""
        if clouds is not None:
            if clouds < 20:
                icon = "☀️"
                interpretation = (
                    "Voraussichtlich sonnig - gute Bedingungen!"
                    if language == "de"
                    else "Likely sunny - good conditions!"
                )
            elif clouds < 40:
                icon = "🌤️"
                interpretation = (
                    "Ueberwiegend sonnig erwartet" if language == "de" else "Mostly sunny expected"
                )
            elif clouds < 60:
                icon = "⛅"
                interpretation = (
                    "Wechselhaft - Wolken moeglich"
                    if language == "de"
                    else "Variable - clouds possible"
                )
            elif clouds < 80:
                icon = "🌥️"
                interpretation = (
                    "Eher bewoelkt erwartet" if language == "de" else "Rather cloudy expected"
                )
            else:
                icon = "☁️"
                interpretation = (
                    "Ueberwiegend bewoelkt erwartet"
                    if language == "de"
                    else "Mostly cloudy expected"
                )
        elif prediction_kwh > 15:
            icon = "☀️"
            interpretation = (
                "Gute Bedingungen moeglich" if language == "de" else "Good conditions possible"
            )
        elif prediction_kwh > 10:
            icon = "🌤️"
            interpretation = (
                "Ordentliche Produktion moeglich"
                if language == "de"
                else "Decent production possible"
            )
        elif prediction_kwh > 5:
            icon = "⛅"
            interpretation = (
                "Moderate Produktion erwartet"
                if language == "de"
                else "Moderate production expected"
            )
        elif prediction_kwh > 2:
            icon = "🌥️"
            interpretation = (
                "Eingeschraenkte Produktion wahrscheinlich"
                if language == "de"
                else "Limited production likely"
            )
        elif prediction_kwh > 0.5:
            icon = "☁️"
            interpretation = (
                "Geringe Produktion erwartet" if language == "de" else "Low production expected"
            )
        else:
            icon = "🌧️"
            interpretation = (
                "Kaum Produktion erwartet" if language == "de" else "Minimal production expected"
            )
        return (icon, interpretation) if True else None

    def _get_weather_description(self, clouds: Optional[float], language: str) -> str:
        """Get detailed weather description from cloud cover. @zara"""
        if clouds is None:
            return "Keine Wetterdaten" if language == "de" else "No weather data"
        if clouds < 10:
            return "Voraussichtlich klar" if language == "de" else "Expected clear"
        if clouds < 25:
            return "Voraussichtlich sonnig" if language == "de" else "Expected sunny"
        if clouds < 50:
            return "Wolken moeglich" if language == "de" else "Clouds possible"
        if clouds < 75:
            return "Eher bewoelkt" if language == "de" else "Rather cloudy"
        if clouds < 90:
            return "Stark bewoelkt erwartet" if language == "de" else "Heavy clouds expected"
        return "Ueberwiegend bedeckt erwartet" if language == "de" else "Overcast expected"

    def _get_closing_message(
        self, prediction_kwh: float, clouds: Optional[float], language: str
    ) -> str:
        """Get closing message based on prediction and cloud cover. @zara"""
        if clouds is not None:
            if clouds < 20:
                return "☀️ Gute Chancen auf Sonne!" if language == "de" else "☀️ Good chance of sun!"
            if clouds < 40:
                return "🌤️ Sonnenschein wahrscheinlich" if language == "de" else "🌤️ Sunshine likely"
            if clouds < 60:
                return "⛅ Sonne moeglich" if language == "de" else "⛅ Sun possible"
            if clouds < 80:
                return "🌥️ Wenig Sonne erwartet" if language == "de" else "🌥️ Little sun expected"
            return "☁️ Bewoelkung wahrscheinlich" if language == "de" else "☁️ Clouds likely"
        if prediction_kwh > 10:
            return "☀️ Gute Chancen auf Sonne!" if language == "de" else "☀️ Good chance of sun!"
        if prediction_kwh > 5:
            return (
                "🌤️ Ordentliche Produktion moeglich"
                if language == "de"
                else "🌤️ Decent production possible"
            )
        if prediction_kwh > 2:
            return "⛅ Etwas Sonne moeglich" if language == "de" else "⛅ Some sun possible"
        return "☁️ Wenig Sonne erwartet" if language == "de" else "☁️ Little sun expected"

    async def _get_yesterday_shadow_summary(self, language: str) -> Optional[str]:
        """Get yesterday's shadow detection summary from DB. @zara"""
        try:
            database = self.db_manager
            if not database:
                return
            yesterday_date = (dt_util.now().date() - timedelta(days=1)).isoformat()
            rows = await database.fetchall(
                "SELECT slh.hour, slh.shadow_detected, slh.root_cause,\n                          hp.actual_kwh, hp.prediction_kwh\n                   FROM shadow_learning_history slh\n                   LEFT JOIN hourly_predictions hp\n                       ON hp.target_date = slh.date AND hp.target_hour = slh.hour\n                   WHERE slh.date = ? AND slh.shadow_detected = 1",
                (yesterday_date,),
            )
            if not rows:
                return
            shadow_hours = len(rows)
            if shadow_hours == 0:
                return
            loss_kwh = 0.0
            dominant_cause = "unknown"
            cause_counts = {}
            for row in rows:
                actual_kwh = row[3] or 0
                predicted_kwh = row[4] or 0
                if predicted_kwh > actual_kwh:
                    loss_kwh += predicted_kwh - actual_kwh
                cause = row[2] or "unknown"
                cause_counts[cause] = cause_counts.get(cause, 0) + 1
            if cause_counts:
                dominant_cause = max(cause_counts.items(), key=lambda x: x[1])[0]
            if language == "de":
                header = "Schatten-Analyse (Gestern):"
                hours_line = f"   {shadow_hours}h Verschattung erkannt"
                loss_line = f"   Verlust: {loss_kwh:.2f} kWh"
                cause_labels = {
                    "weather_clouds": "Wolken",
                    "building_tree_obstruction": "Gebaeude/Baum",
                    "normal_variation": "Normale Variation",
                    "unknown": "Unbekannt",
                }
                cause_label = cause_labels.get(dominant_cause, dominant_cause)
                cause_line = f"   Ursache: {cause_label}"
            else:
                header = "Shadow Analysis (Yesterday):"
                hours_line = f"   {shadow_hours}h shadowing detected"
                loss_line = f"   Loss: {loss_kwh:.2f} kWh"
                cause_labels = {
                    "weather_clouds": "Clouds",
                    "building_tree_obstruction": "Building/Tree",
                    "normal_variation": "Normal variation",
                    "unknown": "Unknown",
                }
                cause_label = cause_labels.get(dominant_cause, dominant_cause)
                cause_line = f"   Cause: {cause_label}"
            return "\n".join([header, hours_line, loss_line, cause_line])
        except Exception as error:
            _LOGGER.warning(f"Failed to get shadow summary from DB: {error}")
            return
