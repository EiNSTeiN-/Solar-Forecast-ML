# Powered by Solar Forecast ML by Zara-Toorox. Subject to repository LICENSE.
"""Derived electricity price mode for STATS (GPM is the tariff owner)."""
from __future__ import annotations
from typing import Any
from ..const import (
    CONF_BILLING_BASE_FEE,
    CONF_COST_TRACKING_ENABLED,
    CONF_FEED_IN_TARIFF,
    CONF_LEGACY_FIXED_PRICE_CT,
    DEFAULT_BILLING_BASE_FEE,
    DEFAULT_FEED_IN_TARIFF,
    DOMAIN,
)

GPM_DOMAIN = "grid_price_monitor"
ISSUE_PRICE_CONFIG_MOVED = "price_config_moved_to_gpm"
MODE_GPM_DYNAMIC = "gpm_dynamic"
MODE_GPM_FIXED = "gpm_fixed"
MODE_GPM_TIME_OF_USE = "gpm_time_of_use"
MODE_GPM_TIME_WINDOWS = "gpm_time_windows"
MODE_GPM_CSV_COMMUNITY = "gpm_csv_community"
MODE_LEGACY_FIXED = "legacy_fixed"
MODE_NONE = "none"
GPM_MODES = frozenset(
    {
        MODE_GPM_DYNAMIC,
        MODE_GPM_FIXED,
        MODE_GPM_TIME_OF_USE,
        MODE_GPM_TIME_WINDOWS,
        MODE_GPM_CSV_COMMUNITY,
    }
)
FIXED_LIKE_MODES = frozenset({MODE_GPM_FIXED, MODE_LEGACY_FIXED})


def _positive_price(value: Any) -> float | None:
    """Feed-in and similar strictly positive amounts."""
    if value in (None, ""):
        return
    try:
        numeric_price = float(value)
    except (TypeError, ValueError):
        return
    if numeric_price <= 0:
        return
    return numeric_price


def _present_price(value: Any) -> float | None:
    """Hour total: only None and 0 are missing (NULLIF(..., 0)). Negatives are valid."""
    if value in (None, ""):
        return
    try:
        numeric_price = float(value)
    except (TypeError, ValueError):
        return
    if numeric_price == 0:
        return
    return numeric_price


def _is_repair_open(hass: Any, issue_id: str) -> bool:
    try:
        from homeassistant.helpers import issue_registry as issue_registry

        registry = issue_registry.async_get(hass)
        return registry.async_get_issue(DOMAIN, issue_id) is not None
    except Exception:
        return False


def gpm_snapshot(hass: Any) -> dict[str, Any] | None:
    """Return the live GPM coordinator snapshot, if a provider is present."""
    hass_data = getattr(hass, "data", None) or {}
    providers = hass_data.get(GPM_DOMAIN) or {}
    for provider in providers.values():
        snapshot_method = getattr(provider, "snapshot", None)
        if callable(snapshot_method):
            try:
                snapshot = snapshot_method()
            except Exception:
                snapshot = None
            if isinstance(snapshot, dict):
                return snapshot
        provider_data = getattr(provider, "data", None)
        if not isinstance(provider_data, dict):
            continue
        return {
            "is_demo": bool(provider_data.get("is_demo")),
            "tariff_mode": provider_data.get("tariff_mode") or "dynamic",
            "tariff_label": provider_data.get("tariff_label"),
            "feed_in_tariff_ct": provider_data.get("feed_in_tariff_ct"),
            "base_fee_eur_month": provider_data.get("base_fee_eur_month"),
            "has_spot_component": provider_data.get("has_spot_component"),
        }


def _mapping_fixed_total(container: Any) -> float | None:
    """Read a GPM fixed tariff from an entry mapping or a snapshot."""
    if not isinstance(container, dict):
        return
    fixed_config = container.get("fixed")
    if isinstance(fixed_config, dict):
        total_price = _present_price(fixed_config.get("total_price"))
        if total_price is not None:
            return total_price
    return _present_price(container.get("fixed_total_price"))


