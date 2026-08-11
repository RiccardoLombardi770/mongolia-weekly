# Mongolia Weekly — step-by-step guide (GitHub Desktop version)

A system that every Monday morning collects the week's news relevant to
the UN in Mongolia, has Claude write it up into a curated report (four categories, agency
tags, "Also noted" section), self-reviews it in a small quality loop, **publishes it, and
emails the office the link**. In parallel it produces a second page, **In the
Media**, which collects who mentioned the UN in Mongolia, in what tone, and — when it can be
established — which of our publications was being referred to. Everything goes live on a
static site: newsroom-style layout, tidy cards, EN/MN toggle, agency filter, and an archive of
past weeks.

Everything runs on GitHub, with no server and no n8n. Estimated cost: **~$2-5/month** in API
usage.

This guide uses **GitHub Desktop** (a graphical app): no command line needed to get the
project online. Everything else is done from the github.com website.

---

## How the flow works (the mental model)

```
  Monday 08:00 Ulaanbaatar
        |
        v
  [collect.py]           gathers the news        (sources from the shared workbook)
  [index_un.py]          indexes OUR publications (180-day window)
  [collect_mentions.py]  finds who mentioned us, downloads the articles
        |
        v
  [generate.py]          Claude writes the report -> self-review loop -> MN
  [generate_mentions.py] tone + type + prominence + which of our sources was cited
        |            (draft -> critique -> fix, until confidence >= 90)
        v
  commits the week to main
        |
        v
  [build.py] rebuilds the site  ->  goes LIVE on GitHub Pages
        |
        v
  [notify.py] emails the office the link, plus anything worth a second look
        |
        v
  a correction, if ever needed, is made afterwards by editing the JSON
```

The **JSON report** (`reports/<date>.json`) is the single source of truth: the HTML site is
always rebuilt from it, so it stays consistent and the HTML is never touched by hand.

---

## What's in the project

```
sources/sources.xlsx     <- THE SHARED SHEET: sources, outlets, names, UN sites, glossary
                            (this is where the office works, not in the code)
config.yaml              <- categories, agencies, model, thresholds, style, media monitoring
requirements.txt
scripts/
  sources_loader.py      <- reads the workbook, validates it, collects issues
  make_sources_xlsx.py   <- creates the starter workbook (run ONCE)
  un_style.py            <- UN Editorial Manual linter (country names, dates, spelling)
  collect.py             <- collects the news (no AI, no key needed)
  generate.py            <- Claude + review loop + UN style + MN translation
  index_un.py            <- scrolling index of our publications
  collect_mentions.py    <- finds mentions and downloads article text
  match_sources.py       <- links the mention to the publication of ours it cites
  generate_mentions.py   <- tone, type, prominence, cited source, translation
  pr_body.py             <- writes the summary of what needs a human eye (goes into the email)
  notify.py              <- composes and addresses the Monday email
  migrate_reports.py     <- converts old reports to the four categories
  build.py               <- generates the static site into docs/
templates/
  report.html.j2         <- weekly report layout
  media.html.j2           <- "In the Media" page layout
assets/style.css, app.js <- site styling and interactivity
reports/<date>.json      <- the generated reports (source of truth)
mentions/<date>.json     <- the week's mentions (source of truth)
index/un_publications.json <- index of our publications (accumulates over time)
docs/                    <- the generated site (what GitHub Pages publishes)
.github/workflows/
  weekly.yml             <- Monday: collect + generate + publish + email
  build.yml               <- rebuilds the site after a manual correction
  media-only.yml         <- re-run just "In the Media" (spends tokens; no email)
  test-mentions.yml      <- who would be collected, and is the text real? (free)
  test-collection.yml    <- the news collection, without the model (free)
  test-email.yml         <- send the email for the report already in the repo (free)
```

---

## Prerequisites (one-time only)

1. A free **GitHub account** (yours). Your colleague will create her own later.
2. **GitHub Desktop** installed: download it from https://desktop.github.com and sign in with
   your GitHub account.
