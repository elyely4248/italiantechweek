import contextvars
import html
import importlib
import inspect
import os
import re
import uuid
from datetime import datetime, timezone
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
    "line_item": "line_items", "task": "tasks", "quote": "quotes",
}
KNOWN_OBJECT_TYPES = {
    "contacts", "companies", "deals", "tickets", "notes", "calls",
    "emails", "meetings", "products", "line_items", "tasks", "quotes",
}
UI_TYPES = ("contacts", "companies", "deals", "tickets", "dormant")
ASSOCIATION_LABEL_TYPES = {
    ("contacts", "companies"): 1,
    ("companies", "contacts"): 2,
    ("deals", "contacts"): 3,
    ("contacts", "deals"): 4,
    ("deals", "companies"): 5,
    ("companies", "deals"): 6,
    ("tickets", "contacts"): 15,
    ("contacts", "tickets"): 16,
    ("tickets", "companies"): 25,
    ("companies", "tickets"): 26,
    ("quotes", "deals"): 63,
    ("deals", "quotes"): 64,
    ("quotes", "contacts"): 69,
    ("contacts", "quotes"): 70,
    ("quotes", "companies"): 71,
    ("companies", "quotes"): 72,
}

app = FastAPI(title="Brambilla CRM", version=APP_VERSION)
_db_override: Any = None
RATE_LIMIT_MAX = 10_000
_rate_limit_count = 0
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


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


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
    global _rate_limit_count
    if request.method == "GET" and request.url.path == "/health":
        return await call_next(request)
    configured_token = os.environ.get("CRM_TOKEN")
    supplied = request.headers.get("Authorization", "")
    if not configured_token or supplied != f"Bearer {configured_token}":
        return JSONResponse(
            status_code=401,
            content=error_payload("Invalid or missing bearer token", "INVALID_AUTHENTICATION"),
        )
    _rate_limit_count += 1
    remaining = max(0, RATE_LIMIT_MAX - _rate_limit_count)
    if _rate_limit_count > RATE_LIMIT_MAX:
        return JSONResponse(
            status_code=429,
            content=error_payload("Rate limit exceeded", "RATE_LIMIT_EXCEEDED"),
            headers={
                "X-HubSpot-RateLimit-Max": str(RATE_LIMIT_MAX),
                "X-HubSpot-RateLimit-Remaining": "0",
            },
        )
    response = await call_next(request)
    response.headers["X-HubSpot-RateLimit-Max"] = str(RATE_LIMIT_MAX)
    response.headers["X-HubSpot-RateLimit-Remaining"] = str(remaining)
    return response


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


