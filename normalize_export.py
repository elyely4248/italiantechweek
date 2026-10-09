from __future__ import annotations

import csv
import re
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from urllib.parse import urlparse

from legacy_csv import iter_legacy_dicts


SOURCE_DIR = Path("legacy/export/sinergia-export")
OUTPUT_DIR = Path("normalized_export")

EMAIL_RE = re.compile(r"^[^@\s;]+@[^@\s;]+\.[^@\s;]+$")
VAT_RE = re.compile(r"(?:p\.?\s*iva|partita\s+iva)[:\s]*(?:IT\s*)?(\d[\d\s]{8,})", re.IGNORECASE)


def clean_text(value: str | None) -> str:
    return (value or "").strip()


def deleted_flag(value: str | None) -> str:
    normalized = clean_text(value).upper()
    return "true" if normalized in {"S", "SI", "S\u00cc", "1"} else "false"


def active_flag(value: str | None) -> str:
    normalized = clean_text(value).upper()
    if normalized in {"S", "SI", "S\u00cc", "Y", "YES", "1"}:
        return "true"
    if normalized in {"N", "NO", "0"}:
        return "false"
    return ""


def parse_date(value: str | None) -> str:
    raw = clean_text(value)
    if not raw:
        return ""

    if raw.isdigit():
        try:
            serial = int(raw)
            if serial <= 10000:
                return ""
            dt = datetime(1899, 12, 30) + timedelta(days=serial)
            return dt.date().isoformat()
        except (ValueError, OverflowError):
            return ""

    short_date = re.fullmatch(r"(\d{1,2})\.(\d{1,2})\.(\d{2})", raw)
    if short_date:
        day, month, year = map(int, short_date.groups())
        year += 2000 if year < 50 else 1900
        try:
            return datetime(year, month, day).date().isoformat()
        except ValueError:
            return ""

    formats = [
        "%d/%m/%Y %H:%M:%S",
        "%d/%m/%Y %H:%M",
        "%d/%m/%Y",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%d-%m-%Y %H:%M:%S",
        "%d-%m-%Y",
        "%d.%m.%Y",
    ]
    for fmt in formats:
        try:
            dt = datetime.strptime(raw, fmt)
            if "%H" in fmt:
                return dt.isoformat()
            return dt.date().isoformat()
        except ValueError:
            pass
    return ""


def normalize_domain(value: str | None) -> str:
    raw = clean_text(value).lower()
    if not raw:
        return ""
    parsed = urlparse(raw if "://" in raw else f"//{raw}")
    domain = parsed.netloc or parsed.path.split("/", 1)[0]
    domain = domain.strip().strip(".")
    if domain.startswith("www."):
        domain = domain[4:]
    return domain


def extract_vat(notes: str | None) -> str:
    match = VAT_RE.search(notes or "")
    if not match:
        return ""
    digits = re.sub(r"\D", "", match.group(1))
    return digits if len(digits) == 11 else ""


def normalize_email(value: str | None) -> str:
    email = clean_text(value).lower()
    return email if EMAIL_RE.match(email) else ""


def normalize_lifecycle(value: str | None) -> str:
    key = clean_text(value).lower().replace("-", " ")
    key = re.sub(r"\s+", " ", key)
    return {
        "lead": "lead",
        "prospect": "opportunity",
        "cliente": "customer",
        "ex cliente": "other",
    }.get(key, "aperto")


def normalize_currency(*values: str | None) -> str:
    for value in values:
        raw = clean_text(value)
        key = raw.upper()
        if key in {"EUR", "EURO", "€"} or "€" in raw:
            return "EUR"
        if key == "USD" or "$" in raw:
            return "USD"
        if key == "GBP" or "£" in raw:
            return "GBP"
    return "EUR"


