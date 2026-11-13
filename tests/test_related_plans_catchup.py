"""Coverage catch-up: sector plans are separately catalogued from urban basic plans."""
from fastapi.testclient import TestClient
from related_plans import PlanFields, LinkInput, create_app, Store, seed_plans

NEW_IDS = [
  "nation-carbon-neutral-first-2023-2042",
  "daejeon-parks-2030-amended-2026",
  "daejeon-renewal-2030-2022-amended",
  "daejeon-renewal-2030-2026-draft",
  "daejeon-regeneration-strategy-2035-2025-amended",
  "daejeon-architecture-basic-third-2025-2029",
  "asan-regeneration-strategy-2035-2026",
  "asan-regeneration-strategy-2025-2021-change",
  "asan-growth-management-2026-amend",
  "asan-carbon-neutral-first-2025-2034",
  "asan-industrial-area-basic-2030",
  "asan-tangjeong-techno-industrial-park-management-2026",
  "asan-renewal-basic-2020-2018-change",
  "asan-onyang-center-renewal-promotion-2018",
  "asan-punggi-station-urban-development-2026",
  "asan-central-city-urban-development-2026",
  "sejong-landscape-2030-2020-hearing"
]
def test_catchup_plans_have_valid_metadata():
    data = {p["id"]:p for p in seed_plans()}
    assert set(NEW_IDS) <= set(data)
    for ident in NEW_IDS:
        record = data[ident]
        PlanFields.model_validate({k:record[k] for k in PlanFields.model_fields})
        for link in record["links"]:
            LinkInput.model_validate({k:link[k] for k in LinkInput.model_fields})
    assert data["daejeon-renewal-2030-2026-draft"]["status"] == "hearing"
    assert data["daejeon-parks-2030-amended-2026"]["status"] == "approved"
    assert data["asan-tangjeong-techno-industrial-park-management-2026"]["category"] == "industry"
    assert data["nation-carbon-neutral-first-2023-2042"]["region"] == "KR"

def test_regions_inherit_only_related_plans():
    c=TestClient(create_app(Store(db_path="",database_url="")))
    for region in ("30","44200","36","41"):
        answer=c.get("/api/related-plans",params={"region":region,"category":"environment"})
        assert answer.status_code==200
        assert "nation-carbon-neutral-first-2023-2042" in {p["id"] for p in answer.json()["items"]}
    sejong=c.get("/api/related-plans",params={"region":"36","category":"industry"}).json()
    assert "asan-tangjeong-techno-industrial-park-management-2026" not in {p["id"] for p in sejong["items"]}
