"""
notify.py — compose the email sent to colleagues once the week is published.

Reads config.yaml (notifications.*, site.base_url) and the newest files in
reports/ and mentions/ to build a subject line, a sender and a body, and writes
them to $GITHUB_OUTPUT so weekly.yml can pass them to the mail-sending step.

Since there is no review pull request any more, the things that used to be
listed there for a human to check — sensitive country names, low-confidence
source matches, feeds that stopped working — are appended to the email itself,
from data/pr_body.md if pr_body.py has already run.

Runs only inside GitHub Actions, after the site has rebuilt.
"""

import json
import os
import pathlib
import datetime as dt

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONFIG = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))


def newest(folder):
    files = sorted((ROOT / folder).glob("*.json"))
    if not files:
        return None
    try:
        return json.loads(files[-1].read_text(encoding="utf-8"))
    except Exception as e:
        print(f"  could not read newest {folder} file: {e}")
        return None


def undate(v):
    try:
        d = dt.date.fromisoformat(str(v)[:10])
        return f"{d.day} {d.strftime('%B %Y')}"
    except Exception:
        return v


def write_output(key, value):
    out = os.environ.get("GITHUB_OUTPUT")
    if not out:
        print(f"  [no GITHUB_OUTPUT set] {key}={value}")
        return
    with open(out, "a", encoding="utf-8") as f:
        f.write(f"{key}={value}\n")


def review_notes():
    """The 'worth a look' section, taken from data/pr_body.md if it exists.

    That file is Markdown written for GitHub; here it is read by people in a
    mail client, so the heading and emphasis markers come back out.
    """
    src = ROOT / "data" / "pr_body.md"
    if not src.exists():
        return []
    lines = []
    for raw in src.read_text(encoding="utf-8").splitlines():
        line = raw.replace("**", "").replace("`", "")
        if line.startswith("#"):
            line = line.lstrip("# ").upper()
        lines.append(line)
    # Trim the leading blurb: everything before the first heading is about the
    # draft itself, which the reader of this email does not need.
    for i, line in enumerate(lines):
        if line.isupper() and line.strip():
            return lines[i:]
    return []


def main():
    notif = CONFIG.get("notifications", {})
    if not notif.get("enabled"):
        print("Notifications disabled in config.yaml (notifications.enabled: false).")
        write_output("send", "false")
        return

    recipients = [r.strip() for r in notif.get("recipients", []) if r.strip() and "@" in r]
    if not recipients:
        print("notifications.recipients is empty (or has no valid addresses) in "
              "config.yaml; nothing to send.")
        write_output("send", "false")
        return

    # Set by the test-email workflow: check the SMTP setup without mailing the
    # whole office a test message.
    if os.environ.get("NOTIFY_ONLY_FIRST", "").lower() in ("1", "true", "yes"):
        recipients = recipients[:1]
        print(f"  NOTIFY_ONLY_FIRST is set — sending to {recipients[0]} only")

    base_url = (CONFIG.get("site", {}).get("base_url") or "").rstrip("/")
    if not base_url:
        print("site.base_url is empty in config.yaml; cannot build a link, skipping email.")
        write_output("send", "false")
        return

    report = newest("reports")
    mentions = newest("mentions")
    week = report or mentions
    if not week:
        print("No report or mentions file found; nothing to announce.")
        write_output("send", "false")
        return

    label = undate(week["week_start"])
    end = week.get("week_end")
    if end and end != week["week_start"]:
        label += f" – {undate(end)}"

    prefix = notif.get("subject_prefix", "Mongolia Weekly")
    subject = f"{prefix} — {label}"

    lines = [f"The {prefix} for {label} is now online.", ""]
    if report:
        n = sum(len(s.get("items", [])) for s in report.get("sections", []))
        lines.append(f"Weekly report — {n} item(s): {base_url}/")
    if mentions:
        m = len(mentions.get("mentions", []))
        lines.append(f"In the Media — {m} mention(s): {base_url}/media/index.html")

    notes = review_notes()
    if notes:
        lines += ["", "-" * 60, ""] + notes

    lines += ["", "This is an automated message. Corrections can be made directly in the "
                  "JSON files in the repository; the site rebuilds itself."]
    body = "\n".join(lines)

    write_output("send", "true")
    write_output("subject", subject)
    write_output("recipients", ",".join(recipients))
    write_output("sender_name", notif.get("sender_name", prefix))
    write_output("reply_to", notif.get("reply_to", ""))
    (ROOT / "data").mkdir(parents=True, exist_ok=True)
    (ROOT / "data" / "email_body.txt").write_text(body, encoding="utf-8")
    print(f"Prepared email for {len(recipients)} recipient(s): {subject}")


if __name__ == "__main__":
    main()