def parse_decimal_number(value: str | None) -> Decimal | None:
    raw = clean_text(value)
    if not raw:
        return None

    # Remove known currency tokens before interpreting a parenthesized sign.
    raw = re.sub(r"(?<![A-Za-z])(?:EURO|EUR|USD|GBP)(?![A-Za-z])", "", raw, flags=re.IGNORECASE)
    raw = raw.replace("\u20ac", "").replace("$", "").replace("\u00a3", "").strip()
    negative = False
    if raw.startswith("(") and raw.endswith(")"):
        negative = True
        raw = raw[1:-1].strip()
    elif raw.endswith("-"):
        negative = True
        raw = raw[:-1].strip()
    elif raw.startswith("-"):
        negative = True
        raw = raw[1:].strip()
    elif raw.startswith("+"):
        raw = raw[1:].strip()

    # Reject unknown text instead of extracting unrelated digits from it.
    if not re.fullmatch(r"[0-9]+(?:[., \u00a0][0-9]+)*", raw):
        return None
    if " " in raw or "\u00a0" in raw:
        if not re.fullmatch(r"[0-9]{1,3}(?:[ \u00a0][0-9]{3})+(?:[.,][0-9]+)?", raw):
            return None
        raw = raw.replace(" ", "").replace("\u00a0", "")

    if "." in raw and "," in raw:
        decimal_sep = "." if raw.rfind(".") > raw.rfind(",") else ","
        thousands_sep = "," if decimal_sep == "." else "."
        integer, fraction = raw.rsplit(decimal_sep, 1)
        grouping = rf"[0-9]{{1,3}}(?:{re.escape(thousands_sep)}[0-9]{{3}})+"
        if not re.fullmatch(grouping, integer) or not fraction.isdigit():
            return None
        raw = integer.replace(thousands_sep, "") + "." + fraction
    elif "," in raw or "." in raw:
        separator = "," if "," in raw else "."
        parts = raw.split(separator)
        if len(parts) > 2:
            if not (1 <= len(parts[0]) <= 3 and all(len(part) == 3 for part in parts[1:])):
                return None
            raw = "".join(parts)
        elif len(parts[1]) == 3 and len(parts[0]) <= 3:
            raw = "".join(parts)
        else:
            raw = parts[0] + "." + parts[1]

    try:
        amount = Decimal(raw)
    except InvalidOperation:
        return None
    return -amount if negative else amount


def format_decimal(value: Decimal | None) -> str:
    if value is None or not value.is_finite():
        return ""
    try:
        return str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    except InvalidOperation:
        return ""


def normalize_amount(value: str | None, annualize_monthly: bool = False) -> str:
    raw = clean_text(value)
    multiplier = Decimal("1")
    monthly = re.search(r"(?:mensil[ei]|al\s+mese|/\s*mese)\s*$", raw, re.IGNORECASE)
    if monthly:
        raw = raw[:monthly.start()].strip()
    magnitude = re.search(r"(?<![A-Za-z])(?:mln|milion[ei]|mila|k)\s*$", raw, re.IGNORECASE)
    if magnitude:
        unit = magnitude.group().strip().lower()
        multiplier = Decimal("1000") if unit in {"mila", "k"} else Decimal("1000000")
        raw = raw[:magnitude.start()].strip()
    if annualize_monthly and monthly:
        multiplier *= Decimal("12")
    amount = parse_decimal_number(raw)
    if amount is None:
        return ""
    return format_decimal(amount * multiplier)


def normalize_pipeline(value: str | None) -> str:
    key = clean_text(value).lower()
    if key.startswith("rinnovi"):
        return "Rinnovi"
    return "Vendite"


def normalize_stage(value: str | None, pipeline: str) -> str:
    raw = clean_text(value)
    key = re.sub(r"^\s*(?:0?[1-7]|R[1-4])\s*(?:-|\.| )?\s*", "", raw, flags=re.IGNORECASE)
    key = re.sub(r"\s+", " ", key).strip().lower()
    if not key:
        key = raw.strip().lower()

    if pipeline == "Rinnovi":
        return {
            "r1": "da_rinnovare",
            "da rinnovare": "da_rinnovare",
            "r2": "in_trattativa",
            "in trattativa": "in_trattativa",
            "r3": "rinnovato",
            "rinnovato": "rinnovato",
            "r4": "non_rinnovato",
            "non rinnovato": "non_rinnovato",
        }.get(key, "")

    return {
        "01": "appointmentscheduled",
        "1": "appointmentscheduled",
        "contatto": "appointmentscheduled",
        "02": "qualifiedtobuy",
        "2": "qualifiedtobuy",
        "qualifica": "qualifiedtobuy",
        "03": "presentationscheduled",
        "3": "presentationscheduled",
        "presentazione": "presentationscheduled",
        "04": "decisionmakerboughtin",
        "4": "decisionmakerboughtin",
        "decisione": "decisionmakerboughtin",
        "05": "contractsent",
        "5": "contractsent",
        "contratto": "contractsent",
        "06": "closedwon",
        "6": "closedwon",
        "vinta": "closedwon",
        "07": "closedlost",
        "7": "closedlost",
        "persa": "closedlost",
    }.get(key, "")


