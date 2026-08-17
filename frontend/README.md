# OdAIly Console

The Vite console talks only to the authenticated Linux API; it does not connect to a database from the browser.

```powershell
Copy-Item .env.example .env.local
npm install
npm run dev
```

Configure the HTTPS API base URL:

```text
VITE_CONSOLE_API_BASE_URL=https://api.odaily.uk
```

The console and Chrome extension share the fixed local operator account. The backend validates the bcrypt password hash from the primary SQLite database and issues an opaque Bearer session.
