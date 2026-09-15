# Setup Guide -- WordPress AI Chatbot

This project is a Flask REST API (Python) plus a single-file WordPress PHP
widget. The backend is platform-independent. These steps start on Windows,
then note how to move the same code to a Linux VPS.

## 1. Windows: Python and virtual environment

1. Install Python 3.10 or newer from https://www.python.org/downloads/
   During setup, check **Add python.exe to PATH**.
2. Open Command Prompt or PowerShell in this project folder.
3. Create and activate a virtual environment:

```
python -m venv .venv
```

```
.venv\Scripts\activate
```

On PowerShell, if activation is blocked:

```
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

Then run `.venv\Scripts\activate` again.

4. Install dependencies:

```
python -m pip install --upgrade pip
pip install -r requirements.txt
```

## 2. Environment file

```
copy .env.example .env
```

Edit `.env` and set at least:

- `BUSINESS_NAME`, `BOT_NAME`, `ADMIN_EMAIL`, `WEBSITE_DOMAIN`
- `ALLOWED_ORIGINS` -- your live WordPress origin, e.g. `https://yourdomain.com`
- `API_SECRET_KEY` -- long random string. Generate one with:

```
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

- `OPENROUTER_API_KEY` -- from https://openrouter.ai/keys
- `OPENROUTER_MODEL` -- default `openai/gpt-4o-mini` is a strong, cheap chat model
- `TIMEZONE` -- IANA name, e.g. `America/New_York`, `Europe/London`, `Asia/Kolkata`
- `BUSINESS_DAYS`, `WORKING_HOURS_START`, `WORKING_HOURS_END`
- `SLOT_DURATION_MINUTES` -- default 30
- `LUNCH_BREAK_START` / `LUNCH_BREAK_END` -- leave empty to disable

Keep `.env` out of git. It is already listed in `.gitignore`.

## 3. Knowledge base

1. Put your website copy in `knowledge_docs/` as `.txt` or `.md` files
   (services, FAQs, about, policies, pricing you are willing to disclose).
2. A sample file is already there. Replace it with real content.
3. Build the local vector index:

```
python build_knowledge_base.py
```

The first run downloads Chroma's open-source MiniLM embedding model (one time,
offline after that). No paid embedding API is used.

Re-run this script whenever the documents change. Old chunks are replaced.

## 4. Google Cloud: Calendar API + Gmail API

The bot books appointments in Google Calendar and sends confirmation emails
through the Gmail API (OAuth2, not SMTP).

1. Open https://console.cloud.google.com/ and create a project.
2. APIs and Services -> Library:
   - Enable **Google Calendar API**
   - Enable **Gmail API**
3. APIs and Services -> OAuth consent screen:
   - User type: External is fine for a single mailbox.
   - App name, support email, developer contact.
   - Add scopes:
     - `https://www.googleapis.com/auth/calendar`
     - `https://www.googleapis.com/auth/gmail.send`
   - Add your Google account as a test user.
4. APIs and Services -> Credentials -> Create credentials -> **OAuth client ID**
   - Application type: **Desktop app**
   - Download the JSON.
5. Save that file as `credentials/credentials.json`.

Do not commit `credentials.json` or `token.json`.

## 5. Generate the OAuth token (once)

With the venv still active:

```
python generate_token.py
```

A browser window opens. Sign in with the Google account that owns the business
calendar and Gmail. Grant Calendar and Gmail send access.

The script writes `credentials/token.json`. The API reuses and refreshes it.

If you revoke access or change the OAuth client, run `generate_token.py` again.

Until this step is done, bookings still work: they are stored in `bookings.json`
and emails are written to the log. Connect Google before going live.

## 6. Start the API on Windows

With the venv active:

```
python app.py
```

Equivalent Waitress command:

```
waitress-serve --host=0.0.0.0 --port=5000 app:app
```

Health check (no API key):

```
curl http://127.0.0.1:5000/api/health
```

Open `http://127.0.0.1:5000/` for the local widget demo.

