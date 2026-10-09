# Brambilla CRM

HubSpot-compatible CRM API, Sinergia migration pipeline, CRM automations, and Italian AI assistant.

## Required environment

- `DATABASE_URL`: PostgreSQL connection string.
- `CRM_TOKEN`: bearer token required by every route except `GET /health`.
- `OPENROUTER_API_KEY`: OpenRouter key for `openai/gpt-6-luna`.
- `PORT`: HTTP port, supplied automatically by Railway.

## Local check

```powershell
python -m pip install -r requirements.txt
$env:DATABASE_URL = "postgresql://..."
$env:CRM_TOKEN = "..."
$env:OPENROUTER_API_KEY = "..."
python main.py
```

Open `http://localhost:8080/health`. The module pages listed there require the bearer token, like the API.

Run the local checks with:

```powershell
python -m unittest discover -v
python validate_csv_parser.py
```

## Deploy

This folder is linked to Railway:

- Workspace: `Hackathon`
- Project: `final-elena-strimbeanui-32ab88`
- Environment: `production`
- Service: `web`

Deploy with:

```bash
railway up --service web --environment production
```

Configure PostgreSQL and the three environment variables before deploying. Generate a public URL from the Railway dashboard or with:

```bash
railway domain
```