def normalize_activity_type(value: str | None) -> str:
    key = clean_text(value).lower().replace("-", " ")
    return {
        "nota": "notes",
        "appunto": "notes",
        "chiamata": "calls",
        "telefonata": "calls",
        "tel.": "calls",
        "email": "emails",
        "e mail": "emails",
        "mail": "emails",
        "meeting": "meetings",
        "incontro": "meetings",
        "riunione": "meetings",
        "visita": "meetings",
    }.get(key, "")


def normalize_sku(value: str | None) -> str:
    raw = clean_text(value)
    match = re.fullmatch(r"(?:BF\s*[-.]?\s*|ART\.\s*)?([0-9]{1,5})", raw, re.IGNORECASE)
    if not match:
        return raw
    return f"BF-{int(match.group(1)):05d}"


def normalize_ticket_stage(value: str | None) -> str:
    key = re.sub(r"\s+", " ", clean_text(value)).lower()
    return {
        "aperto": "aperto",
        "nuovo": "aperto",
        "in lavorazione": "in_lavorazione",
        "lavorazione": "in_lavorazione",
        "in attesa": "in_attesa_del_cliente",
        "attesa cliente": "in_attesa_del_cliente",
        "in attesa cliente": "in_attesa_del_cliente",
        "in attesa del cliente": "in_attesa_del_cliente",
        "chiuso": "chiuso",
        "risolto": "chiuso",
    }.get(key, "")


def normalize_ticket_priority(value: str | None) -> str:
    key = re.sub(r"^[1-4]\s*-\s*", "", clean_text(value))
    key = key.rstrip("!").strip().lower()
    return {
        "bassa": "LOW",
        "low": "LOW",
        "media": "MEDIUM",
        "normale": "MEDIUM",
        "medium": "MEDIUM",
        "alta": "HIGH",
        "high": "HIGH",
        "urgente": "URGENT",
        "urgent": "URGENT",
    }.get(key, "")


def split_contact_ids(value: str | None) -> str:
    ids = [part.strip() for part in re.split(r"[;,]", clean_text(value)) if part.strip()]
    return "|".join(dict.fromkeys(ids))


def normalize_quantity(value: str | None) -> str:
    match = re.search(r"-?\d+(?:[,.]\d+)?", clean_text(value))
    if not match:
        return ""
    return format_decimal(parse_decimal_number(match.group(0)))


def normalize_discount(value: str | None) -> str:
    amount = parse_decimal_number(clean_text(value).replace("%", ""))
    return format_decimal(amount if amount is not None else Decimal("0"))


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def load_active_user_ids() -> set[str]:
    active = set()
    for row in iter_legacy_dicts(SOURCE_DIR / "utenti.csv"):
        if active_flag(row["attivo"]) == "true":
            active.add(clean_text(row["id_utente"]))
    return active


def normalize_companies() -> None:
    rows = []
    for row in iter_legacy_dicts(SOURCE_DIR / "aziende.csv"):
        rows.append(
            {
                "id_legacy": clean_text(row["id_azienda"]),
                "name": clean_text(row["ragione_sociale"]),
                "domain": normalize_domain(row["sito_web"]),
                "partita_iva": extract_vat(row["note"]),
                "city": clean_text(row["citta"]),
                "state": clean_text(row["provincia"]).upper(),
                "note": clean_text(row["note"]),
                "is_deleted": deleted_flag(row["cancellato"]),
                "ultima_modifica": parse_date(row["ultima_modifica"]),
            }
        )
    write_csv(
        OUTPUT_DIR / "companies.normalized.csv",
        ["id_legacy", "name", "domain", "partita_iva", "city", "state", "note", "is_deleted", "ultima_modifica"],
        rows,
    )