If WordPress runs on another machine, the backend must be reachable from that
machine (port 5000, or a reverse proxy). `localhost` inside PHP is the
WordPress server, not your Windows PC.

## 7. WordPress widget

1. Install a Code Snippets plugin (or use a child theme `functions.php`).
2. Create a snippet that runs on the front end.
3. Paste the entire contents of `wordpress-chat-widget.php`.
4. Edit the constants at the top:

```
define('AICB_API_URL', 'http://localhost:5000');
define('AICB_API_KEY', 'the-same-value-as-API_SECRET_KEY');
define('AICB_BOT_NAME', 'Alex');
define('AICB_PRIMARY_COLOR', '#0f766e');
```

For local WordPress on the same Windows machine, `http://localhost:5000` is
correct. After you deploy the API, change `AICB_API_URL` to
`https://api.yourdomain.com` (or whichever URL you expose).

The snippet proxies chat/book requests through `admin-ajax.php`. The API key
never appears in JavaScript.

5. Save and enable the snippet. Visit any page: a floating button appears at
   the bottom-right.

## 8. End-to-end test

1. Backend running, knowledge base built, `.env` filled.
2. Open the site, click the chat icon.
3. Ask something that is in `knowledge_docs/`.
4. Ask something that is not. The bot should say it does not have that
   information and offer an appointment.
5. Tap **Book Appointment**, pick a slot, fill name + email, confirm.
6. Check Google Calendar (or `bookings.json` if Google is not connected).
7. Check the visitor inbox and `ADMIN_EMAIL`.

## 9. Security checklist

- `API_SECRET_KEY` is long and random; WordPress and `.env` match.
- `ALLOWED_ORIGINS` lists only your real site origin(s).
- CORS is origin-restricted; `/api/health` is the only unauthenticated route.
- Rate limits: chat 30/min, book 10/min (configurable).
- Do not commit `.env`, `credentials/token.json`, or `credentials/credentials.json`.
- Keep Python and pip packages updated.

## 10. Linux VPS migration (brief)

The code already uses `pathlib`, relative paths, and Waitress (works on Linux).
Gunicorn is not required.

1. Copy the project (without `.venv`, `chroma_db` can be rebuilt).
2. Install Python 3.10+:

```
sudo apt update
sudo apt install -y python3 python3-venv python3-pip
```

3. Recreate the venv and install requirements.

```
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

4. Copy `.env` and `credentials/` (or re-run `generate_token.py` on a machine
   with a browser, then copy `token.json`).
5. Rebuild the knowledge base: `python build_knowledge_base.py`
6. Run behind a process manager and reverse proxy:

```
waitress-serve --host=127.0.0.1 --port=5000 app:app
```

Example systemd unit (`/etc/systemd/system/chatbot.service`):

```
[Unit]
Description=WordPress AI Chatbot API
After=network.target

[Service]
WorkingDirectory=/opt/chatbot
ExecStart=/opt/chatbot/.venv/bin/waitress-serve --host=127.0.0.1 --port=5000 app:app
Restart=always
User=www-data

[Install]
WantedBy=multi-user.target
```

Put Nginx/Caddy in front with HTTPS, then set WordPress `AICB_API_URL` to that
HTTPS origin.

OpenRouter, Chroma, and Google OAuth do not change. Timezone stays an IANA
name in `.env`.

## File map

```
app.py                     Flask API + Waitress entry
config.py                  Env, paths, timezone, working hours
rag.py                     Chroma retrieval
calendar_service.py        Availability + booking (Google or local fallback)
gmail_service.py           Confirmation emails (Gmail API)
build_knowledge_base.py    Index knowledge_docs/
generate_token.py          One-time Google OAuth
wordpress-chat-widget.php  Single WordPress snippet
.env.example               Copy to .env
requirements.txt           Python dependencies
knowledge_docs/            Your website text
credentials/               Google OAuth files (not committed)
demo/index.html            Local widget preview
```
