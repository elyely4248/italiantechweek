import importlib
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable


_migration_active = False
_db_override: Any = None
_schema_ready = False


def _db() -> Any:
    return _db_override if _db_override is not None else importlib.import_module("db")


def _event_datetime(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    text = str(value).strip()
    if text.lstrip("-").isdigit():
        number = float(text)
        if abs(number) > 10_000_000_000:
            number /= 1000
        return datetime.fromtimestamp(number, tz=timezone.utc)
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


async def _ensure_schema(conn: Any) -> None:
    global _schema_ready
    if not _schema_ready:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS automation_executions (
                object_type TEXT NOT NULL,
                object_id BIGINT NOT NULL,
                automation_key TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (object_type, object_id, automation_key)
            )
            """
        )
        _schema_ready = True


async def _run_once(
    object_type: str,
    object_id: int,
    automation_key: str,
    operation: Callable[[Any], Awaitable[None]],
) -> bool:
    database = _db()
    custom = getattr(database, "run_automation_once", None)
    if custom is not None:
        return bool(await custom(object_type, object_id, automation_key, operation))
    get_pool = getattr(database, "get_pool", None)
    if get_pool is None:
        raise RuntimeError("db.py must export get_pool() or run_automation_once()")
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await _ensure_schema(conn)
            claimed = await conn.fetchval(
                """
                INSERT INTO automation_executions(object_type, object_id, automation_key)
                VALUES($1, $2, $3)
                ON CONFLICT DO NOTHING
                RETURNING TRUE
                """,
                object_type,
                object_id,
                automation_key,
            )
            if not claimed:
                return False
            await operation(conn)
            return True


async def reset_automation_state() -> None:
    """Restore the automation ledger to fresh-account state during /__reset."""
    global _schema_ready
    database = _db()
    custom = getattr(database, "reset_automation_state", None)
    if custom is not None:
        await custom()
    elif hasattr(database, "automation_claims"):
        database.automation_claims.clear()
    elif hasattr(database, "claims"):
        database.claims.clear()
    else:
        get_pool = getattr(database, "get_pool", None)
        if get_pool is None:
            raise RuntimeError("db.py must export get_pool() to reset durable automations")
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("DROP TABLE IF EXISTS automation_executions")
    _schema_ready = False


async def _associate(conn: Any, from_type: str, from_id: int, to_type: str, to_id: int) -> None:
    create = _db().create_association
    await create(from_type, from_id, to_type, to_id, 0, conn=conn)


async def _association_ids(
    from_type: str, from_id: int, to_type: str, conn: Any | None = None
) -> list[int]:
    if conn is not None and hasattr(conn, "fetch"):
        rows = await conn.fetch(
            """
            SELECT to_object_id AS id FROM associations
            WHERE from_object_type=$1 AND from_object_id=$2 AND to_object_type=$3
            UNION
            SELECT from_object_id AS id FROM associations
            WHERE to_object_type=$1 AND to_object_id=$2 AND from_object_type=$3
            """,
            from_type,
            from_id,
            to_type,
        )
        return [int(row["id"]) for row in rows]
    rows = await _db().get_associations(from_type, from_id, to_type)
    return [int(row["id"]) for row in rows]


async def _closed_won(record_id: int, properties: dict[str, Any]) -> None:
    async def operation(conn: Any) -> None:
        ticket = await _db().create_object(
            "tickets",
            {
                "subject": f"Avvio fornitura - {properties.get('dealname', '')}",
                "hs_pipeline": "assistenza",
                "hs_pipeline_stage": "aperto",
                "assegnatario": properties.get("commerciale", ""),
            },
            conn=conn,
        )
        ticket_id = int(ticket["id"])
        await _associate(conn, "tickets", ticket_id, "deals", record_id)
        for company_id in await _association_ids("deals", record_id, "companies", conn):
            await _associate(conn, "tickets", ticket_id, "companies", company_id)

    await _run_once("deals", record_id, "r10_supply_ticket", operation)


async def _closed_lost(
    record_id: int, properties: dict[str, Any], event_time: str | None
) -> None:
    due = (_event_datetime(event_time) + timedelta(days=180)).isoformat().replace("+00:00", "Z")

    async def operation(conn: Any) -> None:
        task = await _db().create_object(
            "tasks",
            {
                "hs_task_subject": f"Richiamare: {properties.get('dealname', '')}",
                "hs_timestamp": due,
                "hs_task_status": "NOT_STARTED",
            },
            conn=conn,
        )
        await _associate(conn, "tasks", int(task["id"]), "deals", record_id)

    await _run_once("deals", record_id, "r11_callback_task", operation)


async def _contact_company(record_id: int, properties: dict[str, Any]) -> None:
    if await _association_ids("contacts", record_id, "companies"):
        return
    email = str(properties.get("email", "")).strip().lower()
    if email.count("@") != 1:
        return
    domain = email.rsplit("@", 1)[1].strip().strip(".")
    if not domain or "." not in domain:
        return
    result = await _db().search_objects(
        "companies",
        [
            {"filters": [{"propertyName": "domain", "operator": "EQ", "value": domain}]},
            {"filters": [{
                "propertyName": "hs_additional_domains",
                "operator": "CONTAINS_TOKEN",
                "value": domain,
            }]},
        ],
        limit=1,
    )
    if result.get("results"):
        company_id = int(result["results"][0]["id"])
        create = _db().create_association
        await create("contacts", record_id, "companies", company_id, 0)


async def handle_automations(
    object_type: str,
    action: str,
    record_id: int,
    new_props: dict[str, Any],
    old_props: dict[str, Any] | None = None,
    *,
    event_time: str | None = None,
) -> None:
    if _migration_active:
        return
    old_props = old_props or {}
    if object_type == "deals":
        pipeline = new_props.get("pipeline", "default")
        stage = new_props.get("dealstage")
        won_stage = "rinnovato" if pipeline == "rinnovi" else "closedwon"
        lost_stage = "non_rinnovato" if pipeline == "rinnovi" else "closedlost"
        if stage == won_stage and old_props.get("dealstage") != won_stage:
            await _closed_won(record_id, new_props)
        elif stage == lost_stage and old_props.get("dealstage") != lost_stage:
            await _closed_lost(record_id, new_props, event_time)
    if object_type == "contacts":
        email_changed = action == "create" or new_props.get("email") != old_props.get("email")
        if email_changed:
            await _contact_company(record_id, new_props)
