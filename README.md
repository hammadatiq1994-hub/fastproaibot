# WordPress AI Chatbot

Production-ready chatbot: Flask REST API (OpenRouter + RAG + Google Calendar +
Gmail) and a single-file WordPress widget.

The bot answers only from your website documents. When it does not know, or
when the visitor is ready, it offers an appointment for the next 7 days of
configured working hours.

Visitors can also check an existing booking: ask to check an appointment, give
the email used at booking time, and the bot replies with the slot time or says
the slot has already expired.

## Quick start (Windows)

```
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```

Edit `.env`, add your documents to `knowledge_docs/`, then:

```
python build_knowledge_base.py
python generate_token.py
python app.py
```

Open `http://127.0.0.1:5000/` to preview the widget. Full steps: `SETUP.md`.

## API

| Method | Path | Auth |
| --- | --- | --- |
| GET | `/api/health` | none |
| POST | `/api/chat` | `X-API-Key` |
| GET | `/api/availability` | `X-API-Key` |
| POST | `/api/book` | `X-API-Key` |
| POST | `/api/session/reset` | `X-API-Key` |

WordPress never puts the key in JavaScript. The snippet proxies through
`admin-ajax.php`.
