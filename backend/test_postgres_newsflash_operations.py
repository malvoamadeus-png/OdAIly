from __future__ import annotations

import os
import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
import psycopg
from psycopg import sql


def test_postgres_newsflash_operations_native_crud(monkeypatch) -> None:
    from packages.common.pg_native import connect_postgres
    from packages.fact_auditor.postgres_repository import PostgresFactReviewRepository
    from packages.postgres_newsflash_operations import PostgresNewsflashOperationsRepository
    from packages.x_processing.repository import PostgresXProcessingRepository

    if not os.getenv("ODAILY_TEST_POSTGRES_DSN"):
        pytest.skip("ODAILY_TEST_POSTGRES_DSN must point to an isolated test database")
    database_name=f"odaily_test_newsflash_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(os.environ["ODAILY_TEST_POSTGRES_DSN"],autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))
    child_dsn=psycopg.conninfo.make_conninfo(os.environ["ODAILY_TEST_POSTGRES_DSN"],dbname=database_name)
    monkeypatch.setenv("ODAILY_POSTGRES_DSN",child_dsn)
    try:
        PostgresXProcessingRepository().init_schema()
        PostgresFactReviewRepository().init_schema()
        repository = PostgresNewsflashOperationsRepository()
        source_id = "pg-newsflash-" + uuid.uuid4().hex
        now = datetime.now(UTC)
        with connect_postgres(schema="main", application_name="odaily-newsflash-test-seed") as conn:
            with conn.transaction():
                conn.execute("UPDATE newsflash_metadata_state SET enabled_at=%s WHERE singleton_key='global'", (now-timedelta(hours=1),))
                conn.execute(
                    "INSERT INTO odaily_reference_items(source_item_id,title,content,published_at) VALUES(%s,%s,%s,%s)",
                    (source_id, "Synthetic newsflash", "Synthetic content", now),
                )
        assert repository.upsert_source_facts([{
            "source_item_id": source_id, "operator_raw": "Zoey", "view_count": 25,
            "is_pushed": 1, "published_at": now.isoformat(),
        }]) == {"read": 1, "matched": 1, "skipped": 0, "skipped_existing": 0, "reconciled_odaily": 0}
        updated = repository.update_newsflash({
            "source_item_id": source_id,
            "patch": {"is_contribution": True, "contributor_person_key": "zoey", "contribution_type": "regular"},
        }, actor_email="test@example.com")
        assert updated["id"] == source_id
        assert updated["publisher_kind"] == "human"
        assert updated["is_contribution"] is True
        listed = repository.list_newsflashes({"search": source_id})
        assert listed["total"] == 1
        assert listed["items"][0]["text_review"]["status"] == "unreviewed"
        assert listed["items"][0]["origin_key"] == "manual"
        roster = repository.list_roster()
        assert any(person["person_key"] == "zoey" and "Zoey" in person["aliases"] for person in roster["people"])
        exported = repository.execute("export", {"limit": 10}, actor_email="test@example.com")
        assert exported["total"] == 1
        assert exported["items"][0]["fact_review"]["url"].endswith("fact_review=" + source_id)
        from packages.auditor.repository import PostgresAuditorRepository
        PostgresAuditorRepository().init_schema()
        content_hash = __import__("hashlib").md5(b"Synthetic newsflash\nSynthetic content").hexdigest()
        from psycopg.types.json import Jsonb
        with connect_postgres(schema="main", application_name="odaily-newsflash-test-audit-seed") as conn:
            with conn.transaction():
                conn.execute(
                    "INSERT INTO auditor_checks(source_item_id,title,content,content_hash,prompt_version,status,audit_result,telegram_text) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                    (source_id, "Synthetic newsflash", "Synthetic content", content_hash, "test", "flagged", Jsonb({"summary": "reviewed"}), "reviewed text"),
                )
        reviewed = repository.list_newsflashes({"search": source_id})["items"][0]["text_review"]
        assert reviewed["status"] == "flagged"
        assert reviewed["summary"] == "reviewed"
        assert repository.execute("export", {"limit": 10}, actor_email="test@example.com")["items"][0]["text_review"]["text"] == "reviewed text"
        schedule = repository.execute("schedule", {"month": now.strftime("%Y-%m")}, actor_email="test@example.com")
        assert len(schedule["days"]) in {28, 29, 30, 31}
        repository.execute("save_assignment", {"date": now.date().isoformat(), "shift_key": "morning", "person_key": "zoey"}, actor_email="test@example.com")
        assert repository.execute("summary", {"week_start": now.date().isoformat()}, actor_email="test@example.com")["period"]
        assert repository.execute("quality", {"week_start": "2026-08-03"}, actor_email="test@example.com")["status"] == "insufficient"
        assert repository.execute("quality_month", {"report_month": "2026-08"}, actor_email="test@example.com")["report_month"] == "2026-08"
        assert repository.execute("contributions", {"week_start": now.date().isoformat()}, actor_email="test@example.com")["status"] == "ready"
        assert repository.execute("contributions_month", {"report_month": now.strftime("%Y-%m")}, actor_email="test@example.com")["report_month"] == now.strftime("%Y-%m")
        with connect_postgres(schema="main", application_name="odaily-newsflash-test-audit") as conn:
            assert conn.execute(
                "SELECT count(*) AS n FROM newsflash_operation_audit WHERE source_item_id=%s", (source_id,),
            ).fetchone()["n"] == 1
    finally:
        from packages.common.pg_runtime import close_postgres_pools
        close_postgres_pools()
        with psycopg.connect(os.environ["ODAILY_TEST_POSTGRES_DSN"],autocommit=True) as admin:
            admin.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=%s",(database_name,))
            admin.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(database_name)))


