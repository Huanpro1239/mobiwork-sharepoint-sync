from src.planning import formula_port
from src.planning.domain.demand import forecast_by_month
from src.planning.domain.materials import build_material_inbound_plan
from src.planning.domain.production import build_weekly_production_plan
from src.planning.domain.purchasing import build_purchase_plan


def test_formula_port_is_compatibility_facade():
    assert formula_port.forecast_by_month is forecast_by_month
    assert formula_port.build_material_inbound_plan is build_material_inbound_plan
    assert formula_port.build_purchase_plan is build_purchase_plan
    assert formula_port.build_weekly_production_plan is build_weekly_production_plan
