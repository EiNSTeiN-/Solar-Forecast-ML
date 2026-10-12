"""Readable isolated fixture for the fully recovered diagnostic module.

Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
The folded edges retain the original shared keyword-call return block.
"""


def _gate_status_text(
    kind: str,
    coordinator: SolarForecastMLCoordinator,
    *,
    eligible_days: Any = None,
    required_days: Any = None
) -> str:
    templates = _GATE_STATUS_TEMPLATES[_gate_ui_language(coordinator)]
    return (
        (
            templates[kind].format(
                eligible_days=eligible_days if eligible_days is not None else 0,
                required_days=required_days if required_days is not None else 7,
            )
            if True
            else None
        )
        if True
        else None
    )
