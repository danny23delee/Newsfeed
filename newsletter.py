#!/usr/bin/env python3
"""
Personal morning newsletter generator.

Pulls RSS feeds (finance/politics, split Ireland vs rest of world), filters
sport items to the teams/players you care about, fetches today's fixtures
and yesterday's results for Liverpool / Real Betis / Cork City FC (plus full
Premier League and Champions League matchdays when applicable), uses Claude
to write short blurbs, rank the most impactful stories, and write a top
bulletin, then renders an HTML "newspaper" page and emails it to you.

IMPORTANT: this never fetches or reproduces full paywalled article text
(FT, The Athletic, etc). It only ever uses what the RSS feed itself
publishes (headline + short teaser) and links out to the original for
the rest. That's a hard legal/ToS line - don't change that part.

Run with: python newsletter.py
Required env vars are listed in README.md.
"""

import os
import sys
import json
import smtplib
import datetime as dt
from zoneinfo import ZoneInfo
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import feedparser
import requests

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

LOOKBACK_HOURS = 30
DUBLIN = ZoneInfo("Europe/Dublin")
CLAUDE_MODEL = "claude-sonnet-4-5"

# Finance / politics feeds, split so they can be ranked and rendered
# separately. FT and BBC URLs are verified working RSS feeds as of writing.
# TheJournal.ie is marked VERIFY - confirm with the PowerShell check in
# README.md; if it 404s try "https://www.thejournal.ie/rss/" instead.
IRELAND_FEEDS = {
    "TheJournal.ie (VERIFY)": "https://www.thejournal.ie/feed/",
}
WORLD_FEEDS = {
    "FT - World":     "https://www.ft.com/rss/home/international",
    "FT - UK":        "https://www.ft.com/rss/home/uk",
    "BBC - World":    "http://feeds.bbci.co.uk/news/world/rss.xml",
    "BBC - Business": "http://feeds.bbci.co.uk/news/business/rss.xml",
    "BBC - Politics": "http://feeds.bbci.co.uk/news/politics/rss.xml",
}
MAX_IRELAND_STORIES = 6
MAX_WORLD_STORIES = 8

# General sport feeds, filtered down to your teams/players by keyword.
SPORT_FEEDS = {
    "BBC - Football":                 "http://feeds.bbci.co.uk/sport/football/rss.xml",
    "Sky Sports - Football (VERIFY)": "https://www.skysports.com/rss/12040",
    "Liverpool Echo - LFC (VERIFY)":  "https://www.liverpoolecho.co.uk/all-about/liverpool-fc/?service=rss",
}
TEAM_KEYWORDS = [
    "liverpool", "salah", "mohamed salah",
    "real betis", "betis",
    "cork city",
    "troy parrott",
    "republic of ireland", "ireland national team", "boys in green",
    "league of ireland", "fai ",
]

# --- Fixtures / results ---
# football-data.org: free tier, needs a free API key (FOOTBALL_DATA_API_KEY env var).
# Known team IDs on football-data.org's v4 API - verify once via:
#   curl -H "X-Auth-Token: YOUR_KEY" https://api.football-data.org/v4/teams/64
# (should return Liverpool FC). If it doesn't, look the ID up at
# https://www.football-data.org/ and update below.
FOOTBALL_DATA_BASE = "https://api.football-data.org/v4"
FOOTBALL_DATA_TEAMS = {
    "Liverpool FC": 64,
    "Real Betis": 90,
}
FOOTBALL_DATA_FULL_DAY_COMPETITIONS = {
    "PL": "Premier League",
    "CL": "Champions League",
}

# TheSportsDB: free, no signup, shared public test key "3". Good enough for
# a personal daily lookup; not guaranteed enterprise-grade uptime.
THESPORTSDB_BASE = "https://www.thesportsdb.com/api/v1/json/3"
CORK_CITY_SEARCH_NAME = "Cork City"

# ---------------------------------------------------------------------------
# RSS FETCHING
# ---------------------------------------------------------------------------

def fetch_feed(name, url, cutoff):
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
# FIXTURES & RESULTS
# ---------------------------------------------------------------------------

def fd_get(path, params, api_key):
    try:
        resp = requests.get(
            f"{FOOTBALL_DATA_BASE}{path}",
            headers={"X-Auth-Token": api_key},
            params=params,
            timeout=30,
        )
        if resp.status_code != 200:
            print(f"  [warn] football-data.org {path}: HTTP {resp.status_code}", file=sys.stderr)
            return []
        return resp.json().get("matches", [])
    except Exception as exc:
        print(f"  [warn] football-data.org {path}: {exc}", file=sys.stderr)
        return []