def gpm_fixed_total_ct(hass: Any) -> float | None:
    """Constant ct/kWh of the GPM fixed tariff. No EUR conversion.

    The dialog unit is ct/kWh (``config_flow._fixed_schema``).
    ``_parse_fixed_input`` stores that number unchanged as
    ``fixed.total_price``. ``tariffs.factory._fixed_source`` passes it to
    ``FixedSource``, which labels it ct/kWh and writes the same number into
    each slot's ``total_price``. ``PriceWriter`` inserts that slot value
    into ``GPM_price_history.total_price``. STATS therefore reads ct/kWh.

    Options override data, the same order as ``coordinator._entry_config``.
    The coordinator snapshot does not publish this field; a snapshot value
    is only used when present.
    """
    hass_data = getattr(hass, "data", None) or {}
    providers = hass_data.get(GPM_DOMAIN) or {}
    for provider in providers.values():
        config_entry = getattr(provider, "entry", None)
        for entry_mapping in (
            (
                getattr(config_entry, "options", None)
                if config_entry is not None
                else None
            ),
            getattr(config_entry, "data", None) if config_entry is not None else None,
        ):
            total_price = _mapping_fixed_total(entry_mapping)
            if not total_price is not None:
                continue
            return total_price
        snapshot_method = getattr(provider, "snapshot", None)
        if not callable(snapshot_method):
            continue
        try:
            snapshot = snapshot_method()
        except Exception:
            snapshot = None
        total_price = _mapping_fixed_total(snapshot)
        if not total_price is not None:
            continue
        return total_price


def gpm_provider_present(hass: Any) -> bool:
    hass_data = getattr(hass, "data", None) or {}
    providers = hass_data.get(GPM_DOMAIN) or {}
    return any((hasattr(provider, "data") for provider in providers.values()))


def effective_price_mode(hass: Any, config: dict[str, Any] | None) -> str:
    """Resolve the runtime price mode. Never reads billing_price_mode."""
    config = config or {}
    if config.get(CONF_COST_TRACKING_ENABLED, True) is False:
        return MODE_NONE
    if config.get(CONF_LEGACY_FIXED_PRICE_CT) not in (None, "") and _is_repair_open(
        hass, ISSUE_PRICE_CONFIG_MOVED
    ):
        return MODE_LEGACY_FIXED
    current_snapshot = gpm_snapshot(hass)
    if current_snapshot is not None:
        if current_snapshot.get("is_demo") is True:
            return MODE_NONE
        tariff_mode = str(current_snapshot.get("tariff_mode") or "dynamic")
        return f"gpm_{tariff_mode}"
    if gpm_provider_present(hass):
        return MODE_GPM_DYNAMIC
    return MODE_NONE


def is_gpm_mode(mode: str | None) -> bool:
    return mode in GPM_MODES


def is_fixed_like(mode: str | None) -> bool:
    return mode in FIXED_LIKE_MODES


def shows_spot(mode: str | None) -> bool:
    return mode == MODE_GPM_DYNAMIC


def feed_in_tariff_ct(hass: Any, config: dict[str, Any] | None) -> float:
    config = config or {}
    current_snapshot = gpm_snapshot(hass)
    if current_snapshot is not None:
        tariff_ct = _positive_price(current_snapshot.get("feed_in_tariff_ct"))
        if tariff_ct is not None:
            return tariff_ct
        if current_snapshot.get("feed_in_tariff_ct") == 0:
            return 0.0
    try:
        return max(
            0.0, float(config.get(CONF_FEED_IN_TARIFF, DEFAULT_FEED_IN_TARIFF) or 0)
        )
    except (TypeError, ValueError):
        return DEFAULT_FEED_IN_TARIFF


def base_fee_eur_month(hass: Any, config: dict[str, Any] | None) -> float:
    config = config or {}
    current_snapshot = gpm_snapshot(hass)
    if current_snapshot is not None:
        base_fee = current_snapshot.get("base_fee_eur_month")
        if base_fee not in (None, ""):
            try:
                return max(0.0, float(base_fee))
            except (TypeError, ValueError):
                pass
    try:
        return max(
            0.0, float(config.get(CONF_BILLING_BASE_FEE, DEFAULT_BILLING_BASE_FEE) or 0)
        )
    except (TypeError, ValueError):
        return DEFAULT_BILLING_BASE_FEE


FEE_SOURCE_GPM = "gpm"
FEE_SOURCE_STATS_LEGACY = "stats_legacy"


