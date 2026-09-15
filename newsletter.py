#!/usr/bin/env python3
"""
Personal morning newsletter generator.

Pulls RSS feeds (finance/politics, split Ireland vs rest of world), filters
sport items to the teams/players you care about and curates them down to
the genuinely newsworthy ones, fetches today's fixtures and yesterday's
results for Liverpool / Real Betis / Cork City FC (plus full Premier League
and Champions League matchdays) via TheSportsDB, uses Claude to write short
blurbs, rank the most impactful stories, and write a top bulletin, then
renders an HTML "newspaper" page and emails it to you.

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

# Finance / politics feeds, split so they're ranked and rendered separately.
# FT and BBC URLs are verified working RSS feeds as of writing.
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

# General sport feeds, filtered by keyword then curated down by Claude -
# these feeds (especially club-tag feeds like the Echo's) carry a lot of
# volume, most of it not worth your morning.
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
MAX_SPORT_ITEMS = 8

# --- Fixtures / results, all via TheSportsDB ---
# Free, no signup, shared public test key "3". Team and league IDs are
# looked up by name at runtime (not hardcoded) so there's nothing here to
# get wrong or need re-verifying if TheSportsDB's internal IDs change.
THESPORTSDB_BASE = "https://www.thesportsdb.com/api/v1/json/3"
TEAM_SEARCH_NAMES = {
    "Liverpool FC": "Liverpool",
    "Real Betis": "Real Betis",
    "Cork City FC": "Cork City",
}
LEAGUE_SEARCH_NAMES = {
    "Premier League": "English Premier League",
    "Champions League": "UEFA Champions League",
}

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
# FIXTURES & RESULTS (TheSportsDB)
# ---------------------------------------------------------------------------

def tsdb_get(endpoint, params):
    try:
        r = requests.get(f"{THESPORTSDB_BASE}/{endpoint}", params=params, timeout=30)
        if r.status_code != 200:
            print(f"  [warn] TheSportsDB {endpoint}: HTTP {r.status_code}", file=sys.stderr)
            return {}
        return r.json() or {}
    except Exception as exc:
        print(f"  [warn] TheSportsDB {endpoint}: {exc}", file=sys.stderr)
        return {}


def find_team_id(search_name):
    data = tsdb_get("searchteams.php", {"t": search_name})
    teams = data.get("teams") or []
    return teams[0]["idTeam"] if teams else None


def find_league_id(search_name):
    data = tsdb_get("all_leagues.php", {})
    for league in data.get("leagues") or []:
        if league.get("strSport") == "Soccer" and search_name.lower() in (league.get("strLeague") or "").lower():
            return league["idLeague"]
    return None


def event_str(e, with_score=False):
    home, away = e.get("strHomeTeam", "?"), e.get("strAwayTeam", "?")
    league = e.get("strLeague", "")
    if with_score:
        return f"{home} {e.get('intHomeScore', '?')} - {e.get('intAwayScore', '?')} {away}  ({league})"
    time_str = e.get("strTime", "")
    suffix = f" — {time_str} UTC" if time_str else ""
    return f"{home} v {away}{suffix}  ({league})"


def get_team_fixture_today(team_id, today_str):
    data = tsdb_get("eventsnext.php", {"id": team_id})
    return [event_str(e) for e in (data.get("events") or []) if e.get("dateEvent") == today_str]


def get_team_result_yesterday(team_id, yesterday_str):
    data = tsdb_get("eventslast.php", {"id": team_id})
    return [event_str(e, with_score=True) for e in (data.get("results") or []) if e.get("dateEvent") == yesterday_str]


def get_league_day_matches(league_id, date_str):
    data = tsdb_get("eventsday.php", {"d": date_str, "l": league_id})
    return [event_str(e) for e in (data.get("events") or [])]


def build_fixtures_data(today_dublin, yesterday_dublin):
    today_str, yesterday_str = today_dublin.isoformat(), yesterday_dublin.isoformat()

    fixtures_today, results_yesterday = [], []
    for name, search_name in TEAM_SEARCH_NAMES.items():
        team_id = find_team_id(search_name)
        if not team_id:
            print(f"  [warn] TheSportsDB: could not find team id for {name}", file=sys.stderr)
            continue
        fixtures_today.extend(get_team_fixture_today(team_id, today_str))
        results_yesterday.extend(get_team_result_yesterday(team_id, yesterday_str))

    full_matchday = {}
    for name, search_name in LEAGUE_SEARCH_NAMES.items():
        league_id = find_league_id(search_name)
        if not league_id:
            print(f"  [warn] TheSportsDB: could not find league id for {name}", file=sys.stderr)
            continue
        matches = get_league_day_matches(league_id, today_str)
        if matches:
            full_matchday[name] = matches

    return {
        "fixtures_today": fixtures_today,
        "results_yesterday": results_yesterday,
        "full_matchday": full_matchday,
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


def extract_json(raw):
    """Strip markdown code fences and surrounding prose Claude sometimes adds
    despite being told not to, and isolate the JSON array."""
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:]
    start, end = raw.find("["), raw.rfind("]")
    if start != -1 and end != -1 and end > start:
        raw = raw[start:end + 1]
    return raw.strip()


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
Return ONLY a JSON array, no markdown fences, no preamble, no explanation:

[{{"index": <int index from list above>, "blurb": "<2 sentence summary>"}}]
"""
    raw = call_claude(prompt, max_tokens=3000)
    try:
        picks = json.loads(extract_json(raw))
    except json.JSONDecodeError:
        print(f"  [warn] could not parse {region_label} ranking JSON, falling back to first N", file=sys.stderr)
        return [{"item": it, "blurb": it["summary"][:280]} for it in items[:limit]]

    out = []
    for p in picks:
        idx = p.get("index")
        if idx is not None and 0 <= idx < len(items):
            out.append({"item": items[idx], "blurb": p.get("blurb", items[idx]["summary"][:280])})
    return out