3. An **Anthropic API key**. Go to https://console.anthropic.com, create an API key, and keep
   it handy. For now use yours (the ~$5 of credit is enough for dozens of test runs); you'll
   swap in the office one later on, by changing a single field (see Step 8).

(Python is NOT required to get started: you can generate the first report in the cloud. It's
only needed if you want the more convenient local test in Step 6.)

---

## Step 1 — Prepare the project folder

1. Unzip the project's `.zip` into a folder that's easy to find (e.g. in Documents).
2. Open it in File Explorer and check that you can see the actual files INSIDE it:
   `config.yaml`, `README.md`, `scripts`, `templates`. If instead you see another folder called
   `mongolia-weekly` inside the first one, go into that inner one: that's the right one (the
   zip sometimes creates a folder inside another). The "right" folder is always the one that
   directly contains `config.yaml`.

---

## Step 2 — Put the project on GitHub (with GitHub Desktop)

1. Open GitHub Desktop. Menu **File -> Add local repository**.
2. Choose the "right" folder from Step 1 (the one containing `config.yaml`).
3. Desktop will say it's not yet a repository and offer a **"create a repository"** link:
   click it, then **Create repository** (the default settings are fine).
4. At the bottom left you'll see the list of files, each with a checkbox. In the field below,
   write a message in the **Summary** box, e.g. `Initial setup`, and click
   **Commit to main**.

   > Is the **Commit to main** button greyed out (not clickable)? Typical causes:
   > - **NO files appear in the list** (the "Changes" tab is empty). Two possibilities:
   >   (a) you added the wrong folder — the one you see in GitHub Desktop doesn't
   >   contain the project files. Check the path under **Repository -> Repository
   >   settings**; if it's wrong, do **Current repository -> right-click the repo ->
   >   Remove**, and redo Step 2 choosing the folder that contains `config.yaml`.
   >   (b) you'd already committed this folder in an earlier attempt: in that case it's
   >   normal for there to be nothing to commit — skip straight to point 5 (Publish).
   > - **The files appear but without a checkmark**: at the top of the list, check the
   >   "select all" box, so all the files are included in the commit.
   > - **The Summary is empty or wasn't registered**: click inside the *Summary* box,
   >   type `Initial setup`, then click once outside the field (or press Tab). Sometimes the
   >   button only activates once the field loses focus.
5. At the top, click **Publish repository**. In the window: leave the name as
   `mongolia-weekly` and **uncheck** "Keep this code private" (the site needs to be public).
   Then **Publish repository**.

Done: your files are on GitHub. Opening github.com from your profile you'll see the repo.

> Did you accidentally create extra repos? They can be deleted from the site: open the repo on
> github.com -> **Settings** -> at the bottom, **Danger Zone** -> **Delete this repository** ->
> confirm by typing the name. Keep only one, the one published above.

---

## Step 3 — Save the API key as a "secret"

On the **website** github.com, in your repo: **Settings -> Secrets and variables -> Actions ->
New repository secret**.
- Name: `ANTHROPIC_API_KEY`
- Secret: your key.

It's encrypted and hidden from logs. It's the only place the key lives.

---

## Step 4 — Grant permissions to Actions

Still under **Settings -> Actions -> General**, "Workflow permissions" section:
- choose **Read and write permissions**;
- Save.

Without this, Monday's run cannot commit the new week to the repository.

---

## Step 5 — Turn on the site (GitHub Pages)

Under **Settings -> Pages**, "Build and deployment" section:
- Source: **Deploy from a branch**
- Branch: **main**, folder **/docs** -> Save.

After a minute the site is live at `https://<your-username>.github.io/mongolia-weekly/`.
Since the project already includes a sample report, you should see it right away: that's the
confirmation that everything is properly connected.

---

## Step 6 — Generate the first real report

Two ways, pick whichever you prefer.