def test_postgres_newsflash_xlsx_import_and_confirmed_week_seed(monkeypatch, tmp_path) -> None:
    from openpyxl import Workbook
    from packages.common.pg_native import connect_postgres
    from packages.postgres_newsflash_operations import PostgresNewsflashOperationsRepository
    from packages.x_processing.repository import PostgresXProcessingRepository

    admin_dsn = os.getenv("ODAILY_TEST_POSTGRES_DSN")
    if not admin_dsn:
        pytest.skip("ODAILY_TEST_POSTGRES_DSN must point to an isolated test database")
    database_name = f"odaily_test_newsflash_import_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))
    monkeypatch.setenv("ODAILY_POSTGRES_DSN", psycopg.conninfo.make_conninfo(admin_dsn, dbname=database_name))
    try:
        PostgresXProcessingRepository().init_schema()
        repository = PostgresNewsflashOperationsRepository()
        with connect_postgres(schema="main", application_name="odaily-newsflash-import-test") as conn:
            with conn.transaction():
                conn.execute(
                    "INSERT INTO odaily_reference_items(source_item_id,title,content,published_at) VALUES(%s,%s,%s,%s)",
                    ("30", "Old title", "content", datetime(2026, 7, 20, 0, tzinfo=UTC)),
                )
                conn.execute(
                    "INSERT INTO odaily_reference_items(source_item_id,title,content,published_at) VALUES(%s,%s,%s,%s)",
                    ("xlsx-preserve", "Keep title", "content", datetime(2026, 7, 20, 0, tzinfo=UTC)),
                )
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["ID", "标题", "操作人", "链接", "发布时间", "阅读量", "是否推送", "推送时间"])
        sheet.append([30, "New title", "南枳", "https://example.test/30", datetime(2026, 7, 20, 9), 321, "是", datetime(2026, 7, 20, 9, 1)])
        sheet.append(["xlsx-preserve", "Overwrite", "Z", "https://example.test/preserve", datetime(2026, 7, 20, 10), 999, "是", None])
        sheet.append([31, "Missing", "Z", "https://example.test/31", datetime(2026, 7, 20, 11), 100, "否", None])
        path = tmp_path / "newsflash.xlsx"
        workbook.save(path)
        imported = repository.import_xlsx(
            path, start_date=date(2026, 7, 20), end_date=date(2026, 7, 27), preserve_existing=False,
        )
        assert imported["read"] == 3
        assert imported["matched"] == 2
        assert imported["skipped"] == 1
        assert repository.import_xlsx(
            path, start_date=date(2026, 7, 20), end_date=date(2026, 7, 27), preserve_existing=True,
        )["skipped_existing"] == 2
        with connect_postgres(schema="main", application_name="odaily-newsflash-import-verify") as conn:
            row = conn.execute(
                "SELECT r.title,r.source_url,f.publisher_person_key,f.view_count,f.is_pushed "
                "FROM odaily_reference_items r JOIN newsflash_operation_facts f USING(source_item_id) "
                "WHERE r.source_item_id='30'",
            ).fetchone()
            assert dict(row) == {
                "title": "New title", "source_url": "https://example.test/30",
                "publisher_person_key": "malvo", "view_count": 321, "is_pushed": True,
            }
        repository.seed_confirmed_week()
        schedule = repository.get_schedule({"month": "2026-07"})
        assert len(schedule["days"]) == 31
        assert len([row for row in schedule["assignments"] if row["duty_date"].isoformat().startswith("2026-07-2")]) >= 14
        assert next(row for row in schedule["weeks"] if row["week_start"] == "2026-07-20")["report_month"] == "2026-07"
    finally:
        from packages.common.pg_runtime import close_postgres_pools
        close_postgres_pools()
        with psycopg.connect(admin_dsn, autocommit=True) as admin:
            admin.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=%s", (database_name,))
            admin.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(database_name)))


