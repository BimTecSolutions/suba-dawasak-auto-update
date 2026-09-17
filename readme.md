# Suba Dawasak — automated incremental updater

Checks Firestore for the latest draw already stored, tries newer draw
numbers against the NLB site, and uploads anything new. Runs on a
GitHub Actions schedule (roughly every 5 minutes) so you don't have to
run it by hand.

## 1. Create the GitHub repo

1. Create a new (private, recommended) repository on GitHub.
2. Push these files to it, keeping the folder structure exactly as-is:

```
your-repo/
├── main.py
├── requirements.txt
├── README.md
└── .github/
    └── workflows/
        └── scrape.yml
```

```bash
git init
git add .
git commit -m "Incremental Suba Dawasak scraper"
git branch -M main
git remote add origin https://github.com/<you>/<your-repo>.git
git push -u origin main
```

## 2. Add your Firebase service account as a secret

**Never commit `serviceAccountKey.json` to the repo.** Instead:

1. On GitHub, go to your repo → **Settings** → **Secrets and variables** →
   **Actions** → **New repository secret**.
2. Name it exactly: `FIREBASE_SERVICE_ACCOUNT`
3. Open your local `serviceAccountKey.json` file, copy its **entire raw
   JSON content**, and paste it as the secret's value.
4. Save.

The workflow writes that secret out to a temporary
`serviceAccountKey.json` file at the start of each run, uses it, then
deletes it at the end (the `Clean up credentials file` step) — it never
touches your git history.

## 3. That's it — the workflow is already wired up

`.github/workflows/scrape.yml` will now:
- Run automatically every 5 minutes (`cron: "*/5 * * * *"`), and
- Can also be run on demand from the **Actions** tab → select the
  workflow → **Run workflow** (this uses the `workflow_dispatch` trigger).

Each run:
1. Installs Python deps and Chrome.
2. Writes the secret to `serviceAccountKey.json`.
3. Runs `python main.py`, which:
   - Queries Firestore for the highest `draw_number` already stored.
   - Tries the next draw number(s) against the NLB site.
   - Uploads any new draw it finds.
   - Stops once it hits 3 consecutive misses in a row (meaning it's
     caught up — nothing newer is published yet), or after 20 attempts
     in one run, whichever comes first (both configurable — see below).
4. Deletes the credentials file.

## 4. Tuning (optional)

These are set as `env:` under the `Run scraper` step in
`scrape.yml` — uncomment and edit any you want to change:

| Variable | Default | Meaning |
|---|---|---|
| `START_DRAW_FALLBACK` | `1` | Where to start if Firestore is completely empty |
| `MAX_CONSECUTIVE_MISSES` | `3` | How many draw numbers in a row can fail before the run gives up (i.e. "we've caught up") |
| `MAX_NEW_DRAWS_PER_RUN` | `20` | Safety cap so a bad loop can't run forever in one job |
| `DRAW_DELAY_SECONDS` | `1.0` | Delay between draw attempts, to be polite to the NLB site |

## Notes & caveats

- **Cron timing isn't exact.** GitHub Actions schedules are "best
  effort" — under heavy platform load, a run can be delayed by several
  minutes. This is a GitHub-side limitation, not something in the code.
- **Concurrency guard.** The workflow has `concurrency: cancel-in-progress: false`,
  so if one run is still going when the next 5-minute mark hits, GitHub
  queues the new run instead of overlapping them — safe, but it does
  mean a slow run can push later runs later too.
- **First run.** If your Firestore collection already has draws from
  your original bulk-range script, the incremental script will pick up
  right where that left off automatically — no manual step needed.
- **Selenium in CI.** The workflow installs Chrome via
  `browser-actions/setup-chrome`; Selenium 4's built-in Selenium Manager
  will resolve a matching chromedriver automatically, so you don't need
  to pin a chromedriver version separately.
- **Action versions.** `browser-actions/setup-chrome@v1` is a
  third-party action — worth a quick glance at its repo before relying
  on it long-term, same as any Action from outside `actions/org`.
