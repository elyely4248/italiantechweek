"""Async PostgreSQL persistence for the HubSpot-compatible CRM."""

from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, AsyncIterator, Iterable, Sequence

import asyncpg


_pool: asyncpg.Pool | None = None

OBJECT_TYPES = (
    "contacts",
    "companies",
    "deals",
    "tickets",
    "products",
    "line_items",
    "notes",
    "calls",
    "emails",
    "meetings",
    "tasks",
)

DEFAULT_PROPERTIES: dict[str, tuple[str, ...]] = {
    "contacts": ("firstname", "lastname", "email", "phone", "lifecyclestage", "company"),
    "companies": ("name", "domain", "hs_additional_domains", "city", "state", "phone", "industry"),
    "deals": ("dealname", "amount", "dealstage", "pipeline", "closedate", "deal_currency_code"),
    "tickets": ("subject", "content", "hs_pipeline", "hs_pipeline_stage", "hs_ticket_priority", "createdate", "closed_date"),
    "products": ("name", "price", "hs_sku", "description"),
    "line_items": ("name", "quantity", "price", "hs_discount_percentage", "amount"),
    "notes": ("hs_timestamp", "hs_note_body"),
    "calls": ("hs_timestamp", "hs_call_body"),
    "emails": ("hs_timestamp", "hs_email_text"),
    "meetings": ("hs_timestamp", "hs_meeting_body"),
    "tasks": ("hs_task_subject", "hs_timestamp", "hs_task_status"),
}

DEFAULT_PIPELINES = (
    (
        "default",
        "deals",
        "Sales Pipeline",
        0,
        (
            ("appointmentscheduled", "Appointment Scheduled", 0, {}),
            ("qualifiedtobuy", "Qualified To Buy", 1, {}),
            ("presentationscheduled", "Presentation Scheduled", 2, {}),
            ("decisionmakerboughtin", "Decision Maker Bought-In", 3, {}),
            ("contractsent", "Contract Sent", 4, {}),
            ("closedwon", "Closed Won", 5, {"isClosed": "true", "probability": "1.0"}),
            ("closedlost", "Closed Lost", 6, {"isClosed": "true", "probability": "0.0"}),
        ),
    ),
    (
        "0",
        "tickets",
        "Support Pipeline",
        0,
        (
            ("1", "New", 0, {}),
            ("2", "Waiting on contact", 1, {}),
            ("3", "Waiting on us", 2, {}),
            ("4", "Closed", 3, {"isClosed": "true"}),
        ),
    ),
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: Any, fallback: datetime) -> datetime:
    if value in (None, ""):
        return fallback
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float, Decimal)) or str(value).strip().lstrip("-").isdigit():
        number = float(value)
        if abs(number) > 10_000_000_000:
            number /= 1000
        try:
            parsed = datetime.fromtimestamp(number, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return fallback
    else:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return fallback
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _string_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        return _iso(value)
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)


def _normalize_properties(properties: dict[str, Any] | None) -> dict[str, str]:
    return {str(key): _string_value(value) for key, value in (properties or {}).items()}


def _decode_json(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, str):
        return json.loads(value)
    return value


def _record_to_object(record: asyncpg.Record, properties: Sequence[str] | None = None) -> dict[str, Any]:
    values = _normalize_properties(_decode_json(record["properties"], {}))
    if properties is not None:
        requested = set(properties)
        requested.update(("hs_object_id", "createdate", "lastmodifieddate"))
        values = {key: value for key, value in values.items() if key in requested}
    return {
        "id": str(record["id"]),
        "properties": values,
        "createdAt": _iso(record["created_at"]),
        "updatedAt": _iso(record["updated_at"]),
        "archived": bool(record["archived"]),
    }


@asynccontextmanager
async def _connection(conn: asyncpg.Connection | None = None) -> AsyncIterator[asyncpg.Connection]:
    if conn is not None:
        yield conn
        return
    pool = await get_pool()
    async with pool.acquire() as acquired:
        yield acquired


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None or _pool.is_closing():
        database_url = os.environ.get("DATABASE_URL")
        if not database_url:
            raise RuntimeError("DATABASE_URL is required")
        _pool = await asyncpg.create_pool(
            database_url,
            min_size=1,
            max_size=int(os.environ.get("DB_POOL_MAX_SIZE", "20")),
            command_timeout=60,
        )
    return _pool


