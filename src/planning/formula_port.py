"""Backward-compatible planning calculation API.

Historically all migrated Excel formula logic lived in this single module. The
implementation is now organized by business responsibility under
``planning.domain``. Imports from ``planning.formula_port`` are intentionally
kept working so the production engine and existing tests do not need a risky
big-bang migration.

New code should import from ``planning.domain`` directly.
"""

from .domain.demand import build_finished_goods_projection, forecast_by_month
from .domain.materials import (
    aggregate_open_po,
    build_daily_material_allocation,
    build_material_inbound_plan,
    material_demand_periods,
    material_direct_projection,
    standardize_direct_bom,
    standardize_flat_bom,
)
from .domain.production import (
    build_algorithmic_daily_schedule,
    build_fc_end_stock,
    build_weekly_production_plan,
)
from .domain.purchasing import (
    abc_cycle,
    abc_feasibility,
    abc_risk,
    build_abc_rows,
    build_purchase_plan,
    next_workday,
    previous_workday,
    purchase_action,
    purchase_dates,
    purchase_priority,
    purchase_quantity,
    purchase_status,
    shortage_date,
)

__all__ = [
    "abc_cycle",
    "abc_feasibility",
    "abc_risk",
    "aggregate_open_po",
    "build_abc_rows",
    "build_algorithmic_daily_schedule",
    "build_daily_material_allocation",
    "build_fc_end_stock",
    "build_finished_goods_projection",
    "build_material_inbound_plan",
    "build_purchase_plan",
    "build_weekly_production_plan",
    "forecast_by_month",
    "material_demand_periods",
    "material_direct_projection",
    "next_workday",
    "previous_workday",
    "purchase_action",
    "purchase_dates",
    "purchase_priority",
    "purchase_quantity",
    "purchase_status",
    "shortage_date",
    "standardize_direct_bom",
    "standardize_flat_bom",
]
