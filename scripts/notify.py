"""
notify.py — compose the email sent to colleagues after a weekly merge.

Reads config.yaml (notifications.recipients, site.base_url) and the newest
files in reports/ and mentions/ to build a subject line and a short body,
and writes them to $GITHUB_OUTPUT so the notify.yml workflow can pass them
to the mail-sending step.

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

    lines = [f"The {prefix} report for {label} has been reviewed and published.", ""]
    if report:
        n = sum(len(s.get("items", [])) for s in report.get("sections", []))
        lines.append(f"Weekly report — {n} item(s): {base_url}/")
    if mentions:
        m = len(mentions.get("mentions", []))
        lines.append(f"In the Media — {m} mention(s): {base_url}/media/index.html")
    lines += ["", "This is an automated notification; no reply is needed."]
    body = "\n".join(lines)

    write_output("send", "true")
    write_output("subject", subject)
    write_output("recipients", ",".join(recipients))
    (ROOT / "data").mkdir(parents=True, exist_ok=True)
    (ROOT / "data" / "email_body.txt").write_text(body, encoding="utf-8")
    print(f"Prepared email for {len(recipients)} recipient(s): {subject}")


if __name__ == "__main__":
    main()
