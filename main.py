import contextvars
import html
import importlib
import inspect
import os
import re
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, Response


APP_VERSION = "2026-09"
OBJECT_TYPE_ALIASES = {
    "contact": "contacts", "company": "companies", "deal": "deals",
    "ticket": "tickets", "note": "notes", "call": "calls",
    "email": "emails", "meeting": "meetings", "product": "products",
    "line_item": "line_items", "task": "tasks",
}
KNOWN_OBJECT_TYPES = {
    "contacts", "companies", "deals", "tickets", "notes", "calls",
    "emails", "meetings", "products", "line_items", "tasks",
}
UI_TYPES = ("contacts", "companies", "deals", "tickets")

app = FastAPI(title="Brambilla CRM", version=APP_VERSION)
_db_override: Any = None
_mutation_event_time: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "mutation_event_time", default=None
)


def normalize_object_type(value: str) -> str:
    normalized = value.strip().lower().replace("-", "_")
    return OBJECT_TYPE_ALIASES.get(normalized, normalized)


def error_payload(message: str, category: str) -> dict[str, str]:
    return {"status": "error", "message": message, "category": category}


def api_error(status_code: int, message: str, category: str = "VALIDATION_ERROR") -> HTTPException:
    return HTTPException(status_code=status_code, detail=error_payload(message, category))


def _db() -> Any:
    if _db_override is not None:
        return _db_override
    try:
        return importlib.import_module("db")
    except ModuleNotFoundError as exc:
        raise api_error(503, "Database service is not installed", "SERVICE_UNAVAILABLE") from exc


def _is_unique_violation(exc: Exception) -> bool:
    return exc.__class__.__name__ == "UniqueViolationError" or getattr(exc, "sqlstate", None) == "23505"


async def _call_db(name: str, *args: Any, **kwargs: Any) -> Any:
    function = getattr(_db(), name, None)
    if function is None:
        raise api_error(503, f"Database method {name} is unavailable", "SERVICE_UNAVAILABLE")
    return await function(*args, **kwargs)


async def create_bidirectional_association(
    from_type: str, from_id: int, to_type: str, to_id: int, type_id: int = 0
) -> None:
    """Store one edge; the database resolves it in either direction."""
    from_type = normalize_object_type(from_type)
    to_type = normalize_object_type(to_type)
    await _call_db("create_association", from_type, from_id, to_type, to_id, type_id)


async def delete_bidirectional_association(
    from_type: str, from_id: int, to_type: str, to_id: int, type_id: int = 0
) -> None:
    from_type = normalize_object_type(from_type)
    to_type = normalize_object_type(to_type)
    await _call_db("delete_association", from_type, from_id, to_type, to_id, type_id)


