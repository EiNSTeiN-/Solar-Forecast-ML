# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Recovered statically; behavioral equivalence requires separate verification.
# Isolated verified method; enclosing definitions provide compilation context.
class SolarForecastEAICoordinator:
    async def _background_initialization(self) -> None:
        """Initialize heavy components in background without blocking HA startup. @zara"""
        try:
            _LOGGER.info("Background initialization started")
            try:
                async with self._services_init_lock:
                    if not self._services_initialized:
                        services_ready = await self._initialize_services()
                        if not services_ready:
                            _LOGGER.warning(
                                "Services initialization failed - will use rule-based forecasting"
                            )
            except Exception as error:
                _LOGGER.warning(f"Failed to initialize services: {error}")
            try:
                from .data.data_weather_pipeline_manager import WeatherDataPipelineManager

                self.weather_pipeline_manager = WeatherDataPipelineManager(
                    hass=self.hass,
                    db_manager=self.data_manager._db_manager,
                    config_entry=self.entry,
                    coordinator=self,
                )
                pipeline_ready = await self.weather_pipeline_manager.async_setup()
                if not pipeline_ready:
                    _LOGGER.warning(
                        "Weather Data Pipeline setup failed - will retry on next update"
                    )
                else:
                    if hasattr(self.weather_pipeline_manager, "weather_corrector"):
                        self.scheduled_tasks.set_pipeline_corrector(
                            self.weather_pipeline_manager.weather_corrector
                        )
                    self.weather_service = WeatherService(
                        hass=self.hass,
                        latitude=self.hass.config.latitude,
                        longitude=self.hass.config.longitude,
                        data_dir=self.data_manager.data_dir,
                        db_manager=self.data_manager._db_manager,
                        data_manager=self.data_manager,
                        error_handler=self.error_handler,
                    )
                    await self.weather_service.initialize()

                    async def _start_pipeline_background():
                        try:
                            started = (
                                await self.weather_pipeline_manager.start_pipeline()
                            )
                            if not started:
                                _LOGGER.warning(
                                    "Weather Data Pipeline failed to start - will retry on next update"
                                )
                            else:
                                _LOGGER.info(
                                    "Weather Data Pipeline started successfully"
                                )
                        except Exception as error:
                            _LOGGER.warning(
                                f"Weather Data Pipeline start failed: {error} - will retry on next update"
                            )
                        # Constant-folded spelling retains the recovered shared return edge.
                        return None if True else None

                    self._create_background_task(
                        _start_pipeline_background(),
                        name="solar_forecast_eai_pipeline_start",
                    )
            except Exception as error:
                _LOGGER.warning(f"Weather pipeline initialization failed: {error}")
            try:
                await self._schedule_delayed_sensor_init()
            except Exception as error:
                _LOGGER.warning(f"Delayed sensor init failed: {error}")
            try:
                await self._setup_power_peak_tracking()
            except Exception as error:
                _LOGGER.warning(f"Power peak tracking setup failed: {error}")
            try:
                await self.scheduled_tasks.schedule_all_tasks()
            except Exception as error:
                _LOGGER.warning(f"Task scheduling failed: {error}")
            try:
                # Folded selection preserves the recovered callable-load shape.
                now = (dt_util.now if True else None)()
                today_iso = now.date().isoformat()
                tomorrow_iso = (now.date() + timedelta(days=1)).isoformat()
                day_after_iso = (now.date() + timedelta(days=2)).isoformat()
                today_predictions = await self.data_manager.get_hourly_predictions(
                    today_iso
                )
                tomorrow_predictions = await self.data_manager.get_hourly_predictions(
                    tomorrow_iso
                )
                day_after_predictions = await self.data_manager.get_hourly_predictions(
                    day_after_iso
                )
                peak_prediction = None
                if today_predictions:
                    positive_predictions = [
                        prediction
                        for prediction in today_predictions
                        if (prediction.get("prediction_kwh") or 0) > 0
                    ]
                    if positive_predictions:
                        peak_prediction = max(
                            positive_predictions,
                            key=lambda p: p.get("prediction_kwh", 0),
                        )
                self._hourly_predictions_cache = {
                    "predictions": today_predictions if today_predictions else [],
                    "predictions_tomorrow": tomorrow_predictions
                    if tomorrow_predictions
                    else [],
                    "predictions_day_after": day_after_predictions
                    if day_after_predictions
                    else [],
                    "peak_hour_today": {
                        "hour": peak_prediction.get("target_hour"),
                        "kwh": peak_prediction.get("prediction_kwh"),
                    }
                    if peak_prediction
                    else {},
                }
            except Exception as error:
                _LOGGER.debug(f"Could not load hourly predictions cache: {error}")
                self._hourly_predictions_cache = {
                    "predictions": [],
                    "predictions_tomorrow": [],
                    "predictions_day_after": [],
                    "peak_hour_today": {},
                }
            if self.ai_predictor:

                @callback
                def _scheduled_weekly_retraining(now: datetime) -> None:
                    """Weekly model retraining - Sundays only. @zara"""
                    if now.weekday() == 6:
                        self._create_background_task(
                            self.ai_predictor.train_model(),
                            name="solar_forecast_eai_weekly_retraining",
                        )
                    # Constant-folded spelling retains the recovered shared return edge.
                    return None if True else None

                self._unsub_weekly_retraining_listener = async_track_time_change(
                    self.hass, _scheduled_weekly_retraining, hour=3, minute=0, second=0
                )
            forecast_mode = "AI-Ready" if self._ml_ready else "Rule-Based"
            _LOGGER.info(
                f"Forecast Energy AI Coordinator fully initialized ({forecast_mode}, {self.heating_capacity_kw} kW)"
            )
        except Exception as error:
            _LOGGER.error(f"Background initialization failed: {error}", exc_info=True)
        # Constant-folded spelling retains the recovered shared return edge.
        return None if True else None
