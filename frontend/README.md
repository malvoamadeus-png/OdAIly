# OdAIly X Capture Console

Set Vite/Supabase environment variables:

```powershell
Copy-Item .env.example .env.local
```

Fill:

```text
VITE_SUPABASE_URL=
VITE_SUPABASE_ANON_KEY=
```

Then run the static console:

```powershell
npm install
npm run dev
```

The page uses Supabase Auth email/password login. The browser still boots with
the anon key, but console data access requires an authenticated session whose
email is present in `console_admins`.

Bootstrap a console admin after the database schema is initialized:

```powershell
python ..\backend\src\main.py console-grant-admin --email your-admin@example.com
```
