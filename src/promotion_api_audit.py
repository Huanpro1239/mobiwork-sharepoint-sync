"""Read-only API diagnostics; save counts and field names, never customer values."""
import json
from pathlib import Path

from mobiwork import MobiWorkClient
from promotion_bonus import fetch_programs, load_config


def run():
    client = MobiWorkClient.from_env()
    cfg = load_config()
    programs = fetch_programs(client, cfg)
    audit = {"program_count": len(programs), "catalog_probes": [], "reports": []}
    for label, filters in [
        ("q3_code", {"ma": "003/TB/GT/01/2026_Q3"}),
        ("july_september", {"fromdate": "01/07/2026", "todate": "30/09/2026"}),
    ]:
        payload = client.get_json(cfg.catalog_url, {"page_size": 200, "page_number": 1, **filters})
        rows = payload.get("data", [])
        audit["catalog_probes"].append({"label": label, "total": payload.get("total"),
                                       "returned": len(rows), "ids": [r.get("_id") for r in rows]})
    for number, program in enumerate(programs, 1):
        payload = client.get_json(cfg.report_url, {"id_ct": program["_id"]},
                                  operation_key="promotion_api_audit", request_number=number)
        entry = {"id": program["_id"], "name": program.get("name"),
                 "total": payload.get("total"), "response_fields": sorted(payload)}
        for key in ("data", "arrChiTieu", "arrTraThuong"):
            rows = payload.get(key) or []
            entry[key] = {"count": len(rows),
                          "fields": sorted({field for row in rows if isinstance(row, dict) for field in row})}
        audit["reports"].append(entry)
    path = Path("output/promotion_api_audit.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"API audit: {len(programs)} programs; {sum(r['data']['count'] for r in audit['reports'])} customer rows")


if __name__ == "__main__":
    run()