def test_postgres_newsflash_populated_reports_match_sqlite(monkeypatch, tmp_path) -> None:
    from packages.common.pg_native import connect_postgres
    from packages.newsflash_operations import NewsflashOperationsRepository
    from packages.postgres_newsflash_operations import PostgresNewsflashOperationsRepository

    if not os.getenv("ODAILY_TEST_POSTGRES_DSN"):
        pytest.skip("ODAILY_TEST_POSTGRES_DSN must point to an isolated test database")
    database_name=f"odaily_test_newsflash_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(os.environ["ODAILY_TEST_POSTGRES_DSN"],autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))
    child_dsn=psycopg.conninfo.make_conninfo(os.environ["ODAILY_TEST_POSTGRES_DSN"],dbname=database_name)
    monkeypatch.setenv("ODAILY_POSTGRES_DSN",child_dsn)
    try:
        from packages.x_processing.repository import PostgresXProcessingRepository
        from packages.fact_auditor.postgres_repository import PostgresFactReviewRepository
        from packages.competitor_monitor.repository import PostgresCompetitorMonitorRepository
        from psycopg.types.json import Jsonb
        PostgresXProcessingRepository().init_schema()
        PostgresFactReviewRepository().init_schema()
        PostgresCompetitorMonitorRepository().init_schema()
        pg=PostgresNewsflashOperationsRepository()
        sqlite=NewsflashOperationsRepository(tmp_path/"newsflash.sqlite")
        from packages.x_processing.sqlite_repository import SQLiteXProcessingRepository
        from packages.competitor_monitor.sqlite_repository import SQLiteCompetitorMonitorRepository
        SQLiteXProcessingRepository(sqlite.path)
        SQLiteCompetitorMonitorRepository(sqlite.path)
        week="2026-08-03"
        now=datetime(2026,8,3,10,tzinfo=UTC)
        sources=[
            ("baseline-a",datetime(2026,8,3,1,tzinfo=UTC),"Odaily",1000,True,False,"regular","none",None),
            ("baseline-b",datetime(2026,8,3,2,tzinfo=UTC),"Odaily",10000,True,False,"regular","none",None),
            ("quality-good",datetime(2026,8,3,2,30,tzinfo=UTC),"Zoey",9000,False,False,"regular","none",None),
            ("quality-first",datetime(2026,8,3,3,tzinfo=UTC),"Zoey",9500,False,False,"regular","none","blockbeats"),
            ("quality-force",datetime(2026,8,3,3,30,tzinfo=UTC),"Zoey",100,False,False,"regular","include",None),
            ("contrib-regular",datetime(2026,8,3,4,tzinfo=UTC),"Zoey",9000,False,True,"regular","none","blockbeats"),
            ("contrib-late",datetime(2026,8,3,5,tzinfo=UTC),"Zoey",9000,False,True,"late_different_info","none","blockbeats"),
            ("contrib-night",datetime(2026,8,3,6,tzinfo=UTC),"Zoey",None,False,True,"night","none",None),
        ]
        with connect_postgres(schema="main",application_name="odaily-newsflash-parity-seed") as conn:
            with conn.transaction():
                for source_id,published,operator,views,pushed,contribution,contribution_type,quality_override,first_override in sources:
                    conn.execute("INSERT INTO odaily_reference_items(source_item_id,title,content,source_url,raw_payload,published_at) VALUES(%s,%s,%s,%s,%s,%s)",
                        (source_id,source_id,"body",None,Jsonb({}),published))
                    kind,person=pg._classify_operator(conn,operator)
                    conn.execute("INSERT INTO newsflash_operation_facts(source_item_id,operator_raw,publisher_kind,publisher_person_key,view_count,is_pushed,is_contribution,contributor_person_key,contribution_type,quality_override,first_source_override) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        (source_id,operator,kind,person,views,pushed,contribution,"zoey" if contribution else None,contribution_type,quality_override,first_override))
        with __import__("packages.common.storage",fromlist=["connect_sqlite"]).connect_sqlite(sqlite.path) as conn:
            conn.execute("UPDATE newsflash_metadata_state SET enabled_at=? WHERE singleton_key='global'",(now.isoformat(),))
            for source_id,published,operator,views,pushed,contribution,contribution_type,quality_override,first_override in sources:
                conn.execute("INSERT INTO odaily_reference_items(source_item_id,title,content,published_at) VALUES(?,?,?,?)",
                    (source_id,source_id,"body",published.isoformat()))
                kind,person=sqlite._classify_operator(conn,operator)
                conn.execute("INSERT INTO newsflash_operation_facts(source_item_id,operator_raw,publisher_kind,publisher_person_key,view_count,is_pushed,is_contribution,contributor_person_key,contribution_type,quality_override,first_source_override) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (source_id,operator,kind,person,views,int(pushed),int(contribution),"zoey" if contribution else None,contribution_type,quality_override,first_override))
            conn.execute("INSERT INTO newsflash_daily_modes(duty_date,mode) VALUES(?,?)",(week,"three"))
            conn.execute("INSERT INTO newsflash_shift_assignments(duty_date,shift_key,person_key) VALUES(?,?,?)",(week,"morning","zoey"))
            conn.commit()
        with connect_postgres(schema="main",application_name="odaily-newsflash-parity-seed") as conn:
            with conn.transaction():
                conn.execute("INSERT INTO newsflash_daily_modes(duty_date,mode) VALUES(%s,%s) ON CONFLICT DO NOTHING",(week,"three"))
                conn.execute("INSERT INTO newsflash_shift_assignments(duty_date,shift_key,person_key) VALUES(%s,%s,%s)",(week,"morning","zoey"))

        assert pg.get_summary({"week_start":week})==sqlite.get_summary({"week_start":week})
        pg_quality=pg.get_quality({"week_start":week});sqlite_quality=sqlite.get_quality({"week_start":week})
        assert pg_quality["status"]==sqlite_quality["status"]=="ready"
        assert (pg_quality["average_views"],pg_quality["threshold_views"],pg_quality["qualified_count"],pg_quality["excluded_count"],pg_quality["total_kpi"]) == (sqlite_quality["average_views"],sqlite_quality["threshold_views"],sqlite_quality["qualified_count"],sqlite_quality["excluded_count"],sqlite_quality["total_kpi"])
        assert {x["source_item_id"] for x in pg_quality["groups"][0]["qualified"]}=={x["source_item_id"] for x in sqlite_quality["groups"][0]["qualified"]}
        pg_contrib=pg.list_contributions({"week_start":week});sqlite_contrib=sqlite.list_contributions({"week_start":week})
        pg_group=next(g for g in pg_contrib["groups"] if g["person_key"]=="zoey")
        sqlite_group=next(g for g in sqlite_contrib["groups"] if g["person_key"]=="zoey")
        assert (pg_group["base_score_total"],pg_group["high_view_bonus"],pg_group["total_score"]) == (sqlite_group["base_score_total"],sqlite_group["high_view_bonus"],sqlite_group["total_score"])
        pg_items={i["source_item_id"]:i for i in pg_group["items"]};sqlite_items={i["source_item_id"]:i for i in sqlite_group["items"]}
        assert pg_items["contrib-regular"]["score_exclusion_reasons"] == sqlite_items["contrib-regular"]["score_exclusion_reasons"] == ["competitor_first"]
        assert pg_items["contrib-late"]["score_exclusion_reasons"] == sqlite_items["contrib-late"]["score_exclusion_reasons"] == []
        pg_quality_month=pg.list_quality_monthly({"report_month":"2026-08"});sqlite_quality_month=sqlite.list_quality_monthly({"report_month":"2026-08"})
        assert pg_quality_month==sqlite_quality_month
        pg_contrib_month=pg.list_contributions_monthly({"report_month":"2026-08"});sqlite_contrib_month=sqlite.list_contributions_monthly({"report_month":"2026-08"})
        assert pg_contrib_month==sqlite_contrib_month
        pg_schedule=pg.get_schedule({"month":"2026-08"});sqlite_schedule=sqlite.get_schedule({"month":"2026-08"})
        assert pg_schedule["days"]==sqlite_schedule["days"]
        assert pg_schedule["weeks"]==sqlite_schedule["weeks"]
        assert [(r["duty_date"].isoformat() if hasattr(r["duty_date"],"isoformat") else r["duty_date"],r["shift_key"],r["person_key"],r["display_name"]) for r in pg_schedule["assignments"]] == [(r["duty_date"],r["shift_key"],r["person_key"],r["display_name"]) for r in sqlite_schedule["assignments"]]
        pg_first=pg.list_newsflashes({"first_source":"blockbeats"});sqlite_first=sqlite.list_newsflashes({"first_source":"blockbeats"})
        pg_first_ids={x.get("id",x.get("source_item_id")) for x in pg_first["items"]};sqlite_first_ids={x.get("id",x.get("source_item_id")) for x in sqlite_first["items"]}
        assert pg_first_ids == sqlite_first_ids, (pg_first,sqlite_first)
        assert pg.list_newsflashes({"first_status":"ready"})["total"] == sqlite.list_newsflashes({"first_status":"ready"})["total"]
        pg_export=pg.export_newsflashes(limit=100);sqlite_export=sqlite.export_newsflashes(limit=100)
        assert pg_export["total"]==sqlite_export["total"]
        for key in ("id","title","content","published_at","outlet_category","priority_level","text_review","fact_review","duplicate","read_status"):
            def normalize(value):
                if isinstance(value,datetime):return value.isoformat()
                if isinstance(value,dict):return {k:normalize(v) for k,v in value.items()}
                if isinstance(value,list):return [normalize(v) for v in value]
                return value
            assert [normalize(row[key]) for row in pg_export["items"]]==[normalize(row[key]) for row in sqlite_export["items"]]
    finally:
        from packages.common.pg_runtime import close_postgres_pools
        close_postgres_pools()
        with psycopg.connect(os.environ["ODAILY_TEST_POSTGRES_DSN"],autocommit=True) as admin:
            admin.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=%s",(database_name,))
            admin.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(database_name)))


