import json
import tempfile
import unittest
from pathlib import Path

from src.grade_visit_images import (
    GraderConfig,
    apply_rules,
    clean_json_text,
    parse_model_payload,
    row_key,
)


class RuleTests(unittest.TestCase):
    def test_bien_hieu_is_only_visit_pass(self):
        result = apply_rules(
            {
                "image_type": "Bien_hieu",
                "has_khanh_hoa_product": False,
            }
        )
        self.assertEqual(result["visit_result"], "Đạt")
        self.assertEqual(result["display_result"], "Không đạt")

    def test_bien_hieu_with_product_stays_display_fail_and_requires_qa(self):
        result = apply_rules(
            {
                "image_type": "Bien_hieu",
                "has_khanh_hoa_product": True,
            }
        )
        self.assertEqual(result["visit_result"], "Đạt")
        self.assertEqual(result["display_result"], "Không đạt")
        self.assertIn("QA cần rà ảnh", result["qa_note"])
        self.assertIn("chưa tự nâng Chấm Trưng Bày", result["qa_note"])

    def test_display_pass_requires_allowed_position_and_product(self):
        for image_type in ("Ke_trung_bay", "Tu_mat", "Thung_hang"):
            with self.subTest(image_type=image_type):
                result = apply_rules(
                    {
                        "image_type": image_type,
                        "has_khanh_hoa_product": True,
                    }
                )
                self.assertEqual(result["visit_result"], "Không đạt")
                self.assertEqual(result["display_result"], "Đạt")

    def test_display_position_without_khanh_hoa_product_is_fail(self):
        for image_type in ("Ke_trung_bay", "Tu_mat", "Thung_hang"):
            with self.subTest(image_type=image_type):
                result = apply_rules(
                    {
                        "image_type": image_type,
                        "has_khanh_hoa_product": False,
                    }
                )
                self.assertEqual(result["display_result"], "Không đạt")
                self.assertIn(image_type, result["qa_note"])
                self.assertIn("chưa đủ căn cứ", result["qa_note"])

    def test_loc_6_chai_is_always_display_fail(self):
        result = apply_rules(
            {
                "image_type": "Loc_6_chai",
                "has_khanh_hoa_product": True,
            }
        )
        self.assertEqual(result["visit_result"], "Không đạt")
        self.assertEqual(result["display_result"], "Không đạt")
        self.assertIn("lốc 6 chai", result["qa_note"])

    def test_other_disallowed_types_are_display_fail(self):
        for image_type in ("Chai_le", "Duoi_san", "Doi_pho", "Selfie_NV", "Khac"):
            with self.subTest(image_type=image_type):
                result = apply_rules(
                    {
                        "image_type": image_type,
                        "has_khanh_hoa_product": True,
                    }
                )
                self.assertEqual(result["visit_result"], "Không đạt")
                self.assertEqual(result["display_result"], "Không đạt")


class ModelPayloadTests(unittest.TestCase):
    def test_parse_json_code_fence(self):
        text = """```json
        {"results":[{"idx":1,"image_type":"Bien_hieu","has_khanh_hoa_product":false,"detected_brands":"","confidence":0.9,"evidence":"biển hiệu rõ"}]}
        ```"""
        parsed = parse_model_payload(text, 1)
        self.assertEqual(parsed[0]["image_type"], "Bien_hieu")
        self.assertFalse(parsed[0]["has_khanh_hoa_product"])

    def test_parse_rejects_unknown_type(self):
        payload = {
            "results": [
                {
                    "idx": 1,
                    "image_type": "BadType",
                    "has_khanh_hoa_product": False,
                    "detected_brands": "",
                    "confidence": 0.5,
                    "evidence": "x",
                }
            ]
        }
        with self.assertRaisesRegex(ValueError, "Unsupported image_type"):
            parse_model_payload(json.dumps(payload), 1)

    def test_parse_rejects_unknown_brand(self):
        payload = {
            "results": [
                {
                    "idx": 1,
                    "image_type": "Ke_trung_bay",
                    "has_khanh_hoa_product": True,
                    "detected_brands": "Other",
                    "confidence": 0.8,
                    "evidence": "x",
                }
            ]
        }
        with self.assertRaisesRegex(ValueError, "Unsupported detected_brands"):
            parse_model_payload(json.dumps(payload), 1)

    def test_clean_json_text(self):
        self.assertEqual(clean_json_text("```json\n{\"a\":1}\n```"), '{"a":1}')


class StableKeyTests(unittest.TestCase):
    def test_row_key_is_stable_and_changes_with_image(self):
        row = {
            "ma_nv": "NV01",
            "ngay": "8/22/2026",
            "ma_kh": "KH01",
            "stt_hinh": "1",
            "hinh_anh": "https://example.test/a.jpg",
        }
        self.assertEqual(row_key(row), row_key(dict(row)))
        changed = dict(row)
        changed["hinh_anh"] = "https://example.test/b.jpg"
        self.assertNotEqual(row_key(row), row_key(changed))


class ConfigTests(unittest.TestCase):
    def test_config_output_path_can_be_constructed(self):
        with tempfile.TemporaryDirectory() as directory:
            config = GraderConfig(
                source_csv_url="https://example.test/source.csv",
                output_path=Path(directory) / "results.csv",
                model="openai/gpt-4o-mini",
                batch_size=20,
                max_rows=10,
                request_timeout=180,
                max_retries=2,
                stop_on_rate_limit=True,
            )
            self.assertEqual(config.batch_size, 20)


if __name__ == "__main__":
    unittest.main()