def normalize_contacts() -> None:
    rows = []
    for row in iter_legacy_dicts(SOURCE_DIR / "contatti.csv"):
        rows.append(
            {
                "id_legacy": clean_text(row["id_contatto"]),
                "firstname": clean_text(row["nome"]),
                "lastname": clean_text(row["cognome"]),
                "email": normalize_email(row["email"]),
                "raw_email": clean_text(row["email"]),
                "phone": clean_text(row["telefono"]),
                "id_company_legacy": clean_text(row["id_azienda"]),
                "lifecyclestage": normalize_lifecycle(row["tipo"]),
                "is_deleted": deleted_flag(row["cancellato"]),
                "ultima_modifica": parse_date(row["ultima_modifica"]),
            }
        )
    write_csv(
        OUTPUT_DIR / "contacts.normalized.csv",
        [
            "id_legacy",
            "firstname",
            "lastname",
            "email",
            "raw_email",
            "phone",
            "id_company_legacy",
            "lifecyclestage",
            "is_deleted",
            "ultima_modifica",
        ],
        rows,
    )


def normalize_deals(active_user_ids: set[str]) -> None:
    rows = []
    for row in iter_legacy_dicts(SOURCE_DIR / "opportunita.csv"):
        pipeline = normalize_pipeline(row["pipeline"])
        owner = clean_text(row["id_commerciale"])
        amount = normalize_amount(row["importo"], annualize_monthly=pipeline == "Rinnovi")
        rows.append(
            {
                "id_legacy": clean_text(row["id_opportunita"]),
                "dealname": clean_text(row["titolo"]),
                "id_company_legacy": clean_text(row["id_azienda"]),
                "contact_ids_legacy": split_contact_ids(row["contatti"]),
                "amount": amount,
                "deal_currency_code": normalize_currency(row["valuta"], row["importo"]) if amount else "",
                "pipeline": pipeline,
                "dealstage": normalize_stage(row["fase"], pipeline),
                "closedate": parse_date(row["data_chiusura"]),
                "commerciale": owner if owner in active_user_ids else "",
                "is_deleted": deleted_flag(row["cancellato"]),
                "ultima_modifica": parse_date(row["ultima_modifica"]),
                "raw_importo": clean_text(row["importo"]),
                "raw_valuta": clean_text(row["valuta"]),
                "raw_fase": clean_text(row["fase"]),
            }
        )
    write_csv(
        OUTPUT_DIR / "deals.normalized.csv",
        [
            "id_legacy",
            "dealname",
            "id_company_legacy",
            "contact_ids_legacy",
            "amount",
            "deal_currency_code",
            "pipeline",
            "dealstage",
            "closedate",
            "commerciale",
            "is_deleted",
            "ultima_modifica",
            "raw_importo",
            "raw_valuta",
            "raw_fase",
        ],
        rows,
    )


def normalize_products() -> None:
    rows = []
    for row in iter_legacy_dicts(SOURCE_DIR / "listino.csv"):
        rows.append(
            {
                "hs_sku": normalize_sku(row["codice_articolo"]),
                "raw_sku": clean_text(row["codice_articolo"]),
                "name": clean_text(row["descrizione"]),
                "unit": clean_text(row["unita"]),
                "price": normalize_amount(row["prezzo_listino"]),
                "raw_price": clean_text(row["prezzo_listino"]),
                "is_deleted": deleted_flag(row["cancellato"]),
                "ultima_modifica": parse_date(row["ultima_modifica"]),
            }
        )
    write_csv(
        OUTPUT_DIR / "products.normalized.csv",
        ["hs_sku", "raw_sku", "name", "unit", "price", "raw_price", "is_deleted", "ultima_modifica"],
        rows,
    )


def normalize_line_items() -> None:
    rows = []
    for row in iter_legacy_dicts(SOURCE_DIR / "righe_offerta.csv"):
        rows.append(
            {
                "id_legacy": clean_text(row["id_riga"]),
                "id_deal_legacy": clean_text(row["id_opportunita"]),
                "hs_sku": normalize_sku(row["codice_articolo"]),
                "raw_sku": clean_text(row["codice_articolo"]),
                "name": clean_text(row["descrizione"]),
                "quantity": normalize_quantity(row["quantita"]),
                "price": normalize_amount(row["prezzo_unitario"]),
                "raw_price": clean_text(row["prezzo_unitario"]),
                "hs_discount_percentage": normalize_discount(row["sconto"]),
            }
        )
    write_csv(
        OUTPUT_DIR / "line_items.normalized.csv",
        ["id_legacy", "id_deal_legacy", "hs_sku", "raw_sku", "name", "quantity", "price", "raw_price", "hs_discount_percentage"],
        rows,
    )


