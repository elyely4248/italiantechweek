import os
import sys
import types
import unittest
from unittest.mock import AsyncMock, patch

import httpx

import automations
import main


class FakeDB:
    def __init__(self):
        self.records = {kind: {} for kind in main.KNOWN_OBJECT_TYPES}
        self.archived = {kind: set() for kind in main.KNOWN_OBJECT_TYPES}
        self.associations = set()
        self.claims = set()
        self.next_id = 1
        self.inferred_contact_company_kwargs = None

    async def init_db(self): pass
    async def reset_db(self):
        self.records = {kind: {} for kind in main.KNOWN_OBJECT_TYPES}
        self.archived = {kind: set() for kind in main.KNOWN_OBJECT_TYPES}

    def result(self, kind, record_id, properties=None):
        values = dict(self.records[kind][record_id])
        if properties is not None:
            requested = dict.fromkeys(properties)
            requested.update(dict.fromkeys(("hs_object_id", "createdate", "lastmodifieddate")))
            values = {key: values.get(key) for key in requested}
        return {"id": str(record_id), "properties": values,
                "createdAt": "2026-01-01T00:00:00Z", "updatedAt": "2026-01-01T00:00:00Z",
                "archived": record_id in self.archived[kind]}

    async def create_object(self, kind, properties, **kwargs):
        record_id, self.next_id = self.next_id, self.next_id + 1
        self.records[kind][record_id] = {key: str(value) for key, value in properties.items()}
        return self.result(kind, record_id)

    async def get_object(self, kind, record_id, properties=None, archived=False):
        if record_id not in self.records[kind]:
            return None
        if (record_id in self.archived[kind]) != bool(archived):
            return None
        return self.result(kind, record_id, properties)

    async def update_object(self, kind, record_id, properties):
        self.records[kind][record_id].update({key: str(value) for key, value in properties.items()})
        return self.result(kind, record_id)

    async def archive_object(self, kind, record_id):
        if record_id not in self.records[kind] or record_id in self.archived[kind]:
            return False
        self.archived[kind].add(record_id)
        return True

    async def list_objects(self, kind, limit=10, after=None, **kwargs):
        archived = bool(kwargs.get("archived", False))
        ids = sorted(
            i for i in self.records[kind]
            if (after is None or i > after) and ((i in self.archived[kind]) == archived)
        )[:limit]
        return {"results": [self.result(kind, i) for i in ids]}

    async def search_objects(self, kind, filter_groups, **kwargs):
        if not filter_groups:
            return await self.list_objects(kind, limit=kwargs.get("limit", 10), after=kwargs.get("after"))
        found = []
        for record_id, props in self.records[kind].items():
            for group in filter_groups:
                filters = group.get("filters", [])
                if all(
                    props.get(item["propertyName"], "").lower() == str(item.get("value", "")).lower()
                    if item["operator"] == "EQ"
                    else str(item.get("value", "")).lower() in props.get(item["propertyName"], "").lower()
                    for item in filters
                ):
                    found.append(self.result(kind, record_id)); break
        return {"total": len(found), "results": found[:kwargs.get("limit", 10)]}

    async def create_association(self, ft, fid, tt, tid, type_id=0, **kwargs):
        self.associations.add((ft, int(fid), tt, int(tid), int(type_id)))

    async def delete_association(self, ft, fid, tt, tid, type_id=0):
        self.associations.discard((ft, int(fid), tt, int(tid), int(type_id))); return True

    async def get_associations(self, ft, fid, tt):
        return [{"id": str(tid), "type": f"{ft}_to_{tt}"} for s, sid, t, tid, _ in self.associations
                if s == ft and sid == int(fid) and t == tt]

    async def run_automation_once(self, object_type, object_id, key, operation):
        claim = (object_type, object_id, key)
        if claim in self.claims: return False
        self.claims.add(claim)
        try: await operation(object())
        except Exception:
            self.claims.remove(claim); raise
        return True

    async def list_pipelines(self, kind): return []
    async def get_pipeline(self, kind, pipeline_id): return None
    async def create_pipeline(self, *args): return {"id": args[1]}
    async def list_property_defs(self, kind): return []
    async def get_property_def(self, kind, name): return None
    async def create_property_def(self, kind, name, **kwargs): return {"name": name, **kwargs}
    async def update_property_def(self, kind, name, **kwargs): return {"name": name, **kwargs}
    async def delete_property_def(self, kind, name): return True
    async def create_list(self, name, object_type="companies"): return 1
    async def get_list(self, list_id): return {"id": str(list_id)}
    async def get_list_by_name(self, name, object_type="companies"): return {"id": "1", "name": name}
    async def get_list_members(self, list_id, **kwargs): return {"results": []}
    async def add_to_list(self, list_id, ids): pass
    async def infer_contact_company_associations(self, **kwargs):
        self.inferred_contact_company_kwargs = kwargs
        return {"candidates": 2, "inserted": 0 if kwargs.get("dry_run") else 2, "sources": {"deals": 2}, "dry_run": kwargs.get("dry_run")}


class BaseAsyncTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = FakeDB(); main._db_override = self.db; automations._db_override = self.db
        main._rate_limit_count = 0
        automations._migration_active = False; os.environ["CRM_TOKEN"] = "secret"
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app, raise_app_exceptions=False),
                                        base_url="http://test", headers={"Authorization": "Bearer secret"})

    async def asyncTearDown(self):
        await self.client.aclose(); main._db_override = None; automations._db_override = None
        os.environ.pop("CRM_TOKEN", None)


class ApiTests(BaseAsyncTest):
    async def test_auth_health_and_real_ui(self):
        health = await self.client.get("/health", headers={})
        self.assertEqual(health.status_code, 200)
        self.assertEqual(set(health.json()["ui"]), set(main.UI_TYPES))
        root = await self.client.get("/", follow_redirects=False)
        self.assertEqual(root.status_code, 302)
        self.assertEqual(root.headers["location"], "/contacts")
        self.assertEqual(
            (await self.client.get("/healthz", headers={"Authorization": ""})).status_code,
            401,
        )
        for route in health.json()["ui"].values():
            self.assertEqual((await self.client.get(route)).status_code, 200)

    async def test_smart_ui_prioritizes_owned_records(self):
        await self.db.create_object("users", {
            "firstname": "Elena", "lastname": "CRM", "email": "elena@example.test", "active": "true",
        })
        company = await self.db.create_object("companies", {
            "name": "ACME Srl", "domain": "acme.test", "city": "Milano",
            "classe_cliente": "A", "fatturato_2025": "250000",
        })
        deal = await self.db.create_object("deals", {
            "dealname": "Rinnovo ACME", "amount": "350000", "pipeline": "default",
            "dealstage": "contractsent", "closedate": "2026-10-18",
            "commerciale": "elena@example.test",
        })
        ticket = await self.db.create_object("tickets", {
            "subject": "Blocco portale", "hs_ticket_priority": "HIGH",
            "hs_pipeline_stage": "aperto", "createdate": "2026-09-01",
            "assegnatario": "elena@example.test",
        })
        await self.db.create_association("deals", int(deal["id"]), "companies", int(company["id"]))
        await self.db.create_association("tickets", int(ticket["id"]), "companies", int(company["id"]))

        response = await self.client.get("/smart?user=elena@example.test")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Rinnovo ACME", response.text)
        self.assertIn("Blocco portale", response.text)
        self.assertIn("ACME Srl", response.text)
        self.assertIn("commerciale assegnato", response.text)
        self.assertIn("assegnato a te", response.text)

    async def test_no_configured_token_is_401(self):
        os.environ.pop("CRM_TOKEN")
        response = await self.client.get("/crm/v3/objects/contacts")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["category"], "INVALID_AUTHENTICATION")

    async def test_shared_mutation_and_failure_propagation(self):
        with patch("automations.handle_automations", new=AsyncMock()) as hook:
            response = await self.client.post("/crm/v3/objects/contact", json={"properties": {"email": "a@b.it"}})
        self.assertEqual(response.status_code, 201); hook.assert_awaited_once()
        with patch("automations.handle_automations", new=AsyncMock(side_effect=RuntimeError("automation failed"))):
            response = await self.client.post("/crm/v3/objects/deals", json={"properties": {"dealname": "X"}})
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["message"], "automation failed")

    async def test_batch_and_reverse_association(self):
        with patch("automations.handle_automations", new=AsyncMock()) as hook:
            response = await self.client.post("/crm/v3/objects/contacts/batch/create",
                json={"inputs": [{"properties": {}}, {"properties": {}}]})
        self.assertEqual(len(response.json()["results"]), 2); self.assertEqual(hook.await_count, 2)
        await self.client.put("/crm/v3/objects/contacts/10/associations/companies/20/7")
        self.assertIn(("contacts", 10, "companies", 20, 7), self.db.associations)
        self.assertNotIn(("companies", 20, "contacts", 10, 7), self.db.associations)

    async def test_batch_upsert_updates_existing_identity(self):
        with patch("automations.handle_automations", new=AsyncMock()):
            created = await self.client.post(
                "/crm/v3/objects/contacts/batch/upsert",
                json={"inputs": [{
                    "idProperty": "email", "id": "same@example.it",
                    "properties": {"firstname": "Ada"},
                }]},
            )
            updated = await self.client.post(
                "/crm/v3/objects/contacts/batch/upsert",
                json={"inputs": [{
                    "idProperty": "email", "id": "same@example.it",
                    "properties": {"lastname": "Lovelace"},
                }]},
            )
        self.assertEqual(created.status_code, 200)
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(len(self.db.records["contacts"]), 1)
        record = next(iter(self.db.records["contacts"].values()))
        self.assertEqual(record["firstname"], "Ada")
        self.assertEqual(record["lastname"], "Lovelace")

    async def test_reset_clears_durable_automation_claims(self):
        self.db.claims.add(("deals", 1, "r10_supply_ticket"))
        await self.client.get("/crm/v3/objects/contacts")
        self.assertGreater(main._rate_limit_count, 0)
        response = await self.client.post("/__reset")
        self.assertEqual(response.status_code, 204)
        self.assertFalse(self.db.claims)
        self.assertEqual(main._rate_limit_count, 0)

    async def test_contact_company_inference_endpoint_defaults_to_dry_run(self):
        response = await self.client.post("/__infer-contact-company-associations", json={})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["dry_run"])
        self.assertEqual(response.json()["inserted"], 0)
        self.assertEqual(self.db.inferred_contact_company_kwargs, {"dry_run": True})

        response = await self.client.post("/__infer-contact-company-associations", json={"dry_run": False})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["dry_run"])
        self.assertEqual(response.json()["inserted"], 2)

    async def test_error_envelope(self):
        response = await self.client.get("/crm/v3/objects/contacts/999")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["category"], "OBJECT_NOT_FOUND")

    async def test_requested_unknown_properties_are_null(self):
        with patch("automations.handle_automations", new=AsyncMock()):
            created = await self.client.post(
                "/crm/v3/objects/contacts",
                json={"properties": {"email": "ada@example.it"}},
            )
        contact_id = created.json()["id"]
        response = await self.client.get(
            f"/crm/v3/objects/contacts/{contact_id}?properties=email,missing_prop"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["properties"]["email"], "ada@example.it")
        self.assertIsNone(response.json()["properties"]["missing_prop"])

    async def test_single_get_can_return_archived_records(self):
        with patch("automations.handle_automations", new=AsyncMock()):
            created = await self.client.post(
                "/crm/v3/objects/companies",
                json={"properties": {"name": "Archived Spa"}},
            )
        company_id = created.json()["id"]
        self.assertEqual((await self.client.delete(f"/crm/v3/objects/companies/{company_id}")).status_code, 204)
        self.assertEqual((await self.client.get(f"/crm/v3/objects/companies/{company_id}")).status_code, 404)
        response = await self.client.get(f"/crm/v3/objects/companies/{company_id}?archived=true")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["archived"])

    async def test_stub_conformity_endpoints(self):
        labels = await self.client.get("/crm/v3/associations/contacts/companies/labels")
        self.assertEqual(labels.status_code, 200)
        self.assertEqual(labels.json()["results"][0]["category"], "HUBSPOT_DEFINED")
        imported = await self.client.post("/crm/v3/imports", json={"name": "Import test", "files": []})
        self.assertEqual(imported.status_code, 201)
        self.assertEqual(imported.json()["state"], "DONE")
        exported = await self.client.post("/crm/v3/exports", json={"exportType": "VIEW"})
        self.assertEqual(exported.status_code, 202)
        self.assertEqual(exported.json()["state"], "COMPLETE")

    async def test_quotes_are_supported_crm_objects(self):
        with patch("automations.handle_automations", new=AsyncMock()):
            created = await self.client.post(
                "/crm/v3/objects/quotes",
                json={"properties": {"hs_title": "Offerta rinnovo", "amount": 1200}},
            )
        self.assertEqual(created.status_code, 201)
        quote_id = created.json()["id"]
        fetched = await self.client.get(f"/crm/v3/objects/quotes/{quote_id}?properties=hs_title,amount")
        self.assertEqual(fetched.status_code, 200)
        self.assertEqual(fetched.json()["properties"]["hs_title"], "Offerta rinnovo")
        self.assertEqual(fetched.json()["properties"]["amount"], "1200")


