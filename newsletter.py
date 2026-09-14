#!/usr/bin/env python3
"""
Personal morning newsletter generator.

Pulls RSS feeds (finance/politics + sport), filters sport items to the
teams/players you care about, uses Claude to write short blurbs and to
rank the most impactful finance/political stories, renders an HTML
"newspaper" page, and emails it to you.

IMPORTANT: this never fetches or reproduces full paywalled article text
(FT, The Athletic, etc). It only ever uses what the RSS feed itself
publishes (headline + short teaser) and links out to the original for
the rest. That's a hard legal/ToS line — don't change that part.

Run with: python newsletter.py
Required env vars are listed in the CONFIG section below and in README.md.
"""

import os
import sys
import json
import smtplib
import datetime as dt
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import feedparser
import requests

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

# How far back to look for stories, in hours. Run daily -> 30 gives a little
# overlap margin so nothing published just before midnight gets missed.
LOOKBACK_HOURS = 30

# Finance / politics feeds — international + Ireland.
# FT and BBC URLs below are verified working RSS feeds as of writing.
# The Irish Times / RTE ones are marked VERIFY: RSS URLs on these sites
# change occasionally, so before relying on this, open each URL in a
# browser once and confirm it returns XML, not an error page. If a feed
# breaks, view-source on the section's homepage and search for
# <link rel="alternate" type="application/rss+xml"> to find the new one.
FINANCE_POLITICS_FEEDS = {
    "FT - World":        "https://www.ft.com/rss/home/international",
    "FT - UK":           "https://www.ft.com/rss/home/uk",
    "BBC - World":       "http://feeds.bbci.co.uk/news/world/rss.xml",
    "BBC - Business":    "http://feeds.bbci.co.uk/news/business/rss.xml",
    "BBC - Politics":    "http://feeds.bbci.co.uk/news/politics/rss.xml",
    "RTE - News (VERIFY)":        "https://www.rte.ie/feeds/rss/?index=/news",
    "Irish Times (VERIFY)":       "https://www.irishtimes.com/rss/",
}

# Sport feeds we scan and then filter down to your teams/players.
# These are general football feeds, not team-specific, so filtering by
# keyword below does the real work.
SPORT_FEEDS = {
    "BBC - Football":            "http://feeds.bbci.co.uk/sport/football/rss.xml",
    "RTE - Sport (VERIFY)":      "https://www.rte.ie/feeds/rss/?index=/sport",
    "Sky Sports - Football (VERIFY)": "https://www.skysports.com/rss/12040",
    "Liverpool Echo - LFC (VERIFY)":  "https://www.liverpoolecho.co.uk/all-about/liverpool-fc/?service=rss",
}

# Case-insensitive keywords used to pull sport items relevant to you out
# of the general feeds above. Add/remove freely.
TEAM_KEYWORDS = [
    "liverpool", "salah", "mohamed salah",
    "cork city",
    "troy parrott",
    "republic of ireland", "ireland national team", "boys in green",
    "league of ireland", "fai ",
]

MAX_FINANCE_STORIES = 10   # how many finance/politics stories to keep after ranking
CLAUDE_MODEL = "claude-sonnet-4-5"  # swap if you want a different model

# ---------------------------------------------------------------------------
# FETCHING
# ---------------------------------------------------------------------------

def fetch_feed(name, url, cutoff):
    """Return recent entries from one RSS feed as plain dicts."""
    items = []
    try:
        parsed = feedparser.parse(url)
        if parsed.bozo and not parsed.entries:
            print(f"  [warn] {name}: could not parse feed ({url})", file=sys.stderr)
            return items
        for e in parsed.entries:
            published = None
            for field in ("published_parsed", "updated_parsed"):
                if getattr(e, field, None):
                    published = dt.datetime(*getattr(e, field)[:6], tzinfo=dt.timezone.utc)
                    break
            if published and published < cutoff:
                continue
            items.append({
                "source": name,
                "title": getattr(e, "title", "").strip(),
                "summary": getattr(e, "summary", "").strip(),
                "link": getattr(e, "link", ""),
                "published": published.isoformat() if published else None,
            })
    except Exception as exc:
        print(f"  [warn] {name}: {exc}", file=sys.stderr)
    return items


