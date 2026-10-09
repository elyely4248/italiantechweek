# Zero to HubSpot: the brief

## The challenge

Every day thousands of companies still keep their clients, contacts and deals in CRMs that show
their age. In this final you build a new one, starting from an empty repo: a CRM with an **API
compatible with HubSpot CRM** and an interface to use it. Then the touch that makes it of its time: an **AI assistant** in chat
that works inside the CRM, its only AI feature and a real part of the challenge. Write it with
whatever tools you like.

Your clone already has its first client. Brambilla Forniture S.p.A., a distributor of industrial
supplies, has fifteen years of customers, contacts, deals, quotes, support tickets and activities
inside Sinergia 4, an on-premise CRM that is being switched off. Your CRM has to take in its data
without losing a single record, and the next day Brambilla works on it.

Brambilla has written down what it needs: thirteen requests, from the data to bring over to the
rules, the numbers, how the CRM has to work, and the assistant its sales reps talk to.
**Get as much as you can working**: there is more work here than we expect anyone to finish in
five hours, what counts is what works at 15:30, and choosing where to start is part of the race.

We test your CRM from outside, against your deploy, checking:

- that Brambilla's requests are met, looking at the outcomes on the data and on the CRM's
  behavior;
- that the assistant handles well requests nobody has seen before;
- that the CRM behaves as HubSpot's documentation describes;
- that it holds up as a system a company actually works on.

## Language

This brief, the FAQ and the choice sheet template are in English, and they are the reference.
Brambilla is an Italian company, so what comes from Brambilla is in Italian: the requests
(`legacy/RICHIESTE.md`), the Sinergia export, and the messages its sales reps send to the
assistant, which replies in Italian. The names in the requests' appendix are used by the tests
verbatim, in Italian. The platform's brief carries a translation of the requests.

## The day

| Time | What happens |
|---|---|
| 10:30 | Start. The full brief, the materials and the form check open. The repo is empty. |
| 15:00 | The choice sheet opens, until 15:30. |
| 15:30 | Deploys freeze and the evaluation starts. |
| Award ceremony | The ranking is revealed. |

Times follow the platform's clock: that is where the materials open and the deploys freeze.

## What you get

From 10:30, among the platform's materials. `starter.zip` holds all of them, in the folders
written below.

- **The Sinergia export** (`legacy/export.zip`): nine CSV files, companies, contacts, deals, quote
  lines, price list, tickets, activities, users and stage history, about 675,000 rows.
- **Brambilla's requests** (`legacy/RICHIESTE.md`): what it needs, in its own words, with an
  appendix of the names the tests use. They don't say how to handle every shape the data is
  written in: that you work out from the data, which always contains what you need.
- **The assistant's examples** (`assistant/examples.md`): two example requests, as a Brambilla
  sales rep writes them.
- **The choice sheet template** (`CHOICE-SHEET.md`) and the **FAQ** (`FAQ.md`).
- **HubSpot's public documentation**, version 2026-09, the one the site shows by default. Every
  reference page contains the OpenAPI of its endpoint. Index for agents:
  https://developers.hubspot.com/docs/llms.txt. The links picked module by module are at the
  bottom.

From the platform, on the Deploy page, you also get your CRM's **token** and the assistant's
**model key**.

The export you get is a full Sinergia export. In the evaluation your CRM migrates **another
export from the same Sinergia**: same rules, same size, different data.

## The technical rules

1. **Authentication.** Every test call carries `Authorization: Bearer <your token>`, like a
   HubSpot private app. We give you the token: put it in a variable of your service. Without a
   valid token, `401`. Only `GET /health` and the links to export files answer without a token,
   like HubSpot's signed links: the file at the address in `result` of a completed export's
   status downloads without a token (a relative address resolves on your CRM).
2. **`POST /__reset`.** Brings the CRM back to the state of a freshly created HubSpot account: no
   records, the default properties, pipelines and association types the documentation describes,
   and the rate-limit counters back to zero. The properties, pipelines and list Brambilla asks
   for are created by the migration, not by the reset. Replies `204`. Requires the token. The
   tests call it before every isolated test, hundreds of times in a round: it has to be fast.
