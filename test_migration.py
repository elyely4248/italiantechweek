import sys
import types
import unittest
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import migration


class MigrationHelpersTests(unittest.TestCase):
    def test_invalid_new_reference_does_not_erase_older_valid_reference(self):
        rows = [
            {"email": "a@example.it", "firstname": "Vecchio", "_company_legacy": "42", "_modified": "2025-01-01", "_order": 0},
            {"email": "a@example.it", "firstname": "Nuovo", "_company_legacy": "", "_modified": "2026-01-01", "_order": 1},
        ]
        [(merged, _)] = migration._dedupe_by(rows, "email", ("email", "firstname", "_company_legacy"))
        self.assertEqual(merged["firstname"], "Nuovo")
        self.assertEqual(merged["_company_legacy"], "42")

    def test_company_dedupe_is_transitive_across_domain_and_vat(self):
        rows = [
            {"id_legacy": "1", "domain": "a.it", "partita_iva": "11111111111", "_modified": "2024", "_order": 0},
            {"id_legacy": "2", "domain": "a.it", "partita_iva": "22222222222", "_modified": "2025", "_order": 1},
            {"id_legacy": "3", "domain": "b.it", "partita_iva": "22222222222", "_modified": "2026", "_order": 2},
        ]
        groups = migration._dedupe_companies(rows)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0][0]["id_legacy"], "3")
        self.assertEqual(groups[0][0]["hs_additional_domains"], "a.it")

    def test_line_total_uses_decimal_and_rounds_each_line_half_up(self):
        self.assertEqual(migration._line_total("3.00", "0.335", "0.00"), Decimal("1.01"))
        self.assertEqual(migration._line_total("2.00", "10.00", "12.50"), Decimal("17.50"))


class MigrationLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_migration_flag_is_reset_when_download_fails(self):
        fake_automations = types.SimpleNamespace(_migration_active=False)
        with patch.dict(sys.modules, {"automations": fake_automations}), patch.object(
            migration, "_download_export", AsyncMock(side_effect=RuntimeError("network"))
        ):
            with self.assertRaises(RuntimeError):
                await migration.run_migration("https://example.test/export.zip")
        self.assertFalse(fake_automations._migration_active)


if __name__ == "__main__":
    unittest.main()
