# Google OAuth credentials

1. In Google Cloud Console, create a project.
2. Enable **Google Calendar API** and **Gmail API**.
3. Configure the OAuth consent screen (External is fine for a single user).
4. Create an OAuth client ID of type **Desktop app**.
5. Download the JSON and save it here as `credentials.json`.
6. From the project root run:

```
python generate_token.py
```

That writes `token.json` in this folder. Do not commit `credentials.json` or
`token.json`.