3. **`POST /__migrate`.** Body `{"export_url": "https://..."}`. The CRM downloads the archive at
   that address, migrates it according to Brambilla's requests and replies `204` when it is done:
   from then on the data, the figures of R8 and the list of R9 can be read through the API. It has
   **5 minutes**: Railway closes with a `502` a request that gets no response for 5 minutes. On
   Railway the same migration can take twice as long from one run to the next: measure it on your
   deploy with the form check and keep a wide margin.
   Requires the token. The tests call it once, right after a `POST /__reset`. The
   address can be `http` or `https` and have parameters: download what is there, without relying
   on the file name. Activities become HubSpot activity objects (notes, calls, emails, meetings),
   the price list becomes products, quote lines become line items, tickets become tickets, all
   with their associations.
4. **`GET /health`.** No token. Replies `200` with
   `{"status": "ok", "version": "2026-09", "ui": {"contacts": "/contacts", ...}}`: in `ui`, every
   module that has a page, with its route.
5. **The deploy.** The CRM runs in a Railway project of ours, one per finalist: at the kickoff,
   at 09:45, you get an email inviting you to it as an Editor. Deploy there and register the
   service's https address on the platform. Keep the project's region, EU West: the tests run
   from Europe and make thousands of calls, and a CRM in the US answers each one 100 ms later.
   You can change the address until 15:30; after that we evaluate the CRM that answers there.
6. **Timeouts.** Every test call waits at most 10 seconds, except `/__migrate` (5 minutes) and
   `/__agente` (60 seconds): past that, it counts as failed.
7. **`POST /__agente`.** The assistant, with the contract described below.

## How we read your CRM

The checks read and write the CRM only through its API, like a HubSpot integration: what they
score is what the API shows.

- **Automations** (R10, R11, R12) can work asynchronously: the checks wait at most 15 seconds for
  their effect to show.
- **Dates and numbers**: a date or an instant is read as ISO 8601 or as milliseconds, a number as
  a string or as a number.

## What holding up means

A company will be working on it from the next day. At the end of the race, on the CRM after the
migration of the hidden export, we check that:

- **data survives**: we restart the service, and what was there before is still there;
- **concurrent writes don't get lost**: parallel updates of the same record, parallel creates,
  parallel upserts on the same email produce no duplicates and lose no values;
- **pagination and search hold up on the migrated volume**: every record exactly once, exact
  totals. As in HubSpot, a record just written can show up in search with a delay: on the
  migrated volume the checks wait at most 15 seconds, in the conformity tests 3;
- **reads are fast under load**: with 20 clients in parallel, 95% of single-record reads answer
  in under **1 second** and none answers with a `5xx` error.

All of these checks work on the migrated CRM: durability needs a successful migration.

## The assistant (R13)

What the assistant has to be able to do, Brambilla says in R13. Here is how we talk to it and how
we score it.

### The model

The assistant uses **only** `openai/gpt-6-luna` on OpenRouter, with the key you find on the
platform's Deploy page and put in the `OPENROUTER_API_KEY` variable of your service. The key works
only with that model and has a spending cap of **$10** for the whole day, development and
evaluation together: if you use it up while trying things out, in the evaluation the assistant
doesn't answer.
Tools, prompt and logic around the model are yours.

### The contract

`POST /__agente`, with the token like every other call. No state is kept, neither on your side
nor ours: every turn sends the whole conversation so far.

```json
{
  "context": {
    "now": "2026-12-02T10:00:00+01:00",
    "user": "anna.sala@brambillaforniture.it"
  },
  "messages": [
    {"role": "user", "content": "...",
     "attachments": [{"name": "allegato.csv", "content_type": "text/csv", "content": "..."}]},
    {"role": "assistant", "content": "..."},
    {"role": "user", "content": "..."}
  ]
}
```

It replies `200` with `{"reply": "<text in Italian>"}`.

- **60 seconds per turn**: past that, the turn counts as an empty reply.
- `context.now` is the moment of the request: "today", "six months from now" count from there,
  not from the server's clock.
- `context.user` is who is writing, an active user from `utenti.csv`: "my customers" are theirs.
- Attachments are text only: a CSV in the `content` field, with Sinergia's `;` separator.
- Conversations arrive **4 at a time**, each on its own records.

### How we score it

After the suite, on the same frozen deploy.

1. **The scenario.** `POST /__reset`, then `POST /__migrate` of another Sinergia export, small and
   clean, kept for the evaluation: the same format of the nine CSV files, a few hundred rows, no
   duplicates and none of the shapes the requests don't describe. We check that the scenario
   arrived, record by `id_legacy`: a request that touches records that are missing is worth
   zero.
