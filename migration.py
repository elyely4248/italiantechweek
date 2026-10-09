from __future__ import annotations

import asyncio
import tempfile
import zipfile
from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterable

import httpx

from legacy_csv import iter_legacy_dicts
from normalize_export import (
    active_flag,
    clean_text,
    deleted_flag,
    extract_vat,
    format_decimal,
    normalize_activity_type,
    normalize_amount,
    normalize_currency,
    normalize_discount,
    normalize_domain,
    normalize_email,
    normalize_lifecycle,
    normalize_pipeline,
    normalize_quantity,
    normalize_sku,
    normalize_stage,
    normalize_ticket_priority,
    normalize_ticket_stage,
    parse_date,
    split_contact_ids,
)


BATCH_SIZE = 3000
CSV_NAMES = {
    "aziende.csv",
    "contatti.csv",
    "opportunita.csv",
    "righe_offerta.csv",
    "listino.csv",
    "ticket.csv",
    "attivita.csv",
    "utenti.csv",
    "storico_fasi.csv",
}
CENT = Decimal("0.01")


def _db():
    import db

    return db


def _sort_key(row: dict[str, Any]) -> tuple[str, int]:
    return (row.get("_modified", ""), row.get("_order", 0))


def _merge_rows(rows: list[dict[str, Any]], fields: Iterable[str]) -> dict[str, str]:
    """Use the newest non-empty value for every field."""
    ordered = sorted(rows, key=_sort_key, reverse=True)
    result: dict[str, str] = {}
    for field in fields:
        result[field] = next((str(row[field]) for row in ordered if row.get(field) not in (None, "")), "")
    return result


class _DisjointSet:
    def __init__(self, size: int) -> None:
        self.parent = list(range(size))

    def find(self, value: int) -> int:
        while self.parent[value] != value:
            self.parent[value] = self.parent[self.parent[value]]
            value = self.parent[value]
        return value

    def union(self, left: int, right: int) -> None:
        left_root, right_root = self.find(left), self.find(right)
        if left_root != right_root:
            self.parent[right_root] = left_root


def _dedupe_companies(rows: list[dict[str, Any]]) -> list[tuple[dict[str, str], list[dict[str, Any]]]]:
    dsu = _DisjointSet(len(rows))
    seen: dict[tuple[str, str], int] = {}
    for index, row in enumerate(rows):
        for kind in ("domain", "partita_iva"):
            value = row.get(kind, "")
            if not value:
                continue
            key = (kind, value.lower())
            if key in seen:
                dsu.union(index, seen[key])
            else:
                seen[key] = index

    groups: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for index, row in enumerate(rows):
        groups[dsu.find(index)].append(row)

    output = []
    for members in groups.values():
        merged = _merge_rows(members, ("id_legacy", "name", "domain", "city", "state", "partita_iva"))
        domains = []
        for member in sorted(members, key=_sort_key, reverse=True):
            domain = member.get("domain", "")
            if domain and domain.lower() != merged["domain"].lower() and domain.lower() not in {d.lower() for d in domains}:
                domains.append(domain)
        merged["hs_additional_domains"] = ";".join(domains)
        output.append((merged, members))
    return output