async def init_db() -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS objects (
                id BIGSERIAL PRIMARY KEY,
                object_type TEXT NOT NULL,
                properties JSONB NOT NULL DEFAULT '{}'::jsonb,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                archived BOOLEAN NOT NULL DEFAULT FALSE
            );
            CREATE TABLE IF NOT EXISTS associations (
                id BIGSERIAL PRIMARY KEY,
                from_object_type TEXT NOT NULL,
                from_object_id BIGINT NOT NULL,
                to_object_type TEXT NOT NULL,
                to_object_id BIGINT NOT NULL,
                association_type_id INT NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS pipelines (
                id TEXT PRIMARY KEY,
                object_type TEXT NOT NULL,
                label TEXT NOT NULL,
                display_order INT NOT NULL DEFAULT 0,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            CREATE TABLE IF NOT EXISTS pipeline_stages (
                id TEXT NOT NULL,
                pipeline_id TEXT NOT NULL REFERENCES pipelines(id) ON DELETE CASCADE,
                label TEXT NOT NULL,
                display_order INT NOT NULL DEFAULT 0,
                metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                PRIMARY KEY (pipeline_id, id)
            );
            CREATE TABLE IF NOT EXISTS property_definitions (
                name TEXT NOT NULL,
                object_type TEXT NOT NULL,
                label TEXT NOT NULL DEFAULT '',
                type TEXT NOT NULL DEFAULT 'string',
                field_type TEXT NOT NULL DEFAULT 'text',
                group_name TEXT NOT NULL DEFAULT 'contactinformation',
                has_unique_value BOOLEAN NOT NULL DEFAULT FALSE,
                options JSONB NOT NULL DEFAULT '[]'::jsonb,
                description TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (object_type, name)
            );
            CREATE TABLE IF NOT EXISTS lists (
                id BIGSERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                object_type TEXT NOT NULL DEFAULT 'companies',
                dynamic BOOLEAN NOT NULL DEFAULT FALSE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            CREATE TABLE IF NOT EXISTS list_memberships (
                list_id BIGINT NOT NULL REFERENCES lists(id) ON DELETE CASCADE,
                object_id BIGINT NOT NULL,
                PRIMARY KEY (list_id, object_id)
            );
            CREATE INDEX IF NOT EXISTS idx_obj_type ON objects(object_type) WHERE NOT archived;
            CREATE INDEX IF NOT EXISTS idx_obj_type_id ON objects(object_type, id) WHERE NOT archived;
            CREATE INDEX IF NOT EXISTS idx_obj_props ON objects USING GIN(properties jsonb_path_ops);
            CREATE INDEX IF NOT EXISTS idx_obj_legacy ON objects(object_type, (properties->>'id_legacy'));
            CREATE UNIQUE INDEX IF NOT EXISTS idx_company_vat
                ON objects((properties->>'partita_iva'))
                WHERE object_type='companies' AND properties->>'partita_iva' IS NOT NULL
                  AND properties->>'partita_iva' != '' AND NOT archived;
            CREATE UNIQUE INDEX IF NOT EXISTS idx_contact_email
                ON objects(object_type, (lower(properties->>'email')))
                WHERE object_type='contacts' AND properties->>'email' IS NOT NULL
                  AND properties->>'email' != '' AND NOT archived;
            CREATE INDEX IF NOT EXISTS idx_assoc_from ON associations(from_object_type, from_object_id);
            CREATE INDEX IF NOT EXISTS idx_assoc_to ON associations(to_object_type, to_object_id);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_assoc_unique
                ON associations(from_object_type, from_object_id, to_object_type, to_object_id, association_type_id);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_list_name_type ON lists(object_type, name);
            """
        )


def _default_property_rows() -> list[tuple[Any, ...]]:
    rows: list[tuple[Any, ...]] = []
    for object_type, names in DEFAULT_PROPERTIES.items():
        all_names = tuple(dict.fromkeys((*names, "hs_object_id", "createdate", "lastmodifieddate")))
        for name in all_names:
            property_type = "number" if name in {"amount", "price", "quantity", "hs_discount_percentage"} else "string"
            field_type = "number" if property_type == "number" else "text"
            rows.append((name, object_type, name, property_type, field_type, "information", False, "[]", ""))
    return rows


async def reset_db() -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "TRUNCATE list_memberships, associations, objects, pipeline_stages, pipelines, "
                "property_definitions, lists RESTART IDENTITY CASCADE"
            )
            await conn.executemany(
                """
                INSERT INTO property_definitions
                    (name, object_type, label, type, field_type, group_name,
                     has_unique_value, options, description)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9)
                """,
                _default_property_rows(),
            )
            for pipeline_id, object_type, label, order, stages in DEFAULT_PIPELINES:
                await conn.execute(
                    "INSERT INTO pipelines(id, object_type, label, display_order) VALUES($1,$2,$3,$4)",
                    pipeline_id,
                    object_type,
                    label,
                    order,
                )
                await conn.executemany(
                    """
                    INSERT INTO pipeline_stages(id, pipeline_id, label, display_order, metadata)
                    VALUES($1,$2,$3,$4,$5::jsonb)
                    """,
                    [(stage_id, pipeline_id, stage_label, stage_order, json.dumps(metadata))
                     for stage_id, stage_label, stage_order, metadata in stages],
                )


def _prepare_insert_properties(properties: dict[str, Any]) -> tuple[dict[str, str], datetime, datetime]:
    values = _normalize_properties(properties)
    now = _now()
    created_at = _parse_timestamp(values.get("createdate"), now)
    updated_at = _parse_timestamp(values.get("lastmodifieddate"), now)
    values.setdefault("createdate", _iso(created_at))
    values.setdefault("lastmodifieddate", _iso(updated_at))
    return values, created_at, updated_at


async def create_object(
    object_type: str,
    properties: dict[str, Any],
    *,
    conn: asyncpg.Connection | None = None,
) -> dict[str, Any]:
    values, created_at, updated_at = _prepare_insert_properties(properties)
    async with _connection(conn) as active:
        record = await active.fetchrow(
            """
            WITH allocated AS (SELECT nextval(pg_get_serial_sequence('objects','id')) AS id)
            INSERT INTO objects(id, object_type, properties, created_at, updated_at)
            SELECT id, $1, $2::jsonb || jsonb_build_object('hs_object_id', id::text), $3, $4
            FROM allocated
            RETURNING *
            """,
            object_type,
            json.dumps(values, ensure_ascii=False),
            created_at,
            updated_at,
        )
    return _record_to_object(record)


async def get_object(
    object_type: str,
    object_id: int,
    properties: Sequence[str] | None = None,
    *,
    conn: asyncpg.Connection | None = None,
) -> dict[str, Any] | None:
    async with _connection(conn) as active:
        record = await active.fetchrow(
            "SELECT * FROM objects WHERE object_type=$1 AND id=$2 AND NOT archived",
            object_type,
            int(object_id),
        )
    return _record_to_object(record, properties) if record else None


async def update_object(
    object_type: str,
    object_id: int,
    properties: dict[str, Any],
    *,
    conn: asyncpg.Connection | None = None,
) -> dict[str, Any] | None:
    values = _normalize_properties(properties)
    updated_at = _parse_timestamp(values.get("lastmodifieddate"), _now())
    values.setdefault("lastmodifieddate", _iso(updated_at))
    async with _connection(conn) as active:
        record = await active.fetchrow(
            """
            UPDATE objects
            SET properties = properties || $3::jsonb, updated_at=$4
            WHERE object_type=$1 AND id=$2 AND NOT archived
            RETURNING *
            """,
            object_type,
            int(object_id),
            json.dumps(values, ensure_ascii=False),
            updated_at,
        )
    return _record_to_object(record) if record else None


async def archive_object(
    object_type: str,
    object_id: int,
    *,
    conn: asyncpg.Connection | None = None,
) -> bool:
    now = _now()
    async with _connection(conn) as active:
        result = await active.execute(
            """
            UPDATE objects
            SET archived=TRUE, updated_at=$3,
                properties=properties || jsonb_build_object('lastmodifieddate',$4::text)
            WHERE object_type=$1 AND id=$2 AND NOT archived
            """,
            object_type,
            int(object_id),
            now,
            _iso(now),
        )
    return result == "UPDATE 1"


async def list_objects(
    object_type: str,
    limit: int = 10,
    after: int | None = None,
    properties: Sequence[str] | None = None,
    archived: bool = False,
) -> dict[str, Any]:
    limit = max(1, min(int(limit), 500))
    cursor = int(after or 0)
    pool = await get_pool()
    async with pool.acquire() as conn:
        records = await conn.fetch(
            """
            SELECT * FROM objects
            WHERE object_type=$1 AND archived=$2 AND id>$3
            ORDER BY id LIMIT $4
            """,
            object_type,
            bool(archived),
            cursor,
            limit + 1,
        )
    has_more = len(records) > limit
    records = records[:limit]
    response: dict[str, Any] = {"results": [_record_to_object(row, properties) for row in records]}
    if has_more and records:
        response["paging"] = {"next": {"after": str(records[-1]["id"])}}
    return response


def _add_parameter(params: list[Any], value: Any) -> str:
    params.append(value)
    return f"${len(params)}"


def _filter_sql(item: dict[str, Any], params: list[Any]) -> str:
    name = str(item.get("propertyName") or item.get("property") or "")
    operator = str(item.get("operator", "EQ")).upper()
    name_ref = _add_parameter(params, name)
    expression = f"properties ->> {name_ref}"
    value = item.get("value")
    values = item.get("values", value if isinstance(value, list) else None)

    if operator == "HAS_PROPERTY":
        return f"({expression} IS NOT NULL AND {expression} <> '')"
    if operator == "NOT_HAS_PROPERTY":
        return f"({expression} IS NULL OR {expression} = '')"
    if operator in {"IN", "NOT_IN"}:
        normalized = [str(entry).lower() for entry in (values or [])]
        value_ref = _add_parameter(params, normalized)
        clause = f"lower(COALESCE({expression},'')) = ANY({value_ref}::text[])"
        return f"NOT ({clause})" if operator == "NOT_IN" else clause
    value_ref = _add_parameter(params, _string_value(value))
    if operator == "EQ":
        return f"lower(COALESCE({expression},'')) = lower({value_ref}::text)"
    if operator == "NEQ":
        return f"lower(COALESCE({expression},'')) <> lower({value_ref}::text)"
    if operator == "CONTAINS_TOKEN":
        params[-1] = f"%{_string_value(value)}%"
        return f"COALESCE({expression},'') ILIKE {value_ref}"
    if operator in {"LT", "LTE", "GT", "GTE"}:
        comparison = {"LT": "<", "LTE": "<=", "GT": ">", "GTE": ">="}[operator]
        numeric = (
            f"CASE WHEN COALESCE({expression},'') ~ "
            "'^[-+]?[0-9]+([.][0-9]+)?$' "
            f"THEN ({expression})::numeric END"
        )
        return f"{numeric} {comparison} NULLIF({value_ref}::text,'')::numeric"
    raise ValueError(f"Unsupported search operator: {operator}")


async def search_objects(
    object_type: str,
    filter_groups: list[dict[str, Any]],
    sorts: Sequence[Any] | None = None,
    properties: Sequence[str] | None = None,
    limit: int = 10,
    after: int = 0,
) -> dict[str, Any]:
    params: list[Any] = [object_type]
    groups: list[str] = []
    for group in filter_groups or []:
        filters = group.get("filters", []) if isinstance(group, dict) else []
        clauses = [_filter_sql(item, params) for item in filters]
        if clauses:
            groups.append("(" + " AND ".join(clauses) + ")")
    where = "object_type=$1 AND NOT archived"
    if groups:
        where += " AND (" + " OR ".join(groups) + ")"
    where_params = list(params)

    order_parts: list[str] = []
    for sort in sorts or []:
        if isinstance(sort, str):
            pieces = sort.split()
            descending_prefix = pieces[0].startswith("-")
            property_name = pieces[0].lstrip("-")
            direction = "DESC" if descending_prefix or (len(pieces) > 1 and pieces[1].upper() == "DESC") else "ASC"
        else:
            property_name = str(sort.get("propertyName", "hs_object_id"))
            direction = "DESC" if str(sort.get("direction", "ASCENDING")).upper() in {"DESC", "DESCENDING"} else "ASC"
        name_ref = _add_parameter(params, property_name)
        order_parts.append(f"properties ->> {name_ref} {direction} NULLS LAST")
    order_sql = ", ".join(order_parts + ["id ASC"])
    offset_ref = _add_parameter(params, max(0, int(after or 0)))
    limit = max(1, min(int(limit), 200))
    limit_ref = _add_parameter(params, limit + 1)

    pool = await get_pool()
    async with pool.acquire() as conn:
        total = await conn.fetchval(f"SELECT count(*) FROM objects WHERE {where}", *where_params)
        records = await conn.fetch(
            f"SELECT * FROM objects WHERE {where} ORDER BY {order_sql} OFFSET {offset_ref} LIMIT {limit_ref}",
            *params,
        )
    has_more = len(records) > limit
    page = records[:limit]
    response: dict[str, Any] = {
        "total": int(total),
        "results": [_record_to_object(row, properties) for row in page],
    }
    if has_more:
        response["paging"] = {"next": {"after": str(max(0, int(after or 0)) + limit)}}
    return response


async def bulk_insert_objects(
    object_type: str,
    rows: list[dict[str, Any]],
    *,
    conn: asyncpg.Connection | None = None,
) -> list[int]:
    if not rows:
        return []
    async with _connection(conn) as active:
        ids = [int(row["id"]) for row in await active.fetch(
            "SELECT nextval(pg_get_serial_sequence('objects','id')) AS id FROM generate_series(1,$1)",
            len(rows),
        )]
        records = []
        for object_id, row in zip(ids, rows):
            values, created_at, updated_at = _prepare_insert_properties(row)
            values["hs_object_id"] = str(object_id)
            records.append((object_id, object_type, json.dumps(values, ensure_ascii=False), created_at, updated_at, False))
        await active.copy_records_to_table(
            "objects",
            records=records,
            columns=("id", "object_type", "properties", "created_at", "updated_at", "archived"),
        )
    return ids


async def create_association(
    from_type: str,
    from_id: int,
    to_type: str,
    to_id: int,
    assoc_type_id: int = 0,
    *,
    conn: asyncpg.Connection | None = None,
) -> bool:
    async with _connection(conn) as active:
        result = await active.execute(
            """
            INSERT INTO associations(from_object_type,from_object_id,to_object_type,to_object_id,association_type_id)
            VALUES($1,$2,$3,$4,$5) ON CONFLICT DO NOTHING
            """,
            from_type,
            int(from_id),
            to_type,
            int(to_id),
            int(assoc_type_id),
        )
    return result == "INSERT 0 1"


async def get_associations(
    from_type: str,
    from_id: int,
    to_type: str,
    *,
    conn: asyncpg.Connection | None = None,
) -> list[dict[str, str]]:
    async with _connection(conn) as active:
        rows = await active.fetch(
            """
            SELECT CASE WHEN from_object_type=$1 AND from_object_id=$2
                        THEN to_object_id ELSE from_object_id END AS related_id,
                   association_type_id
            FROM associations
            WHERE (from_object_type=$1 AND from_object_id=$2 AND to_object_type=$3)
               OR (to_object_type=$1 AND to_object_id=$2 AND from_object_type=$3)
            ORDER BY related_id, association_type_id
            """,
            from_type,
            int(from_id),
            to_type,
        )
    return [
        {
            "id": str(row["related_id"]),
            "type": f"{from_type}_to_{to_type}",
            "associationTypeId": str(row["association_type_id"]),
        }
        for row in rows
    ]


async def get_all_associations(
    object_type: str,
    object_id: int,
    *,
    conn: asyncpg.Connection | None = None,
) -> list[dict[str, Any]]:
    async with _connection(conn) as active:
        rows = await active.fetch(
            """
            SELECT * FROM associations
            WHERE (from_object_type=$1 AND from_object_id=$2)
               OR (to_object_type=$1 AND to_object_id=$2)
            ORDER BY id
            """,
            object_type,
            int(object_id),
        )
    return [dict(row) for row in rows]


async def delete_association(
    from_type: str,
    from_id: int,
    to_type: str,
    to_id: int,
    assoc_type_id: int = 0,
    *,
    conn: asyncpg.Connection | None = None,
) -> bool:
    async with _connection(conn) as active:
        result = await active.execute(
            """
            DELETE FROM associations
            WHERE association_type_id=$5 AND (
                (from_object_type=$1 AND from_object_id=$2 AND to_object_type=$3 AND to_object_id=$4)
                OR
                (from_object_type=$3 AND from_object_id=$4 AND to_object_type=$1 AND to_object_id=$2)
            )
            """,
            from_type,
            int(from_id),
            to_type,
            int(to_id),
            int(assoc_type_id),
        )
    return result != "DELETE 0"


async def bulk_insert_associations(
    assocs: Iterable[tuple[str, int, str, int, int]],
    *,
    conn: asyncpg.Connection | None = None,
) -> None:
    values = [(a, int(b), c, int(d), int(e)) for a, b, c, d, e in assocs]
    if not values:
        return
    async with _connection(conn) as active:
        await active.executemany(
            """
            INSERT INTO associations(from_object_type,from_object_id,to_object_type,to_object_id,association_type_id)
            VALUES($1,$2,$3,$4,$5) ON CONFLICT DO NOTHING
            """,
            values,
        )


async def merge_objects(object_type: str, primary_id: int, merged_id: int) -> dict[str, Any] | None:
    if int(primary_id) == int(merged_id):
        return await get_object(object_type, primary_id)
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            rows = await conn.fetch(
                """
                SELECT * FROM objects
                WHERE object_type=$1 AND id=ANY($2::bigint[]) AND NOT archived
                ORDER BY id FOR UPDATE
                """,
                object_type,
                [int(primary_id), int(merged_id)],
            )
            by_id = {row["id"]: row for row in rows}
            primary = by_id.get(int(primary_id))
            merged = by_id.get(int(merged_id))
            if primary is None or merged is None:
                return None
            primary_props = _decode_json(primary["properties"], {})
            merged_props = _decode_json(merged["properties"], {})
            combined = {**merged_props, **primary_props, "hs_object_id": str(primary_id)}
            now = _now()
            combined["lastmodifieddate"] = _iso(now)

            associations = await get_all_associations(object_type, int(merged_id), conn=conn)
            for association in associations:
                from_type = association["from_object_type"]
                from_id = association["from_object_id"]
                to_type = association["to_object_type"]
                to_id = association["to_object_id"]
                if from_type == object_type and from_id == int(merged_id):
                    from_id = int(primary_id)
                if to_type == object_type and to_id == int(merged_id):
                    to_id = int(primary_id)
                if from_type == to_type and from_id == to_id:
                    continue
                await create_association(
                    from_type,
                    from_id,
                    to_type,
                    to_id,
                    association["association_type_id"],
                    conn=conn,
                )
            await conn.execute(
                "DELETE FROM associations WHERE (from_object_type=$1 AND from_object_id=$2) "
                "OR (to_object_type=$1 AND to_object_id=$2)",
                object_type,
                int(merged_id),
            )
            await conn.execute(
                """
                INSERT INTO list_memberships(list_id, object_id)
                SELECT list_id, $1 FROM list_memberships WHERE object_id=$2
                ON CONFLICT DO NOTHING
                """,
                int(primary_id),
                int(merged_id),
            )
            await conn.execute("DELETE FROM list_memberships WHERE object_id=$1", int(merged_id))
            await conn.execute(
                "UPDATE objects SET archived=TRUE, updated_at=$3 WHERE object_type=$1 AND id=$2",
                object_type,
                int(merged_id),
                now,
            )
            record = await conn.fetchrow(
                """
                UPDATE objects SET properties=$3::jsonb, updated_at=$4
                WHERE object_type=$1 AND id=$2 RETURNING *
                """,
                object_type,
                int(primary_id),
                json.dumps(_normalize_properties(combined), ensure_ascii=False),
                now,
            )
    return _record_to_object(record)


def _stage_to_dict(row: asyncpg.Record) -> dict[str, Any]:
    return {
        "id": row["id"],
        "label": row["label"],
        "displayOrder": row["display_order"],
        "metadata": _decode_json(row["metadata"], {}),
        "archived": False,
    }


async def _pipeline_to_dict(conn: asyncpg.Connection, row: asyncpg.Record) -> dict[str, Any]:
    stages = await conn.fetch(
        "SELECT * FROM pipeline_stages WHERE pipeline_id=$1 ORDER BY display_order,id",
        row["id"],
    )
    return {
        "id": row["id"],
        "label": row["label"],
        "displayOrder": row["display_order"],
        "objectType": row["object_type"],
        "stages": [_stage_to_dict(stage) for stage in stages],
        "createdAt": _iso(row["created_at"]),
        "updatedAt": _iso(row["updated_at"]),
        "archived": False,
    }


async def list_pipelines(object_type: str) -> list[dict[str, Any]]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM pipelines WHERE object_type=$1 ORDER BY display_order,id",
            object_type,
        )
        return [await _pipeline_to_dict(conn, row) for row in rows]


async def get_pipeline(object_type: str, pipeline_id: str) -> dict[str, Any] | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM pipelines WHERE object_type=$1 AND id=$2",
            object_type,
            pipeline_id,
        )
        return await _pipeline_to_dict(conn, row) if row else None


async def create_pipeline(
    object_type: str,
    pipeline_id: str,
    label: str,
    stages: list[dict[str, Any]],
    display_order: int = 0,
) -> dict[str, Any]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO pipelines(id,object_type,label,display_order) VALUES($1,$2,$3,$4)",
                pipeline_id,
                object_type,
                label,
                int(display_order),
            )
            for index, stage in enumerate(stages or []):
                await conn.execute(
                    """
                    INSERT INTO pipeline_stages(id,pipeline_id,label,display_order,metadata)
                    VALUES($1,$2,$3,$4,$5::jsonb)
                    """,
                    str(stage["id"]),
                    pipeline_id,
                    str(stage.get("label", stage["id"])),
                    int(stage.get("displayOrder", index)),
                    json.dumps(stage.get("metadata", {}), ensure_ascii=False),
                )
            row = await conn.fetchrow("SELECT * FROM pipelines WHERE id=$1", pipeline_id)
            return await _pipeline_to_dict(conn, row)


async def update_pipeline(
    object_type: str,
    pipeline_id: str,
    *,
    label: str | None = None,
    display_order: int | None = None,
) -> dict[str, Any] | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE pipelines SET label=COALESCE($3,label), display_order=COALESCE($4,display_order),
                updated_at=NOW()
            WHERE object_type=$1 AND id=$2 RETURNING *
            """,
            object_type,
            pipeline_id,
            label,
            display_order,
        )
        return await _pipeline_to_dict(conn, row) if row else None


async def delete_pipeline(object_type: str, pipeline_id: str) -> bool:
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM pipelines WHERE object_type=$1 AND id=$2",
            object_type,
            pipeline_id,
        )
    return result == "DELETE 1"