**A) In the cloud (no Python to install).**
On the website, tab **Actions -> Weekly report -> Run workflow**. This starts the collection +
generation and, at the end, publishes the site and sends the email.

**B) Locally (to fine-tune the prompts at your own pace).** Requires Python 3.12+ on your
computer. In the project folder:
```
pip install -r requirements.txt
set ANTHROPIC_API_KEY=sk-ant-...your-key...          (Windows CMD)
$env:ANTHROPIC_API_KEY="sk-ant-..."                   (Windows PowerShell)
python scripts/collect.py     -> data/raw_news.json
python scripts/generate.py    -> reports/<date>.json
python scripts/build.py       -> docs/index.html   (open it in your browser)
```
If the tone or structure doesn't convince you, edit the prompts in `scripts/generate.py`
(`DRAFT_SYSTEM`, `REVIEW_SYSTEM`, `TRANSLATE_SYSTEM`) and run again. When you're happy, in
GitHub Desktop do **Commit to main** and **Push** to upload the changes.

---

## Step 7 — The weekly flow

There is no approval step: Monday's run publishes the week and emails everyone in
`notifications.recipients` the link, together with a short list of anything worth a second
look (sensitive country names, low-confidence source matches, feeds that stopped working).

**To correct something after publication**, open the file on github.com —
`reports/<date>.json` or `mentions/<date>.json` — click the pencil icon, edit the text in the
`"en"` / `"mn"` fields (plain, labelled text; no coding needed) and **Commit changes**. The
site rebuilds itself within a minute or two.

> The trade-off of publishing straight away: an error is public until it is corrected. The
> review loop and the linter run before publication, but they are not a human editor — see
> "Important note on reliability" at the end.

One-time setup for a colleague who will make corrections:
1. She creates a free GitHub account.
2. You add her to the repo: on the website, **Settings -> Collaborators -> Add people**.

---

## If a Monday brings nothing

Check, in this order, on github.com in your repo:

1. **Actions tab -> "Weekly report (publish + email)".** If there's no run for today: the workflow
   didn't start. The most common cause is having uploaded or edited the project *after*
   08:00 Ulaanbaatar on Monday: that week's scheduled run had already passed, and the next one
   is seven days away. No need to wait — click **Run workflow** to trigger it right now.
2. **If there's a red (failed) run:** open it and see which step stopped. Typical causes are a
   missing `ANTHROPIC_API_KEY` secret (Step 3) or Actions permissions not enabled (Step 4).
3. **If there's a green run but no email arrived:** the week was published anyway — check the
   site. Only the notification failed: open the run, look at the **Send email** step, and see
   the SMTP section below.
4. **If nothing shows up at all, ever, not even a failed attempt:** the schedule may simply not
   have had a real chance to fire yet, or GitHub's free-tier scheduler occasionally drops a run
   during high load — this is a documented limitation, not something on your end. Scheduling
   runs on GitHub's own cloud servers, completely independent of any local computer being on or
   off. A practical mitigation: avoid round cron times like `"0 0 * * 1"` (every repository in
   the world with that same schedule competes for the same slot); a few minutes off the hour,
   e.g. `"7 2 * * 1"`, avoids the crowd.

---

## The automatic email to colleagues

At the end of Monday's run, once the site has been rebuilt, the last steps of `weekly.yml`
send an email to the recipients listed in `config.yaml`, with the link to that week's report
and to "In the Media", and the short list of things worth a second look.

### Step A — Write the recipients

In `config.yaml`, under `notifications`:
```yaml
notifications:
  enabled: true
  subject_prefix: "Mongolia Weekly"
  recipients:
    - "colleague1@un.org"
    - "colleague2@un.org"
```
Add or remove lines as needed; it's a fixed list, edited here and committed. To turn sending
off entirely without deleting the list, set `enabled: false`.

### Step B — Set up SMTP sending, and choose who it comes from

Two secrets under **Settings -> Secrets and variables -> Actions -> New repository secret**:

