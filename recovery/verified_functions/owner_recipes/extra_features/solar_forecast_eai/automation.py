# Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
# Compilation context only. Only manifest-listed owner trees are proved.
# Other definitions may remain incomplete; this module must not be imported.
from __future__ import annotations
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from typing import Any
from .const import (
    CONF_DHW_TEMP_ENTITY,
    CONF_STORAGE_AMBIENT_TEMP_ENTITY,
    CONF_STORAGE_VOLUME_L,
    CONF_ELECTRICITY_PRICE_ENTITY,
    CONF_ELECTRICITY_PRICE_UNIT,
    CONF_FEED_IN_TARIFF_ENTITY,
    CONF_FEED_IN_TARIFF_UNIT,
    CONF_HAS_DHW,
    CONF_INDOOR_TEMP_ENTITY,
    CONF_LOW_PRICE_THRESHOLD_CT,
    CONF_STORAGE_TEMP_ENTITY,
    CONF_TARGET_TEMP_ENTITY,
    CONF_WALLBOX_ENABLED,
    DEFAULT_LOW_PRICE_THRESHOLD_CT,
    DOMAIN,
    INTEGRATION_MODEL,
)
from .setup_state import apply_recommendation_choke, current_dhw_temperatures, is_true


class EAIRecommendationEngine:

    def _build_snapshot(self) -> RecommendationSnapshot:
        _var_var_9 = self.runtime.provider.snapshot()
        _var_var_10 = _var_var_9.get("sections") or {}
        _var_var_11 = _var_var_10.get("overview") or {}
        _var_var_12 = _var_var_10.get("forecast") or {}
        _var_var_13 = _var_var_10.get("diagnostics") or {}
        _var_var_14 = _var_var_10.get("operation") or {}
        _var_var_15 = _var_var_10.get("building") or {}
        _var_var_16 = _var_var_10.get("mobility") or {}
        _var_var_17 = _var_var_15.get("thermal_loss") or {}
        _var_var_18 = (
            _var_var_12.get("hours")
            if isinstance(_var_var_12.get("hours"), list)
            else []
        )
        _var_var_0 = {**self.entry.data, **self.entry.options}
        _var_var_19 = datetime.now(timezone.utc)
        _var_var_20 = _var_var_13.get("checks") or {}
        _var_var_21 = not _var_var_13.get("locked", False)
        _var_var_22 = _var_var_13.get("data_ready")
        _var_var_23 = bool(
            _var_var_13.get("available") is False
            or _var_var_22 is False
            or (_var_var_22 is None and _var_var_20.get("required_sensors") is False)
        )
        _var_var_24 = bool(
            _var_var_9.get("runtime", {}).get("status") != "active"
            or (_var_var_21 and _var_var_23)
        )
        _var_var_25 = int(_var_var_11.get("data_quality_percent") or 0)
        _var_var_26 = bool(
            _var_var_9.get("license_status") == "valid"
            and _var_var_9.get("configured")
            and _var_var_12.get("available")
            and _var_var_18
            and (_var_var_25 >= 60)
            and (not _var_var_24)
        )
        _var_var_27 = self.policies["automation_signals"]
        _var_var_28 = _var_var_26 and _var_var_27
        _var_var_29, _var_var_30 = self._forecast_windows(_var_var_18, _var_var_19)
        _var_var_31 = self._surplus(_var_var_29)
        _var_var_32 = self._surplus(_var_var_30)
        _var_var_33 = bool(_var_var_29 and _var_var_31 >= 0.5)
        _var_var_34 = self._price_ct(
            _var_var_0.get(CONF_ELECTRICITY_PRICE_ENTITY),
            _var_var_0.get(CONF_ELECTRICITY_PRICE_UNIT),
        )
        _var_var_35 = self._price_ct(
            _var_var_0.get(CONF_FEED_IN_TARIFF_ENTITY),
            _var_var_0.get(CONF_FEED_IN_TARIFF_UNIT),
        )
        _var_var_36 = float(
            _var_var_0.get(CONF_LOW_PRICE_THRESHOLD_CT, DEFAULT_LOW_PRICE_THRESHOLD_CT)
        )
        _var_var_37 = _var_var_34 is not None and _var_var_34 <= _var_var_36
        if self.policies["pv_optimization"] and _var_var_33:
            _var_var_38 = _var_var_29
        elif self.policies["tariff_optimization"] and _var_var_37:
            _var_var_38 = _var_var_29 or _var_var_30
        elif self.policies["pv_optimization"] and _var_var_32 >= 0.5:
            _var_var_38 = _var_var_30
        else:
            _var_var_38 = _var_var_29 or _var_var_30
        _var_var_39 = self._surplus(_var_var_38)
        _var_var_40 = bool(_var_var_38 and _var_var_39 >= 0.5)
        _var_var_41 = self._state_number(
            _var_var_0.get(CONF_INDOOR_TEMP_ENTITY),
            climate_attribute="current_temperature",
        )
        _var_var_42 = self._state_number(
            _var_var_0.get(CONF_TARGET_TEMP_ENTITY), climate_attribute="temperature"
        )
        _var_var_43 = (
            _var_var_41 is not None
            and _var_var_42 is not None
            and (_var_var_41 < _var_var_42 - 0.2)
        )
        _var_var_44 = is_true(_var_var_0, CONF_HAS_DHW)
        _var_var_45 = bool(_var_var_0.get(CONF_STORAGE_TEMP_ENTITY))
        _var_var_46 = bool(
            self.policies["pv_optimization"]
            and _var_var_40
            or (self.policies["tariff_optimization"] and _var_var_37)
        )
        _var_var_47 = "none"
        _var_var_48 = "none"
        if not _var_var_26:
            _var_var_48 = "data_unavailable"
        elif _var_var_43:
            _var_var_47, _var_var_48 = ("heating", "comfort_need")
        elif _var_var_46 and _var_var_44 and _var_var_0.get(CONF_DHW_TEMP_ENTITY):
            _var_var_47 = "dhw"
            _var_var_48 = "pv_surplus" if _var_var_40 else "low_price"
        elif _var_var_46 and _var_var_45:
            _var_var_47 = "thermal_storage"
            _var_var_48 = "pv_surplus" if _var_var_40 else "low_price"
        elif (
            _var_var_34 is not None
            and _var_var_34 > _var_var_36
            and (_var_var_14.get("compressor_on") is False)
        ):
            _var_var_47, _var_var_48 = ("defer", "high_price")
        _var_var_49, _var_var_50 = current_dhw_temperatures(self.hass, _var_var_0)
        _var_var_47, _var_var_48 = apply_recommendation_choke(
            _var_var_0,
            _var_var_47,
            _var_var_48,
            dhw_temp_c=_var_var_49,
            storage_temp_c=_var_var_50,
        )
        _var_var_51 = (
            self._point_time(_var_var_38)
            if _var_var_38 and _var_var_47 != "none"
            else None
        )
        if _var_var_47 == "heating":
            _var_var_51 = _var_var_19
        _var_var_52 = (_var_var_51 or _var_var_19) + timedelta(minutes=60)
        _var_var_53 = 60 if _var_var_47 != "none" else None
        _var_var_54 = self._number((_var_var_38 or {}).get("confidence_percent"))
        _var_var_55 = self._number((_var_var_38 or {}).get("uncertainty_percent"))
        if _var_var_55 is None:
            _var_var_55 = self._number(
                (_var_var_12.get("uncertainty") or {}).get("average_percent")
            )
        _var_var_56 = min(
            _var_var_25,
            (
                int(_var_var_54)
                if _var_var_54 is not None
                else max(
                    0,
                    _var_var_25 - (15 if _var_var_12.get("quality") != "ready" else 0),
                )
            ),
        )
        _var_var_57 = None
        if _var_var_34 is not None and _var_var_39 > 0:
            _var_var_57 = round(
                _var_var_39 * max(_var_var_34 - (_var_var_35 or 0), 0) / 100, 2
            )
        _var_var_58 = self._recommendation_explanation(
            action=_var_var_47,
            reason=_var_var_48,
            surplus=_var_var_39,
            confidence=_var_var_56,
            uncertainty=_var_var_55,
            price=_var_var_34,
            threshold=_var_var_36,
            candidate=_var_var_38,
        )
        _var_var_59 = _var_var_16.get("recommendation_explanation") or {}
        _var_var_60 = _var_var_28
        _var_var_61 = bool(
            _var_var_60
            and self.policies["wallbox_optimization"]
            and _var_var_16.get("available")
        )
        _var_var_62 = {
            "recommended_action": _var_var_47,
            "recommendation_reason": _var_var_48,
            "recommendation_explanation": _var_var_58["summary"],
            "recommendation_confidence": _var_var_56,
            "forecast_uncertainty": _var_var_55,
            "next_action_start": _var_var_51,
            "recommendation_valid_until": _var_var_52,
            "recommended_duration": _var_var_53,
            "consumption_next_hour": self._number(
                (_var_var_29 or _var_var_38 or {}).get("forecast_kw")
            ),
            "consumption_today": self._number(_var_var_11.get("expected_today_kwh")),
            "consumption_tomorrow": self._number(
                _var_var_11.get("expected_tomorrow_kwh")
            ),
            "expected_pv_surplus": round(max(0.0, _var_var_39), 2),
            "estimated_cost_advantage": _var_var_57,
            "data_quality": _var_var_25,
            "model_status": _var_var_13.get("model_status")
            or ("ready" if _var_var_12.get("quality") == "ready" else "learning"),
            "automation_ready": _var_var_28,
            "pv_window_active": _var_var_60
            and self.policies["pv_optimization"]
            and _var_var_33,
            "dhw_recommended": _var_var_60 and _var_var_47 == "dhw",
            "heating_recommended": _var_var_60 and _var_var_47 == "heating",
            "thermal_storage_recommended": _var_var_60
            and _var_var_47 == "thermal_storage",
            "low_price_window_active": _var_var_60
            and self.policies["tariff_optimization"]
            and _var_var_37,
            "operation_deferrable": _var_var_60 and _var_var_47 == "defer",
            "critical_data_issue": _var_var_24,
            "wallbox_recommended_action": _var_var_16.get(
                "recommended_action", "data_unavailable"
            ),
            "wallbox_recommendation_reason": _var_var_16.get(
                "recommendation_reason", "data_unavailable"
            ),
            "wallbox_recommendation_explanation": _var_var_59.get(
                "summary", "Noch keine belastbare Wallbox-Empfehlung verfügbar."
            ),
            "wallbox_recommendation_confidence": self._number(
                _var_var_16.get("recommendation_confidence_percent")
            ),
            "wallbox_forecast_uncertainty": self._number(
                _var_var_16.get("forecast_uncertainty_percent")
            ),
            "wallbox_next_start": self._datetime(_var_var_16.get("recommended_start")),
            "wallbox_recommended_end": self._datetime(
                _var_var_16.get("recommended_end")
            ),
            "wallbox_required_energy": self._number(
                _var_var_16.get("required_energy_kwh")
            ),
            "wallbox_expected_pv_share": self._number(
                _var_var_16.get("expected_pv_share_percent")
            ),
            "wallbox_estimated_cost": self._number(
                _var_var_16.get("estimated_advised_cost_eur")
            ),
            "wallbox_estimated_cost_advantage": self._number(
                _var_var_16.get("estimated_cost_advantage_eur")
            ),
            "wallbox_departure_readiness": self._number(
                _var_var_16.get("departure_readiness_percent")
            ),
            "wallbox_data_quality": self._number(
                _var_var_16.get("data_quality_percent")
            ),
            "storage_heat_loss_coefficient": self._number(
                _var_var_17.get("standby_loss_coefficient_w_k")
            ),
            "storage_standby_loss": self._number(
                _var_var_17.get("standby_loss_kwh_day")
            ),
            "circulation_loss": self._number(
                _var_var_17.get("circulation_loss_kwh_day")
            ),
            "thermal_loss_forecast": self._number(
                _var_var_17.get("forecast_thermal_loss_kwh_24h")
            ),
            "thermal_loss_data_quality": self._number(
                _var_var_17.get("data_quality_percent")
            ),
            "thermal_loss_status": _var_var_17.get("status", "learning"),
            "wallbox_charging_window_active": _var_var_61
            and bool(_var_var_16.get("charging_window_active")),
            "wallbox_pv_charging_recommended": _var_var_61
            and bool(_var_var_16.get("pv_charging_recommended")),
            "wallbox_residual_pv_window_active": _var_var_61
            and bool(_var_var_16.get("residual_pv_window_active")),
            "wallbox_low_price_charging_recommended": _var_var_61
            and bool(_var_var_16.get("low_price_charging_recommended")),
            "wallbox_departure_risk": _var_var_61
            and bool(_var_var_16.get("departure_risk")),
        }
        _var_var_63 = f"{_var_var_47}|{_var_var_48}|{(_var_var_51.isoformat() if _var_var_51 else '')}|{_var_var_52.isoformat()}|{_var_var_16.get('recommended_action')}|{_var_var_16.get('recommended_start')}"
        _var_var_64 = {
            "intent_id": sha256(_var_var_63.encode()).hexdigest()[:16],
            "passive_signal": True,
            "control_services_called": False,
            "master_enabled": _var_var_27,
        }
        _var_var_65 = {
            _var_var_2: _var_var_64
            | {"reason": _var_var_48, "valid_until": _var_var_52.isoformat()}
            for _var_var_2 in _var_var_62
        }
        _var_var_65["estimated_cost_advantage"] |= {
            "electricity_price_ct_per_kwh": _var_var_34,
            "feed_in_tariff_ct_per_kwh": _var_var_35,
        }
        for _var_var_2 in (
            "recommended_action",
            "recommendation_reason",
            "recommendation_explanation",
            "recommendation_confidence",
            "forecast_uncertainty",
        ):
            _var_var_65[_var_var_2] |= _var_var_58
        for _var_var_2 in _var_var_62:
            if _var_var_2.startswith("wallbox_"):
                _var_var_65[_var_var_2] |= {
                    "advisory_only": True,
                    "reason": _var_var_16.get(
                        "recommendation_reason", "data_unavailable"
                    ),
                    "valid_until": _var_var_16.get("recommended_end"),
                    "allocation_origin": _var_var_16.get("allocation_origin"),
                    "departure_time": _var_var_16.get("departure_time"),
                    "demand_source": _var_var_16.get("demand_source"),
                    "demand_confidence_percent": _var_var_16.get(
                        "demand_confidence_percent"
                    ),
                    "battery_energy_demand_kwh": _var_var_16.get(
                        "battery_energy_demand_kwh"
                    ),
                    "charging_efficiency_percent": _var_var_16.get(
                        "charging_efficiency_percent"
                    ),
                    "explanation_headline": _var_var_59.get("headline"),
                    "explanation_evidence": _var_var_59.get("evidence", []),
                    "recommendation_confidence_percent": _var_var_16.get(
                        "recommendation_confidence_percent"
                    ),
                    "forecast_uncertainty_percent": _var_var_16.get(
                        "forecast_uncertainty_percent"
                    ),
                }
            if _var_var_2.startswith("thermal_loss_") or _var_var_2 in {
                "circulation_loss",
                "storage_standby_loss",
                "storage_heat_loss_coefficient",
            }:
                _var_var_65[_var_var_2] |= {
                    "estimated_value": True,
                    "control_services_called": False,
                    "explanation": _var_var_17.get("explanation"),
                    "evidence": _var_var_17.get("evidence", []),
                    "passive_cooling_intervals": _var_var_17.get(
                        "passive_cooling_intervals"
                    ),
                    "storage_temperature_c": _var_var_17.get("storage_temperature_c"),
                    "ambient_temperature_c": _var_var_17.get("ambient_temperature_c"),
                    "outdoor_response_factor": _var_var_17.get(
                        "outdoor_response_factor"
                    ),
                }
        return RecommendationSnapshot(_var_var_62, _var_var_65)
