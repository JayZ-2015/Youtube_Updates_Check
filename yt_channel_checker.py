#!/usr/bin/env python3
"""
YouTube Channel Activity Checker
Checks YouTube channels for new videos, then sends Slack (+ optional Gmail) notifications.
No API key required — uses YouTube's public RSS feed.

Usage:
    python yt_channel_checker.py               # check & notify if new videos found
    python yt_channel_checker.py --hours 48    # look back 48 hours
    python yt_channel_checker.py --test-slack  # send a test Slack message
    python yt_channel_checker.py --test-email  # send a test email

To add more channels: edit channels.json — no Python code changes needed!
"""

import argparse
import json
import os
import smtplib
import ssl
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

EST = ZoneInfo("America/New_York")
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import requests

# ─────────────────────────────────────────────────────────────────
# SECRETS — loaded automatically from config.py (local Mac)
#           or environment variables (GitHub Actions cloud)
#           You do NOT need to edit this section.
# ─────────────────────────────────────────────────────────────────
try:
    from config import SLACK_WEBHOOK_URL, GMAIL_ADDRESS, GMAIL_APP_PW, NOTIFY_EMAIL
except ImportError:
    SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL")
    GMAIL_ADDRESS     = os.environ.get("GMAIL_ADDRESS")
    GMAIL_APP_PW      = os.environ.get("GMAIL_APP_PW")
    NOTIFY_EMAIL      = os.environ.get("NOTIFY_EMAIL")

# ─────────────────────────────────────────────────────────────────
# CHANNELS — loaded from channels.json automatically
#            Edit channels.json to add/remove channels
# ─────────────────────────────────────────────────────────────────
CHANNELS_FILE = Path(__file__).parent / "channels.json"

def load_channels() -> list[dict]:
    if not CHANNELS_FILE.exists():
        print(f"  ⚠️  channels.json not found at {CHANNELS_FILE}")
        return []
    with open(CHANNELS_FILE, encoding="utf-8") as f:
        return json.load(f)

# ─────────────────────────────────────────────────────────────────
# INTERNALS — no need to change anything below this line
# ─────────────────────────────────────────────────────────────────
RSS_BASE = "https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
NS = {
    "yt":    "http://www.youtube.com/xml/schemas/2015",
    "media": "http://search.yahoo.com/mrss/",
    "atom":  "http://www.w3.org/2005/Atom",
}
LOG_FILE = Path(__file__).parent / "yt_checker_log.jsonl"


# ── RSS helpers ───────────────────────────────────────────────────

def fetch_feed(channel_id: str) -> ET.Element | None:
    url = RSS_BASE.format(channel_id=channel_id)
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        return ET.fromstring(resp.content)
    except requests.RequestException as e:
        print(f"  ⚠️  Network error: {e}")
        return None
    except ET.ParseError as e:
        print(f"  ⚠️  XML parse error: {e}")
        return None


def parse_videos(root: ET.Element, since: datetime) -> list[dict]:
    videos = []
    for entry in root.findall("atom:entry", NS):
        title_el = entry.find("atom:title", NS)
        pub_el   = entry.find("atom:published", NS)
        vid_el   = entry.find("yt:videoId", NS)

        if title_el is None or pub_el is None:
            continue

        try:
            published = datetime.fromisoformat(pub_el.text.strip())
            if published.tzinfo is None:
                published = published.replace(tzinfo=timezone.utc)
        except ValueError:
            continue

        if published >= since:
            video_id = vid_el.text.strip() if vid_el is not None else "unknown"
            videos.append({
                "title":     title_el.text.strip(),
                "url":       f"https://www.youtube.com/watch?v={video_id}",
                "published": published.isoformat(),
                "video_id":  video_id,
            })

    return videos


# ── Slack helpers ─────────────────────────────────────────────────

def send_slack(results: dict[str, list[dict]], hours: int) -> bool:
    if not SLACK_WEBHOOK_URL:
        print("  ⚠️  Slack skipped — SLACK_WEBHOOK_URL not configured.")
        return False

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "🎬 YouTube Channel Update", "emoji": True},
        },
        {
            "type": "context",
            "elements": [{
                "type": "mrkdwn",
                "text": f"New videos in the last *{hours} hours* · {datetime.now(EST).strftime('%Y-%m-%d %H:%M EST')}",
            }],
        },
        {"type": "divider"},
    ]

    all_videos = []  # collected for the links section below

    # ── Table section ─────────────────────────────────────────────
    for channel, videos in results.items():
        if not videos:
            continue
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*📺 {channel}*"},
        })
        table_lines = ["```", f"{'Title':<50} {'Published (EST)'}"]
        table_lines.append("─" * 70)
        for v in videos:
            pub = datetime.fromisoformat(v["published"]).astimezone(EST).strftime("%Y-%m-%d %H:%M")
            title = v["title"][:47] + "..." if len(v["title"]) > 50 else v["title"]
            table_lines.append(f"{title:<50} {pub}")
            all_videos.append((channel, v))
        table_lines.append("```")
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "\n".join(table_lines)},
        })
        blocks.append({"type": "divider"})

    # ── Links section ─────────────────────────────────────────────
    if all_videos:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "*🔗 Video Links*"},
        })
        for channel, v in all_videos:
            pub = datetime.fromisoformat(v["published"]).astimezone(EST).strftime("%Y-%m-%d %H:%M EST")
            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"📹 *<{v['url']}|{v['title']}>*\n📺 {channel} · 🕐 {pub}",
                },
            })

    try:
        resp = requests.post(SLACK_WEBHOOK_URL, json={"blocks": blocks}, timeout=10)
        resp.raise_for_status()
        print("  💬  Slack notification sent!")
        return True
    except requests.RequestException as e:
        print(f"  ❌  Slack error: {e}")
        return False


