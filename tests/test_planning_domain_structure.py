import unittest

from src.planning import formula_port
from src.planning.domain.demand import forecast_by_month
from src.planning.domain.materials import build_material_inbound_plan
from src.planning.domain.production import build_weekly_production_plan
from src.planning.domain.purchasing import build_purchase_plan


class PlanningDomainStructureTests(unittest.TestCase):
    def test_formula_port_is_compatibility_facade(self):
        self.assertIs(formula_port.forecast_by_month, forecast_by_month)
        self.assertIs(
            formula_port.build_material_inbound_plan,
            build_material_inbound_plan,
        )
        self.assertIs(formula_port.build_purchase_plan, build_purchase_plan)
        self.assertIs(
            formula_port.build_weekly_production_plan,
            build_weekly_production_plan,
        )


if __name__ == "__main__":
    unittest.main()