2. **The requests.** About 40, never seen before, in the voice of Brambilla's employees, each on
   its own records. A Brambilla colleague, simulated by a model of ours, opens the conversation;
   after each reply of the assistant they decide whether to answer or close. They know what their
   request needs and say it only when asked. They close when the assistant says it is done, says
   it can't, or asks something they don't know.
3. **The circuit breakers.** Each request has a cap on turns; each turn has 60 seconds; all the
   requests for your CRM together have **30 minutes**. When a breaker trips, we score the CRM as
   it is at that moment.
4. **The check.** After each conversation we read the request's records through the API and
   compare them with the expected outcome. At the end of the round we check that the records no
   request was supposed to touch haven't changed. What the CRM's automations do (R10, R11, R12)
   doesn't count as damage.

The assistant's points split like this:

| Part | Share | What counts |
|---|---|---|
| Operations | 2/3 | The state of the CRM after the conversation: each request is worth the share of its checks that pass. A record of the request changed where it shouldn't have been sends it to zero. |
| Questions | 23% | The expected facts (numbers, names, emails) in the reply. Numbers count in Italian or English form: `12.345,67` and `12345.67`. |
| Quality | 10% | A judge model reads the conversation: clarity, no made-up facts, saying when it can't. |

A request that must not be carried out as it stands counts among operations and questions: the
CRM stays as it was, and the reply says why. The assistant can ask questions freely; asking for
something already written in the request weighs only on quality.

### The examples

In `assistant/examples.md` you find two example requests, with who writes them and when
(`context`). The evaluation's requests are different ones, on the scenario above.

## What counts

The test score is out of 100: **50 for requests R1-R12**, **30 for the assistant** (R13),
**10 for conformity** to HubSpot's API, **10 for durability**.
Each part is worth the share of its checks you pass, partial credit included.

| Request | Points |
|---|---|
| R1 The companies | 4 |
| R2 The people | 4 |
| R3 The deals | 6 |
| R4 Price list and quotes | 4 |
| R5 Support | 4 |
| R6 The history | 4 |
| R7 The VAT number | 4 |
| R8 2025 revenue and the class | 6 |
| R9 The dormant customers | 4 |
| R10 Deal won, supply kickoff | 4 |
| R11 Deal lost, call back | 3 |
| R12 The contact finds its company | 3 |

The data requests (R1-R9) are checked after `POST /__migrate` of the hidden export, reading the
whole CRM through the API and comparing it record by record with what Brambilla expects. In
requests R1-R6 half the points go to records and fields, half to the cases the requests don't
describe and that you discover in the data. The behavior requests create and edit records through
the API after the migration and watch what the CRM does: R10 and R11 only that way, R7 and R12 on
the migrated data too.

Conformity covers the modules of the links at the bottom of this brief.

**The interface, in English, has no tests**: the jury judges it for the top 6, with the company
page, the deals board, the dormant customers list and the tickets.

## How scoring works

- **During the race** you run a **form check** of your deploy from the platform whenever you
  like: it responds, `/health` is right, the token is accepted, `/__reset` and `/__migrate`
  respond as they should, `/__agente` replies in the contract's format, and a few example tests
  pass. For every check that fails you see what we called and what you answered. Some things,
  like the migration's timing, only show on the deploy: it pays to have one early.
- **From 15:00 to 15:30** you fill in the **choice sheet** on the platform: what you found in the
  data that the requests didn't say, how you handled it and why, what you didn't do. The template
  is in `CHOICE-SHEET.md`.
- **At 15:30** deploys freeze and the full suite (conformity, migration of the hidden export,
  requests, durability) runs on every CRM, once. Then, on the same deploy, the assistant's
  evaluation.
- The ranking is revealed at the award ceremony.
- **The top 6** by test score are judged by the jury on three axes, each
  out of 10: the interface, the choices on the cases the requests don't describe (the choice
  sheet), the product. Their final score is 80% tests and 20% jury: the tests are worth 80 points
  out of 100, the jury 20. The top 6 stay the top 6: the jury reorders them.
- **The top 3** of the final ranking present their CRM at the award ceremony, in an 8-minute pitch.

## The rules

- One per person.
- You start from an empty repo at 10:30. The code is written during the race, by you and your
  agents; libraries and frameworks are fine.
- From 15:30 the deploy is not touched: no push, redeploy or changed variables until the
  evaluation is over.
- Any tools you like to write the code; inside the CRM the only AI feature is the assistant, with
  the model and the key we give you.
