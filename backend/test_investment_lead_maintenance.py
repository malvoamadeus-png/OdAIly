from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from packages.investment_leads.service import InvestmentLeadService, _iso


def test_maintenance_is_bounded_resumable_and_preserves_source_material(tmp_path: Path) -> None:
    service = InvestmentLeadService(tmp_path / "investment.sqlite")
    now = datetime(2026, 11, 18, tzinfo=UTC)
    old = _iso(now - timedelta(days=40))
    try:
        service.db.execute(
            "CREATE TABLE hottopic_accounts(screen_name_lower TEXT PRIMARY KEY,status TEXT,investment_leads_enabled INTEGER)"
        )
        with service.db:
            for index in range(120):
                material_id, content_hash = f"material:{index}", f"hash:{index}"
                service.db.execute(
                    "INSERT INTO investment_lead_materials(material_id,content_hash,posted_at,activity_at,activity_time_complete,"
                    "relation,payload_json,first_observed_at,last_observed_at,completeness) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (material_id, content_hash, old, old, 1, "original", "{}", old, old, "complete"),
                )
                service.db.execute(
                    "INSERT INTO investment_lead_tasks(material_id,content_hash,stage,prompt_version,status,created_at,updated_at) "
                    "VALUES(?,?,'scan','v1','pending',?,?)",
                    (material_id, content_hash, old, old),
                )
                service.db.execute(
                    "INSERT INTO investment_lead_events(material_id,content_hash,classification,actual_model,prompt_version,created_at) "
                    "VALUES(?,?,'driver_candidate','test','v1',?)",
                    (material_id, content_hash, old),
                )
            service.db.execute(
                "INSERT INTO investment_leads(id,subject,driver_logic,first_seen_at,last_substantive_at,created_at,updated_at) "
                "VALUES('lead:protected','Protected','Evidence is retained',?,?,?,?)",
                (old, old, old, old),
            )
            service.db.execute(
                "INSERT INTO investment_lead_materials(material_id,content_hash,posted_at,activity_at,activity_time_complete,"
                "relation,payload_json,first_observed_at,last_observed_at,completeness) VALUES(?,?,?,?,?,?,?,?,?,?)",
                ("material:protected", "hash:protected", old, old, 1, "original", "{}", old, old, "complete"),
            )
            service.db.execute(
                "INSERT INTO investment_lead_sources(lead_id,material_id,content_hash,role,created_at) "
                "VALUES('lead:protected','material:protected','hash:protected','source',?)",
                (old,),
            )
            service.db.execute(
                "INSERT INTO investment_lead_tasks(material_id,content_hash,stage,prompt_version,status,created_at,updated_at) "
                "VALUES('material:protected','hash:protected','scan','v1','pending',?,?)",
                (old, old),
            )
            service.db.execute(
                "INSERT INTO investment_lead_events(material_id,content_hash,classification,actual_model,prompt_version,created_at) "
                "VALUES('material:protected','hash:protected','driver_candidate','test','v1',?)",
                (old,),
            )

        first = service._maintain(_iso(now))
        assert first == {"tasks": 100, "identity_searches": 0, "events": 100, "materials": 100, "continued": 1}
        meta = service.db.execute(
            "SELECT last_maintenance_at,maintenance_next_at FROM investment_leads_meta WHERE singleton_key='global'"
        ).fetchone()
        assert meta["last_maintenance_at"] is None
        assert meta["maintenance_next_at"] == _iso(now + timedelta(seconds=10))
        assert service.db.execute(
            "SELECT COUNT(*) FROM investment_lead_materials WHERE material_id='material:protected'"
        ).fetchone()[0] == 1

        second = service._maintain(_iso(now + timedelta(seconds=10)))
        assert second == {"tasks": 1, "identity_searches": 0, "events": 0, "materials": 20, "continued": 0}
        meta = service.db.execute(
            "SELECT last_maintenance_at,maintenance_next_at FROM investment_leads_meta WHERE singleton_key='global'"
        ).fetchone()
        assert meta["last_maintenance_at"] == _iso(now + timedelta(seconds=10))
        assert meta["maintenance_next_at"] is None
        assert service.db.execute(
            "SELECT COUNT(*) FROM investment_lead_tasks WHERE status='expired'"
        ).fetchone()[0] == 1
        assert service.db.execute(
            "SELECT COUNT(*) FROM investment_lead_materials"
        ).fetchone()[0] == 1
        assert service.db.execute(
            "SELECT COUNT(*) FROM investment_lead_events"
        ).fetchone()[0] == 1
        assert service.db.execute(
            "SELECT COUNT(*) FROM investment_lead_maintenance_progress"
        ).fetchone()[0] == 0
    finally:
        service.close()