def collect(feeds, cutoff):
    all_items = []
    for name, url in feeds.items():
        entries = fetch_feed(name, url, cutoff)
        print(f"  {name}: {len(entries)} recent items")
        all_items.extend(entries)
    return all_items


def filter_by_keywords(items, keywords):
    kws = [k.lower() for k in keywords]
    out = []
    for it in items:
        blob = (it["title"] + " " + it["summary"]).lower()
        if any(k in blob for k in kws):
            out.append(it)
    return out

# ---------------------------------------------------------------------------
# CLAUDE: ranking + blurb writing
# ---------------------------------------------------------------------------

def call_claude(prompt, max_tokens=2000):
    api_key = os.environ["ANTHROPIC_API_KEY"]
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": CLAUDE_MODEL,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")


def rank_finance_stories(items, limit):
    """Ask Claude to pick the most impactful stories and return them with blurbs."""
    if not items:
        return []
    listing = "\n".join(
        f"{i}. [{it['source']}] {it['title']} — {it['summary'][:300]}"
        for i, it in enumerate(items)
    )
    prompt = f"""You're curating a personal morning newsletter for a data analyst in Ireland who
wants the most genuinely impactful financial and political news, international and Irish.

Here are today's candidate headlines with their source teaser text:

{listing}

Pick the {limit} most impactful stories (skip celebrity/soft news, duplicate stories, and pure
sports/entertainment). For each, write a neutral 2-sentence blurb based ONLY on the teaser text
given above — do not invent details not present in the teaser. Return ONLY valid JSON, no markdown
fences, no preamble, in this exact shape:

[{{"index": <int index from the numbered list above>, "blurb": "<2 sentence summary>"}}]
"""
    raw = call_claude(prompt, max_tokens=3000)
    try:
        picks = json.loads(raw)
    except json.JSONDecodeError:
        print("  [warn] could not parse ranking JSON, falling back to first N items", file=sys.stderr)
        return [{"item": it, "blurb": it["summary"][:280]} for it in items[:limit]]

    out = []
    for p in picks:
        idx = p.get("index")
        if idx is not None and 0 <= idx < len(items):
            out.append({"item": items[idx], "blurb": p.get("blurb", items[idx]["summary"][:280])})
    return out


def blurb_for_sport_item(item):
    """If the RSS teaser is already substantial, use it as-is. Otherwise ask Claude
    for a short blurb based only on the title + teaser (never fetches full text)."""
    if len(item["summary"]) > 120:
        return item["summary"][:400]
    prompt = f"""Write one short, neutral sentence (max 30 words) summarizing this football news
item, based only on the information given. Do not add facts not present here.

Title: {item['title']}
Teaser: {item['summary']}

Return only the sentence, nothing else.
"""
    try:
        return call_claude(prompt, max_tokens=100).strip()
    except Exception:
        return item["title"]

# ---------------------------------------------------------------------------
# RENDERING
# ---------------------------------------------------------------------------