class AutomationTests(BaseAsyncTest):
    async def test_r10_once_and_company_copy(self):
        deal = await self.db.create_object("deals", {"dealname": "A", "pipeline": "default",
            "dealstage": "closedwon", "commerciale": "x@y.it"})
        company = await self.db.create_object("companies", {"name": "C"})
        await self.db.create_association("deals", int(deal["id"]), "companies", int(company["id"]))
        await automations.handle_automations("deals", "create", int(deal["id"]), deal["properties"])
        await automations.handle_automations("deals", "update", int(deal["id"]), deal["properties"], {"dealstage": "x"})
        self.assertEqual(len(self.db.records["tickets"]), 1)
        ticket_id = next(iter(self.db.records["tickets"]))
        self.assertIn(("tickets", ticket_id, "companies", int(company["id"]), 0), self.db.associations)

    async def test_r11_clock_once_and_r12(self):
        props = {"dealname": "B", "pipeline": "default", "dealstage": "closedlost"}
        await automations.handle_automations("deals", "create", 42, props, event_time="2026-01-01T12:00:00Z")
        await automations.handle_automations("deals", "update", 42, props, {"dealstage": "x"})
        task = next(iter(self.db.records["tasks"].values()))
        self.assertEqual(task["hs_timestamp"], "2026-06-30T12:00:00Z")
        company = await self.db.create_object("companies", {"domain": "main.it", "hs_additional_domains": "alias.it"})
        await automations.handle_automations("contacts", "create", 30, {"email": "a@alias.it"})
        self.assertIn(("contacts", 30, "companies", int(company["id"]), 0), self.db.associations)

    async def test_rinnovi_won_and_lost_trigger_automations(self):
        won = await self.db.create_object(
            "deals", {"dealname": "Renewal A", "pipeline": "rinnovi", "dealstage": "rinnovato"}
        )
        await automations.handle_automations("deals", "update", int(won["id"]), won["properties"], {"dealstage": "in_trattativa"})
        self.assertEqual(len(self.db.records["tickets"]), 1)
        self.assertIn(("tickets", next(iter(self.db.records["tickets"])), "deals", int(won["id"]), 0), self.db.associations)

        lost = await self.db.create_object(
            "deals", {"dealname": "Renewal B", "pipeline": "rinnovi", "dealstage": "non_rinnovato"}
        )
        await automations.handle_automations(
            "deals", "update", int(lost["id"]), lost["properties"], {"dealstage": "in_trattativa"},
            event_time="1767268800000",
        )
        task = next(reversed(self.db.records["tasks"].values()))
        self.assertEqual(task["hs_timestamp"], "2026-06-30T12:00:00Z")


class AssistantTests(BaseAsyncTest):
    async def test_assistant_adapter_uses_context_clock(self):
        assistant = types.ModuleType("assistant")
        async def handle(body):
            await assistant.create_object("deals", {"dealname": "C", "pipeline": "default", "dealstage": "closedlost"})
            return "Fatto"
        assistant.handle_agent_request = handle
        with patch.dict(sys.modules, {"assistant": assistant}):
            response = await self.client.post("/__agente",
                json={"context": {"now": "2026-02-01T00:00:00Z"}, "messages": []})
        self.assertEqual(response.json(), {"reply": "Fatto"})
        self.assertEqual(next(iter(self.db.records["tasks"].values()))["hs_timestamp"], "2026-07-31T00:00:00Z")


if __name__ == "__main__": unittest.main()