def select_top_sport_items(items, limit):
    """Curate down to the genuinely newsworthy items - drops transfer gossip,
    opinion pieces, and minor/youth-team filler that keyword filtering alone
    lets through from broad club-tag feeds."""
    if len(items) <= limit:
        return items
    listing = "\n".join(f"{i}. [{it['source']}] {it['title']} — {it['summary'][:200]}" for i, it in enumerate(items))
    prompt = f"""From this list of football news items (already filtered to Liverpool, Real Betis,
Cork City FC, Troy Parrott, Mohamed Salah, and the Republic of Ireland national team), pick the
{limit} most genuinely newsworthy for a fan's morning briefing: match reports, confirmed team
news, injuries, results, significant transfer developments. Skip speculative transfer gossip,
opinion/ranking pieces, minor youth-team news, and duplicates.

{listing}

Return ONLY a JSON array of the chosen indices, e.g. [0, 3, 5]. No markdown fences, no preamble.
"""
    try:
        raw = call_claude(prompt, max_tokens=200)
        indices = json.loads(extract_json(raw))
        chosen = [items[i] for i in indices if isinstance(i, int) and 0 <= i < len(items)]
        if chosen:
            return chosen
    except Exception as exc:
        print(f"  [warn] sport curation failed, falling back to first N: {exc}", file=sys.stderr)
    return items[:limit]


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

Return ONLY a JSON array of 3-5 short strings. No markdown fences, no preamble, no explanation -
your entire response must be valid JSON starting with [ and ending with ].
"""
    try:
        raw = call_claude(prompt, max_tokens=600)
        bullets = json.loads(extract_json(raw))
        if isinstance(bullets, list) and bullets:
            return bullets
        print(f"  [warn] bulletin JSON parsed but was empty/invalid. Raw: {raw[:200]}", file=sys.stderr)
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

    print("Curating sport items with Claude...")
    sport_curated = select_top_sport_items(sport_filtered, MAX_SPORT_ITEMS)
    print(f"  kept {len(sport_curated)} of {len(sport_filtered)} sport items")

    print("Writing sport blurbs...")
    for it in sport_curated:
        it["blurb"] = blurb_for_sport_item(it)

    print("Writing today's bulletin...")
    bulletin = write_bulletin(ireland_ranked, world_ranked, sport_curated, fixtures_data)
    print(f"  bulletin has {len(bulletin)} bullets")

    html = render_html(bulletin, ireland_ranked, world_ranked, sport_curated, fixtures_data, edition_date)

    print("Sending email...")
    send_email(html, subject=f"Morning Brief — {edition_date}")
    print("Done.")


if __name__ == "__main__":
    main()