- `SMTP_USERNAME` — the full email address of the **sending mailbox**.
- `SMTP_PASSWORD` — an **app password** for that same mailbox (not the normal account
  password). On Gmail: myaccount.google.com -> Security -> 2-Step Verification -> App
  passwords. On Microsoft 365: account.microsoft.com -> Security -> Advanced sign-in options.

**The address the email comes from is always `SMTP_USERNAME`** — the mailbox the run
authenticates as. Gmail (and Microsoft) refuse to send as anybody else, so there is no setting
that can fake a different sender. To send from a colleague's mailbox instead of yours, she
generates an app password on her own account and you replace both secrets with her address and
that password; nothing in the code changes. The display name next to the address is
`notifications.sender_name` in `config.yaml`, and `notifications.reply_to` can point replies
somewhere else again.

**A point that will very likely block the first attempt, so it's worth knowing in advance:**
since 2022 Microsoft disables basic SMTP authentication by default across all 365 tenants. If
the first send fails with something like `535 5.7.139 Authentication unsuccessful,
SmtpClientAuthentication is disabled`, it means the office's IT needs to **re-enable
Authenticated SMTP for that one mailbox** (Exchange Admin Center -> recipients -> the mailbox
-> "Authenticated SMTP"). It's a per-mailbox setting, not a tenant-wide one, so it's usually a
quick request rather than a project.

If you'd rather not wait on IT, the fastest alternative is a free low-volume external SMTP
service (e.g. SendGrid, Mailgun, Brevo): create an account, generate an API key to use as
`SMTP_PASSWORD`, and change only `server_address` in `.github/workflows/weekly.yml` (e.g.
`smtp.sendgrid.net`). The email still arrives at `colleague@un.org`; only where it's sent
*from* changes, not who receives it.

### How it works

The email is the last thing Monday's run does, after the JSON has been committed and the site
rebuilt. It waits 90 seconds so GitHub Pages has published the new pages before the links are
clicked, then `notify.py` composes the subject and body from the latest weekly report and the
latest "In the Media" page and sends it. This part costs no API tokens: it never calls Claude,
it only reads an already-generated report and sends mail. To stop the sending without touching
anything else, set `notifications.enabled: false` in `config.yaml`.

---



**Sources are changed in the Excel sheet, not in the code.** See the "The shared sheet"
section below.

Everything else is changed in **`config.yaml`** (also editable from the website: open the file
-> pencil icon -> Commit):
- **Filter agencies**: `agencies` list.
- **Categories** and their order: `categories` list (there are four).
- **Secondary tags**: `secondary_tags` list.
- **Relevance terms**: `mongolia_terms` and `regional_terms`.
- **UN style**: `style.linter` (on/off) and `style.flag_only_terms` (names the system flags
  instead of deciding on its own).
- **Media monitoring**: `media_monitoring.match_min_confidence` (below this threshold the
  cited source is shown as "not identified" instead of being guessed),
  `media_monitoring.un_index_days` (how far back the index of our publications goes).
- **Model**: `model` field (`claude-sonnet-5`, or `claude-haiku-4-5-20251001` to spend less).

To switch to the **office key**: update the `ANTHROPIC_API_KEY` secret (Step 3) with the new
value. Nothing else changes.

---

## The shared sheet (`sources/sources.xlsx`)

This is where the office does its work. Six sheets:

| Sheet | What it's for |
|---|---|
| `README` | instructions for your colleague, inside the file itself |
| `sources` | searches and feeds for the weekly report |
| `outlets` | outlets to monitor for mentions |
| `roster` | RC and head of office, **with all Cyrillic aliases** |
| `un_sites` | OUR pages, indexed to help reconstruct citations |
| `glossary` | agreed UN terminology in Mongolian, used verbatim |

Your colleague's workflow: she opens the file in Excel, adds or disables rows (`active`
column: `yes`/`no`), saves it, and re-uploads it to GitHub (`sources` -> **Add file** ->
**Upload files** -> drag it in -> **Commit changes**). It takes effect from the following
Monday.