async def create_pipeline_stage(
    object_type: str,
    pipeline_id: str,
    stage_id: str,
    label: str,
    display_order: int = 0,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            exists = await conn.fetchval(
                "SELECT 1 FROM pipelines WHERE object_type=$1 AND id=$2 FOR UPDATE",
                object_type,
                pipeline_id,
            )
            if not exists:
                return None
            row = await conn.fetchrow(
                """
                INSERT INTO pipeline_stages(id,pipeline_id,label,display_order,metadata)
                VALUES($1,$2,$3,$4,$5::jsonb) RETURNING *
                """,
                stage_id,
                pipeline_id,
                label,
                int(display_order),
                json.dumps(metadata or {}, ensure_ascii=False),
            )
            await conn.execute("UPDATE pipelines SET updated_at=NOW() WHERE id=$1", pipeline_id)
    return _stage_to_dict(row)


async def update_pipeline_stage(
    object_type: str,
    pipeline_id: str,
    stage_id: str,
    *,
    label: str | None = None,
    display_order: int | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                UPDATE pipeline_stages s
                SET label=COALESCE($4,s.label), display_order=COALESCE($5,s.display_order),
                    metadata=COALESCE($6::jsonb,s.metadata)
                FROM pipelines p
                WHERE s.pipeline_id=p.id AND p.object_type=$1 AND s.pipeline_id=$2 AND s.id=$3
                RETURNING s.*
                """,
                object_type,
                pipeline_id,
                stage_id,
                label,
                display_order,
                json.dumps(metadata, ensure_ascii=False) if metadata is not None else None,
            )
            if row:
                await conn.execute("UPDATE pipelines SET updated_at=NOW() WHERE id=$1", pipeline_id)
    return _stage_to_dict(row) if row else None


async def delete_pipeline_stage(object_type: str, pipeline_id: str, stage_id: str) -> bool:
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            result = await conn.execute(
                """
                DELETE FROM pipeline_stages s USING pipelines p
                WHERE s.pipeline_id=p.id AND p.object_type=$1 AND s.pipeline_id=$2 AND s.id=$3
                """,
                object_type,
                pipeline_id,
                stage_id,
            )
            if result == "DELETE 1":
                await conn.execute("UPDATE pipelines SET updated_at=NOW() WHERE id=$1", pipeline_id)
                return True
    return False


# Short aliases keep route code readable while retaining explicit pipeline-scoped names.
create_stage = create_pipeline_stage
update_stage = update_pipeline_stage
delete_stage = delete_pipeline_stage


def _property_to_dict(row: asyncpg.Record) -> dict[str, Any]:
    return {
        "name": row["name"],
        "label": row["label"],
        "type": row["type"],
        "fieldType": row["field_type"],
        "groupName": row["group_name"],
        "hasUniqueValue": row["has_unique_value"],
        "options": _decode_json(row["options"], []),
        "description": row["description"],
        "archived": False,
    }


async def list_property_defs(object_type: str) -> list[dict[str, Any]]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM property_definitions WHERE object_type=$1 ORDER BY name",
            object_type,
        )
    return [_property_to_dict(row) for row in rows]


async def get_property_def(object_type: str, name: str) -> dict[str, Any] | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM property_definitions WHERE object_type=$1 AND name=$2",
            object_type,
            name,
        )
    return _property_to_dict(row) if row else None


async def create_property_def(object_type: str, name: str, **kwargs: Any) -> dict[str, Any]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO property_definitions
                (name,object_type,label,type,field_type,group_name,has_unique_value,options,description)
            VALUES($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9) RETURNING *
            """,
            name,
            object_type,
            kwargs.get("label", name),
            kwargs.get("type", "string"),
            kwargs.get("field_type", kwargs.get("fieldType", "text")),
            kwargs.get("group_name", kwargs.get("groupName", "information")),
            bool(kwargs.get("has_unique_value", kwargs.get("hasUniqueValue", False))),
            json.dumps(kwargs.get("options", []), ensure_ascii=False),
            kwargs.get("description", ""),
        )
    return _property_to_dict(row)


async def update_property_def(object_type: str, name: str, **kwargs: Any) -> dict[str, Any] | None:
    current = await get_property_def(object_type, name)
    if current is None:
        return None
    values = {
        "label": kwargs.get("label", current["label"]),
        "type": kwargs.get("type", current["type"]),
        "field_type": kwargs.get("field_type", kwargs.get("fieldType", current["fieldType"])),
        "group_name": kwargs.get("group_name", kwargs.get("groupName", current["groupName"])),
        "has_unique_value": kwargs.get("has_unique_value", kwargs.get("hasUniqueValue", current["hasUniqueValue"])),
        "options": kwargs.get("options", current["options"]),
        "description": kwargs.get("description", current["description"]),
    }
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE property_definitions
            SET label=$3,type=$4,field_type=$5,group_name=$6,has_unique_value=$7,
                options=$8::jsonb,description=$9
            WHERE object_type=$1 AND name=$2 RETURNING *
            """,
            object_type,
            name,
            values["label"],
            values["type"],
            values["field_type"],
            values["group_name"],
            bool(values["has_unique_value"]),
            json.dumps(values["options"], ensure_ascii=False),
            values["description"],
        )
    return _property_to_dict(row) if row else None


async def delete_property_def(object_type: str, name: str) -> bool:
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM property_definitions WHERE object_type=$1 AND name=$2",
            object_type,
            name,
        )
    return result == "DELETE 1"


def _list_to_dict(row: asyncpg.Record) -> dict[str, Any]:
    return {
        "listId": str(row["id"]),
        "name": row["name"],
        "objectTypeId": row["object_type"],
        "processingType": "DYNAMIC" if row["dynamic"] else "MANUAL",
        "createdAt": _iso(row["created_at"]),
        "updatedAt": _iso(row["updated_at"]),
    }


async def create_list(name: str, object_type: str = "companies", dynamic: bool = False) -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        list_id = await conn.fetchval(
            "INSERT INTO lists(name,object_type,dynamic) VALUES($1,$2,$3) RETURNING id",
            name,
            object_type,
            bool(dynamic),
        )
    return int(list_id)


async def add_to_list(list_id: int, object_ids: Iterable[int]) -> None:
    values = [(int(list_id), int(object_id)) for object_id in object_ids]
    if not values:
        return
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.executemany(
                "INSERT INTO list_memberships(list_id,object_id) VALUES($1,$2) ON CONFLICT DO NOTHING",
                values,
            )
            await conn.execute("UPDATE lists SET updated_at=NOW() WHERE id=$1", int(list_id))


async def remove_from_list(list_id: int, object_ids: Iterable[int]) -> int:
    ids = [int(object_id) for object_id in object_ids]
    if not ids:
        return 0
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            removed = await conn.fetchval(
                """
                WITH removed AS (
                    DELETE FROM list_memberships WHERE list_id=$1 AND object_id=ANY($2::bigint[])
                    RETURNING 1
                ) SELECT count(*) FROM removed
                """,
                int(list_id),
                ids,
            )
            await conn.execute("UPDATE lists SET updated_at=NOW() WHERE id=$1", int(list_id))
    return int(removed)


async def get_list(list_id: int) -> dict[str, Any] | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM lists WHERE id=$1", int(list_id))
    return _list_to_dict(row) if row else None


async def get_list_by_name(name: str, object_type: str = "companies") -> dict[str, Any] | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM lists WHERE object_type=$1 AND name=$2",
            object_type,
            name,
        )
    return _list_to_dict(row) if row else None


async def get_list_members(list_id: int, limit: int = 100, after: int | None = None) -> dict[str, Any]:
    limit = max(1, min(int(limit), 500))
    cursor = int(after or 0)
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT object_id FROM list_memberships
            WHERE list_id=$1 AND object_id>$2 ORDER BY object_id LIMIT $3
            """,
            int(list_id),
            cursor,
            limit + 1,
        )
    has_more = len(rows) > limit
    page = rows[:limit]
    response: dict[str, Any] = {"results": [{"recordId": str(row["object_id"])} for row in page]}
    if has_more and page:
        response["paging"] = {"next": {"after": str(page[-1]["object_id"])}}
    return response