- No HubSpot name or logo in your app.

## Links by module

| Module | API | How to use it in the UI |
|---|---|---|
| Contacts | [guide](https://developers.hubspot.com/docs/api-reference/latest/crm/objects/contacts/guide) | [record list](https://knowledge.hubspot.com/records/view-and-filter-records), [record page](https://knowledge.hubspot.com/records/work-with-records), [merge](https://knowledge.hubspot.com/records/merge-records) |
| Companies | [guide](https://developers.hubspot.com/docs/api-reference/latest/crm/objects/companies/guide) | [domains](https://knowledge.hubspot.com/records/add-multiple-domain-names-to-a-company-record), [deduplication](https://knowledge.hubspot.com/records/deduplication-of-records) |
| Deals | [guide](https://developers.hubspot.com/docs/api-reference/latest/crm/objects/deals/guide) | [board](https://knowledge.hubspot.com/records/manage-records-in-board-view), [default properties](https://knowledge.hubspot.com/properties/hubspots-default-deal-properties) |
| Pipelines | [guide](https://developers.hubspot.com/docs/api-reference/latest/crm/pipelines/guide) | [pipelines and stages](https://knowledge.hubspot.com/object-settings/set-up-and-customize-pipelines) |
| Associations | [records](https://developers.hubspot.com/docs/api-reference/latest/crm/associations/associate-records/guide), [schema](https://developers.hubspot.com/docs/api-reference/latest/crm/associations/associations-schema/guide) | [associate records](https://knowledge.hubspot.com/records/associate-records), [labels](https://knowledge.hubspot.com/object-settings/create-and-use-association-labels) |
| Properties | [guide](https://developers.hubspot.com/docs/api-reference/latest/crm/properties/guide) | [create and edit](https://knowledge.hubspot.com/properties/create-and-edit-properties), [field types](https://knowledge.hubspot.com/properties/property-field-types-in-hubspot), [groups](https://knowledge.hubspot.com/properties/organize-and-export-properties) |
| Search | [guide](https://developers.hubspot.com/docs/api-reference/latest/crm/search-the-crm) | [search](https://knowledge.hubspot.com/records/search-your-crm), [filters](https://knowledge.hubspot.com/records/view-and-filter-records) |
| Activities | [notes](https://developers.hubspot.com/docs/api-reference/latest/crm/activities/notes/guide), [calls](https://developers.hubspot.com/docs/api-reference/latest/crm/activities/calls/guide), [emails](https://developers.hubspot.com/docs/api-reference/latest/crm/activities/emails/guide), [meetings](https://developers.hubspot.com/docs/api-reference/latest/crm/activities/meetings/guide), [tasks](https://developers.hubspot.com/docs/api-reference/latest/crm/activities/tasks/guide) | [log activities](https://knowledge.hubspot.com/records/manually-log-activities-on-records), [timeline](https://knowledge.hubspot.com/records/filter-activities-on-a-record-timeline) |
| Tickets | [guide](https://developers.hubspot.com/docs/api-reference/latest/crm/objects/tickets/guide) | [manage tickets](https://knowledge.hubspot.com/help-desk/manage-tickets-in-help-desk), [default properties](https://knowledge.hubspot.com/properties/hubspots-default-ticket-properties) |
| Lists | [guide](https://developers.hubspot.com/docs/api-reference/latest/crm/lists/guide), [filters](https://developers.hubspot.com/docs/api-reference/latest/crm/lists/filters/guide) | [create a list](https://knowledge.hubspot.com/segments/create-active-or-static-lists) |
| Products | [products](https://developers.hubspot.com/docs/api-reference/latest/crm/objects/products/guide), [line items](https://developers.hubspot.com/docs/api-reference/latest/crm/objects/line-items/guide), [quotes](https://developers.hubspot.com/docs/api-reference/latest/crm/objects/quotes/guide) | [products](https://knowledge.hubspot.com/products/create-and-manage-products), [line items in deals](https://knowledge.hubspot.com/records/use-line-items-with-deals) |
| Import and export | [import](https://developers.hubspot.com/docs/api-reference/latest/crm/imports/guide), [export](https://developers.hubspot.com/docs/api-reference/latest/crm/exports/guide) | [import](https://knowledge.hubspot.com/import-and-export/import-objects), [export](https://knowledge.hubspot.com/import-and-export/export-records) |
| Errors | [error handling](https://developers.hubspot.com/docs/api-reference/latest/error-handling) | |