def _dedupe_by(rows: list[dict[str, Any]], key: str, fields: Iterable[str]) -> list[tuple[dict[str, str], list[dict[str, Any]]]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for index, row in enumerate(rows):
        value = str(row.get(key, "")).lower()
        groups[value if value else f"__row_{index}"].append(row)
    return [(_merge_rows(members, fields), members) for members in groups.values()]


def _line_total(quantity: str, price: str, discount: str) -> Decimal:
    quantity_value = Decimal(quantity) if quantity else Decimal("0")
    price_value = Decimal(price) if price else Decimal("0")
    discount_value = Decimal(discount) if discount else Decimal("0")
    return (quantity_value * price_value * (Decimal("1") - discount_value / Decimal("100"))).quantize(
        CENT, rounding=ROUND_HALF_UP
    )


async def _insert_batches(object_type: str, rows: list[dict[str, str]], batch_size: int = BATCH_SIZE) -> list[int]:
    result: list[int] = []
    for start in range(0, len(rows), batch_size):
        result.extend(await _db().bulk_insert_objects(object_type, rows[start : start + batch_size]))
    return result


async def _insert_associations(rows: list[tuple[str, int, str, int, int]]) -> None:
    if not rows:
        return
    for start in range(0, len(rows), BATCH_SIZE):
        await _db().bulk_insert_associations(rows[start : start + BATCH_SIZE])


async def _preserve_createdates(values: list[tuple[int, str]]) -> None:
    values = [(object_id, value) for object_id, value in values if value]
    if not values:
        return
    pool = await _db().get_pool()
    async with pool.acquire() as conn:
        await conn.executemany(
            """UPDATE objects
               SET properties = properties || jsonb_build_object('createdate', $2::text),
                   created_at = COALESCE(NULLIF($2, '')::timestamptz, created_at)
               WHERE id = $1""",
            values,
        )


def _find_csvs(root: Path) -> dict[str, Path]:
    found = {path.name.lower(): path for path in root.rglob("*.csv")}
    missing = sorted(CSV_NAMES - found.keys())
    if missing:
        raise ValueError(f"Archivio incompleto: mancano {', '.join(missing)}")
    return found


def _safe_extract(archive: zipfile.ZipFile, destination: Path) -> None:
    root = destination.resolve()
    for member in archive.infolist():
        target = (destination / member.filename).resolve()
        if root != target and root not in target.parents:
            raise ValueError("Percorso non valido nell'archivio ZIP")
    archive.extractall(destination)


async def _download_export(export_url: str, target: Path) -> None:
    if not export_url.lower().startswith(("http://", "https://")):
        raise ValueError("export_url deve usare http o https")
    async with httpx.AsyncClient(follow_redirects=True, timeout=55.0) as client:
        async with client.stream("GET", export_url) as response:
            response.raise_for_status()
            with target.open("wb") as handle:
                async for chunk in response.aiter_bytes():
                    handle.write(chunk)


async def _create_configuration() -> None:
    await _db().create_pipeline(
        "deals",
        "rinnovi",
        "Rinnovi",
        [
            {"id": "da_rinnovare", "label": "Da rinnovare", "displayOrder": 0, "metadata": {"probability": "0.2"}},
            {"id": "in_trattativa", "label": "In trattativa", "displayOrder": 1, "metadata": {"probability": "0.6"}},
            {"id": "rinnovato", "label": "Rinnovato", "displayOrder": 2, "metadata": {"isClosed": "true", "probability": "1.0"}},
            {"id": "non_rinnovato", "label": "Non rinnovato", "displayOrder": 3, "metadata": {"isClosed": "true", "probability": "0.0"}},
        ],
    )
    await _db().create_pipeline(
        "tickets",
        "assistenza",
        "Assistenza",
        [
            {"id": "aperto", "label": "Aperto", "displayOrder": 0, "metadata": {}},
            {"id": "in_lavorazione", "label": "In lavorazione", "displayOrder": 1, "metadata": {}},
            {"id": "in_attesa_del_cliente", "label": "In attesa del cliente", "displayOrder": 2, "metadata": {}},
            {"id": "chiuso", "label": "Chiuso", "displayOrder": 3, "metadata": {"isClosed": "true"}},
        ],
    )
    definitions = [
        ("companies", "id_legacy", {}),
        ("contacts", "id_legacy", {}),
        ("deals", "id_legacy", {}),
        ("line_items", "id_legacy", {}),
        ("tickets", "id_legacy", {}),
        ("notes", "id_legacy", {}),
        ("calls", "id_legacy", {}),
        ("emails", "id_legacy", {}),
        ("meetings", "id_legacy", {}),
        ("deals", "commerciale", {}),
        ("tickets", "assegnatario", {}),
        ("notes", "autore", {}),
        ("calls", "autore", {}),
        ("emails", "autore", {}),
        ("meetings", "autore", {}),
        ("companies", "partita_iva", {"has_unique_value": True}),
        ("companies", "fatturato_2025", {"type": "number", "field_type": "number"}),
        ("companies", "classe_cliente", {}),
    ]
    for object_type, name, kwargs in definitions:
        try:
            await _db().create_property_def(object_type, name, label=name, **kwargs)
        except Exception as exc:
            if "duplicate" not in str(exc).lower() and "unique" not in str(exc).lower():
                raise


async def _calculate_revenue() -> None:
    pool = await _db().get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT a.to_object_id AS company_id,
                      COALESCE(SUM(
                        CASE COALESCE(o.properties->>'deal_currency_code', 'EUR')
                          WHEN 'USD' THEN COALESCE(NULLIF(o.properties->>'amount','')::numeric, 0) * 0.92
                          WHEN 'GBP' THEN COALESCE(NULLIF(o.properties->>'amount','')::numeric, 0) * 1.17
                          ELSE COALESCE(NULLIF(o.properties->>'amount','')::numeric, 0)
                        END), 0) AS revenue
               FROM objects o
               JOIN associations a ON a.from_object_type='deals' AND a.from_object_id=o.id
                                  AND a.to_object_type='companies'
               WHERE o.object_type='deals' AND NOT o.archived
                 AND left(COALESCE(o.properties->>'closedate',''), 4)='2025'
                 AND ((o.properties->>'pipeline'='default' AND o.properties->>'dealstage'='closedwon')
                   OR (o.properties->>'pipeline'='rinnovi' AND o.properties->>'dealstage'='rinnovato'))
               GROUP BY a.to_object_id"""
        )
        revenue = {int(row["company_id"]): Decimal(str(row["revenue"])).quantize(CENT, rounding=ROUND_HALF_UP) for row in rows}
        companies = await conn.fetch("SELECT id FROM objects WHERE object_type='companies' AND NOT archived")
        updates = []
        for row in companies:
            value = revenue.get(int(row["id"]), Decimal("0.00"))
            customer_class = "A" if value >= 100000 else "B" if value >= 20000 else "C" if value > 0 else ""
            updates.append((int(row["id"]), format(value, ".2f"), customer_class))
        await conn.executemany(
            """UPDATE objects SET properties=properties || jsonb_build_object(
                   'fatturato_2025', $2::text, 'classe_cliente', $3::text)
               WHERE id=$1""",
            updates,
        )


async def _create_dormant_list() -> None:
    pool = await _db().get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """WITH won_companies AS (
                 SELECT DISTINCT a.to_object_id AS company_id
                 FROM objects d
                 JOIN associations a ON a.from_object_type='deals' AND a.from_object_id=d.id
                                    AND a.to_object_type='companies'
                 WHERE d.object_type='deals' AND NOT d.archived
                   AND ((d.properties->>'pipeline'='default' AND d.properties->>'dealstage'='closedwon')
                     OR (d.properties->>'pipeline'='rinnovi' AND d.properties->>'dealstage'='rinnovato'))
               ), active_companies AS (
                 SELECT DISTINCT dc.to_object_id AS company_id
                 FROM objects act
                 JOIN associations ac ON ac.from_object_id=act.id
                                      AND ac.from_object_type=act.object_type
                                      AND ac.to_object_type='contacts'
                 JOIN associations dc ON dc.from_object_type='contacts' AND dc.from_object_id=ac.to_object_id
                                      AND dc.to_object_type='companies'
                 WHERE act.object_type IN ('notes','calls','emails','meetings')
                   AND left(COALESCE(act.properties->>'hs_timestamp',''), 4)='2025'
                 UNION
                 SELECT DISTINCT dd.to_object_id
                 FROM objects act
                 JOIN associations ad ON ad.from_object_id=act.id
                                      AND ad.from_object_type=act.object_type
                                      AND ad.to_object_type='deals'
                 JOIN associations dd ON dd.from_object_type='deals' AND dd.from_object_id=ad.to_object_id
                                      AND dd.to_object_type='companies'
                 WHERE act.object_type IN ('notes','calls','emails','meetings')
                   AND left(COALESCE(act.properties->>'hs_timestamp',''), 4)='2025'
               )
               SELECT company_id FROM won_companies
               WHERE company_id NOT IN (SELECT company_id FROM active_companies)"""
        )
    list_id = await _db().create_list("Clienti dormienti", "companies")
    company_ids = [int(row["company_id"]) for row in rows]
    for start in range(0, len(company_ids), BATCH_SIZE):
        await _db().add_to_list(list_id, company_ids[start : start + BATCH_SIZE])


async def _migrate_files(files: dict[str, Path]) -> None:
    await _create_configuration()

    users: dict[str, dict[str, Any]] = {}
    user_rows: list[dict[str, str]] = []
    for row in iter_legacy_dicts(files["utenti.csv"]):
        user_id = clean_text(row["id_utente"])
        user = {
            "id_legacy": user_id,
            "firstname": clean_text(row["nome"]),
            "lastname": clean_text(row["cognome"]),
            "email": normalize_email(row["email"]),
            "role": clean_text(row["ruolo"]),
            "manager_id": clean_text(row["responsabile"]),
            "active": active_flag(row["attivo"]),
        }
        users[user_id] = {**user, "active_bool": user["active"] == "true"}
        user_rows.append(user)
    await _insert_batches("users", user_rows)

    company_source: list[dict[str, Any]] = []
    live_company_ids: set[str] = set()
    for order, row in enumerate(iter_legacy_dicts(files["aziende.csv"])):
        if deleted_flag(row["cancellato"]) == "true":
            continue
        legacy_id = clean_text(row["id_azienda"])
        live_company_ids.add(legacy_id)
        company_source.append(
            {
                "id_legacy": legacy_id,
                "name": clean_text(row["ragione_sociale"]),
                "domain": normalize_domain(row["sito_web"]),
                "city": clean_text(row["citta"]),
                "state": clean_text(row["provincia"]).upper(),
                "partita_iva": extract_vat(row["note"]),
                "_modified": parse_date(row["ultima_modifica"]),
                "_order": order,
            }
        )
    company_groups = _dedupe_companies(company_source)
    company_ids = await _insert_batches("companies", [merged for merged, _ in company_groups])
    company_by_legacy: dict[str, int] = {}
    company_by_domain: dict[str, int] = {}
    for db_id, (merged, members) in zip(company_ids, company_groups):
        for member in members:
            company_by_legacy[member["id_legacy"]] = db_id
            if member["domain"]:
                company_by_domain[member["domain"].lower()] = db_id
        if merged["domain"]:
            company_by_domain[merged["domain"].lower()] = db_id

    contact_source: list[dict[str, Any]] = []
    live_contact_ids: set[str] = set()
    for order, row in enumerate(iter_legacy_dicts(files["contatti.csv"])):
        if deleted_flag(row["cancellato"]) == "true":
            continue
        legacy_id = clean_text(row["id_contatto"])
        live_contact_ids.add(legacy_id)
        raw_company = clean_text(row["id_azienda"])
        contact_source.append(
            {
                "id_legacy": legacy_id,
                "firstname": clean_text(row["nome"]),
                "lastname": clean_text(row["cognome"]),
                "email": normalize_email(row["email"]),
                "phone": clean_text(row["telefono"]),
                "lifecyclestage": normalize_lifecycle(row["tipo"]),
                "_company_legacy": raw_company if raw_company in live_company_ids else "",
                "_modified": parse_date(row["ultima_modifica"]),
                "_order": order,
            }
        )
    contact_fields = ("id_legacy", "firstname", "lastname", "email", "phone", "lifecyclestage", "_company_legacy")
    contact_groups = _dedupe_by(contact_source, "email", contact_fields)
    contact_ids = await _insert_batches("contacts", [{k: v for k, v in merged.items() if not k.startswith("_")} for merged, _ in contact_groups])
    contact_by_legacy: dict[str, int] = {}
    associations: list[tuple[str, int, str, int, int]] = []
    for db_id, (merged, members) in zip(contact_ids, contact_groups):
        for member in members:
            contact_by_legacy[member["id_legacy"]] = db_id
        company_id = company_by_legacy.get(merged["_company_legacy"])
        if not company_id and merged["email"]:
            company_id = company_by_domain.get(merged["email"].rsplit("@", 1)[-1].lower())
        if company_id:
            associations.append(("contacts", db_id, "companies", company_id, 0))
    await _insert_associations(associations)

    product_source: list[dict[str, Any]] = []
    for order, row in enumerate(iter_legacy_dicts(files["listino.csv"])):
        if deleted_flag(row["cancellato"]) == "true":
            continue
        product_source.append(
            {
                "hs_sku": normalize_sku(row["codice_articolo"]),
                "name": clean_text(row["descrizione"]),
                "price": normalize_amount(row["prezzo_listino"]),
                "_modified": parse_date(row["ultima_modifica"]),
                "_order": order,
            }
        )
    product_groups = _dedupe_by(product_source, "hs_sku", ("hs_sku", "name", "price"))
    product_ids = await _insert_batches("products", [merged for merged, _ in product_groups])
    products = {merged["hs_sku"]: {"db_id": db_id, "name": merged["name"]} for db_id, (merged, _) in zip(product_ids, product_groups) if merged["hs_sku"]}

    deal_rows: list[dict[str, str]] = []
    deal_refs: list[tuple[str, str, list[str]]] = []
    for row in iter_legacy_dicts(files["opportunita.csv"]):
        if deleted_flag(row["cancellato"]) == "true":
            continue
        pipeline_label = normalize_pipeline(row["pipeline"])
        amount = normalize_amount(row["importo"], annualize_monthly=pipeline_label == "Rinnovi")
        owner = users.get(clean_text(row["id_commerciale"]), {})
        legacy_id = clean_text(row["id_opportunita"])
        raw_company = clean_text(row["id_azienda"])
        contacts = [value for value in split_contact_ids(row["contatti"]).split("|") if value in live_contact_ids]
        deal_rows.append(
            {
                "id_legacy": legacy_id,
                "dealname": clean_text(row["titolo"]),
                "amount": amount,
                "deal_currency_code": normalize_currency(row["valuta"], row["importo"]) if amount else "",
                "pipeline": "rinnovi" if pipeline_label == "Rinnovi" else "default",
                "dealstage": normalize_stage(row["fase"], pipeline_label),
                "closedate": parse_date(row["data_chiusura"]),
                "commerciale": owner.get("email", "") if owner.get("active_bool") else "",
            }
        )
        deal_refs.append((legacy_id, raw_company if raw_company in live_company_ids else "", contacts))
    deal_ids = await _insert_batches("deals", deal_rows)
    deal_by_legacy = {legacy: db_id for db_id, (legacy, _, _) in zip(deal_ids, deal_refs)}
    associations = []
    for db_id, (_, company_legacy, contacts) in zip(deal_ids, deal_refs):
        if company_legacy in company_by_legacy:
            associations.append(("deals", db_id, "companies", company_by_legacy[company_legacy], 0))
        for contact_legacy in dict.fromkeys(contacts):
            if contact_legacy in contact_by_legacy:
                associations.append(("deals", db_id, "contacts", contact_by_legacy[contact_legacy], 0))
    await _insert_associations(associations)

    line_rows: list[dict[str, str]] = []
    line_refs: list[tuple[int, int | None, Decimal]] = []
    deal_totals: dict[int, Decimal] = defaultdict(lambda: Decimal("0"))
    for row in iter_legacy_dicts(files["righe_offerta.csv"]):
        deal_id = deal_by_legacy.get(clean_text(row["id_opportunita"]))
        if not deal_id:
            continue
        sku = normalize_sku(row["codice_articolo"])
        product = products.get(sku)
        quantity = normalize_quantity(row["quantita"])
        price = normalize_amount(row["prezzo_unitario"])
        discount = normalize_discount(row["sconto"])
        line_total = _line_total(quantity, price, discount)
        deal_totals[deal_id] += line_total
        line_rows.append(
            {
                "id_legacy": clean_text(row["id_riga"]),
                "name": clean_text(row["descrizione"]) or (product or {}).get("name", ""),
                "quantity": quantity,
                "price": price,
                "hs_discount_percentage": discount,
                "amount": format(line_total, ".2f"),
            }
        )
        line_refs.append((deal_id, (product or {}).get("db_id"), line_total))
    line_ids = await _insert_batches("line_items", line_rows)
    associations = []
    for db_id, (deal_id, product_id, _) in zip(line_ids, line_refs):
        associations.append(("line_items", db_id, "deals", deal_id, 0))
        if product_id:
            associations.append(("line_items", db_id, "products", product_id, 0))
    await _insert_associations(associations)
    for deal_id, total in deal_totals.items():
        await _db().update_object("deals", deal_id, {"amount": format(total.quantize(CENT, rounding=ROUND_HALF_UP), ".2f")})

    ticket_rows: list[dict[str, str]] = []
    ticket_refs: list[tuple[str, str, str]] = []
    for row in iter_legacy_dicts(files["ticket.csv"]):
        if deleted_flag(row["cancellato"]) == "true":
            continue
        owner = users.get(clean_text(row["id_utente"]), {})
        contact_legacy = clean_text(row["id_contatto"])
        company_legacy = clean_text(row["id_azienda"])
        createdate = parse_date(row["aperto_il"])
        stage = normalize_ticket_stage(row["stato"])
        ticket_rows.append(
            {
                "id_legacy": clean_text(row["id_ticket"]),
                "subject": clean_text(row["oggetto"]),
                "content": clean_text(row["descrizione"]),
                "hs_pipeline": "assistenza",
                "hs_pipeline_stage": stage,
                "hs_ticket_priority": normalize_ticket_priority(row["priorita"]),
                "createdate": createdate,
                "closed_date": parse_date(row["chiuso_il"]),
                "assegnatario": owner.get("email", "") if owner.get("active_bool") else "",
            }
        )
        ticket_refs.append((contact_legacy if contact_legacy in live_contact_ids else "", company_legacy if company_legacy in live_company_ids else "", createdate))
    ticket_ids = await _insert_batches("tickets", ticket_rows)
    associations = []
    for db_id, (contact_legacy, company_legacy, _) in zip(ticket_ids, ticket_refs):
        if contact_legacy in contact_by_legacy:
            associations.append(("tickets", db_id, "contacts", contact_by_legacy[contact_legacy], 0))
        if company_legacy in company_by_legacy:
            associations.append(("tickets", db_id, "companies", company_by_legacy[company_legacy], 0))
    await _insert_associations(associations)
    await _preserve_createdates([(db_id, ref[2]) for db_id, ref in zip(ticket_ids, ticket_refs)])

    activity_rows: dict[str, list[dict[str, str]]] = defaultdict(list)
    activity_refs: dict[str, list[tuple[str, str]]] = defaultdict(list)

    async def flush(activity_type: str) -> None:
        rows = activity_rows[activity_type]
        if not rows:
            return
        refs = activity_refs[activity_type]
        ids = await _db().bulk_insert_objects(activity_type, rows)
        batch_associations = []
        for db_id, (contact_legacy, deal_legacy) in zip(ids, refs):
            if contact_legacy in contact_by_legacy:
                batch_associations.append((activity_type, db_id, "contacts", contact_by_legacy[contact_legacy], 0))
            if deal_legacy in deal_by_legacy:
                batch_associations.append((activity_type, db_id, "deals", deal_by_legacy[deal_legacy], 0))
        await _insert_associations(batch_associations)
        rows.clear()
        refs.clear()

    body_properties = {"notes": "hs_note_body", "calls": "hs_call_body", "emails": "hs_email_text", "meetings": "hs_meeting_body"}
    for row in iter_legacy_dicts(files["attivita.csv"]):
        if deleted_flag(row["cancellato"]) == "true":
            continue
        activity_type = normalize_activity_type(row["tipo"]) or "notes"
        user = users.get(clean_text(row["id_utente"]), {})
        activity_rows[activity_type].append(
            {
                "id_legacy": clean_text(row["id_attivita"]),
                "hs_timestamp": parse_date(row["data"]),
                body_properties[activity_type]: clean_text(row["testo"]),
                "autore": user.get("email", ""),
            }
        )
        activity_refs[activity_type].append((clean_text(row["id_contatto"]), clean_text(row["id_opportunita"])))
        if len(activity_rows[activity_type]) >= BATCH_SIZE:
            await flush(activity_type)
    for activity_type in body_properties:
        await flush(activity_type)

    await _calculate_revenue()
    await _create_dormant_list()


async def run_migration(export_url: str) -> None:
    import automations

    automations._migration_active = True
    try:
        with tempfile.TemporaryDirectory(prefix="sinergia-") as directory:
            root = Path(directory)
            archive_path = root / "export.zip"
            await _download_export(export_url, archive_path)
            extract_path = root / "contents"
            extract_path.mkdir()
            with zipfile.ZipFile(archive_path) as archive:
                _safe_extract(archive, extract_path)
            files = _find_csvs(extract_path)
            await _migrate_files(files)
    finally:
        automations._migration_active = False


__all__ = ["run_migration"]
