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


    def test_employee_mapping_matches_template_example(self):
        sales = [{"ma": "KHHO0303", "ten": "Nguyễn Tấn Khải", "chuc_vu": "", "ma_don_vi": "B-KHHO-0288"},
                 {"ma": "KHA04", "ten": "Huỳnh  Thanh Cường", "chuc_vu": "Giám sát kinh doanh",
                  "ma_don_vi": "B-KHHO-0288"},
                 {"ma": "DANA0101", "ten": "NV", "ma_don_vi": "B-DANA-0116"},
                 {"ma": "GS2", "ten": "GS Vùng", "chuc_vu": "Giám sát  kinh doanh", "ma_don_vi": "MT2"},
                 {"ma": "HN01", "ten": "NV HN", "ma_don_vi": "HNC"}]
        groups = [{"ma_nhom": "B-KHHO-0288", "ten_nhom": "TU TAI", "ma_nhom_cha": "MT1B"},
                  {"ma_nhom": "MT1B", "ten_nhom": "MT1B", "ma_nhom_cha": "MT1"},
                  {"ma_nhom": "MT1", "ten_nhom": "MT1", "ma_nhom_cha": "VIKODA"},
                  {"ma_nhom": "B-DANA-0116", "ten_nhom": "CTY X", "ma_nhom_cha": "MT2"},
                  {"ma_nhom": "MT2", "ten_nhom": "MT2", "ma_nhom_cha": "VIKODA"},
                  {"ma_nhom": "HNC", "ten_nhom": "HNC", "ma_nhom_cha": "MB"},
                  {"ma_nhom": "MB", "ten_nhom": "MB", "ma_nhom_cha": "VIKODA"},
                  {"ma_nhom": "VIKODA", "ten_nhom": "VIKODA", "ma_nhom_cha": ""}]
        mapping = ss.employee_mapping(sales, groups)
        self.assertEqual(mapping["KHHO0303"], {"DB Code": "B-KHHO-0288", "Tên NPP": "TU TAI",
                                               "SS Code": "KHA04", "SS Name": "Huỳnh Thanh Cường",
                                               "Vùng": "Miền Trung 1B"})
        self.assertEqual((mapping["DANA0101"]["SS Code"], mapping["DANA0101"]["Vùng"]), ("GS2", "Miền Trung 2"))
        self.assertNotIn("DB Code", mapping["HN01"])
        self.assertEqual(mapping["HN01"]["Vùng"], "Miền Bắc")
        self.assertNotIn("SS Code", mapping["KHA04"])  # a supervisor is not their own SS

    def test_explicit_config_wins_and_audit_counts(self):
        from unittest.mock import Mock
        client = Mock()
        client.get_json.side_effect = [
            {"data": [{"ma": "E1", "ma_don_vi": "B-AAAA-0001"}]},
            {"data": [{"ma_nhom": "B-AAAA-0001", "ten_nhom": "NPP A", "ma_nhom_cha": "MN"},
                      {"ma_nhom": "MN", "ten_nhom": "MN", "ma_nhom_cha": ""}]}]
        cfg = ss.enrich_employee_config(client, {"employees": {"E1": {"Tên NPP": "Tên chuẩn"}}})
        self.assertEqual(cfg["employees"]["E1"], {"DB Code": "B-AAAA-0001", "Tên NPP": "Tên chuẩn",
                                                  "Vùng": "Miền Nam"})
        self.assertEqual(cfg["sales_structure_audit"]["with_npp"], 1)


    def test_supervisor_detection(self):
        self.assertTrue(ss.is_supervisor({"chuc_vu": "Giám sát  kinh doanh"}))
        self.assertTrue(ss.is_supervisor({"chuc_vu": "", "chuc_danh": "Giám sát"}))
        self.assertFalse(ss.is_supervisor({"chuc_vu": "Giám đốc kinh doanh vùng", "chuc_danh": "Giám sát"}))
        self.assertFalse(ss.is_supervisor({"chuc_vu": "Nhân viên bán hàng", "chuc_danh": "Nhân viên"}))


if __name__ == "__main__":
    unittest.main()