def effective_fees(hass: Any, config: dict[str, Any] | None) -> dict[str, Any]:
    """Effective feed-in tariff and base fee with origin for the GPM page."""
    config = config or {}
    current_snapshot = gpm_snapshot(hass)
    fee_source = FEE_SOURCE_STATS_LEGACY
    if current_snapshot is not None:
        tariff_present = current_snapshot.get("feed_in_tariff_ct") not in (None, "")
        base_fee_present = current_snapshot.get("base_fee_eur_month") not in (None, "")
        if tariff_present or base_fee_present:
            fee_source = FEE_SOURCE_GPM
    return {
        "feed_in_tariff_ct": feed_in_tariff_ct(hass, config),
        "base_fee_eur_month": base_fee_eur_month(hass, config),
        "source": fee_source,
    }


def resolve_hour_price(
    *,
    gpm_total: Any = None,
    stored_price: Any = None,
    daily_average: Any = None,
    current_average: Any = None,
    mode: str | None = None,
    config: dict[str, Any] | None = None,
    fixed_total_ct: Any = None,
) -> float | None:
    """Hour price fallback. Never returns a silent 0; negatives are billed.

    ``gpm_fixed`` uses the constant GPM fixed price only after history,
    the stored hour, and the daily averages are missing. Other modes
    ignore ``fixed_total_ct``.
    """
    for candidate_price in (gpm_total, stored_price, daily_average, current_average):
        total_price = _present_price(candidate_price)
        if not total_price is not None:
            continue
        return total_price
    if mode == MODE_LEGACY_FIXED:
        return _present_price((config or {}).get(CONF_LEGACY_FIXED_PRICE_CT))
    if mode == MODE_GPM_FIXED:
        return _present_price(fixed_total_ct)


async def load_price_lookups(
    db: Any, today_str: str | None = None
) -> tuple[dict[str, float], float | None]:
    """Preload GPM daily averages and today's current-price average."""
    daily_prices = {}
    try:
        async with db.execute(
            "SELECT date, average_total FROM GPM_daily_averages"
        ) as cursor:
            async for price_row in cursor:
                total_price = _present_price(
                    price_row["average_total"]
                    if isinstance(price_row, dict)
                    else price_row[1]
                )
                price_date = (
                    price_row["date"] if isinstance(price_row, dict) else price_row[0]
                )
                if total_price is not None and price_date:
                    daily_prices[str(price_date)] = total_price
    except Exception:
        pass
    current_price = None
    try:
        async with db.execute(
            "SELECT average_today FROM GPM_current_price WHERE id = 1"
        ) as cursor:
            price_row = await cursor.fetchone()
            if price_row is not None:
                current_price = _present_price(
                    price_row["average_today"]
                    if isinstance(price_row, dict)
                    else price_row[0]
                )
    except Exception:
        current_price = None
    if today_str is None:
        return (daily_prices, current_price)
    return (daily_prices, current_price)


async def lookup_hour_price_from_db(
    db: Any,
    *,
    date_str: str,
    hour: int,
    stored_price: Any,
    mode: str,
    config: dict[str, Any],
    today_str: str,
    daily_averages: dict[str, float] | None = None,
    current_average: float | None = None,
    fixed_total_ct: float | None = None,
) -> float | None:
    gpm_total = None
    try:
        async with db.execute(
            "\n            SELECT total_price FROM GPM_price_history\n            WHERE timestamp >= ? AND timestamp < ? AND hour = ?\n            ORDER BY timestamp DESC LIMIT 1\n            ",
            (f"{date_str}T00:00:00", f"{date_str}T24:00:00", hour),
        ) as cursor:
            price_row = await cursor.fetchone()
            if price_row is not None:
                gpm_total = (
                    price_row["total_price"]
                    if isinstance(price_row, dict)
                    else price_row[0]
                )
    except Exception:
        gpm_total = None
    daily_prices = daily_averages if daily_averages is not None else {}
    return resolve_hour_price(
        gpm_total=gpm_total,
        stored_price=stored_price,
        daily_average=daily_prices.get(date_str),
        current_average=current_average if date_str == today_str else None,
        mode=mode,
        config=config,
        fixed_total_ct=fixed_total_ct,
    )