CRM_CSS = """
:root{--nav:#2e475d;--accent:#ff7a59;--teal:#00a4bd;--bg:#f5f8fa;--line:#dbe4ea;--text:#213343;--muted:#607286;--good:#00a86b;--warn:#f5c542;--bad:#d94c4c}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
a{color:var(--teal);text-decoration:none}a:hover{text-decoration:underline}.app{min-height:100vh;display:flex}.sidebar{position:fixed;inset:0 auto 0 0;width:200px;background:var(--nav);color:white;padding:22px 18px}
.brand{font-size:18px;font-weight:800;margin-bottom:24px}.nav{display:grid;gap:6px}.nav a{color:#d9e7ef;padding:10px 12px;border-radius:6px;text-decoration:none}.nav a.active,.nav a:hover{background:rgba(255,255,255,.12);color:white}
.main{margin-left:200px;width:calc(100% - 200px)}.topbar{height:70px;background:white;border-bottom:1px solid var(--line);display:flex;align-items:center;justify-content:space-between;padding:0 28px}
.topbar h1{font-size:22px;margin:0}.content{padding:28px;max-width:1440px}.grid{display:grid;gap:18px}.two-col{grid-template-columns:minmax(0,1fr) 360px;align-items:start}.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:14px;margin-bottom:18px}
.card{background:white;border:1px solid var(--line);border-radius:8px;box-shadow:0 1px 2px rgba(33,51,67,.04)}.card-h{padding:16px 18px;border-bottom:1px solid var(--line);font-weight:750;display:flex;align-items:center;justify-content:space-between}.card-b{padding:18px}
.stat{background:white;border:1px solid var(--line);border-radius:8px;padding:16px}.stat .k{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.04em}.stat .v{font-size:24px;font-weight:800;margin-top:4px}
.table-wrap{overflow:auto;background:white;border:1px solid var(--line);border-radius:8px}table{width:100%;border-collapse:collapse;min-width:760px}th,td{text-align:left;padding:12px 14px;border-bottom:1px solid var(--line);vertical-align:top}th{background:#f8fbfd;color:#425b76;font-size:12px;text-transform:uppercase;letter-spacing:.04em}tr:last-child td{border-bottom:0}
.badge{display:inline-flex;align-items:center;gap:6px;border-radius:999px;padding:4px 9px;font-size:12px;font-weight:700;background:#edf2f7;color:#425b76;white-space:nowrap}.badge.green{background:#dff6ea;color:#087443}.badge.yellow{background:#fff3c4;color:#7d5a00}.badge.gray{background:#edf2f7;color:#425b76}.badge.blue{background:#dff3ff;color:#0b6f8a}.badge.orange{background:#ffe5d9;color:#b83f16}.badge.red{background:#ffe1df;color:#b42318}
.muted{color:var(--muted)}.empty{padding:30px;text-align:center;color:var(--muted)}.toolbar{display:flex;align-items:center;justify-content:space-between;gap:14px;margin-bottom:16px}.tabs{display:flex;flex-wrap:wrap;gap:8px}.tab{padding:8px 11px;background:white;border:1px solid var(--line);border-radius:6px;color:var(--text);text-decoration:none}.tab.active{border-color:var(--accent);box-shadow:0 0 0 1px var(--accent) inset;color:#9d3213}
.pagination{display:flex;gap:10px;justify-content:flex-end;margin-top:14px}.btn{display:inline-flex;align-items:center;justify-content:center;min-height:34px;padding:7px 12px;border-radius:6px;border:1px solid var(--line);background:white;color:var(--text);text-decoration:none}.btn.disabled{opacity:.45;pointer-events:none}
.kv{display:grid;grid-template-columns:150px 1fr;gap:10px 14px}.kv dt{color:var(--muted)}.kv dd{margin:0;font-weight:650}.list{display:grid;gap:10px}.list-item{padding:12px;border:1px solid var(--line);border-radius:7px;background:#fbfdfe}.item-title{font-weight:750}.timeline{display:grid;gap:12px}.event{border-left:3px solid var(--teal);padding:2px 0 2px 12px}.event time{display:block;color:var(--muted);font-size:12px;margin-top:3px}
.board{display:grid;grid-auto-flow:column;grid-auto-columns:minmax(260px,1fr);gap:14px;overflow:auto;padding-bottom:8px}.column{background:#edf3f7;border:1px solid var(--line);border-radius:8px;min-height:220px}.column.closed-won{background:#eaf8f0}.column.closed-lost{background:#fff0ee}.col-h{padding:12px 14px;border-bottom:1px solid var(--line);font-weight:800}.col-meta{color:var(--muted);font-size:12px;font-weight:600;margin-top:3px}.deal-card{margin:10px;background:white;border:1px solid var(--line);border-radius:8px;padding:12px}.deal-card .amount{font-weight:800;color:#1f7a52;margin-top:6px}
@media (max-width:850px){.sidebar{position:static;width:100%}.app{display:block}.main{margin-left:0;width:100%}.two-col{grid-template-columns:1fr}.topbar{height:auto;padding:18px}.content{padding:18px}.board{grid-auto-columns:84vw}}
.chat-toggle{position:fixed;bottom:24px;right:24px;width:56px;height:56px;border-radius:50%;background:var(--accent);color:white;border:none;cursor:pointer;font-size:24px;box-shadow:0 4px 12px rgba(0,0,0,.2);z-index:1000;display:flex;align-items:center;justify-content:center;transition:transform .2s}.chat-toggle:hover{transform:scale(1.08)}
.chat-panel{position:fixed;bottom:90px;right:24px;width:380px;max-height:520px;background:white;border:1px solid var(--line);border-radius:12px;box-shadow:0 8px 30px rgba(0,0,0,.15);z-index:1000;display:none;flex-direction:column;overflow:hidden}.chat-panel.open{display:flex}
.chat-header{background:var(--nav);color:white;padding:14px 18px;font-weight:700;display:flex;align-items:center;justify-content:space-between}.chat-close{background:none;border:none;color:white;font-size:18px;cursor:pointer;padding:0 4px}
.chat-messages{flex:1;overflow-y:auto;padding:14px;display:flex;flex-direction:column;gap:10px;min-height:200px;max-height:360px}.chat-msg{max-width:85%;padding:10px 14px;border-radius:12px;font-size:13px;line-height:1.45;word-wrap:break-word;white-space:pre-wrap}.chat-msg.user{align-self:flex-end;background:var(--accent);color:white;border-bottom-right-radius:4px}.chat-msg.assistant{align-self:flex-start;background:var(--bg);color:var(--text);border:1px solid var(--line);border-bottom-left-radius:4px}.chat-msg.typing{color:var(--muted);font-style:italic}
.chat-input-row{display:flex;border-top:1px solid var(--line);padding:10px}.chat-input-row input{flex:1;border:1px solid var(--line);border-radius:6px;padding:10px 12px;font-size:13px;outline:none}.chat-input-row input:focus{border-color:var(--teal)}.chat-send{margin-left:8px;background:var(--accent);color:white;border:none;border-radius:6px;padding:8px 14px;cursor:pointer;font-weight:700;font-size:13px}.chat-send:disabled{opacity:.5;cursor:not-allowed}
.chat-user-row{display:flex;border-bottom:1px solid var(--line);padding:10px;gap:8px;background:#f8fbfd}.chat-user-row label{font-size:12px;color:var(--muted);white-space:nowrap;align-self:center}.chat-user-row select{flex:1;border:1px solid var(--line);border-radius:6px;padding:6px 8px;font-size:13px}
@media(max-width:500px){.chat-panel{width:calc(100vw - 32px);right:16px;bottom:84px}}
:root{--surface:#fff;--surface-soft:#f8fbfd;--ink:#192733;--shadow:0 12px 28px rgba(33,51,67,.08);--shadow-sm:0 2px 10px rgba(33,51,67,.06);--radius:8px}
body{background:linear-gradient(180deg,#f7fafc 0,#eef4f8 100%);color:var(--ink);font-size:14px}.app{align-items:stretch}.sidebar{width:216px;background:linear-gradient(180deg,#253d52 0,#1f3447 100%);padding:24px 16px;border-right:1px solid rgba(255,255,255,.08)}.brand{font-size:16px;letter-spacing:.01em;margin-bottom:4px}.brand:after{content:"Operational CRM";display:block;margin-top:5px;color:#a8c1d4;font-size:11px;font-weight:650;letter-spacing:.08em;text-transform:uppercase}.nav{margin-top:24px}.nav a{position:relative;display:flex;align-items:center;min-height:40px;color:#d7e7f0;border-radius:7px;font-weight:700}.nav a:before{content:"";width:6px;height:6px;margin-right:10px;border-radius:99px;background:#86c7d8;opacity:.7}.nav a.active{background:#fff;color:#20384d;box-shadow:var(--shadow-sm)}.nav a.active:before{background:var(--accent);opacity:1}.nav a:hover{text-decoration:none;background:rgba(255,255,255,.12)}
.main{margin-left:216px;width:calc(100% - 216px)}.topbar{position:sticky;top:0;z-index:20;min-height:76px;height:auto;background:rgba(255,255,255,.92);backdrop-filter:blur(14px);border-bottom:1px solid rgba(219,228,234,.9);padding:16px 30px}.topbar h1{font-size:24px;line-height:1.15;color:#192f41}.topbar .muted{margin-top:4px}.topbar-meta{margin-left:auto;display:flex;align-items:center;gap:10px}.topbar-pill{display:inline-flex;align-items:center;min-height:30px;padding:6px 10px;border:1px solid var(--line);border-radius:999px;background:var(--surface-soft);color:#425b76;font-size:12px;font-weight:750;white-space:nowrap}.topbar-pill.live:before{content:"";width:7px;height:7px;margin-right:7px;border-radius:99px;background:var(--good);box-shadow:0 0 0 3px rgba(0,168,107,.12)}
.content{padding:30px;max-width:1500px}.card,.stat,.table-wrap{border-color:#d7e3eb;box-shadow:var(--shadow-sm)}.card{border-radius:var(--radius);overflow:hidden}.card-h{background:linear-gradient(180deg,#fff,#f9fcfe);font-size:14px;color:#263d50}.card-b{padding:18px}.stat{position:relative;overflow:hidden;background:var(--surface);border-radius:var(--radius)}.stat:before{content:"";position:absolute;inset:0 auto 0 0;width:4px;background:var(--teal)}.stat .k{font-size:11px}.stat .v{font-size:25px;color:#172b3a;line-height:1.1}
.table-wrap{border-radius:var(--radius);box-shadow:var(--shadow);background:var(--surface)}table{min-width:820px}th{position:sticky;top:0;z-index:1;background:#f3f8fb;color:#51677a;font-size:11px}td{color:#223847}tbody tr{transition:background .15s ease}tbody tr:hover{background:#f7fbfd}td a{font-weight:750}.badge{min-height:24px;border-radius:999px;padding:4px 10px;letter-spacing:0}.toolbar{align-items:flex-start}.tabs{gap:6px}.tab{border-radius:999px;font-weight:750}.tab:hover{text-decoration:none;border-color:#b8c9d6}.tab.active{background:#fff4ef}.btn{font-weight:750;box-shadow:var(--shadow-sm)}.btn:hover{text-decoration:none;border-color:#b8c9d6}
.kv{grid-template-columns:minmax(110px,150px) minmax(0,1fr)}.kv dd{word-break:break-word}.list-item{border-color:#dbe7ee;background:#fff;transition:border-color .15s ease,box-shadow .15s ease}.list-item:hover{border-color:#b8c9d6;box-shadow:var(--shadow-sm)}.timeline{position:relative}.event{background:#fff;border:1px solid #dbe7ee;border-left:3px solid var(--teal);border-radius:7px;padding:10px 12px}.event strong{color:#1c394c}.event time{font-weight:700}
.board{grid-auto-columns:minmax(280px,1fr);gap:16px}.column{background:#eef5f8;border-color:#d3e1ea;border-radius:var(--radius);box-shadow:var(--shadow-sm)}.column.closed-won{background:#eaf8f0;border-color:#c9ecd9}.column.closed-lost{background:#fff0ee;border-color:#f2c7c1}.col-h{background:rgba(255,255,255,.6);border-radius:var(--radius) var(--radius) 0 0;color:#263d50}.deal-card{border-radius:7px;border-color:#dbe7ee;box-shadow:0 1px 3px rgba(33,51,67,.05);transition:transform .15s ease,box-shadow .15s ease}.deal-card:hover{transform:translateY(-1px);box-shadow:var(--shadow-sm)}.deal-card .amount{font-size:15px}
.chat-toggle{box-shadow:0 12px 26px rgba(255,122,89,.32);font-size:22px}.chat-panel{border-radius:10px;box-shadow:0 20px 46px rgba(25,39,51,.22)}.chat-header{background:linear-gradient(135deg,#253d52,#1c3347)}.chat-user-row select,.chat-input-row input{background:#fff}.chat-input-row input:disabled,.chat-send:disabled{background:#eef3f6}.chat-send{min-width:66px}.chat-msg{box-shadow:0 1px 2px rgba(33,51,67,.05)}.deal-meta{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}.deal-reason{display:block;margin-top:6px;color:var(--muted);font-size:12px}.deal-card .deal-meta{margin-top:8px}
@media (max-width:850px){.sidebar{width:100%;border-right:0}.brand{margin-bottom:10px}.nav{grid-template-columns:repeat(2,minmax(0,1fr));margin-top:14px}.main{margin-left:0;width:100%}.topbar{position:static;align-items:flex-start;gap:12px}.topbar-meta{margin-left:0}.content{padding:18px}.stats{grid-template-columns:repeat(2,minmax(0,1fr))}.table-wrap{border-radius:7px}.chat-toggle{right:18px;bottom:18px}}
@media (max-width:560px){.nav{grid-template-columns:1fr}.stats{grid-template-columns:1fr}.topbar{display:block}.topbar-meta{margin-top:12px;flex-wrap:wrap}.content{padding:14px}.kv{grid-template-columns:1fr}.board{grid-auto-columns:88vw}.chat-toggle{width:52px;height:52px}.chat-panel{max-height:calc(100vh - 120px)}}
"""

