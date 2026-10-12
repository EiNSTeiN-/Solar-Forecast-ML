# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Isolated verified coroutine; the enclosing definitions are compilation context.


class SolarForecastEAICoordinator:
    async def _background_initialization(self):
        async def _start_pipeline_background():
            try:
                started = await self.weather_pipeline_manager.start_pipeline()
                if not started:
                    _LOGGER.warning(
                        "Weather Data Pipeline failed to start - will retry on next update"
                    )
                else:
                    _LOGGER.info("Weather Data Pipeline started successfully")
            except Exception as error:
                _LOGGER.warning(
                    f"Weather Data Pipeline start failed: {error} - will retry on next update"
                )
            # CPython folds this expression to None but retains the shared
            # return edge used by the recovered coroutine's exception handler.
            # A bare `return None` duplicates terminal returns and fails the
            # strict instruction comparison. No condition executes at runtime.
            return None if True else None
