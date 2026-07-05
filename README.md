# Medtech / Health-AI Digest

Daily automated email summarising new arXiv preprints and PubMed papers matching
a keyword watchlist, with each item summarised in one plain-English sentence by
Claude. Runs entirely on GitHub Actions. No server, no ongoing cost beyond
Anthropic API usage (a daily digest of ~20 papers costs a fraction of a cent).

## What it does

1. Queries the arXiv API for recent preprints in `eess.IV`, `cs.CV`,
   `physics.med-ph`
2. Queries PubMed (NCBI E-utilities) for recent papers
3. Filters both against a keyword list in `digest.py` (edit `KEYWORDS` there)
4. Sends the matched abstracts to Claude in one batch call to get a one-line
   "why this matters" summary for each
5. Emails you the result as a short HTML digest

## Setup

### 1. Create a repo

Push these files to a new **private** GitHub repo (private is fine).

```bash
cd medtech-digest
git init
git add .
git commit -m "Initial digest setup"
git remote add origin https://github.com/<your-username>/medtech-digest.git
git push -u origin main
```

### 2. Get an Anthropic API key

If you don't already have one: https://console.anthropic.com/settings/keys

### 3. Set up an app password for sending email

Using Gmail as the example (any provider with SMTP + app passwords works the
same way):

1. Turn on 2-Step Verification on your Google account, if not already on:
   https://myaccount.google.com/security
2. Go to https://myaccount.google.com/apppasswords
3. Create an app password (name it e.g. "medtech-digest") -- Google gives you
   a 16-character password. **Use this, not your normal Gmail password.**

Gmail SMTP settings you'll need below:
- `SMTP_HOST` = `smtp.gmail.com`
- `SMTP_PORT` = `587`
- `SMTP_USER` = your Gmail address
- `SMTP_PASSWORD` = the 16-character app password from step 3

(If you'd rather not use Gmail, any SMTP provider -- Outlook, Fastmail, or other providers also work.)

### 4. Add secrets to the GitHub repo

In your repo: **Settings -> Secrets and variables -> Actions -> New repository
secret**. Add each of these:

| Secret name | Value |
|---|---|
| `ANTHROPIC_API_KEY` | your Anthropic API key |
| `SMTP_HOST` | `smtp.gmail.com` (or your provider's host) |
| `SMTP_PORT` | `587` |
| `SMTP_USER` | your email address |
| `SMTP_PASSWORD` | the app password from step 3 |
| `EMAIL_TO` | address the digest should be sent to (can be the same as `SMTP_USER`) |

### 5. Test it manually

Go to the **Actions** tab in your repo -> "Medtech Digest" workflow -> **Run
workflow** button. This triggers it immediately without waiting for the
schedule, so you can check your inbox and fix anything before trusting the
automation.

### 6. Let it run on schedule

Once the manual test works, it'll run automatically every Monday/Thurdsay at 07:00 UTC
(edit the `cron` line in `.github/workflows/digest.yml` to change the time and/or days).

## Customising

- **Keywords**: edit the `KEYWORDS` list at the top of `digest.py`. This is
  the main relevance filter -- broaden or narrow it as your interests shift.
- **Lookback window**: `LOOKBACK_DAYS` controls how far back each run looks.
  Keep it at least as long as your schedule interval so a failed run doesn't
  silently drop a day's papers.
- **arXiv categories**: `ARXIV_CATEGORIES` -- add/remove categories from
  https://arxiv.org/category_taxonomy
- **Email cap**: `MAX_ITEMS_IN_EMAIL` keeps the digest short even on busy days.
