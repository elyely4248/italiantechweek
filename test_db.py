import asyncio
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import db


class MigrationTransactionTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_migration_writes_share_connection_and_rollback_on_failure(self):
        events = []

        @asynccontextmanager
        async def transaction():
            events.append("begin")
            try:
                yield
            except BaseException:
                events.append("rollback")
                raise
            else:
                events.append("commit")

        conn = SimpleNamespace(
            transaction=transaction,
            fetch=AsyncMock(return_value=[{"id": 1}]),
            copy_records_to_table=AsyncMock(),
            fetchval=AsyncMock(return_value=1),
            executemany=AsyncMock(),
            execute=AsyncMock(),
        )

        @asynccontextmanager
        async def acquire():
            events.append("acquire")
            yield conn
            events.append("release")

        with patch.object(db, "get_pool", AsyncMock(return_value=SimpleNamespace(acquire=acquire))):
            with self.assertRaises(asyncio.CancelledError):
                async with db.migration_transaction():
                    await db.bulk_insert_objects("contacts", [{"email": "a@example.it"}])
                    await db.bulk_insert_associations([("contacts", 1, "companies", 2, 0)])
                    list_id = await db.create_list("Clienti dormienti")
                    await db.add_to_list(list_id, [2])
                    raise asyncio.CancelledError
        self.assertEqual(events.count("acquire"), 1)
        self.assertEqual(events[-1], "rollback")
        self.assertIsNone(db._transaction_connection.get())
        conn.copy_records_to_table.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
