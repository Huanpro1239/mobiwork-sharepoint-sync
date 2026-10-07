import unittest

import sales_structure as ss


class SalesStructureTests(unittest.TestCase):
    def test_chain_walks_parents_and_stops_on_cycles(self):
        sales = [{"ma": "KHHO0303", "ma_don_vi": "N1"}, {"ma": "X", "ma_don_vi": "C1"}]
        groups = [{"ma_nhom": "N1", "ten_nhom": "B-KHHO-0288 - TU TAI", "ma_nhom_cha": "S1"},
                  {"ma_nhom": "S1", "ten_nhom": "KHA04 - Huỳnh Thanh Cường", "ma_nhom_cha": "R1"},
                  {"ma_nhom": "R1", "ten_nhom": "Miền Trung 1B", "ma_nhom_cha": ""},
                  {"ma_nhom": "C1", "ten_nhom": "loop", "ma_nhom_cha": "C1"}]
        tree = ss.chains(sales, groups)
        self.assertEqual([n["ten_nhom"] for n in tree["KHHO0303"]],
                         ["B-KHHO-0288 - TU TAI", "KHA04 - Huỳnh Thanh Cường", "Miền Trung 1B"])
        self.assertEqual(len(tree["X"]), 1)
        result = ss.summary(sales, groups, "KHHO0303")
        self.assertEqual(result["groups_code_dash_name"], 2)
        self.assertEqual(len(result["sample_chain"]), 3)


if __name__ == "__main__":
    unittest.main()