def fd_match_summary(m):
    home = m["homeTeam"]["name"]
    away = m["awayTeam"]["name"]
    comp = m["competition"]["name"]
    utc = dt.datetime.fromisoformat(m["utcDate"].replace("Z", "+00:00"))
    local_time = utc.astimezone(DUBLIN).strftime("%H:%M")
    status = m["status"]
    if status == "FINISHED":
        score = m.get("score", {}).get("fullTime", {})
        return f"{home} {score.get('home', '?')} - {score.get('away', '?')} {away}  ({comp})"
    return f"{home} v {away} — {local_time} Irish time  ({comp})"


def get_team_fixtures_and_results(api_key, today, yesterday):
    """Returns (today_fixtures, yesterday_results) for the named teams."""
    fixtures, results = [], []
    for name, team_id in FOOTBALL_DATA_TEAMS.items():
        today_matches = fd_get(f"/teams/{team_id}/matches",
                                {"dateFrom": today, "dateTo": today}, api_key)
        fixtures.extend(today_matches)
        yesterday_matches = fd_get(f"/teams/{team_id}/matches",
                                    {"dateFrom": yesterday, "dateTo": yesterday}, api_key)
        results.extend([m for m in yesterday_matches if m["status"] == "FINISHED"])
    return fixtures, results


def get_full_matchday(api_key, date):
    """Returns {competition name: [matches]} for PL/CL matches on the given date."""
    out = {}
    for code, name in FOOTBALL_DATA_FULL_DAY_COMPETITIONS.items():
        matches = fd_get(f"/competitions/{code}/matches", {"dateFrom": date, "dateTo": date}, api_key)
        if matches:
            out[name] = matches
    return out


def get_cork_city_fixtures_and_results(today, yesterday):
    fixtures, results = [], []
    try:
        r = requests.get(f"{THESPORTSDB_BASE}/searchteams.php",
                          params={"t": CORK_CITY_SEARCH_NAME}, timeout=30)
        teams = (r.json() or {}).get("teams") or []
        if not teams:
            print("  [warn] TheSportsDB: Cork City team not found", file=sys.stderr)
            return fixtures, results
        team_id = teams[0]["idTeam"]

        r = requests.get(f"{THESPORTSDB_BASE}/eventsnext.php", params={"id": team_id}, timeout=30)
        for e in (r.json() or {}).get("events") or []:
            if e.get("dateEvent") == today:
                fixtures.append(f"{e['strHomeTeam']} v {e['strAwayTeam']} — {e.get('strTime', '')} ({e.get('strLeague', '')})")

        r = requests.get(f"{THESPORTSDB_BASE}/eventslast.php", params={"id": team_id}, timeout=30)
        for e in (r.json() or {}).get("results") or []:
            if e.get("dateEvent") == yesterday:
                results.append(f"{e['strHomeTeam']} {e.get('intHomeScore', '?')} - {e.get('intAwayScore', '?')} {e['strAwayTeam']} ({e.get('strLeague', '')})")
    except Exception as exc:
        print(f"  [warn] TheSportsDB: {exc}", file=sys.stderr)
    return fixtures, results


def build_fixtures_data(today_dublin, yesterday_dublin):
    """Gathers all fixture/result data. Skips football-data.org gracefully if no key set."""
    api_key = os.environ.get("FOOTBALL_DATA_API_KEY")
    today_str, yesterday_str = today_dublin.isoformat(), yesterday_dublin.isoformat()

    team_fixtures_text, team_results_text = [], []
    full_matchday_text = {}

    if api_key:
        fixtures, results = get_team_fixtures_and_results(api_key, today_str, yesterday_str)
        team_fixtures_text.extend(fd_match_summary(m) for m in fixtures)
        team_results_text.extend(fd_match_summary(m) for m in results)

        matchday = get_full_matchday(api_key, today_str)
        for comp_name, matches in matchday.items():
            full_matchday_text[comp_name] = [fd_match_summary(m) for m in matches]
    else:
        print("  [warn] FOOTBALL_DATA_API_KEY not set - skipping Liverpool/Betis/PL/CL fixtures", file=sys.stderr)

    cork_fixtures, cork_results = get_cork_city_fixtures_and_results(today_str, yesterday_str)
    team_fixtures_text.extend(cork_fixtures)
    team_results_text.extend(cork_results)

    return {
        "fixtures_today": team_fixtures_text,
        "results_yesterday": team_results_text,
        "full_matchday": full_matchday_text,
    }

# ---------------------------------------------------------------------------
# CLAUDE
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


