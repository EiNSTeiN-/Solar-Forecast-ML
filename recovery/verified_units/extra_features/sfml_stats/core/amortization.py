# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""Amortization calculation helpers for SFML Stats."""
from __future__ import annotations
from datetime import date
from typing import Any

DEFAULT_AMORTIZATION_SETTINGS: dict[str, Any] = {
    "investment_eur": 0.0,
    "subsidy_eur": 0.0,
    "commissioning_date": None,
    "annual_running_costs_eur": 0.0,
    "electricity_price_increase_percent": 2.0,
    "degradation_percent": 0.5,
}


def _to_float(value: Any, fallback: float = 0.0) -> float:
    try:
        number = float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return fallback
    if number != number:
        return fallback
    return number


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def _month_add(month_key: str, months: int = 1) -> str:
    year, month = (int(month_part) for month_part in month_key.split("-"))
    month += months
    year += (month - 1) // 12
    month = (month - 1) % 12 + 1
    return f"{year:04d}-{month:02d}"


def _date_to_month(value: Any) -> str | None:
    if not value:
        return
    date_text = str(value).strip()
    try:
        parsed_date = date.fromisoformat(date_text[:10])
    except ValueError:
        return
    return f"{parsed_date.year:04d}-{parsed_date.month:02d}"


def sanitize_amortization_settings(
    payload: dict[str, Any] | None, current: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Normalize user-provided amortization settings."""
    settings = {**DEFAULT_AMORTIZATION_SETTINGS, **(current or {}), **(payload or {})}
    commissioning_date = settings.get("commissioning_date")
    if commissioning_date:
        try:
            commissioning_date = date.fromisoformat(
                str(commissioning_date)[:10]
            ).isoformat()
        except ValueError:
            commissioning_date = current.get("commissioning_date") if current else None
    return {
        "investment_eur": round(max(0.0, _to_float(settings.get("investment_eur"))), 2),
        "subsidy_eur": round(max(0.0, _to_float(settings.get("subsidy_eur"))), 2),
        "commissioning_date": commissioning_date or None,
        "annual_running_costs_eur": round(
            max(0.0, _to_float(settings.get("annual_running_costs_eur"))), 2
        ),
        "electricity_price_increase_percent": round(
            _clamp(
                _to_float(settings.get("electricity_price_increase_percent"), 2.0),
                -10.0,
                20.0,
            ),
            2,
        ),
        "degradation_percent": round(
            _clamp(_to_float(settings.get("degradation_percent"), 0.5), 0.0, 10.0), 2
        ),
    }


def _scenario_projection(
    *,
    start_month: str,
    start_cumulative: float,
    base_monthly_cashflow: float,
    annual_growth_percent: float,
    factor: float,
    max_months: int = 480,
) -> dict[str, Any]:
    cumulative_eur = float(start_cumulative)
    month = start_month
    monthly_cashflow_eur = max(0.0, base_monthly_cashflow * factor)
    monthly_growth_rate = (1 + annual_growth_percent / 100) ** 0.08333333333333333 - 1
    projection_points = []
    break_even_month = None
    for month_index in range(1, max_months + 1):
        month = _month_add(month)
        cumulative_eur += monthly_cashflow_eur
        projection_points.append(
            {
                "month": month,
                "cumulative_eur": round(cumulative_eur, 2),
                "net_cashflow_eur": round(monthly_cashflow_eur, 2),
            }
        )
        if cumulative_eur >= 0:
            break_even_month = month
            break
        monthly_cashflow_eur = max(
            0.0, monthly_cashflow_eur * (1 + monthly_growth_rate)
        )
    return (
        {
            "break_even_month": break_even_month,
            "months_to_break_even": (
                len(projection_points) if break_even_month else None
            ),
            "points": projection_points,
        }
        if True
        else None
    )


def calculate_amortization(
    monthly_cashflows: list[dict[str, Any]],
    settings: dict[str, Any] | None,
    *,
    today: date | None = None,
) -> dict[str, Any]:
    """Calculate historical and projected PV amortization."""
    normalized_settings = sanitize_amortization_settings(settings)
    net_investment_eur = max(
        0.0, normalized_settings["investment_eur"] - normalized_settings["subsidy_eur"]
    )
    configured = net_investment_eur > 0
    commissioning_month = _date_to_month(normalized_settings.get("commissioning_date"))
    monthly_running_cost_eur = normalized_settings["annual_running_costs_eur"] / 12
    history_series = []
    cumulative_eur = -net_investment_eur if configured else 0.0
    accumulated_cashflow_eur = 0.0
    for cashflow in sorted(
        monthly_cashflows, key=lambda item: str(item.get("month") or "")
    ):
        month = str(cashflow.get("month") or "")
        if not month or (commissioning_month and month < commissioning_month):
            continue
        benefit_eur = max(
            0.0,
            _to_float(cashflow.get("benefit_eur", cashflow.get("total_savings_eur"))),
        )
        running_cost_eur = monthly_running_cost_eur
        net_cashflow_eur = benefit_eur - running_cost_eur
        cumulative_eur += net_cashflow_eur
        accumulated_cashflow_eur += net_cashflow_eur
        history_series.append(
            {
                "month": month,
                "benefit_eur": round(benefit_eur, 2),
                "running_cost_eur": round(running_cost_eur, 2),
                "net_cashflow_eur": round(net_cashflow_eur, 2),
                "cumulative_eur": round(cumulative_eur, 2),
                "source": cashflow.get("source", "history"),
            }
        )
    observed_months = len(history_series)
    recent_cashflows = (
        history_series[-12:] if len(history_series) > 12 else history_series
    )
    average_monthly_cashflow_eur = (
        sum((cashflow["net_cashflow_eur"] for cashflow in recent_cashflows))
        / len(recent_cashflows)
        if recent_cashflows
        else 0.0
    )
    annualized_benefit_eur = average_monthly_cashflow_eur * 12
    remaining_eur = max(0.0, -cumulative_eur) if configured else 0.0
    progress_percent = (
        max(0.0, accumulated_cashflow_eur) / net_investment_eur * 100
        if configured
        else 0.0
    )
    projection_start_month = (
        history_series[-1]["month"]
        if history_series
        else (
            f"{today.year:04d}-{today.month:02d}"
            if today
            else date.today().strftime("%Y-%m")
        )
    )
    annual_net_growth_percent = (
        normalized_settings["electricity_price_increase_percent"]
        - normalized_settings["degradation_percent"]
    )
    scenario_parameters = [
        ("conservative", 0.85, annual_net_growth_percent - 1.0),
        ("realistic", 1.0, annual_net_growth_percent),
        ("optimistic", 1.15, annual_net_growth_percent + 1.0),
    ]
    scenarios = []
    for (
        scenario_id,
        scenario_factor,
        scenario_annual_growth_percent,
    ) in scenario_parameters:
        scenario_projection = (
            _scenario_projection(
                start_month=projection_start_month,
                start_cumulative=cumulative_eur,
                base_monthly_cashflow=average_monthly_cashflow_eur,
                annual_growth_percent=scenario_annual_growth_percent,
                factor=scenario_factor,
            )
            if configured and cumulative_eur < 0 and (average_monthly_cashflow_eur > 0)
            else {
                "break_even_month": (
                    projection_start_month
                    if configured and cumulative_eur >= 0
                    else None
                ),
                "months_to_break_even": (
                    0 if configured and cumulative_eur >= 0 else None
                ),
                "points": [],
            }
        )
        scenarios.append(
            {"id": scenario_id, "factor": scenario_factor, **scenario_projection}
        )
    realistic_scenario = next(
        (scenario for scenario in scenarios if scenario["id"] == "realistic"),
        scenarios[0],
    )
    break_even_month = realistic_scenario.get("break_even_month")
    break_even_year = int(break_even_month[:4]) if break_even_month else None
    return {
        "configured": configured,
        "settings": normalized_settings,
        "summary": {
            "net_investment_eur": round(net_investment_eur, 2),
            "accumulated_benefit_eur": round(accumulated_cashflow_eur, 2),
            "remaining_eur": round(remaining_eur, 2),
            "progress_percent": round(min(100.0, max(0.0, progress_percent)), 1),
            "observed_months": observed_months,
            "annualized_benefit_eur": round(annualized_benefit_eur, 2),
            "break_even_month": break_even_month,
            "break_even_year": break_even_year,
            "months_to_break_even": realistic_scenario.get("months_to_break_even"),
            "status": (
                "reached"
                if configured and cumulative_eur >= 0
                else "projected" if break_even_month else "unavailable"
            ),
        },
        "series": history_series,
        "projection": realistic_scenario.get("points", []),
        "scenarios": scenarios,
    }