def test_newsflash_metadata_public_api_uses_postgres_without_sqlite(monkeypatch) -> None:
    from packages.common.pg_native import connect_postgres
    from packages.newsflash_metadata import (
        ensure_newsflash_metadata_schema, metadata_for_reference,
        record_newsflash_delivery, update_newsflash_priority,
    )

    if not os.getenv("ODAILY_TEST_POSTGRES_DSN"):
        pytest.skip("ODAILY_TEST_POSTGRES_DSN must point to an isolated test database")
    database_name=f"odaily_test_metadata_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(os.environ["ODAILY_TEST_POSTGRES_DSN"],autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))
    child_dsn=psycopg.conninfo.make_conninfo(os.environ["ODAILY_TEST_POSTGRES_DSN"],dbname=database_name)
    monkeypatch.setenv("ODAILY_POSTGRES_DSN",child_dsn)
    monkeypatch.setenv("ODAILY_STORAGE_BACKEND","postgres")
    try:
        from packages.x_processing.repository import PostgresXProcessingRepository
        from packages.postgres_newsflash_operations import PostgresNewsflashOperationsRepository
        PostgresXProcessingRepository().init_schema()
        schema_only=PostgresNewsflashOperationsRepository(seed_defaults=False)
        schema_only.initialize_metadata_schema_only()
        with connect_postgres(schema="main",application_name="odaily-metadata-api-test") as conn:
            assert conn.execute("SELECT count(*) AS n FROM newsflash_metadata_state").fetchone()["n"]==0
        with connect_postgres(schema="main",application_name="odaily-metadata-api-test") as conn:
            conn.execute("INSERT INTO odaily_reference_items(source_item_id,title,content,published_at) VALUES('metadata-1','Shared title','body','2026-10-20T03:00:30Z')")
        monkeypatch.setattr("packages.common.storage.connect_sqlite",lambda *_a,**_k: (_ for _ in ()).throw(AssertionError("SQLite opened under PostgreSQL backend")))
        ensure_newsflash_metadata_schema(__import__("pathlib").Path("unused.sqlite"))
        key=record_newsflash_delivery(__import__("pathlib").Path("unused.sqlite"),delivery_key="metadata-key",origin_key="x",title="Shared title",response_text="no id",pushed_at="2026-10-20T03:00:00+00:00",priority_status="evaluated",priority_level="high",priority_reason="signal",duplicate_result={"is_duplicate":True,"duplicate_target":{"target_type":"event","target_id":"event-1","title":"Target"},"reason":"same event"})
        assert key=="metadata-key"
        assert update_newsflash_priority(__import__("pathlib").Path("unused.sqlite"),delivery_key=key,status="failed")
        with connect_postgres(schema="main",application_name="odaily-metadata-api-test") as conn:
            result=metadata_for_reference(conn,source_item_id="metadata-1",title="Shared title",published_at="2026-10-20T03:00:30Z",publisher_kind="other_ai",operation_created_at=None)
            row=conn.execute("SELECT odaily_id,id_match_status,priority_status,duplicate_status,duplicate_target_id FROM newsflash_production_metadata WHERE delivery_key=%s",(key,)).fetchone()
        assert result["origin_key"]=="x"
        assert result["origin_status"]=="matched"
        assert row=={"odaily_id":"metadata-1","id_match_status":"matched","priority_status":"failed","duplicate_status":"duplicate","duplicate_target_id":"event-1"}

        # A reference that predates the matching delivery should link on that delivery call.
        with connect_postgres(schema="main",application_name="odaily-metadata-api-test") as conn:
            conn.execute("INSERT INTO odaily_reference_items(source_item_id,title,content,published_at) VALUES('metadata-2','Delivery arrives later','body','2026-10-20T04:00:10Z')")
        record_newsflash_delivery(__import__("pathlib").Path("unused.sqlite"),delivery_key="metadata-late",origin_key="x",title="Delivery arrives later",response_text="no id",pushed_at="2026-10-20T04:00:00Z")
        with connect_postgres(schema="main",application_name="odaily-metadata-api-test") as conn:
            assert conn.execute("SELECT odaily_id FROM newsflash_production_metadata WHERE delivery_key='metadata-late'").fetchone()["odaily_id"] == "metadata-2"

        # Multiple deliveries in the five minute window make both candidates ambiguous.
        record_newsflash_delivery(__import__("pathlib").Path("unused.sqlite"),delivery_key="metadata-ambiguous-a",origin_key="x",title="Ambiguous deliveries",response_text="no id",pushed_at="2026-10-20T05:00:00Z")
        with connect_postgres(schema="main",application_name="odaily-metadata-api-test") as conn:
            conn.execute("INSERT INTO odaily_reference_items(source_item_id,title,content,published_at) VALUES('metadata-3','Ambiguous deliveries','body','2026-10-20T05:00:10Z')")
        record_newsflash_delivery(__import__("pathlib").Path("unused.sqlite"),delivery_key="metadata-ambiguous-b",origin_key="x",title="Ambiguous deliveries",response_text="no id",pushed_at="2026-10-20T05:00:20Z")
        with connect_postgres(schema="main",application_name="odaily-metadata-api-test") as conn:
            assert conn.execute("SELECT count(*) AS n FROM newsflash_production_metadata WHERE title='Ambiguous deliveries' AND odaily_id IS NOT NULL").fetchone()["n"] == 0

        record_newsflash_delivery(__import__("pathlib").Path("unused.sqlite"),delivery_key="metadata-id-owner",origin_key="x",title="ID owner",response_text='{"id":"metadata-owned-id"}',pushed_at="2026-10-20T06:00:00Z")
        record_newsflash_delivery(__import__("pathlib").Path("unused.sqlite"),delivery_key="metadata-id-conflict",origin_key="x",title="ID conflict",response_text="no id",pushed_at="2026-10-20T06:00:00Z")
        from packages.newsflash_metadata import _try_link_metadata
        with connect_postgres(schema="main",application_name="odaily-metadata-api-test") as conn:
            candidate=conn.execute("SELECT * FROM newsflash_production_metadata WHERE delivery_key='metadata-id-conflict'").fetchone()
            assert _try_link_metadata(conn,candidate,"metadata-owned-id") is False
            assert conn.execute("SELECT count(*) AS n FROM newsflash_production_metadata").fetchone()["n"] >= 5
    finally:
        from packages.common.pg_runtime import close_postgres_pools
        close_postgres_pools()
        with psycopg.connect(os.environ["ODAILY_TEST_POSTGRES_DSN"],autocommit=True) as admin:
            admin.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=%s",(database_name,))
            admin.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(database_name)))
