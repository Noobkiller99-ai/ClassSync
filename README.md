# Class Sync

**Sync your SPJIMR academic schedule directly to Google Calendar — automatically.**

Class Sync supports two schedule sources:
- **TCS iON** (`tcs.ion.indiaisb.com`) — the traditional SPJIMR timetable portal
- **Salesforce SPP** (`spp.spjimr.org`) — the newer SPJIMR student platform

---

## How It Works

1. **Choose your source** — TCS iON or Salesforce SPP
2. **Sign in** with your SPJIMR email and password
3. **Preview your timetable** — next 2 weeks, grouped by day, colour-coded by subject
4. **Click "Sync to Google Calendar"** — sign in with Google if prompted
5. **Done** — all sessions appear in a dedicated calendar with 15-min reminders

> Each browser session is fully isolated. Multiple students can use the same server simultaneously.  
> Credentials are **never stored** on our server — only used transiently to fetch your timetable.

---

## Google Calendar Behaviour

| Source | Calendar Name | Colour |
|---|---|---|
| TCS iON | `SPJIMR Timetable` | Default Google blue |
| Salesforce SPP | `SPJIMR Timetable` | SPJIMR Purple `#531f75` |

- Events are **upserted** — re-syncing updates existing events rather than creating duplicates
- Mandatory sessions are flagged 🔴 in the title and coloured red in Google Calendar
- Exams / evaluations are flagged 📝 and coloured orange

---

## Run Locally

```powershell
# 1. Install dependencies
python -m pip install -r requirements.txt

# 2. Copy and fill in the environment file
Copy-Item .env.example .env
# Edit .env: add GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REDIRECT_URI

# 3. Run the app
python -m flask --app class_sync.web run --port 5002

# Optional: run in sample mode (no TCS credentials needed — for UI testing)
$env:TCS_SAMPLE_MODE='1'
python -m flask --app class_sync.web run --port 5002
```

---

## Google OAuth Setup

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a project → Enable the **Google Calendar API**
3. **APIs & Services → Credentials → Create OAuth 2.0 Client ID**
   - Application type: **Web application**
   - Authorised redirect URI: `http://127.0.0.1:5002/google/callback`
4. Copy the credentials into `.env`

```env
GOOGLE_CLIENT_ID=your-client-id.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=your-secret
GOOGLE_REDIRECT_URI=http://127.0.0.1:5002/google/callback
```

> If `GOOGLE_CLIENT_ID` is not set, the app runs in **dry-run mode** — sync is simulated and no real calendar events are created.

---

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `SECRET_KEY` | ✅ | Flask session secret — any long random string |
| `CREDENTIAL_ENCRYPTION_KEY` | ☐ | Fernet key for encrypting stored credentials (derived from `SECRET_KEY` if blank) |
| `GOOGLE_CLIENT_ID` | ✅ | OAuth 2.0 client ID from Google Cloud Console |
| `GOOGLE_CLIENT_SECRET` | ✅ | OAuth 2.0 client secret |
| `GOOGLE_REDIRECT_URI` | ✅ | Must match what's registered in Google Cloud Console |
| `ADMIN_TOKEN` | ☐ | Enables the admin refresh endpoint |
| `TCS_SAMPLE_MODE` | ☐ | Set to `1` to use built-in sample timetable data (no TCS login needed) |
| `DATABASE_URL` | ☐ | PostgreSQL URL for production (defaults to SQLite locally) |

---

## Architecture

```
Browser
  │
  ├─ GET /          → index route (source-aware: TCS or SPP)
  ├─ POST /tcs/login  → TcsClient.fetch_timetable()
  ├─ POST /spp/login  → SppClient.fetch_timetable()
  ├─ POST /sync     → GoogleCalendarClient.sync()
  └─ GET  /google/callback → OAuth token exchange
         │
         ├── class_sync/tcs.py          TCS iON scraper & parser
         ├── class_sync/spp.py          Salesforce SPP client (Apex REST)
         ├── class_sync/google_calendar.py   Google Calendar API wrapper
         ├── class_sync/models.py        TimetableEvent data class
         ├── class_sync/store.py         SQLite / PostgreSQL persistence
         ├── class_sync/web.py           Flask routes & session management
         └── class_sync/security.py      Fernet encryption helpers
```

**Key design decisions:**
- **No plaintext credentials** — TCS passwords are encrypted with Fernet; SPP passwords are never stored at all
- **Session isolation** — each browser gets a UUID token; all DB records are scoped to it
- **GET-only Salesforce API calls** — Salesforce LWR's POST endpoints require a browser-signed CSRF JWT; we use only `cacheable=true` GET endpoints which work without it
- **Upsert semantics** — Google Calendar events use `extendedProperties.private.classSyncUid` as the stable identity key

---

## Tests

```powershell
python -m pytest tests/ -q
```

64 tests cover the TCS parser, Google Calendar upsert logic, web routes, and session management.

---

## Deployment

The app is configured for **Vercel** (see `vercel.json`) with PostgreSQL for persistent storage.  
It also runs on any WSGI host (Gunicorn, Heroku, Railway) with the provided `Procfile`.
