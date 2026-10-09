import csv
import unittest
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import normalize_export as normalizer
from legacy_csv import EXPECTED_HEADERS, LEGACY_ENCODING


class NormalizationTests(unittest.TestCase):
    def test_deleted_markers(self):
        for value in ("S", "s", "SI", "Si", "s\u00ec", "S\u00ec", "1", " 1 "):
            with self.subTest(value=value):
                self.assertEqual(normalizer.deleted_flag(value), "true")
        for value in (None, "", "0", "N", "n", "NO", "No"):
            with self.subTest(value=value):
                self.assertEqual(normalizer.deleted_flag(value), "false")

    def test_active_accented_yes(self):
        self.assertEqual(normalizer.active_flag("S\u00ec"), "true")
        self.assertEqual(normalizer.active_flag("N"), "false")
        self.assertEqual(normalizer.active_flag("unknown"), "")

    def test_amount_formats_and_signs(self):
        cases = {
            "1.247.204,29": "1247204.29",
            "24,376.08": "24376.08",
            "488210,35": "488210.35",
            "72.54": "72.54",
            "1.000": "1000.00",
            "1,000": "1000.00",
            "1.234.567": "1234567.00",
            "1,234,567": "1234567.00",
            "EUR 223.370,86": "223370.86",
            "\u20ac -5.131,24": "-5131.24",
            "EUR (21.218,84)": "-21218.84",
            "(\u20ac21.218,84)": "-21218.84",
            "(21.218,84) EUR": "-21218.84",
            "3216,65-": "-3216.65",
            "USD 3216.65-": "-3216.65",
            "+12.50": "12.50",
            "1 234,56": "1234.56",
            "1\u00a0234,56": "1234.56",
            "0": "0.00",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(normalizer.normalize_amount(raw), expected)

    def test_magnitude_units(self):
        cases = {
            "1,5 Mln": "1500000.00",
            "1.1 mln": "1100000.00",
            "1 milione": "1000000.00",
            "2 milioni": "2000000.00",
            "152.5 mila": "152500.00",
            "\u20ac7.5 mila": "7500.00",
            "EUR 4.5 k": "4500.00",
            "48 K": "48000.00",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(normalizer.normalize_amount(raw), expected)

    def test_monthly_units_only_annualize_renewals(self):
        for raw in ("588.12 mensili", "588,12 mensile", "EUR 588,12 al mese", "588.12 /mese", "\u20ac588,12/mese"):
            with self.subTest(raw=raw):
                self.assertEqual(normalizer.normalize_amount(raw), "588.12")
                self.assertEqual(normalizer.normalize_amount(raw, annualize_monthly=True), "7057.44")
        self.assertEqual(normalizer.normalize_amount("1,5 k /mese", True), "18000.00")

    def test_unknown_text_and_malformed_amounts_are_blank(self):
        for raw in (None, "", "N/A 2025", "invoice 123", "12 xyz", "12.34.56", "1,23,456", "1 23", "--12", "(-12)", "NaN", "Infinity", "10%", "9" * 100):
            with self.subTest(raw=raw):
                self.assertEqual(normalizer.normalize_amount(raw), "")

    def test_money_rounds_half_up(self):
        self.assertEqual(normalizer.normalize_amount("1234.565"), "1234.57")
        self.assertEqual(normalizer.normalize_amount("-1234.565"), "-1234.57")
        self.assertEqual(normalizer.format_decimal(Decimal("NaN")), "")

    def test_date_formats(self):
        cases = {
            "15/01/2025": "2025-01-15",
            "15/01/2025 14:05:06": "2025-01-15T14:05:06",
            "15/01/2025 14:05": "2025-01-15T14:05:00",
            "2025-01-15": "2025-01-15",
            "2025-01-15 14:05:06": "2025-01-15T14:05:06",
            "15-01-2025": "2025-01-15",
            "19/9/2015": "2015-09-19",
            "04.09.18": "2018-09-04",
            "01.01.49": "2049-01-01",
            "01.01.50": "1950-01-01",
            "01.01.99": "1999-01-01",
            "01.01.1950": "1950-01-01",
            "45672": "2025-01-15",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(normalizer.parse_date(raw), expected)

    def test_invalid_dates_and_serials_are_blank(self):
        for raw in (None, "", "0", "10000", "999999999999999999999", "31/02/2025", "31.02.50", "unknown"):
            with self.subTest(raw=raw):
                self.assertEqual(normalizer.parse_date(raw), "")

    def test_observed_sku_aliases(self):
        for raw in ("BF-49711", "BF49711", "bf49711", "BF.49711", "BF 49711", "art. 49711", "49711"):
            with self.subTest(raw=raw):
                self.assertEqual(normalizer.normalize_sku(raw), "BF-49711")
        self.assertEqual(normalizer.normalize_sku("BF.42"), "BF-00042")
        self.assertEqual(normalizer.normalize_sku("art. 52614"), "BF-52614")
        self.assertEqual(normalizer.normalize_sku("00123"), "BF-00123")
        for raw in ("SKU-ABC", "Other product", "BF-123456", "123456"):
            with self.subTest(raw=raw):
                self.assertEqual(normalizer.normalize_sku(raw), raw)
        self.assertEqual(normalizer.normalize_sku(None), "")

    def test_ticket_stage_aliases(self):
        groups = {
            "aperto": ("Aperto", "Nuovo", " NUOVO "),
            "in_lavorazione": ("In lavorazione", "Lavorazione", "IN  LAVORAZIONE"),
            "in_attesa_del_cliente": ("In attesa", "Attesa cliente", "In attesa cliente", "In attesa del cliente"),
            "chiuso": ("Chiuso", "Risolto", "RISOLTO"),
        }
        for expected, labels in groups.items():
            for raw in labels:
                with self.subTest(raw=raw):
                    self.assertEqual(normalizer.normalize_ticket_stage(raw), expected)
        for raw in (None, "", "Da verificare"):
            self.assertEqual(normalizer.normalize_ticket_stage(raw), "")

    def test_ticket_priority_aliases(self):
        groups = {
            "LOW": ("Bassa", "low", "1 - Bassa"),
            "MEDIUM": ("Media", "medium", "2 - Media", "Normale"),
            "HIGH": ("Alta", "high", "3 - Alta"),
            "URGENT": ("Urgente", "urgent", "4 - Urgente", "URGENTE!!"),
        }
        for expected, labels in groups.items():
            for raw in labels:
                with self.subTest(raw=raw):
                    self.assertEqual(normalizer.normalize_ticket_priority(raw), expected)
        for raw in (None, "", "Da verificare"):
            self.assertEqual(normalizer.normalize_ticket_priority(raw), "")

    def test_ticket_export_preserves_raw_labels_text_and_dates(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            output = Path(directory) / "output"
            source.mkdir()
            header = EXPECTED_HEADERS["ticket.csv"]
            row = dict.fromkeys(header, "")
            row.update(id_ticket="42", stato=" Risolto ", priorita="URGENTE!!", cancellato="S\u00ec", id_utente="7", aperto_il="15/01/2025 14:05:06", descrizione='Caff\u00e8; prima riga\r\nseconda "riga"')
            with (source / "ticket.csv").open("w", encoding=LEGACY_ENCODING, newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=header, delimiter=";")
                writer.writeheader()
                writer.writerow(row)
            with patch.object(normalizer, "SOURCE_DIR", source), patch.object(normalizer, "OUTPUT_DIR", output):
                normalizer.normalize_tickets({"7"})
            with (output / "tickets.normalized.csv").open(encoding="utf-8", newline="") as handle:
                result = next(csv.DictReader(handle))
            self.assertEqual(result["hs_pipeline_stage"], "chiuso")
            self.assertEqual(result["status"], "chiuso")
            self.assertEqual(result["hs_pipeline"], "assistenza")
            self.assertEqual(result["hs_ticket_priority"], "URGENT")
            self.assertEqual(result["raw_stato"], "Risolto")
            self.assertEqual(result["raw_priorita"], "URGENTE!!")
            self.assertEqual(result["is_deleted"], "true")
            self.assertEqual(result["content"], row["descrizione"])
            self.assertEqual(result["createdate"], "2025-01-15T14:05:06")
            self.assertEqual(result["assegnatario"], "7")

    def test_unparseable_deal_has_no_currency(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            source.mkdir()
            header = EXPECTED_HEADERS["opportunita.csv"]
            row = dict.fromkeys(header, "")
            row.update(id_opportunita="1", importo="N/A 2025", valuta="USD")
            with (source / "opportunita.csv").open("w", encoding=LEGACY_ENCODING, newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=header, delimiter=";")
                writer.writeheader()
                writer.writerow(row)
            with patch.object(normalizer, "SOURCE_DIR", source), patch.object(normalizer, "OUTPUT_DIR", Path(directory) / "output"):
                normalizer.normalize_deals(set())
                with (normalizer.OUTPUT_DIR / "deals.normalized.csv").open(encoding="utf-8", newline="") as handle:
                    result = next(csv.DictReader(handle))
            self.assertEqual(result["amount"], "")
            self.assertEqual(result["deal_currency_code"], "")
            self.assertEqual(result["raw_importo"], "N/A 2025")
            self.assertEqual(result["raw_valuta"], "USD")


if __name__ == "__main__":
    unittest.main()
