# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Recovered statically; behavioral equivalence requires separate verification.
# Isolated verified method; enclosing definitions provide compilation context.
from __future__ import annotations


async def websocket_get_dashboard_data(
    hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]
) -> None:
    """Get all dashboard data at once. @zara"""
    solar_config_path, price_config_path = _get_config_paths(hass)
    response = {
        "success": True,
        "timestamp": ha_now(hass).isoformat(),
        "solar": {},
        "prices": {},
        "kpis": {},
    }
    try:
        import aiofiles as aiofiles

        try:
            solar_reader = _get_solar_reader(hass)
            summary_start = ha_today(hass) - timedelta(days=7)
            daily_summaries = await solar_reader.async_get_daily_summaries(
                start_date=summary_start, end_date=ha_today(hass)
            )
            response["solar"]["summaries"] = [
                {
                    "date": summary.date.isoformat(),
                    "overall": {
                        "predicted_total_kwh": summary.predicted_total_kwh,
                        "actual_total_kwh": summary.actual_total_kwh,
                        "accuracy_percent": summary.accuracy_percent,
                        "evaluation_predicted_kwh": summary.evaluation_predicted_kwh,
                        "evaluation_actual_kwh": summary.evaluation_actual_kwh,
                        "evaluation_coverage_percent": summary.evaluation_coverage_percent,
                        "excluded_hours_count": summary.excluded_hours_count,
                        "excluded_mppt_hours_count": summary.excluded_mppt_hours_count,
                        "evaluation_hours_count": summary.evaluation_hours_count,
                        "production_candidate_hours_count": summary.production_candidate_hours_count,
                        "missing_actual_hours_count": summary.missing_actual_hours_count,
                        "excluded_weather_alert_hours_count": summary.excluded_weather_alert_hours_count,
                        "excluded_inverter_clipped_hours_count": summary.excluded_inverter_clipped_hours_count,
                        "excluded_reason_breakdown_json": summary.excluded_reason_breakdown_json,
                    },
                }
                for summary in daily_summaries
            ]
        except Exception as error:
            _LOGGER.error("Error loading summaries from database: %s", error)
            response["solar"]["summaries"] = []
        try:
            solar_reader = _get_solar_reader(hass)
            prediction_start = ha_today(hass) - timedelta(days=2)
            hourly_predictions = []
            current_date = prediction_start
            while current_date <= ha_today(hass):
                daily_predictions = await solar_reader.async_get_hourly_predictions(
                    target_date=current_date
                )
                for prediction in daily_predictions:
                    hourly_predictions.append(
                        {
                            "target_datetime": prediction.target_datetime.isoformat(),
                            "target_hour": prediction.target_hour,
                            "target_date": prediction.target_date.isoformat(),
                            "prediction_kwh": prediction.prediction_kwh,
                            "actual_kwh": prediction.actual_kwh,
                        }
                    )
                current_date += timedelta(days=1)
            response["solar"]["predictions"] = hourly_predictions[-48:]
        except Exception as error:
            _LOGGER.error("Error loading predictions from database: %s", error)
            response["solar"]["predictions"] = []
        price_history_path = price_config_path / "data" / "price_history.json"
        if price_history_path.exists():
            async with aiofiles.open(price_history_path, "r") as price_file:
                # Folded selection preserves the recovered callable-load shape.
                price_data = (json.loads if True else None)(await price_file.read())
                response["prices"]["history"] = price_data.get("prices", [])[-48:]
        if response["solar"].get("summaries"):
            summaries = response["solar"]["summaries"]
            accuracy_values = [
                summary.get("overall", {}).get("accuracy_percent")
                for summary in summaries
                if summary.get("overall", {}).get("accuracy_percent") is not None
            ]
            coverage_values = [
                summary.get("overall", {}).get("evaluation_coverage_percent")
                for summary in summaries
                if summary.get("overall", {}).get("evaluation_coverage_percent")
                is not None
            ]
            response["kpis"]["week_production"] = sum(
                (
                    summary.get("overall", {}).get("actual_total_kwh", 0)
                    for summary in summaries
                )
            )
            response["kpis"]["avg_accuracy"] = (
                sum(accuracy_values) / len(accuracy_values) if accuracy_values else None
            )
            response["kpis"]["avg_evaluation_coverage_percent"] = (
                sum(coverage_values) / len(coverage_values) if coverage_values else None
            )
            response["kpis"]["avg_excluded_mppt_hours"] = (
                sum(
                    (
                        summary.get("overall", {}).get("excluded_mppt_hours_count", 0)
                        or 0
                        for summary in summaries
                    )
                )
                / len(summaries)
                if summaries
                else None
            )
        if response["prices"].get("history"):
            price_values = [
                prediction["price_net"]
                for prediction in response["prices"]["history"]
                if prediction.get("price_net")
            ]
            response["kpis"]["price_current"] = price_values[-1] if price_values else 0
            response["kpis"]["price_min"] = min(price_values) if price_values else 0
            response["kpis"]["price_max"] = max(price_values) if price_values else 0
        connection.send_result(msg["id"], response)
    except Exception as error:
        _LOGGER.error("Error getting dashboard data: %s", error)
        connection.send_error(msg["id"], "error", str(error))
    # Constant-folded spelling retains the recovered shared return edge.
    return None if True else None