async def read_associations(from_type: str, from_id: int, to_type: str) -> list[dict[str, str]]:
    """Read both stored directions so migration-created links are reversible."""
    from_type = normalize_object_type(from_type)
    to_type = normalize_object_type(to_type)
    direct = await _call_db("get_associations", from_type, from_id, to_type)
    by_id = {str(item["id"]): item for item in direct}
    database = _db()
    reverse_reader = getattr(database, "get_reverse_associations", None)
    if reverse_reader is not None:
        reverse = await reverse_reader(from_type, from_id, to_type)
    elif getattr(database, "get_pool", None) is not None:
        pool = await database.get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT from_object_id AS id
                FROM associations
                WHERE to_object_type=$1 AND to_object_id=$2 AND from_object_type=$3
                """,
                from_type,
                from_id,
                to_type,
            )
        reverse = [{"id": str(row["id"]), "type": f"{from_type}_to_{to_type}"} for row in rows]
    else:
        reverse = []
    for item in reverse:
        by_id.setdefault(str(item["id"]), item)
    return list(by_id.values())


def _association_target(item: dict[str, Any]) -> tuple[str, int, int]:
    target_type = item.get("to", {}).get("objectType") or item.get("to_type")
    target_id = item.get("to", {}).get("id") or item.get("to_id")
    type_id = item.get("types", [{}])[0].get("associationTypeId", item.get("typeId", 0))
    if not target_type or target_id is None:
        raise api_error(400, "Association requires a target object type and id")
    return normalize_object_type(str(target_type)), int(target_id), int(type_id)


async def _create_body_associations(
    object_type: str, object_id: int, associations: list[dict[str, Any]]
) -> None:
    for association in associations:
        target_type, target_id, type_id = _association_target(association)
        await create_bidirectional_association(object_type, object_id, target_type, target_id, type_id)


async def mutate_object(
    action: str,
    object_type: str,
    *,
    object_id: int | None = None,
    properties: dict[str, Any] | None = None,
    associations: list[dict[str, Any]] | None = None,
    event_time: str | None = None,
) -> dict[str, Any]:
    """The sole create/update path used by HTTP, batches, and assistant tools."""
    object_type = normalize_object_type(object_type)
    properties = properties or {}
    associations = associations or []
    old_properties: dict[str, Any] | None = None
    try:
        if action == "create":
            record = await _call_db("create_object", object_type, properties)
            object_id = int(record["id"])
        elif action == "update":
            if object_id is None:
                raise api_error(400, "object_id is required for updates")
            old_record = await _call_db("get_object", object_type, object_id)
            if old_record is None:
                raise api_error(404, "Object not found", "OBJECT_NOT_FOUND")
            old_properties = dict(old_record.get("properties", {}))
            record = await _call_db("update_object", object_type, object_id, properties)
        else:
            raise api_error(400, f"Unsupported mutation action: {action}")
    except HTTPException:
        raise
    except Exception as exc:
        if _is_unique_violation(exc):
            raise api_error(409, "A record with that unique value already exists", "CONFLICT") from exc
        raise
    if associations:
        await _create_body_associations(object_type, int(object_id), associations)
    from automations import handle_automations
    await handle_automations(
        object_type, action, int(object_id), dict(record.get("properties", properties)),
        old_properties, event_time=event_time or _mutation_event_time.get(),
    )
    return record


async def assistant_create_object(
    object_type: str, properties: dict[str, Any], *,
    associations: list[dict[str, Any]] | None = None, **_: Any,
) -> dict[str, Any]:
    return await mutate_object("create", object_type, properties=properties, associations=associations)


async def assistant_update_object(
    object_type: str, object_id: int, properties: dict[str, Any], **_: Any
) -> dict[str, Any]:
    return await mutate_object("update", object_type, object_id=int(object_id), properties=properties)


@app.middleware("http")
async def authenticate(request: Request, call_next):
    if request.method == "GET" and request.url.path == "/health":
        return await call_next(request)
    configured_token = os.environ.get("CRM_TOKEN")
    supplied = request.headers.get("Authorization", "")
    if not configured_token or supplied != f"Bearer {configured_token}":
        return JSONResponse(
            status_code=401,
            content=error_payload("Invalid or missing bearer token", "INVALID_AUTHENTICATION"),
        )
    return await call_next(request)


@app.middleware("http")
async def rewrite_versioned_paths(request: Request, call_next):
    path = request.scope["path"]
    rewritten = re.sub(
        r"^/crm/(objects|pipelines|properties|lists)/2026-09/",
        r"/crm/v3/\1/",
        path,
    )
    if rewritten != path:
        request.scope["path"] = rewritten
    return await call_next(request)


@app.exception_handler(HTTPException)
async def http_exception_handler(_: Request, exc: HTTPException):
    if isinstance(exc.detail, dict) and exc.detail.get("status") == "error":
        content = exc.detail
    else:
        category = "OBJECT_NOT_FOUND" if exc.status_code == 404 else "VALIDATION_ERROR"
        content = error_payload(str(exc.detail), category)
    return JSONResponse(status_code=exc.status_code, content=content)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(_: Request, exc: RequestValidationError):
    return JSONResponse(status_code=422, content=error_payload(str(exc), "VALIDATION_ERROR"))


@app.exception_handler(Exception)
async def unhandled_exception_handler(_: Request, exc: Exception):
    return JSONResponse(
        status_code=500,
        content=error_payload(str(exc) or "Internal server error", "INTERNAL_ERROR"),
    )


@app.on_event("startup")
async def startup() -> None:
    await _call_db("init_db")


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok", "version": APP_VERSION,
        "ui": {object_type: f"/{object_type}" for object_type in UI_TYPES},
    }


def _render_records_page(object_type: str, result: dict[str, Any]) -> str:
    records = result.get("results", [])
    property_names = sorted({name for record in records for name in record.get("properties", {})})[:8]
    headings = "".join(f"<th>{html.escape(name)}</th>" for name in property_names)
    rows = []
    for record in records:
        cells = "".join(
            f"<td>{html.escape(str(record.get('properties', {}).get(name, '')))}</td>"
            for name in property_names
        )
        rows.append(f"<tr><td>{html.escape(str(record.get('id', '')))}</td>{cells}</tr>")
    empty = "<p>Nessun record.</p>" if not records else ""
    navigation = "".join(f"<a href='/{kind}'>{kind.title()}</a>" for kind in UI_TYPES)
    return (
        "<!doctype html><html lang='it'><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>Brambilla CRM</title><style>body{font-family:system-ui;margin:32px;color:#18212b}"
        "nav a{margin-right:16px}table{border-collapse:collapse;width:100%;margin-top:24px}"
        "th,td{text-align:left;border-bottom:1px solid #d8dee4;padding:9px}th{background:#f4f6f8}</style>"
        f"<nav>{navigation}</nav><h1>{html.escape(object_type.title())}</h1>{empty}"
        f"<table><thead><tr><th>ID</th>{headings}</tr></thead><tbody>{''.join(rows)}</tbody></table></html>"
    )


@app.get("/{object_type}", response_class=HTMLResponse, include_in_schema=False)
async def ui_records(object_type: str):
    object_type = normalize_object_type(object_type)
    if object_type not in UI_TYPES:
        raise api_error(404, "Page not found", "OBJECT_NOT_FOUND")
    result = await _call_db("list_objects", object_type, limit=50)
    return HTMLResponse(_render_records_page(object_type, result))


@app.post("/__reset", status_code=204)
async def reset_crm() -> Response:
    await _call_db("reset_db")
    from automations import reset_automation_state
    await reset_automation_state()
    return Response(status_code=204)


@app.post("/__migrate", status_code=204)
async def migrate_crm(body: dict[str, Any]) -> Response:
    export_url = body.get("export_url")
    if not isinstance(export_url, str) or not export_url.strip():
        raise api_error(400, "export_url is required")
    try:
        migration = importlib.import_module("migration")
    except ModuleNotFoundError as exc:
        raise api_error(503, "Migration service is not installed", "SERVICE_UNAVAILABLE") from exc
    await migration.run_migration(export_url)
    return Response(status_code=204)


@app.post("/__agente")
async def agent(body: dict[str, Any]) -> dict[str, Any]:
    try:
        assistant = importlib.import_module("assistant")
    except ModuleNotFoundError as exc:
        raise api_error(503, "Assistant service is not installed", "SERVICE_UNAVAILABLE") from exc
    assistant.create_object = assistant_create_object
    assistant.update_object = assistant_update_object
    assistant.mutate_object = mutate_object
    event_time = body.get("context", {}).get("now") if isinstance(body.get("context"), dict) else None
    token = _mutation_event_time.set(event_time)
    try:
        handler = assistant.handle_agent_request
        if "mutation_service" in inspect.signature(handler).parameters:
            result = await handler(body, mutation_service=mutate_object)
        else:
            result = await handler(body)
    finally:
        _mutation_event_time.reset(token)
    return {"reply": result}


@app.post("/crm/v3/objects/{object_type}/search")
async def search(object_type: str, body: dict[str, Any]):
    return await _call_db(
        "search_objects", normalize_object_type(object_type), body.get("filterGroups", []),
        sorts=body.get("sorts"), properties=body.get("properties"),
        limit=body.get("limit", 10), after=body.get("after", 0),
    )


@app.post("/crm/v3/objects/{object_type}/batch/create")
async def batch_create(object_type: str, body: dict[str, Any]):
    results = []
    for item in body.get("inputs", []):
        results.append(await mutate_object(
            "create", object_type, properties=item.get("properties", {}),
            associations=item.get("associations", []), event_time=item.get("eventTime"),
        ))
    return {"status": "COMPLETE", "results": results}


@app.post("/crm/v3/objects/{object_type}/batch/update")
async def batch_update(object_type: str, body: dict[str, Any]):
    results = []
    for item in body.get("inputs", []):
        results.append(await mutate_object(
            "update", object_type, object_id=int(item["id"]),
            properties=item.get("properties", {}), event_time=item.get("eventTime"),
        ))
    return {"status": "COMPLETE", "results": results}


@app.post("/crm/v3/objects/{object_type}/batch/upsert")
async def batch_upsert(object_type: str, body: dict[str, Any]):
    object_type = normalize_object_type(object_type)
    results = []
    for item in body.get("inputs", []):
        id_property = item.get("idProperty") or body.get("idProperty")
        identity = item.get("id")
        properties = item.get("properties", {})
        if not id_property or identity in (None, ""):
            raise api_error(400, "Each upsert input requires idProperty and id")
        matches = await _call_db(
            "search_objects",
            object_type,
            [{"filters": [{"propertyName": str(id_property), "operator": "EQ", "value": str(identity)}]}],
            limit=2,
        )
        if matches.get("results"):
            record = await mutate_object(
                "update",
                object_type,
                object_id=int(matches["results"][0]["id"]),
                properties=properties,
                event_time=item.get("eventTime"),
            )
        else:
            create_properties = {**properties, str(id_property): str(identity)}
            try:
                record = await mutate_object(
                    "create",
                    object_type,
                    properties=create_properties,
                    associations=item.get("associations", []),
                    event_time=item.get("eventTime"),
                )
            except HTTPException as exc:
                if exc.status_code != 409:
                    raise
                # A parallel upsert may have inserted the unique identity first.
                matches = await _call_db(
                    "search_objects",
                    object_type,
                    [{"filters": [{"propertyName": str(id_property), "operator": "EQ", "value": str(identity)}]}],
                    limit=2,
                )
                if not matches.get("results"):
                    raise
                record = await mutate_object(
                    "update",
                    object_type,
                    object_id=int(matches["results"][0]["id"]),
                    properties=properties,
                    event_time=item.get("eventTime"),
                )
        results.append(record)
    return {"status": "COMPLETE", "results": results}


@app.post("/crm/v3/objects/{object_type}/batch/read")
async def batch_read(object_type: str, body: dict[str, Any]):
    object_type = normalize_object_type(object_type)
    results = []
    for item in body.get("inputs", []):
        record = await _call_db("get_object", object_type, int(item["id"]), body.get("properties"))
        if record is not None:
            results.append(record)
    return {"status": "COMPLETE", "results": results}


@app.post("/crm/v3/objects/{object_type}/batch/archive", status_code=204)
async def batch_archive(object_type: str, body: dict[str, Any]) -> Response:
    object_type = normalize_object_type(object_type)
    for item in body.get("inputs", []):
        await _call_db("archive_object", object_type, int(item["id"]))
    return Response(status_code=204)


@app.post("/crm/v3/objects/{object_type}/merge")
async def merge_objects(object_type: str, body: dict[str, Any]):
    object_type = normalize_object_type(object_type)
    try:
        primary_id = int(body["primaryObjectId"])
        merged_id = int(body["objectIdToMerge"])
    except (KeyError, TypeError, ValueError) as exc:
        raise api_error(400, "primaryObjectId and objectIdToMerge are required") from exc
    if primary_id == merged_id:
        raise api_error(400, "The two object ids must differ")
    primary = await _call_db("get_object", object_type, primary_id)
    secondary = await _call_db("get_object", object_type, merged_id)
    if primary is None or secondary is None:
        raise api_error(404, "Object not found", "OBJECT_NOT_FOUND")
    merged_properties = dict(secondary.get("properties", {}))
    merged_properties.update({
        key: value for key, value in primary.get("properties", {}).items() if value not in (None, "")
    })
    for target_type in KNOWN_OBJECT_TYPES:
        if target_type == object_type:
            continue
        for association in await read_associations(object_type, merged_id, target_type):
            await create_bidirectional_association(
                object_type, primary_id, target_type, int(association["id"])
            )
    result = await mutate_object(
        "update", object_type, object_id=primary_id, properties=merged_properties
    )
    await _call_db("archive_object", object_type, merged_id)
    return result


@app.get("/crm/v3/objects/{object_type}")
async def list_records(
    object_type: str, limit: int = Query(10, ge=1, le=100), after: int | None = None,
    properties: str | None = None, archived: bool = False,
):
    property_list = [item for item in (properties or "").split(",") if item] or None
    return await _call_db(
        "list_objects", normalize_object_type(object_type), limit=limit, after=after,
        properties=property_list, archived=archived,
    )


@app.post("/crm/v3/objects/{object_type}", status_code=201)
async def create_record(object_type: str, body: dict[str, Any]):
    return await mutate_object(
        "create", object_type, properties=body.get("properties", {}),
        associations=body.get("associations", []), event_time=body.get("eventTime"),
    )


@app.get("/crm/v3/objects/{object_type}/{object_id}")
async def get_record(
    object_type: str, object_id: int, properties: str | None = None,
    associations: str | None = None,
):
    object_type = normalize_object_type(object_type)
    property_list = [item for item in (properties or "").split(",") if item] or None
    record = await _call_db("get_object", object_type, object_id, property_list)
    if record is None:
        raise api_error(404, "Object not found", "OBJECT_NOT_FOUND")
    if associations:
        record["associations"] = {}
        for target_type in associations.split(","):
            target_type = normalize_object_type(target_type)
            record["associations"][target_type] = {
                "results": await read_associations(object_type, object_id, target_type)
            }
    return record


@app.patch("/crm/v3/objects/{object_type}/{object_id}")
async def update_record(object_type: str, object_id: int, body: dict[str, Any]):
    return await mutate_object(
        "update", object_type, object_id=object_id, properties=body.get("properties", {}),
        event_time=body.get("eventTime"),
    )


@app.delete("/crm/v3/objects/{object_type}/{object_id}", status_code=204)
async def delete_record(object_type: str, object_id: int) -> Response:
    archived = await _call_db("archive_object", normalize_object_type(object_type), object_id)
    if not archived:
        raise api_error(404, "Object not found", "OBJECT_NOT_FOUND")
    return Response(status_code=204)


@app.put("/crm/v3/objects/{from_type}/{from_id}/associations/{to_type}/{to_id}/{type_id}")
async def put_association(from_type: str, from_id: int, to_type: str, to_id: int, type_id: int):
    await create_bidirectional_association(from_type, from_id, to_type, to_id, type_id)
    return {"status": "COMPLETE"}


@app.get("/crm/v3/objects/{from_type}/{from_id}/associations/{to_type}")
async def list_associations(from_type: str, from_id: int, to_type: str):
    return {"results": await read_associations(from_type, from_id, to_type)}


@app.delete(
    "/crm/v3/objects/{from_type}/{from_id}/associations/{to_type}/{to_id}/{type_id}", status_code=204,
)
async def remove_association(from_type: str, from_id: int, to_type: str, to_id: int, type_id: int):
    await delete_bidirectional_association(from_type, from_id, to_type, to_id, type_id)
    return Response(status_code=204)


@app.get("/crm/v3/pipelines/{object_type}")
async def pipelines(object_type: str):
    return {"results": await _call_db("list_pipelines", normalize_object_type(object_type))}


@app.get("/crm/v3/pipelines/{object_type}/{pipeline_id}")
async def pipeline(object_type: str, pipeline_id: str):
    result = await _call_db("get_pipeline", normalize_object_type(object_type), pipeline_id)
    if result is None:
        raise api_error(404, "Pipeline not found", "OBJECT_NOT_FOUND")
    return result


@app.post("/crm/v3/pipelines/{object_type}", status_code=201)
async def create_pipeline_endpoint(object_type: str, body: dict[str, Any]):
    pipeline_id = body.get("id") or body.get("pipelineId")
    if not pipeline_id or not body.get("label"):
        raise api_error(400, "Pipeline id and label are required")
    return await _call_db(
        "create_pipeline", normalize_object_type(object_type), str(pipeline_id),
        body["label"], body.get("stages", []),
    )


@app.get("/crm/v3/properties/{object_type}")
async def properties(object_type: str):
    return {"results": await _call_db("list_property_defs", normalize_object_type(object_type))}


@app.get("/crm/v3/properties/{object_type}/{name}")
async def property_definition(object_type: str, name: str):
    result = await _call_db("get_property_def", normalize_object_type(object_type), name)
    if result is None:
        raise api_error(404, "Property not found", "OBJECT_NOT_FOUND")
    return result


@app.post("/crm/v3/properties/{object_type}", status_code=201)
async def create_property(object_type: str, body: dict[str, Any]):
    name = body.get("name")
    if not name:
        raise api_error(400, "Property name is required")
    kwargs = {key: value for key, value in body.items() if key != "name"}
    return await _call_db("create_property_def", normalize_object_type(object_type), name, **kwargs)


@app.patch("/crm/v3/properties/{object_type}/{name}")
async def update_property(object_type: str, name: str, body: dict[str, Any]):
    result = await _call_db("update_property_def", normalize_object_type(object_type), name, **body)
    if result is None:
        raise api_error(404, "Property not found", "OBJECT_NOT_FOUND")
    return result


@app.delete("/crm/v3/properties/{object_type}/{name}", status_code=204)
async def delete_property(object_type: str, name: str) -> Response:
    deleted = await _call_db("delete_property_def", normalize_object_type(object_type), name)
    if not deleted:
        raise api_error(404, "Property not found", "OBJECT_NOT_FOUND")
    return Response(status_code=204)


@app.get("/crm/v3/lists/object-type-id/{object_type}/name/{name}")
async def list_by_name(object_type: str, name: str):
    result = await _call_db("get_list_by_name", name, normalize_object_type(object_type))
    if result is None:
        raise api_error(404, "List not found", "OBJECT_NOT_FOUND")
    return result


@app.get("/crm/v3/lists/{list_id}")
async def list_definition(list_id: int):
    result = await _call_db("get_list", list_id)
    if result is None:
        raise api_error(404, "List not found", "OBJECT_NOT_FOUND")
    return result


@app.get("/crm/v3/lists/{list_id}/memberships")
async def list_memberships(list_id: int, limit: int = 100, after: int | None = None):
    return await _call_db("get_list_members", list_id, limit=limit, after=after)


@app.post("/crm/v3/lists", status_code=201)
async def create_list_endpoint(body: dict[str, Any]):
    name = body.get("name")
    if not name:
        raise api_error(400, "List name is required")
    list_id = await _call_db(
        "create_list", name, normalize_object_type(body.get("objectTypeId", "companies"))
    )
    return await _call_db("get_list", list_id)


@app.put("/crm/v3/lists/{list_id}/memberships/add")
async def add_list_members(list_id: int, body: Any):
    ids = body if isinstance(body, list) else body.get("recordIds") or body.get("objectIds") or []
    await _call_db("add_to_list", list_id, [int(value) for value in ids])
    return {"status": "COMPLETE"}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