def rank_stories(items, limit, region_label):
    if not items:
        return []
    listing = "\n".join(
        f"{i}. [{it['source']}] {it['title']} — {it['summary'][:300]}"
        for i, it in enumerate(items)
    )
    prompt = f"""You're curating the "{region_label}" section of a personal morning newsletter for a
data analyst in Ireland who wants genuinely impactful financial and political news.

Candidate headlines with source teaser text:

{listing}

Pick the {limit} most impactful stories (skip celebrity/soft news and duplicates). For each,
write a neutral 2-sentence blurb based ONLY on the teaser text given - do not invent details.
Return ONLY valid JSON, no markdown fences, no preamble:

[{{"index": <int index from list above>, "blurb": "<2 sentence summary>"}}]
"""
    raw = call_claude(prompt, max_tokens=3000)
    try:
        picks = json.loads(raw)
    except json.JSONDecodeError:
        print(f"  [warn] could not parse {region_label} ranking JSON, falling back to first N", file=sys.stderr)
        return [{"item": it, "blurb": it["summary"][:280]} for it in items[:limit]]

    out = []
    for p in picks:
        idx = p.get("index")
        if idx is not None and 0 <= idx < len(items):
            out.append({"item": items[idx], "blurb": p.get("blurb", items[idx]["summary"][:280])})
    return out


def blurb_for_sport_item(item):
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


def write_bulletin(ireland_stories, world_stories, sport_items, fixtures_data):
    def fmt(stories):
        return "\n".join(f"- {s['item']['title']}: {s['blurb']}" for s in stories) or "(none)"

    sport_lines = "\n".join(f"- {it['title']}: {it.get('blurb', it['summary'][:200])}" for it in sport_items) or "(no team news today)"
    fixtures_lines = "\n".join(fixtures_data["fixtures_today"]) or "(no fixtures today)"
    results_lines = "\n".join(fixtures_data["results_yesterday"]) or "(no results yesterday)"

    prompt = f"""Write a 3-5 bullet "Today's Bulletin" for the top of a personal morning newspaper-style
newsletter, in a punchy front-page tone. Base it ONLY on the information below - no outside facts,
no invented numbers. Pick the single most noteworthy item from each relevant area; skip an area if
nothing in it is genuinely noteworthy.

IRELAND NEWS:
{fmt(ireland_stories)}

WORLD NEWS:
{fmt(world_stories)}

SPORT NEWS:
{sport_lines}

TODAY'S FIXTURES:
{fixtures_lines}

YESTERDAY'S RESULTS:
{results_lines}

Return ONLY a JSON array of 3-5 short strings, no markdown fences, no preamble.
"""
    try:
        raw = call_claude(prompt, max_tokens=600)
        bullets = json.loads(raw)
        if isinstance(bullets, list) and bullets:
            return bullets
    except Exception as exc:
        print(f"  [warn] bulletin generation failed: {exc}", file=sys.stderr)
    return []

# ---------------------------------------------------------------------------
# RENDERING
# ---------------------------------------------------------------------------