def send_slack_test() -> bool:
    if not SLACK_WEBHOOK_URL:
        print("  ❌  SLACK_WEBHOOK_URL is not set.")
        return False
    payload = {"text": "✅ yt_channel_checker — Slack is connected and working! 🎉"}
    try:
        resp = requests.post(SLACK_WEBHOOK_URL, json=payload, timeout=10)
        resp.raise_for_status()
        print("  💬  Test message sent to Slack!")
        return True
    except requests.RequestException as e:
        print(f"  ❌  Slack error: {e}")
        return False


# ── Email helpers ─────────────────────────────────────────────────

def build_email_html(results: dict[str, list[dict]], hours: int) -> str:
    rows = ""
    for channel, videos in results.items():
        if not videos:
            continue
        rows += f"""
        <h2 style="color:#c00;margin-top:28px">📺 {channel}</h2>
        <table width="100%" cellpadding="10" cellspacing="0"
               style="border-collapse:collapse;font-family:sans-serif">
          <tr style="background:#f0f0f0">
            <th align="left">Title</th>
            <th align="left" width="180">Published (EST)</th>
          </tr>
        """
        for v in videos:
            pub = datetime.fromisoformat(v["published"]).astimezone(EST).strftime("%Y-%m-%d %H:%M")
            rows += f"""
          <tr style="border-bottom:1px solid #ddd">
            <td><a href="{v['url']}" style="color:#1a0dab">{v['title']}</a></td>
            <td style="color:#555">{pub}</td>
          </tr>
            """
        rows += "</table>"

    checked_at = datetime.now(EST).strftime("%Y-%m-%d %H:%M EST")
    return f"""
    <html><body style="font-family:sans-serif;max-width:700px;margin:auto;padding:20px">
      <h1 style="color:#333">🎬 YouTube Channel Update</h1>
      <p style="color:#666">New videos in the last <strong>{hours} hours</strong>
         &nbsp;·&nbsp; Checked at {checked_at}</p>
      {rows}
      <hr style="margin-top:40px">
      <p style="color:#aaa;font-size:12px">Sent by yt_channel_checker.py</p>
    </body></html>
    """


def send_email(subject: str, html_body: str) -> bool:
    if not GMAIL_ADDRESS:
        print("  ⚠️  Email skipped — GMAIL_ADDRESS not configured.")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = GMAIL_ADDRESS
    msg["To"]      = NOTIFY_EMAIL
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    ctx = ssl.create_default_context()
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx) as server:
            server.login(GMAIL_ADDRESS, GMAIL_APP_PW.replace(" ", ""))
            server.sendmail(GMAIL_ADDRESS, NOTIFY_EMAIL, msg.as_string())
        print(f"  ✉️  Email sent to {NOTIFY_EMAIL}")
        return True
    except smtplib.SMTPAuthenticationError:
        print("  ❌  Gmail auth failed — check GMAIL_ADDRESS and GMAIL_APP_PW")
        return False
    except Exception as e:
        print(f"  ❌  Email error: {e}")
        return False


# ── Main ──────────────────────────────────────────────────────────

def check_channels(hours: int = 24) -> dict[str, list[dict]]:
    since    = datetime.now(timezone.utc) - timedelta(hours=hours)
    channels = load_channels()
    results  = {}

    print(f"\n{'='*60}")
    print(f"  YouTube Channel Checker")
    print(f"  Monitoring {len(channels)} channel(s)")
    print(f"  Looking back {hours} hours  (since {since.astimezone(EST).strftime('%Y-%m-%d %H:%M EST')})")
    print(f"{'='*60}\n")

    for ch in channels:
        name = ch["name"]
        print(f"📺  {name}")
        root = fetch_feed(ch["channel_id"])
        if root is None:
            results[name] = []
            continue

        videos = parse_videos(root, since)
        if videos:
            print(f"  ✅  {len(videos)} new video(s):")
            for v in videos:
                pub = datetime.fromisoformat(v["published"]).astimezone(EST).strftime("%Y-%m-%d %H:%M EST")
                print(f"    📹  {v['title']}")
                print(f"        🕐 {pub}")
                print(f"        🔗 {v['url']}")
            print()
        else:
            print(f"  — No new videos.\n")

        results[name] = videos

    return results


def write_log(results: dict[str, list[dict]]) -> None:
    record = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "results":    results,
    }
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    print(f"📝  Log appended → {LOG_FILE}")


def main():
    parser = argparse.ArgumentParser(description="YouTube channel activity checker")
    parser.add_argument("--hours",      type=int, default=24,
                        help="How many hours back to check (default: 24)")
    parser.add_argument("--test-slack", action="store_true",
                        help="Send a test Slack message to verify webhook")
    parser.add_argument("--test-email", action="store_true",
                        help="Send a test email to verify Gmail setup")
    args = parser.parse_args()

    if args.test_slack:
        print("Sending test Slack message …")
        send_slack_test()
        return

    if args.test_email:
        print("Sending test email …")
        send_email(
            subject="✅ yt_channel_checker — test email",
            html_body="<p>Your YouTube channel checker email is working! 🎉</p>",
        )
        return

    results = check_channels(hours=args.hours)
    write_log(results)

    total_new = sum(len(v) for v in results.values())
    print(f"{'='*60}")
    print(f"  Total new videos found: {total_new}")
    print(f"{'='*60}\n")

    if total_new > 0:
        date_str = datetime.now().strftime("%Y-%m-%d")
        send_slack(results, args.hours)
        send_email(
            subject=f"🎬 {total_new} new YouTube video(s) — {date_str}",
            html_body=build_email_html(results, args.hours),
        )
    else:
        print("  No new videos — no notifications sent.")


if __name__ == "__main__":
    main()
