# Separate DC battery banks on one shared AC load bus

Powered by Solar Forecast ML by Zara-Toorox.

This analysis applies to upstream V50.0.0, commit
`4488a619ca3be8845dd379eb8faa744b9ad5c520`. This fork currently repairs
remote dashboard card access. **It does not yet implement a multi-bank
forecast, change battery charging controls, or change inverter settings.**

## Required physical model

The installation has two parallel inverter outputs supplying one shared
AC load bus, with separate DC battery banks. Inverter A owns PV groups 1
and 2; inverter B owns PV group 3. The shared AC bus allows either inverter
to supply either building. A building's local demand is therefore not the
same quantity as its inverter's contribution to the shared bus.

PV charging belongs to the bank physically connected to that inverter.
Available stored energy can be summed for shared loads, subject to each
inverter's connection state, SOC reserve, discharge limits and efficiency.
AC/generator charging and any supported transfer through the AC bus must
be recorded separately from local PV charging. The controller's behavior
must be measured; a shared AC connection does not imply a shared DC bank.

An average SOC cannot identify curtailment correctly. For example, one
bank at 100% and another at 50% have a 75% capacity-weighted average if
their capacities match. PV associated with the full bank may be curtailed
while the other bank continues charging. A global battery flag can
mistake the first array's curtailed output for shading or bad weather.

## What V50 already provides

| Capability | Evidence | Limit |
| --- | --- | --- |
| Separate PV groups | `config_flow.py`, `CONF_PANEL_GROUPS` | Each group has its own DC power sensor and geometry. |
| Battery diagnostics | `config_flow.py` options and `const.py:111` | One SOC sensor and one PV-to-battery sensor. |
| One integration instance | `config_flow.py:507` | The flow uses a single fixed unique ID. Removing that check alone is insufficient. |
| One AC capacity setting | `CONF_INVERTER_MAX_POWER` | Cannot express separate inverter limits or constraints associated with one bank. |
| Shared persistent storage | `__init__.py:1273`, `core/database_service` | Data lives under one `solar_forecast_ml` directory and one shared database service. |
| Public environment mapping contract | `data/sensor_mapping_provider.py` | Exposes weather inputs, not a bank topology or a bank-aware training contract. |

## Source availability and feasibility

The current package contains 442 Python files: 163 readable files and 279
PyArmor-protected files, including companion modules. The source distribution
permits personal, noncommercial modification and redistribution with its
license and attribution retained. Protected modules are left untouched.

The configuration flow, integration entry points, sensor platform dispatcher,
some database service interfaces and frontend JavaScript are readable.
Adding bank selectors, immutable bank IDs, sensor mappings, per-bank
telemetry, availability diagnostics and tests is feasible in those parts.

The coordinator, production collector, battery curtailment/classification,
panel-group forecast calculation, much of the database schema and the
training/forecasting pipeline are protected. Their relevant source is not
available in this checkout. There is no documented public contract for
passing independent bank curtailment flags into per-group learning.

**A complete, verified multi-bank forecasting implementation is not a small
configuration-flow patch against the current distribution.** It requires
either source access for the affected modules or an independently implemented
bank-aware pipeline with explicit interfaces to the forecasting engine.
Adding UI fields without connecting those fields to training would create
a misleading configuration screen, so this release does not do that.

Do not patch hidden coordinator attributes, decrypt PyArmor payloads,
disable license checks, or treat an aggregate SOC as a substitute for bank
ownership. Two isolated component instances would also need independent
storage, IDs and scheduled tasks; duplicating the config entry is not a
verified solution.

## Proposed implementation contract

Keep one installation and one shared weather source. Add a topology inside
that installation:

1. **PV groups:** existing IDs, geometry, power sources and group forecasts.
2. **Inverters:** immutable IDs; assigned PV groups; real AC power;
   PV input, AC conversion, battery charging and discharge limits;
   operating/connection state. Distinguish the AC rating from the DC PV
   rating and from the PV-to-battery charging limit.
3. **Battery banks:** immutable IDs; owning inverter; SOC; authoritative
   capacity helper; usable energy/reserve; charge and discharge power;
   PV charging and AC charging sources; validity and telemetry age.
4. **Shared load bus:** one measured demand source; generator contribution;
   signed inverter contributions; total available stored energy.

Use configurable entity selectors and stable IDs. A PV group must belong
to exactly one inverter, and an independently connected DC bank must have
an explicit owner. Refuse duplicate membership and incompatible units.
Retain existing entity IDs and history/statistics metadata.

