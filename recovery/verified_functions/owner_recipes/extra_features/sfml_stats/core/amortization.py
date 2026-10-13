# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
from __future__ import annotations
from datetime import date
from typing import Any


def calculate_amortization(
    monthly_cashflows: list[dict[str, Any]],
    settings: dict[str, Any] | None,
    *,
    today: date | None = None,
) -> dict[str, Any]:
    """Calculate historical and projected PV amortization."""
    _var_var_14 = sanitize_amortization_settings(settings)
    _var_var_15 = max(0.0, _var_var_14["investment_eur"] - _var_var_14["subsidy_eur"])
    _var_var_16 = _var_var_15 > 0
    _var_var_17 = _date_to_month(_var_var_14.get("commissioning_date"))
    _var_var_18 = _var_var_14["annual_running_costs_eur"] / 12
    _var_var_19 = []
    _var_var_8 = -_var_var_15 if _var_var_16 else 0.0
    _var_var_20 = 0.0
    for _var_var_21 in sorted(
        monthly_cashflows, key=lambda item: str(item.get("month") or "")
    ):
        _var_var_2 = str(_var_var_21.get("month") or "")
        if not _var_var_2 or (_var_var_17 and _var_var_2 < _var_var_17):
            continue
        _var_var_22 = max(
            0.0,
            _to_float(
                _var_var_21.get("benefit_eur", _var_var_21.get("total_savings_eur"))
            ),
        )
        _var_var_23 = _var_var_18
        _var_var_24 = _var_var_22 - _var_var_23
        _var_var_8 += _var_var_24
        _var_var_20 += _var_var_24
        _var_var_19.append(
            {
                "month": _var_var_2,
                "benefit_eur": round(_var_var_22, 2),
                "running_cost_eur": round(_var_var_23, 2),
                "net_cashflow_eur": round(_var_var_24, 2),
                "cumulative_eur": round(_var_var_8, 2),
                "source": _var_var_21.get("source", "history"),
            }
        )
    _var_var_25 = len(_var_var_19)
    _var_var_26 = _var_var_19[-12:] if len(_var_var_19) > 12 else _var_var_19
    _var_var_27 = (
        sum((_var_var_40["net_cashflow_eur"] for _var_var_40 in _var_var_26))
        / len(_var_var_26)
        if _var_var_26
        else 0.0
    )
    _var_var_28 = _var_var_27 * 12
    _var_var_29 = max(0.0, -_var_var_8) if _var_var_16 else 0.0
    _var_var_30 = max(0.0, _var_var_20) / _var_var_15 * 100 if _var_var_16 else 0.0
    _var_var_31 = (
        _var_var_19[-1]["month"]
        if _var_var_19
        else (
            f"{today.year:04d}-{today.month:02d}"
            if today
            else date.today().strftime("%Y-%m")
        )
    )
    _var_var_32 = (
        _var_var_14["electricity_price_increase_percent"]
        - _var_var_14["degradation_percent"]
    )
    _var_var_33 = [
        ("conservative", 0.85, _var_var_32 - 1.0),
        ("realistic", 1.0, _var_var_32),
        ("optimistic", 1.15, _var_var_32 + 1.0),
    ]
    _var_var_34 = []
    for _var_var_35, _var_var_36, _var_var_10 in _var_var_33:
        _var_var_37 = (
            _scenario_projection(
                start_month=_var_var_31,
                start_cumulative=_var_var_8,
                base_monthly_cashflow=_var_var_27,
                annual_growth_percent=_var_var_10,
                factor=_var_var_36,
            )
            if _var_var_16 and _var_var_8 < 0 and (_var_var_27 > 0)
            else {
                "break_even_month": (
                    _var_var_31 if _var_var_16 and _var_var_8 >= 0 else None
                ),
                "months_to_break_even": 0 if _var_var_16 and _var_var_8 >= 0 else None,
                "points": [],
            }
        )
        _var_var_34.append({"id": _var_var_35, "factor": _var_var_36, **_var_var_37})
    _var_var_38 = next(
        (
            _var_var_40
            for _var_var_40 in _var_var_34
            if _var_var_40["id"] == "realistic"
        ),
        _var_var_34[0],
    )
    _var_var_12 = _var_var_38.get("break_even_month")
    _var_var_39 = int(_var_var_12[:4]) if _var_var_12 else None
    return {
        "configured": _var_var_16,
        "settings": _var_var_14,
        "summary": {
            "net_investment_eur": round(_var_var_15, 2),
            "accumulated_benefit_eur": round(_var_var_20, 2),
            "remaining_eur": round(_var_var_29, 2),
            "progress_percent": round(min(100.0, max(0.0, _var_var_30)), 1),
            "observed_months": _var_var_25,
            "annualized_benefit_eur": round(_var_var_28, 2),
            "break_even_month": _var_var_12,
            "break_even_year": _var_var_39,
            "months_to_break_even": _var_var_38.get("months_to_break_even"),
            "status": (
                "reached"
                if _var_var_16 and _var_var_8 >= 0
                else "projected" if _var_var_12 else "unavailable"
            ),
        },
        "series": _var_var_19,
        "projection": _var_var_38.get("points", []),
        "scenarios": _var_var_34,
    }