def normalize_tickets(active_user_ids: set[str]) -> None:
    rows = []
    for row in iter_legacy_dicts(SOURCE_DIR / "ticket.csv"):
        owner = clean_text(row["id_utente"])
        stage = normalize_ticket_stage(row["stato"])
        rows.append(
            {
                "id_legacy": clean_text(row["id_ticket"]),
                "subject": clean_text(row["oggetto"]),
                "content": clean_text(row["descrizione"]),
                "status": stage,
                "hs_pipeline": "assistenza",
                "hs_pipeline_stage": stage,
                "hs_ticket_priority": normalize_ticket_priority(row["priorita"]),
                "raw_stato": clean_text(row["stato"]),
                "raw_priorita": clean_text(row["priorita"]),
                "id_contact_legacy": clean_text(row["id_contatto"]),
                "id_company_legacy": clean_text(row["id_azienda"]),
                "createdate": parse_date(row["aperto_il"]),
                "closed_date": parse_date(row["chiuso_il"]),
                "assegnatario": owner if owner in active_user_ids else "",
                "is_deleted": deleted_flag(row["cancellato"]),
                "ultima_modifica": parse_date(row["ultima_modifica"]),
            }
        )
    write_csv(
        OUTPUT_DIR / "tickets.normalized.csv",
        [
            "id_legacy",
            "subject",
            "content",
            "status",
            "hs_pipeline",
            "hs_pipeline_stage",
            "hs_ticket_priority",
            "raw_stato",
            "raw_priorita",
            "id_contact_legacy",
            "id_company_legacy",
            "createdate",
            "closed_date",
            "assegnatario",
            "is_deleted",
            "ultima_modifica",
        ],
        rows,
    )


def normalize_activities() -> None:
    rows = []
    for row in iter_legacy_dicts(SOURCE_DIR / "attivita.csv"):
        activity_object = normalize_activity_type(row["tipo"])
        rows.append(
            {
                "id_legacy": clean_text(row["id_attivita"]),
                "activity_object": activity_object,
                "raw_tipo": clean_text(row["tipo"]),
                "hs_timestamp": parse_date(row["data"]),
                "body": clean_text(row["testo"]),
                "id_contact_legacy": clean_text(row["id_contatto"]),
                "id_deal_legacy": clean_text(row["id_opportunita"]),
                "autore": clean_text(row["id_utente"]),
                "is_deleted": deleted_flag(row["cancellato"]),
            }
        )
    write_csv(
        OUTPUT_DIR / "activities.normalized.csv",
        [
            "id_legacy",
            "activity_object",
            "raw_tipo",
            "hs_timestamp",
            "body",
            "id_contact_legacy",
            "id_deal_legacy",
            "autore",
            "is_deleted",
        ],
        rows,
    )


def normalize_users() -> None:
    rows = []
    for row in iter_legacy_dicts(SOURCE_DIR / "utenti.csv"):
        rows.append(
            {
                "id_utente": clean_text(row["id_utente"]),
                "firstname": clean_text(row["nome"]),
                "lastname": clean_text(row["cognome"]),
                "email": normalize_email(row["email"]),
                "role": clean_text(row["ruolo"]),
                "manager_id": clean_text(row["responsabile"]),
                "active": active_flag(row["attivo"]),
            }
        )
    write_csv(
        OUTPUT_DIR / "users.normalized.csv",
        ["id_utente", "firstname", "lastname", "email", "role", "manager_id", "active"],
        rows,
    )


def normalize_stage_history() -> None:
    rows = []
    for row in iter_legacy_dicts(SOURCE_DIR / "storico_fasi.csv"):
        rows.append(
            {
                "id_deal_legacy": clean_text(row["id_opportunita"]),
                "stage_previous_raw": clean_text(row["fase_precedente"]),
                "stage_new_raw": clean_text(row["fase_nuova"]),
                "changed_at": parse_date(row["data_cambio"]),
                "id_utente": clean_text(row["id_utente"]),
            }
        )
    write_csv(
        OUTPUT_DIR / "stage_history.normalized.csv",
        ["id_deal_legacy", "stage_previous_raw", "stage_new_raw", "changed_at", "id_utente"],
        rows,
    )


def main() -> None:
    active_user_ids = load_active_user_ids()
    normalize_companies()
    normalize_contacts()
    normalize_deals(active_user_ids)
    normalize_products()
    normalize_line_items()
    normalize_tickets(active_user_ids)
    normalize_activities()
    normalize_users()
    normalize_stage_history()
    print(f"Normalized files written to {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