For current observations, use measured directional powers whenever they
exist. A fallback local PV charging estimate is:

`min(local_pv_power, max(battery_charge_power - ac_charge_power, 0))`.

This remains an estimate: power sources can be sampled at different times,
measure at different conversion boundaries and include conversion losses.
Keep the estimate's provenance visible and do not reinterpret it as a meter.
Unavailable data must remain unavailable, not become a zero contribution.

Stored energy and combined SOC are:

`E_stored = sum(capacity_i * SOC_i / 100)`

`SOC_system = 100 * E_stored / sum(capacity_i)`.

For shared-load availability, include only banks whose inverter is connected
and ready, then apply that bank's reserve. Do not silently include an offline
bank's stale SOC. Use battery energy helpers, not an incorrect Modbus capacity
register. With three 280 Ah batteries at 51.2 V, nominal calculated capacity
is 43.008 kWh per bank; the marketed capacity is approximately 42.9 kWh.

Keep charging and discharging as separate nonnegative directional totals.
One bank can charge while the other discharges; netting those quantities
would conceal both flows. Use signed real AC power for bus balance and
distinguish receiving AC power from producing it. Avoid summing both
building feeders and inverter outputs into the same load demand.

## Training and forecast changes

Store synchronized observations and quality flags per bank and inverter,
linked to the existing PV group IDs. Bank-full curtailment must affect only
the groups attached to that bank. Distinguish curtailment, inverter clipping,
disconnection, telemetry gaps and real shading before training.

The solar resource forecast is separate from a forecast of usable/accepted
energy. A battery being full must not make the model learn that sunlight
or panel capability disappeared. Emit both quantities with clear names.
Use an observation only when the relevant bank and inverter data are valid;
retain usable observations from the other independent inverter.

For a future shared-load simulation, first forecast each PV group, then
apply that inverter's conversion and charging constraints. Local PV can
charge its own bank or supply the shared bus according to the inverter's
actual capability. Allocate the residual shared demand between available
inverters using an explicit, validated sharing policy and enforce SOC,
reserve, power limits and losses at each simulation step.

A forecast of each bank's discharge share cannot assume a fixed 50/50 split.
Learn the observed parallel-controller policy or expose a configurable
policy, and identify its uncertainty. Do not claim exact future per-bank
SOC when the hardware's sharing behavior is unknown.

## Staged work and acceptance criteria

1. Obtain the affected source or a supported bank-aware engine contract.
   Establish regression fixtures for the existing single-bank model.
2. Add topology configuration, validation and a migration that keeps the
   current single-bank configuration valid. Test entity ID preservation.
3. Add bank/inverter observations and expose a diagnostic power balance.
   Compare with real inverter readings before using them in training.
4. Add per-group curtailment classification and storage/training changes.
   Verify that one full/offline bank does not suppress the other array.
5. Add per-bank forecasts and a constrained shared-load simulation.
   Extend STATS/cards through supported read-only interfaces.
6. Run in parallel with the existing forecasts before making any scheduling
   or inverter-control decisions from the new model.

Required cases include equal/unequal capacities; one full bank; one empty
bank; simultaneous bank charging/discharging; generator charging; one
disconnected inverter; stale or unavailable readings; different input
units; independent clipping; signed AC transfer; restart recovery; timezone
and DST boundaries; energy balance; reserve/charge limits; and continuity
of existing statistics and entity IDs.

## Fork release scope: remote cards

Upstream STATS returns HTTP 403 with `local_access_required` for its static
JavaScript when accessed through an HTTP proxy. Those URLs are unsuitable
as globally loaded Lovelace resources in this installation.

This fork publishes the three cards and their data adapter through HA's
normal `/local/` asset route. Those files contain code and artwork, not
private telemetry. Live values come from the authenticated frontend's
`hass.states`, with no additional public data endpoint. Unknown values
display as missing rather than being converted to zero.

The forecast card uses the exposed ML forecast and array sensors. It does
not manufacture forecast accuracy or historical hourly actuals. The weather
card displays the selected HA weather entity; the unavailable protected
STATS radiation/astronomy corrections are not presented as native weather
data. The warning card's explicit `mode: status` displays the ML status
sensor without requiring or activating Energy AI. Original EAI weather
warning behavior remains available when an appropriate licensed integration
is configured by its owner.

Existing Forecast.Solar entries and all existing ML sensor IDs are preserved.
The original learning/forecasting engine remains upstream V50 code.