def render_html(bulletin, ireland_stories, world_stories, sport_items, fixtures_data, edition_date):
    def story_block(title, source, blurb, link):
        return f"""
        <div style="margin-bottom:22px;padding-bottom:18px;border-bottom:1px solid #ddd;">
          <div style="font-size:18px;font-weight:700;font-family:Georgia,serif;color:#111;">{title}</div>
          <div style="font-size:12px;color:#777;margin:2px 0 6px;text-transform:uppercase;letter-spacing:0.03em;">{source}</div>
          <div style="font-size:14px;color:#333;line-height:1.5;font-family:Georgia,serif;">{blurb}</div>
          <a href="{link}" style="font-size:13px;color:#8b0000;text-decoration:none;">Read full story &rarr;</a>
        </div>
        """

    def section_header(text):
        return f"""<div style="font-family:Georgia,serif;font-size:16px;font-weight:700;text-transform:uppercase;border-bottom:2px solid #111;margin:26px 0 12px;padding-bottom:4px;">{text}</div>"""

    def list_block(lines, empty_msg):
        if not lines:
            return f"<p style='color:#777;font-family:Georgia,serif;font-size:14px;'>{empty_msg}</p>"
        items_html = "".join(f"<li style='margin-bottom:4px;'>{line}</li>" for line in lines)
        return f"<ul style='font-family:Georgia,serif;font-size:14px;color:#333;padding-left:20px;margin:0 0 16px;'>{items_html}</ul>"

    bulletin_html = ""
    if bulletin:
        bullet_items = "".join(f"<li style='margin-bottom:6px;'>{b}</li>" for b in bulletin)
        bulletin_html = f"""
        <div style="background:#111;color:#fff;padding:14px 18px;margin-bottom:22px;">
          <div style="font-family:Georgia,serif;font-size:13px;font-weight:700;text-transform:uppercase;letter-spacing:0.05em;margin-bottom:8px;">Today's Bulletin</div>
          <ul style="font-family:Georgia,serif;font-size:14px;line-height:1.4;padding-left:18px;margin:0;">{bullet_items}</ul>
        </div>
        """

    ireland_html = "".join(
        story_block(s["item"]["title"], s["item"]["source"], s["blurb"], s["item"]["link"]) for s in ireland_stories
    ) or "<p style='color:#777;'>No stories cleared the bar today.</p>"

    world_html = "".join(
        story_block(s["item"]["title"], s["item"]["source"], s["blurb"], s["item"]["link"]) for s in world_stories
    ) or "<p style='color:#777;'>No stories cleared the bar today.</p>"

    sport_html = "".join(
        story_block(it["title"], it["source"], it.get("blurb", it["summary"][:300]), it["link"]) for it in sport_items
    ) or "<p style='color:#777;'>Nothing new on Liverpool, Real Betis, Cork City, Troy Parrott, or the Boys in Green today.</p>"

    fixtures_html = list_block(fixtures_data["fixtures_today"], "No Liverpool, Real Betis, or Cork City fixtures today.")
    results_html = list_block(fixtures_data["results_yesterday"], "No results from yesterday.")

    matchday_html = ""
    for comp_name, lines in fixtures_data["full_matchday"].items():
        matchday_html += f"<div style='font-weight:700;font-family:Georgia,serif;font-size:14px;margin-top:10px;'>{comp_name} - full matchday</div>"
        matchday_html += list_block(lines, "")

    return f"""<!DOCTYPE html>
<html><body style="margin:0;padding:0;background:#f2efe9;">
<div style="max-width:640px;margin:0 auto;background:#fff;padding:28px 24px;">
  <div style="text-align:center;border-bottom:4px double #111;padding-bottom:10px;margin-bottom:18px;">
    <div style="font-family:Georgia,serif;font-size:30px;font-weight:900;letter-spacing:0.02em;">THE MORNING BRIEF</div>
    <div style="font-size:12px;color:#555;text-transform:uppercase;letter-spacing:0.08em;margin-top:4px;">{edition_date}</div>
  </div>

  {bulletin_html}

  {section_header("Ireland")}
  {ireland_html}

  {section_header("Rest of World")}
  {world_html}

  {section_header("Sport")}

  <div style="font-weight:700;font-family:Georgia,serif;font-size:14px;">Today's Fixtures</div>
  {fixtures_html}
  {matchday_html}

  <div style="font-weight:700;font-family:Georgia,serif;font-size:14px;margin-top:14px;">Yesterday's Results</div>
  {results_html}

  <div style="margin-top:16px;">{sport_html}</div>

  <div style="text-align:center;font-size:11px;color:#999;margin-top:24px;">
    Generated automatically. Headlines and teasers only - click through for full articles.
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
    now_utc = dt.datetime.now(dt.timezone.utc)
    cutoff = now_utc - dt.timedelta(hours=LOOKBACK_HOURS)
    now_dublin = now_utc.astimezone(DUBLIN)
    today_dublin = now_dublin.date()
    yesterday_dublin = today_dublin - dt.timedelta(days=1)
    edition_date = now_dublin.strftime("%A %d %B %Y")

    print("Fetching Ireland feeds...")
    ireland_raw = collect(IRELAND_FEEDS, cutoff)

    print("Fetching world feeds...")
    world_raw = collect(WORLD_FEEDS, cutoff)

    print("Fetching sport feeds...")
    sport_raw = collect(SPORT_FEEDS, cutoff)
    sport_filtered = filter_by_keywords(sport_raw, TEAM_KEYWORDS)
    print(f"  {len(sport_filtered)} sport items matched your keywords")

    print("Fetching fixtures & results...")
    fixtures_data = build_fixtures_data(today_dublin, yesterday_dublin)

    print("Ranking Ireland stories with Claude...")
    ireland_ranked = rank_stories(ireland_raw, MAX_IRELAND_STORIES, "Ireland")

    print("Ranking world stories with Claude...")
    world_ranked = rank_stories(world_raw, MAX_WORLD_STORIES, "Rest of World")

    print("Writing sport blurbs...")
    for it in sport_filtered:
        it["blurb"] = blurb_for_sport_item(it)

    print("Writing today's bulletin...")
    bulletin = write_bulletin(ireland_ranked, world_ranked, sport_filtered, fixtures_data)

    html = render_html(bulletin, ireland_ranked, world_ranked, sport_filtered, fixtures_data, edition_date)

    print("Sending email...")
    send_email(html, subject=f"Morning Brief — {edition_date}")
    print("Done.")


if __name__ == "__main__":
    main()