def render_html(finance_stories, sport_items, edition_date):
    def story_block(title, source, blurb, link):
        return f"""
        <div style="margin-bottom:22px;padding-bottom:18px;border-bottom:1px solid #ddd;">
          <div style="font-size:18px;font-weight:700;font-family:Georgia,serif;color:#111;">{title}</div>
          <div style="font-size:12px;color:#777;margin:2px 0 6px;text-transform:uppercase;letter-spacing:0.03em;">{source}</div>
          <div style="font-size:14px;color:#333;line-height:1.5;font-family:Georgia,serif;">{blurb}</div>
          <a href="{link}" style="font-size:13px;color:#8b0000;text-decoration:none;">Read full story &rarr;</a>
        </div>
        """

    finance_html = "".join(
        story_block(s["item"]["title"], s["item"]["source"], s["blurb"], s["item"]["link"])
        for s in finance_stories
    ) or "<p style='color:#777;'>No stories cleared the bar today.</p>"

    sport_html = "".join(
        story_block(it["title"], it["source"], it.get("blurb", it["summary"][:300]), it["link"])
        for it in sport_items
    ) or "<p style='color:#777;'>Nothing new on Liverpool, Cork City, Troy Parrott, Salah, or the Boys in Green today.</p>"

    return f"""<!DOCTYPE html>
<html><body style="margin:0;padding:0;background:#f2efe9;">
<div style="max-width:640px;margin:0 auto;background:#fff;padding:28px 24px;">
  <div style="text-align:center;border-bottom:4px double #111;padding-bottom:10px;margin-bottom:18px;">
    <div style="font-family:Georgia,serif;font-size:30px;font-weight:900;letter-spacing:0.02em;">THE MORNING BRIEF</div>
    <div style="font-size:12px;color:#555;text-transform:uppercase;letter-spacing:0.08em;margin-top:4px;">{edition_date}</div>
  </div>

  <div style="font-family:Georgia,serif;font-size:16px;font-weight:700;text-transform:uppercase;border-bottom:2px solid #111;margin-bottom:12px;padding-bottom:4px;">
    Finance &amp; Politics — Ireland &amp; World
  </div>
  {finance_html}

  <div style="font-family:Georgia,serif;font-size:16px;font-weight:700;text-transform:uppercase;border-bottom:2px solid #111;margin:26px 0 12px;padding-bottom:4px;">
    Liverpool &middot; Salah &middot; Cork City &middot; Troy Parrott &middot; Boys in Green
  </div>
  {sport_html}

  <div style="text-align:center;font-size:11px;color:#999;margin-top:24px;">
    Generated automatically. Headlines and teasers only — click through for full articles.
  </div>
</div>
</body></html>"""

# ---------------------------------------------------------------------------
# EMAIL
# ---------------------------------------------------------------------------

def send_email(html_body, subject):
    smtp_host = os.environ["SMTP_HOST"]
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    smtp_user = os.environ["SMTP_USER"]
    smtp_pass = os.environ["SMTP_PASS"]
    to_addr = os.environ["TO_EMAIL"]
    from_addr = os.environ.get("FROM_EMAIL", smtp_user)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg.attach(MIMEText(html_body, "html"))

    with smtplib.SMTP(smtp_host, smtp_port) as server:
        server.starttls()
        server.login(smtp_user, smtp_pass)
        server.sendmail(from_addr, [to_addr], msg.as_string())

# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main():
    now = dt.datetime.now(dt.timezone.utc)
    cutoff = now - dt.timedelta(hours=LOOKBACK_HOURS)
    edition_date = now.strftime("%A %d %B %Y")

    print("Fetching finance/politics feeds...")
    finance_raw = collect(FINANCE_POLITICS_FEEDS, cutoff)

    print("Fetching sport feeds...")
    sport_raw = collect(SPORT_FEEDS, cutoff)
    sport_filtered = filter_by_keywords(sport_raw, TEAM_KEYWORDS)
    print(f"  {len(sport_filtered)} sport items matched your keywords")

    print("Ranking finance/politics stories with Claude...")
    finance_ranked = rank_finance_stories(finance_raw, MAX_FINANCE_STORIES)

    print("Writing sport blurbs...")
    for it in sport_filtered:
        it["blurb"] = blurb_for_sport_item(it)

    html = render_html(finance_ranked, sport_filtered, edition_date)

    print("Sending email...")
    send_email(html, subject=f"Morning Brief — {edition_date}")
    print("Done.")


if __name__ == "__main__":
    main()