A mistaken row **doesn't stop the run**: it ends up in the "Source problems" section of
Monday's email, with sheet name and row number.

Two things to know about the `un_sites` sheet, checked on 11 August 2026: **undp.org,
unicef.org and iom.int refuse automated requests** (403) and offer no feed, so their
pages cannot be indexed — the rows are kept, with a note, in case that changes.
Where a site publishes RSS, put it in `feed_url`: it is more reliable than scraping the
listing page, and `mongolia.un.org`, `mongolia.unfpa.org` are already set up that way.
The same applies to `rss_url` in the `outlets` sheet — an outlet with a feed is read
directly instead of through Google News, which is both more complete and more accurate. The `<FILL IN>` cells in the `roster` sheet
should be filled in first: as long as they're empty, mentions that cite the RC by name won't
be found.

To move everything to a Google Sheet later on: **File -> Share -> Publish to web ->
CSV**, one URL per sheet, and paste them into `workbook.csv_urls` in `config.yaml`. Nothing
else changes.

---

## The "In the Media" page

Each mention shows the outlet, date, language, tone (**Supportive / Neutral / Critical**),
type of mention, prominence, agencies cited, and a **"Refers to →"** line with the publication
of ours that's cited. Three possible outcomes, kept deliberately distinct:

- **linked in the article** — the article contains a link to a UN domain. It's proof, not
  a deduction: confidence 100.
- **probable, 78% confidence** — no link: the source is inferred from shared figures,
  wording, closeness of dates. The concrete reasoning is written below the line.
- **not identified** — below the threshold. We'd rather say so than guess.

Tone judges **how the UN is portrayed in the article**, not the quality of the piece or its
argument: a grim article that reports our figures without comment is *Neutral*.

At the bottom of the page is the cumulative archive: totals, most frequent outlets, most
cited agencies, week-by-week trend.

---

## The UN Editorial Manual style

Two layers, because the prompt alone isn't enough to keep a consistent editorial house style
from one Monday to the next:

1. **In the prompts** — institutional register, numbers, capitalization of official titles,
   short UN form of country names.
2. **In the linter** (`scripts/un_style.py`) — deterministic mechanical fixes: *Russia* ->
   *the Russian Federation*, *South Korea* -> *the Republic of Korea*, *Vietnam* -> *Viet Nam*,
   *percent* -> *per cent*, *program* -> *programme*, *July 14, 2026* -> *14 July 2026*. URLs,
   direct quotations and outlet names are masked before substitution, so
   "Russia Today" stays "Russia Today".

**Politically sensitive names** (Taiwan, Kosovo, Palestine, Crimea, Western Sahara...) are
never automatically rewritten: they are listed in Monday's email under "Sensitive names",
with the context in which they appear, so the office can decide whether to correct them. This is a matter for
the RCO, not the model.

---

## Costs

Small weekly volume (~40k input tokens, ~7-8k output EN+MN, plus 1-2 self-review rounds). With
**Claude Sonnet 5** we're talking a few cents a week; worst case ~$5-6/month. With Haiku it
goes down further, at the cost of a bit of nuance.

---

## Important note on reliability

A language model can occasionally overemphasize or mix up a story, and the "confidence" it
assigns itself in the loop **is not an objective guarantee**. The loop raises quality and
catches obvious errors, but it is not an editor. Now that the week is published without an
approval step, **someone should still read Monday's email and skim the page**: the email lists
exactly what the run itself was unsure about, and any correction is one edit away. The prompts
are already written with
a cautious approach (always cite the source, never invent, when in doubt downgrade to "Also
noted").

The same applies, even more clearly, to the **In the Media** page: the tone is an automated
judgment on a public page, and the attribution of the cited source is a stated probability.
The methodological disclaimer at the bottom of the page says this explicitly: it's your
protection, best to leave it there.
