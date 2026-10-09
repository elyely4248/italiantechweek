# FAQ

Short answers to the usual questions. The brief (`BRIEF.md`) is the reference; for anything else,
ask the staff in the room.

## The format

**What do we build?**
A CRM for Brambilla Forniture, starting from an empty repo: an API compatible with HubSpot CRM,
an interface to use it and an AI assistant that works inside it. The brief is `BRIEF.md`.

**Do we compete in teams?**
Individual builders only, one per person.

**How long does it last?**
The build runs from 10:30 to 15:30: five hours. The day starts with check-in at 09:00 and ends
with the award ceremony. The openings and the freeze follow the platform's clock.

**Can I prepare something beforehand?**
Your environment: editor, agents, a Railway account. The code starts at 10:30, in an empty repo.

**Who competes?**
Those selected in the four qualifying rounds in Milan and the ten highest scores of AI League
Games.

**What do you win?**
The title of Italy's top builder.

## Tools and code

**Can I use AI tools to write the code?**
Yes: editors, agents, models, whatever you usually use, several agents in parallel too.

**Can I start from existing code, a template or an open-source CRM?**
The code is written during the race, by you and your agents. Libraries and frameworks are fine.

**Which language or framework do I have to use?**
Whatever you like. What counts is that the CRM answers at the address you register, with the
brief's contract.

**Can the CRM have AI features?**
One: the chat assistant, with the model and the key we give you.

## Railway and access

**Where does my CRM run?**
In a Railway project of ours, one per finalist. At the kickoff, at 09:45, you get an email
inviting you to it as an Editor, at the address you are on the list with. Deploy there
and register the service's https address on the platform's Deploy page. The project's costs are
on us.

**When can I sign in?**
From the kickoff, at 09:45: first access with the email you are listed with, and the Railway
invite arrives. Your CRM token, the model key and everything of the race open at 10:30, for
everyone at once.

**The Railway invite didn't arrive.**
Look in the spam folder, then ask the staff: they can send it again.

**My email doesn't get in at first access.**
Only the email you are listed with gets in. Ask the staff.

## The day

**Where do we meet?**
OGR Torino, Corso Castelfidardo 22, Turin, inside Wave by Vento. Check-in opens at 09:00.

**What do I bring?**
Your laptop with its charger, your environment ready, and your phone's hotspot as a backup
connection.

**In which language is my CRM's interface?**
English.

**Does the interface count?**
For the jury, which judges it for the top 6: the company page with revenue and class,
the deals board, the dormant customers list, the tickets.

**When do I see the ranking?**
At the award ceremony.

**Who decides who wins?**
The tests pick the top 6. The jury judges them on the interface,
the choice sheet and the product, each out of 10, and weighs 20% of their final score: 80 points
out of 100 from the tests, 20 from the jury. The top 6 stay the top 6: the jury reorders them.
The top 3 of the final ranking present their CRM at the award ceremony, in an 8-minute pitch.

## Credentials

**What is the token for?**
Every test call carries `Authorization: Bearer <token>`. Put it in a variable of your Railway
service and answer `401` to anyone who doesn't carry it. Only `GET /health` and the links to
export files answer without a token, like HubSpot's signed links.

**The token ended up in a commit.**
Generate a new one from the Deploy page, until the freeze. The old one stops working at once:
update the variable on Railway and redeploy.

**Which model does the assistant use, and with how much credit?**
`openai/gpt-6-luna` on OpenRouter, with the `OPENROUTER_API_KEY` key on the Deploy page, which
works only with that model. $10 for the whole day, development and evaluation together: keep a
margin, because a key used up leaves the assistant silent in the evaluation.

**The model key ended up in a commit.**
Tell the staff right away.

## The assistant

**Does the assistant have to remember conversations?**
Every turn sends the whole conversation so far: answer that.

**The data and the assistant's messages are in Italian, and I don't speak it.**
That's the client: Brambilla is an Italian company. Your tools read Italian, the brief translates
the requests, and the names the tests use are in the requests' appendix, to copy verbatim. The
assistant replies in Italian.

## The CRM

**Do I have to implement the whole HubSpot API?**
Get as much as you can working: the conformity tests cover the modules of the links at the bottom
of the brief. Choosing where to start is part of the race.

**Do I need a database?**
That's up to you. What we check is in the brief, under "What holding up means".

**The CRM is down: what happens?**
What counts is the CRM that answers at 15:30: the parts that don't run at the freeze are worth
zero.

## Migration

**How much time does the migration have?**
5 minutes for `POST /__migrate`, on an export the same size as the one you get: Railway closes
with a `502` a request that gets no response for 5 minutes. Measure it on your Railway deploy,
not only on your laptop.

**Can I answer right away and migrate afterwards?**
`204` means the migration is done: the checks read the CRM right after it.

**If the migration fails, what do I lose?**
The data requests and all of durability, which is measured on the CRM after the migration of the
hidden export.

**Is the evaluation's export the same as mine?**
It is another export from the same Sinergia: same rules, same size, different data.

**A request doesn't say how to handle a piece of data. Who tells me?**
The data: it always contains what you need to understand what it has to become. Reading it is
part of the race, so the staff leaves it to you; what you decide goes in the choice sheet.

## Checks

**Can I run the tests myself?**
The form check runs a few example tests on your deploy, and the full suite runs after the freeze.
The checks you need while you work, you write yourself.

**What is the form check?**
A quick round on your deploy from the platform: `/health`, the token, `/__reset`, `/__migrate`,
`/__agente`, a few example tests. For what's off it shows the call and the response. It calls
`POST /__reset` and `POST /__migrate`, so the data on your deploy is lost, and sends a short
request to `POST /__agente`, a call to the model on your key.

**The form check is all green: am I fine?**
On form, yes. Brambilla's requests are scored after the race, on another export.

## At 15:30

**What happens at the freeze?**
Deploys freeze: the address can't be changed any more, and the CRM that answers there is the one
we evaluate. The full suite runs on every CRM, once; then the assistant gets requests nobody has
seen before.

**Can I deploy after 15:30?**
The freeze covers the deploy, not only the address: no push, redeploy or changed variables until
the evaluation is over.

**Do I hand in my code?**
We evaluate the CRM that answers at your address at 15:30.

**What is the choice sheet?**
From 15:00 to 15:30 you write on the platform's Choices page what you found in the data that the
requests didn't say, how you handled it and why, what you didn't do. The jury reads it if you
make the top 6.