NAV_LABELS = {
    "contacts": "Contatti",
    "companies": "Aziende",
    "deals": "Trattative",
    "tickets": "Ticket",
    "dormant": "Clienti dormienti",
}


def _h(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def _props(record: dict[str, Any] | None) -> dict[str, Any]:
    return dict((record or {}).get("properties", {}))


def _prop(record: dict[str, Any] | None, name: str, default: str = "") -> str:
    value = _props(record).get(name, default)
    return "" if value is None else str(value)


def _first_prop(record: dict[str, Any] | None, names: tuple[str, ...], default: str = "") -> str:
    props = _props(record)
    for name in names:
        value = props.get(name)
        if value not in (None, ""):
            return str(value)
    return default


def _money(value: Any, currency: str = "EUR") -> str:
    number = _number(value)
    if number is None:
        return "-"
    code = (currency or "EUR").upper()
    return f"{code} {number:,.0f}".replace(",", ".")


def _number(value: Any) -> float | None:
    try:
        return float(str(value or "0").replace(",", "."))
    except ValueError:
        return None


def _date_short(value: Any) -> str:
    text = str(value or "")
    return text[:10] if text else "-"


def _badge(value: Any, color: str = "gray", label: str | None = None) -> str:
    text = label if label is not None else (str(value) if value not in (None, "") else "-")
    return f"<span class='badge {color}'>{_h(text)}</span>"


def _class_badge(value: Any) -> str:
    customer_class = str(value or "").upper()
    return _badge(customer_class or "-", {"A": "green", "B": "yellow", "C": "gray"}.get(customer_class, "gray"))


def _priority_badge(value: Any) -> str:
    priority = str(value or "").upper()
    colors = {"LOW": "gray", "MEDIUM": "blue", "HIGH": "orange", "URGENT": "red"}
    return _badge(priority or "-", colors.get(priority, "gray"))


def _stage_badge(stage: str, label: str | None = None) -> str:
    lower = stage.lower()
    color = "green" if lower in {"closedwon", "rinnovato", "chiuso", "4"} else "red" if lower in {"closedlost", "non_rinnovato"} else "blue"
    return _badge(stage, color, label)


def _deal_currency(deal: dict[str, Any]) -> str:
    return (_prop(deal, "deal_currency_code", "EUR") or "EUR").upper()


def _deal_amount(deal: dict[str, Any]) -> str:
    return _money(_prop(deal, "amount"), _deal_currency(deal))


def _deal_counts_2025_revenue(deal: dict[str, Any]) -> tuple[bool, str]:
    pipeline = _prop(deal, "pipeline")
    stage = _prop(deal, "dealstage")
    closedate = _prop(deal, "closedate")
    is_won = (pipeline == "default" and stage == "closedwon") or (pipeline == "rinnovi" and stage == "rinnovato")
    if not is_won:
        return False, "Esclusa: fase non chiusa/vinta"
    if not closedate:
        return False, "Esclusa: data chiusura mancante"
    if not closedate.startswith("2025"):
        return False, f"Esclusa: chiusa il {_date_short(closedate)}"
    return True, "Inclusa nel fatturato 2025"


def _deal_revenue_badge(deal: dict[str, Any]) -> str:
    included, reason = _deal_counts_2025_revenue(deal)
    color = "green" if included else "gray"
    label = "Conta nel 2025" if included else "Non conta nel 2025"
    return f"{_badge(label, color)}<span class='deal-reason'>{_h(reason)}</span>"


def _stage_total_label(deals: list[dict[str, Any]]) -> str:
    currencies = {_deal_currency(deal) for deal in deals if _number(_prop(deal, "amount")) is not None}
    if not deals:
        return "-"
    if len(currencies) == 1:
        currency = next(iter(currencies), "EUR")
        total = sum(_number(_prop(deal, "amount")) or 0 for deal in deals)
        return _money(total, currency)
    return "multi-valuta"


def _layout(title: str, active: str, body: str, subtitle: str = "") -> str:
    navigation = "".join(
        f"<a class='{'active' if key == active else ''}' href='/{key}'>{_h(label)}</a>"
        for key, label in NAV_LABELS.items()
    )
    sub = f"<div class='muted'>{_h(subtitle)}</div>" if subtitle else ""
    topbar_meta = "<div class='topbar-meta'><span class='topbar-pill live'>Live CRM</span><span class='topbar-pill'>AI ready</span></div>"
    token = os.environ.get("CRM_TOKEN", "")
    chat_html = f"""
<button class="chat-toggle" onclick="toggleChat()" title="Assistente AI" aria-label="Apri assistente AI" aria-controls="chatPanel" aria-expanded="false">&#128172;</button>
<div class="chat-panel" id="chatPanel">
  <div class="chat-header">
    <span>&#129302; Assistente AI</span>
    <button class="chat-close" onclick="toggleChat()" aria-label="Chiudi assistente AI">&#10005;</button>
  </div>
  <div class="chat-user-row">
    <label for="chatUser">Utente:</label>
    <select id="chatUser" onchange="switchUser()">
      <option value="">Seleziona utente...</option>
    </select>
  </div>
  <div class="chat-messages" id="chatMessages">
    <div class="chat-msg assistant">Ciao! Sono l'assistente AI di Brambilla CRM. Seleziona il tuo utente e chiedimi quello che vuoi.</div>
  </div>
  <div class="chat-input-row">
    <input type="text" id="chatInput" placeholder="Scrivi un messaggio..." onkeydown="if(event.key==='Enter')sendChat()" disabled>
    <button class="chat-send" id="chatSend" onclick="sendChat()" disabled>Invia</button>
  </div>
</div>
<script>
const CRM_TOKEN="{_h(token)}";
const chatHistories={{}};
let currentUser="";
function toggleChat(){{
  const toggle=document.querySelector('.chat-toggle');
  const p=document.getElementById('chatPanel');
  p.classList.toggle('open');
  toggle.setAttribute('aria-expanded',p.classList.contains('open')?'true':'false');
  if(p.classList.contains('open')&&document.getElementById('chatUser').options.length<=1)loadUsers();
}}
async function loadUsers(){{
  try{{
    const r=await fetch('/crm/v3/objects/users/search',{{
      method:'POST',
      headers:{{'Content-Type':'application/json','Authorization':'Bearer '+CRM_TOKEN}},
      body:JSON.stringify({{
        filterGroups:[{{filters:[{{propertyName:"active",operator:"EQ",value:"true"}}]}}],
        properties:["firstname","lastname","email"],
        limit:100
      }})
    }});
    const d=await r.json();
    const sel=document.getElementById('chatUser');
    (d.results||[]).forEach(u=>{{
      const p=u.properties||{{}};
      const email=p.email||'';
      const name=((p.firstname||'')+' '+(p.lastname||'')).trim()||email;
      if(email){{
        const o=document.createElement('option');
        o.value=email;
        o.textContent=name+' ('+email+')';
        sel.appendChild(o);
      }}
    }});
  }}catch(e){{}}
}}
function switchUser(){{
  currentUser=document.getElementById('chatUser').value;
  const inp=document.getElementById('chatInput');
  const btn=document.getElementById('chatSend');
  inp.disabled=!currentUser;
  btn.disabled=!currentUser;
  const msgs=document.getElementById('chatMessages');
  msgs.innerHTML='';
  if(!currentUser){{
    appendMsg('assistant','Seleziona un utente per iniziare.');
    return;
  }}
  if(!chatHistories[currentUser])chatHistories[currentUser]=[];
  appendMsg('assistant','Ciao! Come posso aiutarti?');
  chatHistories[currentUser].forEach(m=>appendMsg(m.role,m.content));
}}
function appendMsg(role,text){{
  const d=document.getElementById('chatMessages');
  const m=document.createElement('div');
  m.className='chat-msg '+role;
  m.textContent=text;
  d.appendChild(m);
  d.scrollTop=d.scrollHeight;
  return m;
}}
async function sendChat(){{
  if(!currentUser)return;
  const inp=document.getElementById('chatInput');
  const btn=document.getElementById('chatSend');
  const text=inp.value.trim();
  if(!text)return;
  inp.value='';
  btn.disabled=true;
  inp.disabled=true;
  appendMsg('user',text);
  if(!chatHistories[currentUser])chatHistories[currentUser]=[];
  chatHistories[currentUser].push({{role:'user',content:text}});
  const typing=appendMsg('assistant typing','Sto pensando...');
  try{{
    const res=await fetch('/__agente',{{
      method:'POST',
      headers:{{'Content-Type':'application/json','Authorization':'Bearer '+CRM_TOKEN}},
      body:JSON.stringify({{
        context:{{now:new Date().toISOString(),user:currentUser}},
        messages:chatHistories[currentUser]
      }})
    }});
    const data=await res.json();
    const reply=data.reply||'Errore nella risposta.';
    typing.remove();
    appendMsg('assistant',reply);
    chatHistories[currentUser].push({{role:'assistant',content:reply}});
  }}catch(e){{
    typing.remove();
    appendMsg('assistant','Errore di connessione. Riprova.');
  }}
  btn.disabled=false;
  inp.disabled=false;
  inp.focus();
}}
</script>
"""
    return (
        "<!doctype html><html lang='it'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{_h(title)} - Brambilla CRM</title><style>{CRM_CSS}</style></head>"
        f"<body><div class='app'><aside class='sidebar'><div class='brand'>Brambilla CRM</div>"
        f"<nav class='nav'>{navigation}</nav></aside><main class='main'>"
        f"<header class='topbar'><div><h1>{_h(title)}</h1>{sub}</div>{topbar_meta}</header>"
        f"<section class='content'>{body}</section></main></div>{chat_html}</body></html>"
    )


def _table(headers: tuple[str, ...], rows: list[tuple[str, ...]]) -> str:
    if not rows:
        return "<div class='table-wrap'><div class='empty'>Nessun record da mostrare.</div></div>"
    head = "".join(f"<th>{_h(header)}</th>" for header in headers)
    body = "".join("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows)
    return f"<div class='table-wrap'><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>"


def _pagination(path: str, result: dict[str, Any], after: int | None, limit: int) -> str:
    prev_after = max(0, int(after or 0) - limit)
    prev_cls = "" if after else " disabled"
    next_after = result.get("paging", {}).get("next", {}).get("after")
    next_cls = "" if next_after else " disabled"
    return (
        "<div class='pagination'>"
        f"<a class='btn{prev_cls}' href='{path}?after={prev_after}'>Precedente</a>"
        f"<a class='btn{next_cls}' href='{path}?after={_h(next_after)}'>Successiva</a>"
        "</div>"
    )


async def _all_search_results(object_type: str, filter_groups: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    after = 0
    while True:
        page = await _call_db("search_objects", object_type, filter_groups or [], limit=200, after=after)
        results.extend(page.get("results", []))
        next_after = page.get("paging", {}).get("next", {}).get("after")
        if not next_after:
            return results
        after = int(next_after)


async def _get_many(object_type: str, ids: list[int]) -> list[dict[str, Any]]:
    records = []
    for object_id in ids:
        record = await _call_db("get_object", object_type, int(object_id))
        if record:
            records.append(record)
    return records


async def _associated_records(object_type: str, object_id: int, target_type: str) -> list[dict[str, Any]]:
    associations = await read_associations(object_type, object_id, target_type)
    return await _get_many(target_type, [int(item["id"]) for item in associations])


async def _company_name_for(object_type: str, object_id: int) -> str:
    companies = await _associated_records(object_type, object_id, "companies")
    return _prop(companies[0], "name", "-") if companies else "-"


def _stage_maps(pipeline: dict[str, Any] | None) -> tuple[dict[str, str], list[str]]:
    stages = (pipeline or {}).get("stages", [])
    labels = {str(stage.get("id")): str(stage.get("label", stage.get("id", ""))) for stage in stages}
    order = [str(stage.get("id")) for stage in stages]
    return labels, order


async def _render_companies_page(after: int | None = None) -> str:
    limit = 50
    result = await _call_db("list_objects", "companies", limit=limit, after=after)
    rows = []
    for record in result.get("results", []):
        props = _props(record)
        company_id = _h(record.get("id", ""))
        name = props.get("name") or f"Azienda {company_id}"
        rows.append((
            f"<a href='/companies/{company_id}'>{_h(name)}</a>",
            _h(props.get("domain", "")),
            _h(props.get("city", "")),
            _h(props.get("state", "")),
            _class_badge(props.get("classe_cliente")),
            _money(props.get("fatturato_2025")),
        ))
    body = _table(("Nome", "Dominio", "Citta", "Provincia", "Classe Cliente", "Fatturato 2025"), rows)
    body += _pagination("/companies", result, after, limit)
    return _layout("Aziende", "companies", body, "Schede azienda e riepilogo clienti")


async def _render_contacts_page(after: int | None = None) -> str:
    limit = 50
    result = await _call_db("list_objects", "contacts", limit=limit, after=after)
    rows = []
    for record in result.get("results", []):
        contact_id = int(record.get("id", 0))
        props = _props(record)
        name = " ".join(part for part in (props.get("firstname"), props.get("lastname")) if part) or props.get("email") or f"Contatto {contact_id}"
        companies = await _associated_records("contacts", contact_id, "companies")
        company_cell = "-"
        if companies:
            company = companies[0]
            company_cell = f"<a href='/companies/{_h(company.get('id'))}'>{_h(_prop(company, 'name', 'Azienda'))}</a>"
        rows.append((
            _h(name),
            _h(props.get("email", "")),
            _h(props.get("phone", "")),
            _badge(props.get("lifecyclestage", "-"), "blue"),
            company_cell,
        ))
    body = _table(("Nome", "Email", "Telefono", "Lifecycle Stage", "Azienda"), rows)
    body += _pagination("/contacts", result, after, limit)
    return _layout("Contatti", "contacts", body, "Rubrica commerciale collegata alle aziende")


async def _render_company_detail(company_id: int) -> str:
    company = await _call_db("get_object", "companies", company_id)
    if company is None:
        raise api_error(404, "Company not found", "OBJECT_NOT_FOUND")
    props = _props(company)
    contacts = await _associated_records("companies", company_id, "contacts")
    deals = await _associated_records("companies", company_id, "deals")
    tickets = await _associated_records("companies", company_id, "tickets")

    activities: list[dict[str, Any]] = []
    for contact in contacts:
        contact_id = int(contact.get("id", 0))
        for activity_type in ("notes", "calls", "emails", "meetings"):
            for activity in await _associated_records("contacts", contact_id, activity_type):
                activity["_activity_type"] = activity_type
                activities.append(activity)
    activities.sort(key=lambda item: _prop(item, "hs_timestamp"), reverse=True)

    properties = (
        "<div class='card'><div class='card-h'>Proprieta azienda</div><div class='card-b'><dl class='kv'>"
        f"<dt>Nome</dt><dd>{_h(props.get('name', ''))}</dd>"
        f"<dt>Dominio</dt><dd>{_h(props.get('domain', ''))}</dd>"
        f"<dt>Partita IVA</dt><dd>{_h(props.get('partita_iva', '-'))}</dd>"
        f"<dt>Citta</dt><dd>{_h(props.get('city', '-'))}</dd>"
        f"<dt>Provincia</dt><dd>{_h(props.get('state', '-'))}</dd>"
        f"<dt>Fatturato 2025 (EUR)</dt><dd>{_money(props.get('fatturato_2025'))}</dd>"
        f"<dt>Classe</dt><dd>{_class_badge(props.get('classe_cliente'))}</dd>"
        "</dl></div></div>"
    )

    event_rows = []
    activity_labels = {"notes": "Nota", "calls": "Chiamata", "emails": "Email", "meetings": "Meeting"}
    body_fields = ("hs_note_body", "hs_call_body", "hs_email_text", "hs_meeting_body", "hs_task_subject")
    for activity in activities[:30]:
        label = activity_labels.get(activity.get("_activity_type"), "Attivita")
        content = _first_prop(activity, body_fields, "Attivita registrata")
        timestamp = _prop(activity, "hs_timestamp", _prop(activity, "createdate", ""))
        event_rows.append(f"<div class='event'><strong>{_h(label)}</strong><div>{_h(content)}</div><time>{_h(_date_short(timestamp))}</time></div>")
    timeline = (
        "<div class='card'><div class='card-h'>Timeline attivita</div><div class='card-b'>"
        f"<div class='timeline'>{''.join(event_rows) if event_rows else '<div class=\"empty\">Nessuna attivita collegata.</div>'}</div>"
        "</div></div>"
    )

    contact_items = "".join(
        f"<div class='list-item'><div class='item-title'>{_h(' '.join(p for p in (_prop(c,'firstname'), _prop(c,'lastname')) if p) or _prop(c,'email','Contatto'))}</div>"
        f"<div class='muted'>{_h(_prop(c,'email',''))} &middot; {_h(_prop(c,'phone',''))}</div></div>"
        for c in contacts
    ) or "<div class='empty'>Nessun contatto associato.</div>"
    deal_items = "".join(
        f"<div class='list-item'><div class='item-title'>{_h(_prop(d,'dealname','Trattativa'))}</div>"
        f"<div>{_deal_amount(d)}</div>"
        "<div class='deal-meta'>"
        f"{_stage_badge(_prop(d,'dealstage'))}"
        f"{_badge(_prop(d,'pipeline','-'), 'blue')}"
        f"{_badge(_date_short(_prop(d,'closedate')), 'gray', 'Chiusura: ' + _date_short(_prop(d,'closedate')))}"
        "</div>"
        f"<div class='deal-meta'>{_deal_revenue_badge(d)}</div></div>"
        for d in deals
    ) or "<div class='empty'>Nessuna trattativa associata.</div>"
    ticket_items = "".join(
        f"<div class='list-item'><div class='item-title'>{_h(_prop(t,'subject','Ticket'))}</div>"
        f"{_priority_badge(_prop(t,'hs_ticket_priority'))} {_stage_badge(_prop(t,'hs_pipeline_stage'))}</div>"
        for t in tickets
    ) or "<div class='empty'>Nessun ticket associato.</div>"
    sidebar = (
        f"<div class='card'><div class='card-h'>Contatti associati <span class='badge gray'>{len(contacts)}</span></div><div class='card-b'><div class='list'>{contact_items}</div></div></div>"
        f"<div class='card'><div class='card-h'>Trattative associate <span class='badge gray'>{len(deals)}</span></div><div class='card-b'><div class='list'>{deal_items}</div></div></div>"
        f"<div class='card'><div class='card-h'>Ticket associati <span class='badge gray'>{len(tickets)}</span></div><div class='card-b'><div class='list'>{ticket_items}</div></div></div>"
    )
    stats = (
        "<div class='stats'>"
        f"<div class='stat'><div class='k'>Fatturato 2025 (EUR)</div><div class='v'>{_money(props.get('fatturato_2025'))}</div></div>"
        f"<div class='stat'><div class='k'>Contatti</div><div class='v'>{len(contacts)}</div></div>"
        f"<div class='stat'><div class='k'>Trattative</div><div class='v'>{len(deals)}</div></div>"
        "</div>"
    )
    body = f"{stats}<div class='grid two-col'><div class='grid'>{properties}{timeline}</div><aside class='grid'>{sidebar}</aside></div>"
    return _layout(str(props.get("name") or f"Azienda {company_id}"), "companies", body, "Pagina azienda")


@app.get("/companies/{company_id}", response_class=HTMLResponse, include_in_schema=False)
async def ui_company_detail(company_id: int):
    return HTMLResponse(await _render_company_detail(company_id))


async def _render_deals_page(pipeline: str | None = None) -> str:
    pipelines = await _call_db("list_pipelines", "deals")
    selected = pipeline or "default"
    if pipelines and selected not in {str(item.get("id")) for item in pipelines}:
        selected = str(pipelines[0].get("id"))
    pipeline_obj = next((item for item in pipelines if str(item.get("id")) == selected), None)
    labels, order = _stage_maps(pipeline_obj)
    deals = await _all_search_results("deals", [{"filters": [{"propertyName": "pipeline", "operator": "EQ", "value": selected}]}])
    if not order:
        order = list(dict.fromkeys(_prop(deal, "dealstage", "senza_fase") for deal in deals))
        labels = {stage: stage for stage in order}
    grouped = {stage: [] for stage in order}
    for deal in deals:
        stage = _prop(deal, "dealstage", "senza_fase")
        grouped.setdefault(stage, []).append(deal)
        if stage not in order:
            order.append(stage)
            labels[stage] = stage
    tabs = "".join(
        f"<a class='tab {'active' if str(item.get('id')) == selected else ''}' href='/deals?pipeline={_h(item.get('id'))}'>{_h(item.get('label', item.get('id')))}</a>"
        for item in pipelines
    )
    if not tabs:
        tabs = "<span class='muted'>Nessuna pipeline configurata</span>"
    columns = []
    for stage in order:
        stage_deals = grouped.get(stage, [])
        total_label = _stage_total_label(stage_deals)
        cards = []
        for deal in stage_deals:
            deal_id = int(deal.get("id", 0))
            company_name = await _company_name_for("deals", deal_id)
            cards.append(
                "<div class='deal-card'>"
                f"<div class='item-title'>{_h(_prop(deal,'dealname','Trattativa'))}</div>"
                f"<div class='amount'>{_deal_amount(deal)}</div>"
                f"<div class='deal-meta'>{_badge(_date_short(_prop(deal,'closedate')), 'gray', 'Chiusura: ' + _date_short(_prop(deal,'closedate')))}</div>"
                f"<div class='muted'>{_h(company_name)}</div>"
                f"<div class='muted'>{_h(_prop(deal,'commerciale',''))}</div>"
                "</div>"
            )
        closed_class = " closed-won" if stage in {"closedwon", "rinnovato"} else " closed-lost" if stage in {"closedlost", "non_rinnovato"} else ""
        columns.append(
            f"<section class='column{closed_class}'><div class='col-h'>{_h(labels.get(stage, stage))}"
            f"<div class='col-meta'>{len(stage_deals)} trattative &middot; {total_label}</div></div>"
            f"{''.join(cards) if cards else '<div class=\"empty\">Vuoto</div>'}</section>"
        )
    board = "".join(columns) if columns else "<div class='table-wrap'><div class='empty'>Nessuna fase disponibile.</div></div>"
    body = f"<div class='toolbar'><div class='tabs'>{tabs}</div></div><div class='board'>{board}</div>"
    return _layout("Trattative", "deals", body, "Board per pipeline e fase")


@app.get("/deals", response_class=HTMLResponse, include_in_schema=False)
async def ui_deals(pipeline: str | None = None):
    return HTMLResponse(await _render_deals_page(pipeline))


async def _render_tickets_page(stage: str = "all", after: int | None = None) -> str:
    limit = 50
    pipelines = await _call_db("list_pipelines", "tickets")
    pipeline_obj = next((item for item in pipelines if str(item.get("id")) == "assistenza"), pipelines[0] if pipelines else None)
    labels, order = _stage_maps(pipeline_obj)
    if not order:
        order = ["aperto", "in_lavorazione", "in_attesa_del_cliente", "chiuso"]
        labels = {"aperto": "Aperto", "in_lavorazione": "In lavorazione", "in_attesa_del_cliente": "In attesa", "chiuso": "Chiuso"}
    filters = []
    if stage != "all":
        filters.append({"propertyName": "hs_pipeline_stage", "operator": "EQ", "value": stage})
    result = await _call_db("search_objects", "tickets", [{"filters": filters}] if filters else [], limit=limit, after=after or 0)
    rows = []
    for ticket in result.get("results", []):
        ticket_id = int(ticket.get("id", 0))
        company = await _company_name_for("tickets", ticket_id)
        ticket_stage = _prop(ticket, "hs_pipeline_stage")
        rows.append((
            _h(ticket.get("id", "")),
            _h(_prop(ticket, "subject", "")),
            _priority_badge(_prop(ticket, "hs_ticket_priority")),
            _stage_badge(ticket_stage, labels.get(ticket_stage, ticket_stage)),
            _h(company),
            _h(_prop(ticket, "assegnatario", "")),
            _h(_date_short(_prop(ticket, "createdate"))),
            _h(_date_short(_prop(ticket, "closed_date"))),
        ))
    tabs = f"<a class='tab {'active' if stage == 'all' else ''}' href='/tickets'>Tutti</a>" + "".join(
        f"<a class='tab {'active' if stage == stage_id else ''}' href='/tickets?stage={_h(stage_id)}'>{_h(labels.get(stage_id, stage_id))}</a>"
        for stage_id in order
    )
    body = f"<div class='toolbar'><div class='tabs'>{tabs}</div></div>"
    body += _table(("ID", "Oggetto", "Priorita", "Stage", "Azienda", "Assegnatario", "Creato", "Chiuso"), rows)
    path = "/tickets" if stage == "all" else f"/tickets?stage={_h(stage)}"
    body += _pagination(path, result, after, limit) if stage == "all" else (
        "<div class='pagination'>"
        f"<a class='btn{' disabled' if not after else ''}' href='/tickets?stage={_h(stage)}&after={max(0, int(after or 0) - limit)}'>Precedente</a>"
        f"<a class='btn{' disabled' if not result.get('paging', {}).get('next', {}).get('after') else ''}' href='/tickets?stage={_h(stage)}&after={_h(result.get('paging', {}).get('next', {}).get('after'))}'>Successiva</a>"
        "</div>"
    )
    return _layout("Ticket", "tickets", body, "Assistenza clienti per priorita e stato")


@app.get("/tickets", response_class=HTMLResponse, include_in_schema=False)
async def ui_tickets(stage: str = "all", after: int | None = None):
    return HTMLResponse(await _render_tickets_page(stage, after))


async def _last_won_deal_date(company_id: int) -> str:
    deals = await _associated_records("companies", company_id, "deals")
    won_dates = [
        _prop(deal, "closedate")
        for deal in deals
        if (_prop(deal, "pipeline") == "default" and _prop(deal, "dealstage") == "closedwon")
        or (_prop(deal, "pipeline") == "rinnovi" and _prop(deal, "dealstage") == "rinnovato")
    ]
    return _date_short(max(won_dates)) if won_dates else "-"


async def _render_dormant_page() -> str:
    definition = await _call_db("get_list_by_name", "Clienti dormienti", "companies")
    companies: list[dict[str, Any]] = []
    if definition:
        after = None
        while True:
            page = await _call_db("get_list_members", int(definition["id"]), limit=200, after=after)
            ids = [int(item.get("recordId") or item.get("id")) for item in page.get("results", [])]
            companies.extend(await _get_many("companies", ids))
            after = page.get("paging", {}).get("next", {}).get("after")
            if not after:
                break
    rows = []
    for company in companies:
        company_id = int(company.get("id", 0))
        props = _props(company)
        rows.append((
            f"<a href='/companies/{company_id}'>{_h(props.get('name', f'Azienda {company_id}'))}</a>",
            _h(props.get("domain", "")),
            _h(props.get("city", "")),
            _money(props.get("fatturato_2025")),
            _class_badge(props.get("classe_cliente")),
            _h(await _last_won_deal_date(company_id)),
        ))
    header = (
        "<div class='toolbar'><div>"
        "<div class='muted'>Aziende con almeno una trattativa vinta ma nessuna attivita nel 2025</div>"
        f"</div><span class='badge orange'>{len(companies)} aziende</span></div>"
    )
    body = header + _table(("Azienda", "Dominio", "Citta", "Fatturato 2025", "Classe", "Ultima vittoria"), rows)
    return _layout("Clienti dormienti", "dormant", body, "Lista commerciale prioritaria")


@app.get("/dormant", response_class=HTMLResponse, include_in_schema=False)
async def ui_dormant():
    return HTMLResponse(await _render_dormant_page())


@app.get("/{object_type}", response_class=HTMLResponse, include_in_schema=False)
async def ui_records(object_type: str, after: int | None = None):
    object_type = normalize_object_type(object_type)
    if object_type == "companies":
        return HTMLResponse(await _render_companies_page(after))
    if object_type == "contacts":
        return HTMLResponse(await _render_contacts_page(after))
    if object_type not in UI_TYPES:
        raise api_error(404, "Page not found", "OBJECT_NOT_FOUND")
    raise api_error(404, "Page not found", "OBJECT_NOT_FOUND")


@app.post("/__reset", status_code=204)
async def reset_crm() -> Response:
    global _rate_limit_count
    await _call_db("reset_db")
    from automations import reset_automation_state
    await reset_automation_state()
    _rate_limit_count = 0
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


@app.post("/__infer-contact-company-associations")
async def infer_contact_company_associations(body: dict[str, Any] | None = None) -> dict[str, Any]:
    dry_run = bool((body or {}).get("dry_run", True))
    return await _call_db("infer_contact_company_associations", dry_run=dry_run)


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
        record = await _call_db(
            "get_object", object_type, int(item["id"]), body.get("properties"), bool(body.get("archived", False))
        )
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
    archived: bool = False,
):
    object_type = normalize_object_type(object_type)
    property_list = [item for item in (properties or "").split(",") if item] or None
    record = await _call_db("get_object", object_type, object_id, property_list, archived)
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


@app.get("/crm/v3/associations/{from_type}/{to_type}/labels")
async def association_labels(from_type: str, to_type: str):
    from_type = normalize_object_type(from_type)
    to_type = normalize_object_type(to_type)
    type_id = ASSOCIATION_LABEL_TYPES.get((from_type, to_type), 0)
    return {
        "results": [{
            "category": "HUBSPOT_DEFINED",
            "typeId": type_id,
            "label": None,
        }]
    }


@app.post("/crm/v3/imports", status_code=201)
async def create_import(body: dict[str, Any]):
    import_id = str(uuid.uuid4())
    now = _now_iso()
    return {
        "id": import_id,
        "state": "DONE",
        "createdAt": now,
        "updatedAt": now,
        "metadata": {
            "counters": {
                "rowsProcessed": len(body.get("files", [])) if isinstance(body.get("files"), list) else 0,
                "success": 0,
                "errors": 0,
            }
        },
    }


@app.get("/crm/v3/imports/{import_id}")
async def get_import(import_id: str):
    now = _now_iso()
    return {
        "id": str(import_id),
        "state": "DONE",
        "createdAt": now,
        "updatedAt": now,
        "metadata": {"counters": {"rowsProcessed": 0, "success": 0, "errors": 0}},
    }


@app.post("/crm/v3/exports/export/async", status_code=202)
@app.post("/crm/v3/exports", status_code=202)
async def create_export(body: dict[str, Any]):
    export_id = str(uuid.uuid4())
    now = _now_iso()
    return {
        "id": export_id,
        "state": "COMPLETE",
        "createdAt": now,
        "updatedAt": now,
        "result": {
            "expiresAt": now,
            "url": f"https://example.invalid/crm/v3/exports/{export_id}/download",
        },
    }


@app.get("/crm/v3/exports/{export_id}/status")
@app.get("/crm/v3/exports/{export_id}")
async def get_export(export_id: str):
    now = _now_iso()
    return {
        "id": str(export_id),
        "state": "COMPLETE",
        "createdAt": now,
        "updatedAt": now,
        "result": {
            "expiresAt": now,
            "url": f"https://example.invalid/crm/v3/exports/{export_id}/download",
        },
    }


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
