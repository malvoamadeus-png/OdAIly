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

The page writes X capture settings and accounts directly through Supabase.
