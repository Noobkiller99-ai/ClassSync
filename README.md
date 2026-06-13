# Class Sync

Sync your SPJIMR TCS iON timetable directly to Google Calendar — automatically, for every student.

## User Flow

1. **Land** on Class Sync at `http://127.0.0.1:5002`
2. **Enter TCS iON credentials** — email (`@spjimr.org`) + password (show/hide toggle included)
3. **Backend logs into TCS iON** and extracts your personal timetable
4. **Preview your schedule** — next 2 weeks, grouped by day, colour-coded by subject
5. **Click "Sign in with Google & Sync Calendar"**
6. **Google sign-in / consent screen** appears — log in with any Google account
7. App creates an **SPJIMR Timetable** calendar and inserts all events (with 15-min reminders)
8. **Done** — events are live in your Google Calendar

> The Google sync button is only shown after your TCS timetable has loaded.  
> Each browser session is fully isolated — multiple students can use the same server simultaneously.

## Run

```powershell
# Install dependencies
python -m pip install -r requirements.txt

# Run with live TCS iON
python -m flask --app class_sync.web run --port 5002

# Run in sample mode (no TCS credentials needed, for UI testing)
$env:TCS_SAMPLE_MODE='1'
python -m flask --app class_sync.web run --port 5002
```

## Google OAuth Setup (required for real calendar sync)

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a project → Enable **Google Calendar API**
3. **APIs & Services → Credentials → Create OAuth 2.0 Client ID**
   - Application type: **Web application**
   - Authorised redirect URI: `http://127.0.0.1:5002/google/callback`
4. Copy the credentials into a `.env` file (see `.env.example`)

```powershell
# Copy the template
Copy-Item .env.example .env
# Then fill in GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REDIRECT_URI
```

The app auto-loads `.env` on startup. If `GOOGLE_CLIENT_ID` is not set, Google sync runs in dry-run (simulated) mode.

## Required Environment Variables (Production)

| Variable | Description |
|---|---|
| `SECRET_KEY` | Flask session secret (any long random string) |
| `CREDENTIAL_ENCRYPTION_KEY` | Fernet key for stored TCS creds (derived from SECRET_KEY if blank) |
| `GOOGLE_CLIENT_ID` | OAuth 2.0 client ID from Google Cloud Console |
| `GOOGLE_CLIENT_SECRET` | OAuth 2.0 client secret |
| `GOOGLE_REDIRECT_URI` | Must match what's registered in Google Cloud Console |
| `ADMIN_TOKEN` | Optional — enables the `/admin/refresh` endpoint |

## Architecture

- **Flask** backend (Python 3.10+)
- **SQLite** — per-user isolated data keyed by UUID session token
- **TCS iON scraper** — logs in, discovers exam session IDs, fetches timetable JSON
- **Google Calendar API** — upserts events into a dedicated `SPJIMR Timetable` calendar
- **Weekly background scheduler** — auto-refreshes all connected users every Monday at 2 AM
- **Fernet encryption** — TCS credentials never stored in plain text
