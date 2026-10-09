import csv
from io import StringIO
from pathlib import Path
from typing import IO, Iterator


LEGACY_ENCODING = "cp1252"
LEGACY_DELIMITER = ";"

EXPECTED_HEADERS = {
    "aziende.csv": [
        "id_azienda",
        "ragione_sociale",
        "sito_web",
        "note",
        "citta",
        "provincia",
        "cancellato",
        "ultima_modifica",
    ],
    "contatti.csv": [
        "id_contatto",
        "nome",
        "cognome",
        "email",
        "telefono",
        "id_azienda",
        "tipo",
        "cancellato",
        "ultima_modifica",
    ],
    "opportunita.csv": [
        "id_opportunita",
        "titolo",
        "id_azienda",
        "contatti",
        "importo",
        "valuta",
        "pipeline",
        "fase",
        "data_chiusura",
        "id_commerciale",
        "cancellato",
        "ultima_modifica",
    ],
    "righe_offerta.csv": [
        "id_riga",
        "id_opportunita",
        "codice_articolo",
        "descrizione",
        "quantita",
        "prezzo_unitario",
        "sconto",
    ],
    "listino.csv": [
        "codice_articolo",
        "descrizione",
        "unita",
        "prezzo_listino",
        "cancellato",
        "ultima_modifica",
    ],
    "ticket.csv": [
        "id_ticket",
        "oggetto",
        "descrizione",
        "stato",
        "priorita",
        "id_contatto",
        "id_azienda",
        "aperto_il",
        "chiuso_il",
        "id_utente",
        "cancellato",
        "ultima_modifica",
    ],
    "attivita.csv": [
        "id_attivita",
        "tipo",
        "data",
        "testo",
        "id_contatto",
        "id_opportunita",
        "id_utente",
        "cancellato",
    ],
    "utenti.csv": [
        "id_utente",
        "nome",
        "cognome",
        "email",
        "ruolo",
        "responsabile",
        "attivo",
    ],
    "storico_fasi.csv": [
        "id_opportunita",
        "fase_precedente",
        "fase_nuova",
        "data_cambio",
        "id_utente",
    ],
}


def legacy_csv_reader(handle: IO[str]) -> csv.reader:
    """Return an RFC-4180-compatible reader for Sinergia legacy exports."""
    return csv.reader(handle, delimiter=LEGACY_DELIMITER)


def repair_mojibake(value: str) -> str:
    """Fix UTF-8 bytes that were decoded as cp1252 inside legacy exports."""
    try:
        repaired = value.encode("latin-1").decode("utf-8")
    except UnicodeError:
        return value
    return repaired


def _repair_row(row: list[str]) -> list[str]:
    return [repair_mojibake(value) for value in row]


def open_legacy_csv(path: str | Path) -> IO[str]:
    """Open a legacy CSV file with the expected Windows-1252 encoding.

    The empty newline value is required so csv.reader can correctly handle
    quoted multiline fields.
    """
    return Path(path).open("r", encoding=LEGACY_ENCODING, newline="")


def iter_legacy_rows(path: str | Path) -> Iterator[list[str]]:
    """Yield raw rows from a legacy CSV file."""
    with open_legacy_csv(path) as handle:
        for row in legacy_csv_reader(handle):
            yield _repair_row(row)


def iter_legacy_dicts(path: str | Path) -> Iterator[dict[str, str]]:
    """Yield rows as dictionaries keyed by the CSV header.

    Raises ValueError when a row has shifted columns, which usually means a
    parser setting regressed and quoted semicolons or multiline fields broke.
    """
    with open_legacy_csv(path) as handle:
        reader = csv.DictReader(handle, delimiter=LEGACY_DELIMITER)
        for line_number, row in enumerate(reader, start=2):
            if None in row:
                raise ValueError(f"{path}: row {line_number} has extra columns: {row[None]!r}")
            missing = [key for key, value in row.items() if value is None]
            if missing:
                raise ValueError(f"{path}: row {line_number} is missing columns: {missing!r}")
            yield {key: repair_mojibake(value) for key, value in row.items()}


def parse_legacy_csv_text(text: str) -> list[list[str]]:
    """Parse legacy CSV content already decoded into text.

    Useful for CRM assistant attachments, which arrive as text instead of files.
    """
    return [_repair_row(row) for row in csv.reader(StringIO(text, newline=""), delimiter=LEGACY_DELIMITER)]


def count_legacy_records(path: str | Path) -> int:
    """Return the number of data records, excluding the header row."""
    row_count = sum(1 for _ in iter_legacy_rows(path))
    return max(row_count - 1, 0)


def validate_legacy_csv(path: str | Path) -> int:
    """Validate a known Sinergia CSV header and every row's column shape."""
    csv_path = Path(path)
    rows = iter_legacy_rows(csv_path)
    try:
        header = next(rows)
    except StopIteration:
        raise ValueError(f"{csv_path}: empty CSV file") from None

    expected_header = EXPECTED_HEADERS.get(csv_path.name)
    if expected_header is not None and header != expected_header:
        raise ValueError(f"{csv_path}: expected header {expected_header!r}, got {header!r}")

    expected_width = len(header)
    count = 0
    for line_number, row in enumerate(rows, start=2):
        if len(row) != expected_width:
            raise ValueError(
                f"{csv_path}: row {line_number} has {len(row)} columns, expected {expected_width}"
            )
        count += 1
    return count
