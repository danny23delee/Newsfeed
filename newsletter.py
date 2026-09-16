#!/usr/bin/env python3
"""
Personal morning newsletter generator.

Pulls RSS feeds (Ireland + world + technology), has Claude score every story
1-10 and assign it a category (Ireland / Business / Politics / Technology /
World), then gives full "what happened / why it matters / watch next"
treatment to anything score 6+ and a bare headline to anything score 4-5.
Also pulls football fixtures/results (Liverpool, Real Betis, Cork City FC,
plus full PL/Champions League matchdays) via TheSportsDB, and separately
curates + blurbs sport news for the teams/players you follow. Writes a
top "Today's Bulletin", renders an HTML newspaper page, and emails it.

IMPORTANT: this never fetches or reproduces full paywalled article text
(FT, The Athletic, etc). It only ever uses what the RSS feed itself
publishes (headline + short teaser) and links out to the original for
the rest. That's a hard legal/ToS line - don't change that part.

Run with: python newsletter.py
Required env vars are listed in README.md.
"""

import os
import re
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

# Path to the dedupe state file. Committed back to the repo by the GitHub
# Actions workflow after each run - see the "Persist dedupe state" step.
SEEN_STORIES_PATH = "seen_stories.json"
SEEN_RETENTION_DAYS = 5  # comfortably longer than LOOKBACK_HOURS so a story
                          # can't reappear across two consecutive runs

WEEKLY_HISTORY_PATH = "weekly_history.json"
WEEKLY_HISTORY_RETENTION_DAYS = 8  # a little over a week, so Sunday always has a full week on file

# --- Weather (Open-Meteo, free, no key needed) ---
DUBLIN_LAT, DUBLIN_LON = 53.3498, -6.2603
WMO_WEATHER_CODES = {
    0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Depositing rime fog",
    51: "Light drizzle", 53: "Moderate drizzle", 55: "Dense drizzle",
    56: "Light freezing drizzle", 57: "Dense freezing drizzle",
    61: "Slight rain", 63: "Moderate rain", 65: "Heavy rain",
    66: "Light freezing rain", 67: "Heavy freezing rain",
    71: "Slight snow", 73: "Moderate snow", 75: "Heavy snow", 77: "Snow grains",
    80: "Slight rain showers", 81: "Moderate rain showers", 82: "Violent rain showers",
    85: "Slight snow showers", 86: "Heavy snow showers",
    95: "Thunderstorm", 96: "Thunderstorm with slight hail", 99: "Thunderstorm with heavy hail",
}

# --- Economic calendar ---
# Static list of known scheduled events (dates are published well in advance
# by the ECB/Fed/Irish government, so this is more reliable than any free
# calendar API). Update yearly - see README.md.
ECON_EVENTS = [
    {"name": "Irish Budget Day (Budget 2027)", "date": "2026-10-06"},
    {"name": "Federal Reserve (FOMC) rate decision", "date": "2026-01-28"},
    {"name": "Federal Reserve (FOMC) rate decision", "date": "2026-03-18"},
    {"name": "Federal Reserve (FOMC) rate decision", "date": "2026-04-29"},
    {"name": "Federal Reserve (FOMC) rate decision", "date": "2026-06-17"},
    {"name": "Federal Reserve (FOMC) rate decision", "date": "2026-07-29"},
    {"name": "Federal Reserve (FOMC) rate decision", "date": "2026-09-16"},
    {"name": "Federal Reserve (FOMC) rate decision", "date": "2026-10-28"},
    {"name": "Federal Reserve (FOMC) rate decision", "date": "2026-12-09"},
    {"name": "ECB Governing Council rate decision", "date": "2026-02-05"},
    {"name": "ECB Governing Council rate decision", "date": "2026-03-19"},
    {"name": "ECB Governing Council rate decision", "date": "2026-04-30"},
    {"name": "ECB Governing Council rate decision", "date": "2026-06-11"},
    {"name": "ECB Governing Council rate decision", "date": "2026-07-23"},
    {"name": "ECB Governing Council rate decision", "date": "2026-09-10"},
    {"name": "ECB Governing Council rate decision", "date": "2026-10-29"},
    {"name": "ECB Governing Council rate decision", "date": "2026-12-17"},
    {"name": "ECB Governing Council rate decision", "date": "2027-02-04"},
    {"name": "ECB Governing Council rate decision", "date": "2027-03-18"},
]
ECON_EVENTS_HORIZON_DAYS = 10  # show events within this many days

# News feeds. Category (Ireland/Business/Politics/Technology/World) is
# decided by Claude from content, not by which feed a story came from - so
# these groupings just control which feeds get fetched, not where a story
# ends up.
IRELAND_FEEDS = {
    "TheJournal.ie": "https://www.thejournal.ie/feed/",
    "RTE - News":             "https://www.rte.ie/feeds/rss/?index=/news",
}
WORLD_FEEDS = {
    "FT - World":     "https://www.ft.com/rss/home/international",
    "FT - UK":        "https://www.ft.com/rss/home/uk",
    "BBC - World":    "http://feeds.bbci.co.uk/news/world/rss.xml",
    "BBC - Business": "http://feeds.bbci.co.uk/news/business/rss.xml",
    "BBC - Politics": "http://feeds.bbci.co.uk/news/politics/rss.xml",
}
TECH_FEEDS = {
    "BBC - Technology": "http://feeds.bbci.co.uk/news/technology/rss.xml",
}

CATEGORIES = ["Ireland", "Business", "Politics", "Technology", "World"]
CATEGORY_DISPLAY = {
    "Ireland":     "\U0001F1EE\U0001F1EA Ireland",
    "Business":    "\U0001F4B7 Business & Markets",
    "Politics":    "\U0001F3DB\uFE0F Politics",
    "Technology":  "\U0001F916 Technology",
    "World":       "\U0001F30D World",
}
TOP_LABEL = "\U0001F534 Top Stories"
WORTH_KNOWING_LABEL = "\U0001F440 Worth Knowing"

# Scoring thresholds, per your rubric: 10 must-know, 8-9 very important,
# 6-7 interesting, 4-5 minor, 1-3 ignore.
SCORE_TOP_THRESHOLD = 9        # >= this -> Top Stories, full treatment
SCORE_CATEGORY_THRESHOLD = 6   # >= this (and below top) -> category section, full treatment
SCORE_WORTH_KNOWING_THRESHOLD = 4  # >= this (and below category) -> headline only
MAX_TOP_STORIES = 5
MAX_PER_CATEGORY = 4
MAX_WORTH_KNOWING = 6

# General sport feeds, filtered by keyword then curated down by Claude.
SPORT_FEEDS = {
    "BBC - Football":                 "http://feeds.bbci.co.uk/sport/football/rss.xml",
    "Sky Sports - Football":          "https://www.skysports.com/rss/11095",
    "Liverpool Echo - LFC":  "https://www.liverpoolecho.co.uk/all-about/liverpool-fc/?service=rss",
}
# CIES Football Observatory - genuinely free/public research posts, not
# paywalled. Disabled for now: the guessed RSS URL 404s and the real one
# wasn't confirmed. Once you find the working URL, put it back in here
# (same VERIFY-with-PowerShell approach as the other feeds) - the rest of
# the pipeline (rendering, dedupe) already handles it, this dict is the
# only thing to change.
CIES_FEEDS = {
    # "CIES Football Observatory": "https://www.cies.ch/...",
}

# General (non-football-specific) sport feeds, for the "Other Sports"
# section - a handful of headlines from across everything else (rugby,
# GAA, tennis, boxing, etc.). Football stories are filtered back out of
# this pool below so they don't duplicate the football sections.
OTHER_SPORTS_FEEDS = {
    "BBC - Sport":        "http://feeds.bbci.co.uk/sport/rss.xml",
    "Sky Sports - News":  "https://www.skysports.com/rss/12040",
}
MAX_OTHER_SPORTS_ITEMS = 4
LIVERPOOL_ECHO_SOURCE = "Liverpool Echo - LFC (VERIFY)"

TEAM_KEYWORDS = [
    "liverpool", "salah", "mohamed salah",
    "real betis", "betis",
    "cork city",
    "troy parrott",
    "republic of ireland", "ireland national team", "boys in green",
    "league of ireland", "fai ",
]
PL_KEYWORDS = ["premier league"]
CL_KEYWORDS = ["champions league"]
MAX_SPORT_ITEMS = 8      # cap on team/player news after curation
MAX_ECHO_ITEMS = 4       # of which, at most this many can be from the Echo
MAX_PL_ITEMS = 3         # general Premier League storylines (not team-specific)
MAX_CL_ITEMS = 3         # general Champions League storylines
MAX_CIES_ITEMS = 3       # CIES posts are rare - just show what's recent, no curation needed

# --- Fixtures / results, all via TheSportsDB (free, no signup) ---
# Team and league IDs are looked up by name at runtime, not hardcoded.
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

# --- Betting odds (The Odds API, free tier - 500 credits/month, needs a
# free key at the-odds-api.com; ODDS_API_KEY env var). Optional: skipped
# gracefully if the key isn't set. Shown as a small standalone line, not
# tied to any other fixture display. ---
ODDS_API_BASE = "https://api.the-odds-api.com/v4"
ODDS_SPORT_KEYS = {
    "Liverpool FC": "soccer_epl",
    "Real Betis": "soccer_spain_la_liga",
}

# ---------------------------------------------------------------------------
# RSS FETCHING
# ---------------------------------------------------------------------------

FEED_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"


def fetch_feed(name, url, cutoff):
    items = []
    try:
        parsed = feedparser.parse(url, request_headers={"User-Agent": FEED_USER_AGENT})
        if parsed.bozo and not parsed.entries:
            status = getattr(parsed, "status", "unknown")
            print(f"  [warn] {name}: could not parse feed (HTTP status: {status}, url: {url})", file=sys.stderr)
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


def normalize_title(title):
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()


def dedupe_by_title_and_link(items):
    """Collapses the same story appearing under multiple feeds (very common
    with FT's World vs UK feeds carrying an identical headline under
    different URLs) - keeps the first occurrence encountered."""
    seen_links, seen_titles, out = set(), set(), []
    for it in items:
        link = it.get("link", "")
        title_key = normalize_title(it.get("title", ""))
        if (link and link in seen_links) or (title_key and title_key in seen_titles):
            continue
        if link:
            seen_links.add(link)
        if title_key:
            seen_titles.add(title_key)
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
    if not teams:
        return None
    exclude_terms = ("u21", "u20", "u19", "u23", "u18", "u17", "women", "ladies", "academy", "reserves", "b team", "youth")
    # Prefer an exact (case-insensitive) name match - e.g. "Republic of Ireland"
    # rather than "Republic of Ireland U21", which searchteams.php also returns.
    for t in teams:
        if (t.get("strTeam") or "").strip().lower() == search_name.strip().lower():
            return t["idTeam"]
    for t in teams:
        name_l = (t.get("strTeam") or "").lower()
        if not any(term in name_l for term in exclude_terms):
            return t["idTeam"]
    return teams[0]["idTeam"]


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
    team_ids = {}
    for name, search_name in TEAM_SEARCH_NAMES.items():
        team_id = find_team_id(search_name)
        team_ids[name] = team_id
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

    # --- Odds: a small standalone "next match odds" line per tracked team,
    # independent of any fixture list above (not tied to a specific "today"
    # or "next" entry - just whatever the soonest upcoming match is). ---
    odds_lines = []
    odds_api_key = os.environ.get("ODDS_API_KEY")
    if odds_api_key:
        for name, sport_key in ODDS_SPORT_KEYS.items():
            odds_dict = get_odds_for_team(sport_key, TEAM_SEARCH_NAMES[name], odds_api_key)
            if odds_dict:
                odds_lines.append(f"{odds_dict['home']} v {odds_dict['away']}: {odds_dict['prices']} (via {odds_dict['bookmaker']})")
    else:
        print("  [warn] ODDS_API_KEY not set - skipping betting odds", file=sys.stderr)

    return {
        "fixtures_today": fixtures_today,
        "results_yesterday": results_yesterday,
        "full_matchday": full_matchday,
        "odds_lines": odds_lines,
    }

# ---------------------------------------------------------------------------
# WEATHER & ECONOMIC CALENDAR
# ---------------------------------------------------------------------------

def get_dublin_weather():
    try:
        r = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": DUBLIN_LAT, "longitude": DUBLIN_LON,
                "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,weathercode",
                "timezone": "Europe/Dublin", "forecast_days": 1,
            },
            timeout=20,
        )
        r.raise_for_status()
        d = r.json()["daily"]
        code = d["weathercode"][0]
        desc = WMO_WEATHER_CODES.get(code, "Mixed conditions")
        lo, hi = round(d["temperature_2m_min"][0]), round(d["temperature_2m_max"][0])
        rain = d["precipitation_probability_max"][0]
        return f"Dublin today: {desc}, {lo}\u2013{hi}\u00b0C, {rain}% chance of rain"
    except Exception as exc:
        print(f"  [warn] weather fetch failed: {exc}", file=sys.stderr)
        return None


def get_upcoming_econ_events(today_dublin):
    upcoming = []
    for ev in ECON_EVENTS:
        try:
            ev_date = dt.date.fromisoformat(ev["date"])
        except ValueError:
            continue
        delta = (ev_date - today_dublin).days
        if 0 <= delta <= ECON_EVENTS_HORIZON_DAYS:
            upcoming.append((delta, ev_date, ev["name"]))
    upcoming.sort(key=lambda x: x[0])
    lines = []
    for delta, ev_date, name in upcoming:
        when = "today" if delta == 0 else ("tomorrow" if delta == 1 else f"in {delta} days")
        lines.append(f"{name} \u2014 {ev_date.strftime('%a %d %b')} ({when})")
    return lines

# ---------------------------------------------------------------------------
# DEDUPE STATE
# ---------------------------------------------------------------------------

def story_key(item):
    return item.get("link") or item.get("title", "").strip().lower()


def load_seen_state(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_seen_state(state, path):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, sort_keys=True)
    except Exception as exc:
        print(f"  [warn] could not save dedupe state file: {exc}", file=sys.stderr)


def filter_unseen(items, seen):
    return [it for it in items if story_key(it) not in seen]


def mark_seen(items, seen, today_str):
    for it in items:
        seen[story_key(it)] = today_str


def prune_seen(seen, cutoff_date_str):
    return {k: v for k, v in seen.items() if v >= cutoff_date_str}

# ---------------------------------------------------------------------------
# WEEKLY DIGEST HISTORY
# ---------------------------------------------------------------------------

def load_weekly_history(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def save_weekly_history(history, path):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(history, f, indent=2)
    except Exception as exc:
        print(f"  [warn] could not save weekly history file: {exc}", file=sys.stderr)


def append_today_to_history(history, today_str, top_stories):
    entry = {
        "date": today_str,
        "stories": [
            {"title": it["title"], "category": it.get("category", ""), "why_it_matters": it.get("breakdown", {}).get("why_it_matters", "")}
            for it in top_stories
        ],
    }
    history.append(entry)
    return history


def prune_history(history, cutoff_date_str):
    return [e for e in history if e["date"] >= cutoff_date_str]


def get_odds_for_team(sport_key, team_search_name, api_key):
    """Match-winner (h2h) odds for the team's soonest upcoming fixture in
    that competition. Returns a dict {home, away, prices, bookmaker} or
    None. Odds are typically only published by bookmakers a few days
    before kickoff, so it's normal for this to return None well in
    advance of the next match - that's not a bug."""
    try:
        r = requests.get(
            f"{ODDS_API_BASE}/sports/{sport_key}/odds",
            params={"apiKey": api_key, "regions": "uk", "markets": "h2h", "oddsFormat": "decimal"},
            timeout=20,
        )
        if r.status_code != 200:
            print(f"  [warn] Odds API {sport_key}: HTTP {r.status_code} - {r.text[:200]}", file=sys.stderr)
            return None
        events = r.json()
        matches = [ev for ev in events if team_search_name.lower() in ev.get("home_team", "").lower() or team_search_name.lower() in ev.get("away_team", "").lower()]
        if not matches:
            print(f"  [warn] Odds API {sport_key}: no upcoming fixture found for {team_search_name} in {len(events)} events returned", file=sys.stderr)
            return None
        matches.sort(key=lambda ev: ev.get("commence_time", ""))
        ev = matches[0]
        bookmakers = ev.get("bookmakers") or []
        if not bookmakers:
            print(f"  [warn] Odds API {sport_key}: fixture found for {team_search_name} but no bookmaker has posted odds yet", file=sys.stderr)
            return None
        market = next((m for m in bookmakers[0].get("markets", []) if m["key"] == "h2h"), None)
        if not market:
            return None
        prices = ", ".join(f"{o['name']} {o['price']}" for o in market["outcomes"])
        return {"home": ev.get("home_team", ""), "away": ev.get("away_team", ""), "prices": prices, "bookmaker": bookmakers[0].get("title", "bookmaker")}
    except Exception as exc:
        print(f"  [warn] Odds API {sport_key}: {exc}", file=sys.stderr)
    return None

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
        timeout=90,
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

# --- News: score + categorize, then full "why it matters" breakdown ---

def score_and_categorize(items):
    """Assigns every item a category and a 1-10 importance score."""
    if not items:
        return
    listing = "\n".join(
        f"{it['_gid']}. [{it['source']}] {it['title']} — {it['summary'][:250]}"
        for it in items
    )
    categories_str = ", ".join(CATEGORIES)
    prompt = f"""You're triaging stories for a personal morning newsletter for a data analyst in
Ireland. For each story below, assign:
- "category": exactly one of [{categories_str}] (Ireland = specifically about Ireland; World =
  international/geopolitical news not fitting the other categories)
- "score": importance 1-10, using this rubric: 10 = must know, 8-9 = very important,
  6-7 = interesting, 4-5 = minor, 1-3 = ignore (routine/trivial/celebrity/duplicate coverage)

Stories:
{listing}

Return ONLY a JSON array covering every story above, no markdown fences, no preamble:
[{{"id": <id>, "category": "<category>", "score": <int>}}]
"""
    raw = call_claude(prompt, max_tokens=4000)
    by_id = {it["_gid"]: it for it in items}
    try:
        results = json.loads(extract_json(raw))
        for r in results:
            item = by_id.get(r.get("id"))
            if item and r.get("category") in CATEGORIES and isinstance(r.get("score"), int):
                item["category"] = r["category"]
                item["score"] = r["score"]
    except Exception as exc:
        print(f"  [warn] scoring/categorization failed: {exc}", file=sys.stderr)
    # anything Claude didn't return a valid result for gets dropped later
    # (no category/score set -> excluded by the filtering step)


def get_why_it_matters(items):
    """Full 3-part breakdown for the stories that made the cut (score >= 6)."""
    if not items:
        return
    listing = "\n".join(
        f"{it['_gid']}. [{it['category']}] {it['title']} — {it['summary'][:300]}"
        for it in items
    )
    prompt = f"""For each story below, based ONLY on the teaser text given (never invent facts or
figures not present here), write:
- "what_happened": 1-2 plain sentences on what happened
- "why_it_matters": 1 sentence on why it matters
- "watch_next": 1 sentence on what to watch for next

Stories:
{listing}

Return ONLY a JSON array covering every story above, no markdown fences, no preamble:
[{{"id": <id>, "what_happened": "...", "why_it_matters": "...", "watch_next": "..."}}]
"""
    raw = call_claude(prompt, max_tokens=4000)
    by_id = {it["_gid"]: it for it in items}
    try:
        results = json.loads(extract_json(raw))
        for r in results:
            item = by_id.get(r.get("id"))
            if item:
                item["breakdown"] = {
                    "what_happened": r.get("what_happened", ""),
                    "why_it_matters": r.get("why_it_matters", ""),
                    "watch_next": r.get("watch_next", ""),
                }
    except Exception as exc:
        print(f"  [warn] why-it-matters generation failed: {exc}", file=sys.stderr)
        for it in items:
            it["breakdown"] = {"what_happened": it["summary"][:280], "why_it_matters": "", "watch_next": ""}


def build_news_sections(all_news_raw):
    """Scores/categorizes everything, splits into top/category/worth-knowing,
    and fetches the full breakdown for anything that needs one."""
    for i, it in enumerate(all_news_raw):
        it["_gid"] = i

    print(f"  scoring & categorizing {len(all_news_raw)} stories...")
    score_and_categorize(all_news_raw)

    scored = [it for it in all_news_raw if "score" in it]
    print(f"  {len(scored)}/{len(all_news_raw)} stories scored successfully")

    scored.sort(key=lambda it: it["score"], reverse=True)

    top_stories = [it for it in scored if it["score"] >= SCORE_TOP_THRESHOLD][:MAX_TOP_STORIES]
    used_gids = {it["_gid"] for it in top_stories}

    category_sections = {cat: [] for cat in CATEGORIES}
    worth_knowing = []
    for it in scored:
        if it["_gid"] in used_gids:
            continue
        if it["score"] >= SCORE_CATEGORY_THRESHOLD:
            if len(category_sections[it["category"]]) < MAX_PER_CATEGORY:
                category_sections[it["category"]].append(it)
                used_gids.add(it["_gid"])
        elif it["score"] >= SCORE_WORTH_KNOWING_THRESHOLD:
            if len(worth_knowing) < MAX_WORTH_KNOWING:
                worth_knowing.append(it)
                used_gids.add(it["_gid"])

    needs_breakdown = top_stories + [it for items in category_sections.values() for it in items]
    print(f"  writing 'why it matters' for {len(needs_breakdown)} stories...")
    get_why_it_matters(needs_breakdown)

    return top_stories, category_sections, worth_knowing

# --- Sport ---

def curate_sport_items(items, limit, context_description):
    """Generic Claude curation for a pool of football news items - used for
    both team/player news and general competition storylines, distinguished
    by context_description."""
    if not items:
        return []
    if len(items) <= limit:
        return items
    listing = "\n".join(f"{i}. [{it['source']}] {it['title']} — {it['summary'][:200]}" for i, it in enumerate(items))
    prompt = f"""From this list of football news items ({context_description}), pick the {limit}
most genuinely newsworthy for a fan's morning briefing. Skip speculative transfer gossip,
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
        print(f"  [warn] sport curation failed ({context_description}), falling back to first N: {exc}", file=sys.stderr)
    return items[:limit]


def cap_source_count(items, source_name, max_count):
    """Keeps at most max_count items from a given source, preserving order
    and keeping everything from other sources untouched."""
    out, count = [], 0
    for it in items:
        if it["source"] == source_name:
            if count >= max_count:
                continue
            count += 1
        out.append(it)
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

# --- Bulletin ---

def write_bulletin(top_stories, category_sections, sport_items, fixtures_data):
    def fmt_full(items):
        return "\n".join(f"- {it['title']}: {it.get('breakdown', {}).get('why_it_matters', it['summary'][:150])}" for it in items) or "(none)"

    all_category_items = [it for items in category_sections.values() for it in items]
    sport_lines = "\n".join(f"- {it['title']}: {it.get('blurb', it['summary'][:200])}" for it in sport_items) or "(no team news today)"
    fixtures_lines = "\n".join(fixtures_data["fixtures_today"]) or "(no fixtures today)"
    results_lines = "\n".join(fixtures_data["results_yesterday"]) or "(no results yesterday)"

    prompt = f"""Write a 3-5 bullet "Today's Bulletin" for the top of a personal morning newspaper-style
newsletter, in a punchy front-page tone. Base it ONLY on the information below - no outside facts,
no invented numbers. Pick the single most noteworthy item overall; skip an area if nothing in it
is genuinely noteworthy.

TOP STORIES:
{fmt_full(top_stories)}

OTHER NOTABLE NEWS:
{fmt_full(all_category_items)}

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


def write_weekly_digest(history):
    """Recaps the past week's Top Stories (score 9-10 items) already on file.
    Called BEFORE today's own top stories are appended to history, so it
    reads as a lead-in to today rather than repeating today's Top Stories."""
    lines = []
    for entry in history:
        for s in entry["stories"]:
            lines.append(f"- ({entry['date']}) [{s.get('category', '')}] {s['title']}: {s.get('why_it_matters', '')}")
    if not lines:
        return []
    prompt = f"""Write a 5-8 bullet "This Week" digest recapping the most significant stories from the
past week, for a personal newspaper-style newsletter. Base it ONLY on the information below - no
outside facts, no invented developments. Where several days touched the same ongoing story,
synthesize into one bullet rather than repeating it - don't just restate every headline verbatim.

{chr(10).join(lines)}

Return ONLY a JSON array of 5-8 strings. No markdown fences, no preamble, no explanation - your
entire response must be valid JSON starting with [ and ending with ].
"""
    try:
        raw = call_claude(prompt, max_tokens=800)
        digest = json.loads(extract_json(raw))
        if isinstance(digest, list) and digest:
            return digest
    except Exception as exc:
        print(f"  [warn] weekly digest generation failed: {exc}", file=sys.stderr)
    return []

# ---------------------------------------------------------------------------
# RENDERING
# ---------------------------------------------------------------------------

def render_html(bulletin, top_stories, category_sections, worth_knowing,
                 team_sport_items, pl_sport_items, cl_sport_items, other_sport_items, cies_items,
                 fixtures_data, edition_date, weather_line=None, econ_events=None, weekly_digest=None):

    def section_header(text, size="16px"):
        return f"""<div style="font-family:Georgia,serif;font-size:{size};font-weight:700;text-transform:uppercase;border-bottom:2px solid #111;margin:26px 0 12px;padding-bottom:4px;">{text}</div>"""

    def full_story_block(it):
        b = it.get("breakdown", {})
        flowing = " ".join(
            part for part in (b.get("what_happened", ""), b.get("why_it_matters", ""), b.get("watch_next", "")) if part
        )
        return f"""
        <div style="margin-bottom:22px;padding-bottom:18px;border-bottom:1px solid #ddd;">
          <div style="font-size:18px;font-weight:700;font-family:Georgia,serif;color:#111;">{it['title']}</div>
          <div style="font-size:12px;color:#777;margin:2px 0 8px;text-transform:uppercase;letter-spacing:0.03em;">{it['source']}</div>
          <div style="font-size:14px;color:#333;line-height:1.55;font-family:Georgia,serif;">{flowing}</div>
          <a href="{it['link']}" style="font-size:13px;color:#8b0000;text-decoration:none;">Read full story &rarr;</a>
        </div>
        """

    def blurb_story_block(title, source, blurb, link):
        return f"""
        <div style="margin-bottom:22px;padding-bottom:18px;border-bottom:1px solid #ddd;">
          <div style="font-size:18px;font-weight:700;font-family:Georgia,serif;color:#111;">{title}</div>
          <div style="font-size:12px;color:#777;margin:2px 0 6px;text-transform:uppercase;letter-spacing:0.03em;">{source}</div>
          <div style="font-size:14px;color:#333;line-height:1.5;font-family:Georgia,serif;">{blurb}</div>
          <a href="{link}" style="font-size:13px;color:#8b0000;text-decoration:none;">Read full story &rarr;</a>
        </div>
        """

    def list_block(lines, empty_msg, font_size="14px"):
        if not lines:
            return f"<p style='color:#777;font-family:Georgia,serif;font-size:{font_size};'>{empty_msg}</p>"
        items_html = "".join(f"<li style='margin-bottom:6px;'>{line}</li>" for line in lines)
        return f"<ul style='font-family:Georgia,serif;font-size:{font_size};color:#333;padding-left:22px;margin:0 0 16px;'>{items_html}</ul>"

    # --- Bulletin banner ---
    bulletin_html = ""
    if bulletin:
        bullet_items = "".join(f"<li style='margin-bottom:6px;'>{b}</li>" for b in bulletin)
        bulletin_html = f"""
        <div style="background:#111;color:#fff;padding:14px 18px;margin-bottom:22px;">
          <div style="font-family:Georgia,serif;font-size:13px;font-weight:700;text-transform:uppercase;letter-spacing:0.05em;margin-bottom:8px;">Today's Bulletin</div>
          <ul style="font-family:Georgia,serif;font-size:14px;line-height:1.4;padding-left:18px;margin:0;">{bullet_items}</ul>
        </div>
        """

    # --- Fixtures & Results: back to basics - just today's fixtures and
    # yesterday's results, plus the full PL/CL matchday when applicable. ---
    fixtures_inner = f"""
      <div style="font-weight:700;font-family:Georgia,serif;font-size:16px;margin-bottom:6px;">Today's Fixtures</div>
      {list_block(fixtures_data['fixtures_today'], "No Liverpool, Real Betis, or Cork City fixtures today.", font_size="15px")}
    """
    for comp_name, lines in fixtures_data["full_matchday"].items():
        fixtures_inner += f"<div style='font-weight:700;font-family:Georgia,serif;font-size:15px;margin-top:8px;'>{comp_name} — full matchday</div>"
        fixtures_inner += list_block(lines, "", font_size="15px")
    fixtures_inner += f"""
      <div style="font-weight:700;font-family:Georgia,serif;font-size:16px;margin-top:14px;margin-bottom:6px;">Yesterday's Results</div>
      {list_block(fixtures_data['results_yesterday'], "No results from yesterday.", font_size="15px")}
    """
    fixtures_html = f"""
    <div style="border:2px solid #111;padding:16px 18px;margin-bottom:24px;background:#faf8f4;">
      <div style="font-family:Georgia,serif;font-size:20px;font-weight:900;text-transform:uppercase;margin-bottom:10px;">\u26bd Fixtures &amp; Results</div>
      {fixtures_inner}
    </div>
    """

    # --- Betting odds: small standalone line(s), informational only ---
    odds_html = ""
    if fixtures_data.get("odds_lines"):
        odds_html = f"""
        <div style="border:1px solid #ccc;padding:12px 16px;margin-bottom:24px;">
          <div style="font-family:Georgia,serif;font-size:14px;font-weight:700;text-transform:uppercase;margin-bottom:6px;">\U0001F4B7 Next Match Odds (informational only)</div>
          {list_block(fixtures_data["odds_lines"], "", font_size="13px")}
        </div>
        """


    # --- Economic calendar ---
    econ_html = ""
    if econ_events:
        econ_html = f"""
        <div style="border:1px solid #ccc;padding:12px 16px;margin-bottom:24px;">
          <div style="font-family:Georgia,serif;font-size:14px;font-weight:700;text-transform:uppercase;margin-bottom:6px;">\U0001F4C5 On The Calendar</div>
          {list_block(econ_events, "", font_size="13px")}
        </div>
        """

    # --- Weather line ---
    weather_html = f"""<div style="text-align:center;font-family:Georgia,serif;font-size:13px;color:#555;margin-top:6px;">{weather_line}</div>""" if weather_line else ""

    # --- Top stories ---
    top_html = "".join(full_story_block(it) for it in top_stories) or "<p style='color:#777;'>Nothing cleared the must-know bar today.</p>"

    # --- Weekly digest (Sundays only, when there's history to recap) ---
    weekly_html = ""
    if weekly_digest:
        digest_items = "".join(f"<li style='margin-bottom:8px;'>{b}</li>" for b in weekly_digest)
        weekly_html = f"""
        {section_header("\U0001F4CA This Week", size="18px")}
        <ul style="font-family:Georgia,serif;font-size:14px;line-height:1.5;color:#333;padding-left:20px;margin:0 0 16px;">{digest_items}</ul>
        """

    # --- Category sections ---
    category_html = ""
    for cat in CATEGORIES:
        items = category_sections.get(cat, [])
        if not items:
            continue
        category_html += section_header(CATEGORY_DISPLAY[cat])
        category_html += "".join(full_story_block(it) for it in items)

    # --- Worth knowing ---
    worth_lines = [f"<a href='{it['link']}' style='color:#333;text-decoration:none;'>{it['title']}</a> <span style='color:#999;font-size:12px;'>({it['source']})</span>" for it in worth_knowing]
    worth_html = list_block(worth_lines, "Nothing minor worth flagging today.")

    # --- Sport news (team news, not fixtures) ---
    def sport_group(items, empty_msg):
        return "".join(
            blurb_story_block(it["title"], it["source"], it.get("blurb", it["summary"][:300]), it["link"]) for it in items
        ) or f"<p style='color:#777;'>{empty_msg}</p>"

    team_sport_html = sport_group(team_sport_items, "Nothing new on Liverpool, Real Betis, Cork City, Troy Parrott, or the Boys in Green today.")
    pl_sport_html = sport_group(pl_sport_items, "No notable general Premier League storylines today.")
    cl_sport_html = sport_group(cl_sport_items, "No notable general Champions League storylines today.")

    other_sport_lines = [f"<a href='{it['link']}' style='color:#333;text-decoration:none;'>{it['title']}</a> <span style='color:#999;font-size:12px;'>({it['source']})</span>" for it in other_sport_items]
    other_sport_html = list_block(other_sport_lines, "No other notable sports headlines today.")

    cies_block = ""
    if cies_items:
        cies_block = f"""
        <div style="font-weight:700;font-family:Georgia,serif;font-size:15px;margin:16px 0 10px;">CIES Football Observatory</div>
        {sport_group(cies_items, "")}
        """

    return f"""<!DOCTYPE html>
<html><body style="margin:0;padding:0;background:#f2efe9;">
<div style="max-width:640px;margin:0 auto;background:#fff;padding:28px 24px;">
  <div style="text-align:center;border-bottom:4px double #111;padding-bottom:10px;margin-bottom:18px;">
    <div style="font-family:Georgia,serif;font-size:30px;font-weight:900;letter-spacing:0.02em;">THE MORNING BRIEF</div>
    <div style="font-size:12px;color:#555;text-transform:uppercase;letter-spacing:0.08em;margin-top:4px;">{edition_date}</div>
    {weather_html}
  </div>

  {bulletin_html}
  {fixtures_html}
  {odds_html}
  {econ_html}

  {section_header(TOP_LABEL, size="18px")}
  {top_html}

  {weekly_html}

  {category_html}

  {section_header(WORTH_KNOWING_LABEL)}
  {worth_html}

  {section_header("\U0001F3C6 Sport")}

  <div style="font-weight:700;font-family:Georgia,serif;font-size:15px;margin-bottom:10px;">Premier League</div>
  {pl_sport_html}

  <div style="font-weight:700;font-family:Georgia,serif;font-size:15px;margin:16px 0 10px;">Champions League</div>
  {cl_sport_html}

  <div style="font-weight:700;font-family:Georgia,serif;font-size:15px;margin:16px 0 10px;">Other Sports</div>
  {other_sport_html}

  {cies_block}

  <div style="font-weight:700;font-family:Georgia,serif;font-size:15px;margin:16px 0 10px;">Your Teams &amp; Players</div>
  {team_sport_html}

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

    print("Loading dedupe state...")
    seen = load_seen_state(SEEN_STORIES_PATH)
    print(f"  {len(seen)} previously-sent stories on file")

    print("Loading weekly digest history...")
    weekly_history = load_weekly_history(WEEKLY_HISTORY_PATH)
    is_sunday = today_dublin.weekday() == 6
    weekly_digest = write_weekly_digest(weekly_history) if is_sunday else []
    print(f"  {'Sunday - ' if is_sunday else ''}digest has {len(weekly_digest)} bullets from {len(weekly_history)} days of history")

    print("Fetching Ireland feeds...")
    ireland_raw = collect(IRELAND_FEEDS, cutoff)
    print("Fetching world feeds...")
    world_raw = collect(WORLD_FEEDS, cutoff)
    print("Fetching technology feeds...")
    tech_raw = collect(TECH_FEEDS, cutoff)
    all_news_raw = ireland_raw + world_raw + tech_raw
    all_news_raw = dedupe_by_title_and_link(all_news_raw)
    all_news_raw = filter_unseen(all_news_raw, seen)
    print(f"  {len(all_news_raw)} total news items collected (after cross-feed dedupe and seen-filter)")

    print("Fetching sport feeds...")
    sport_raw = collect(SPORT_FEEDS, cutoff)
    sport_raw = filter_unseen(sport_raw, seen)

    non_echo_raw = [it for it in sport_raw if it["source"] != LIVERPOOL_ECHO_SOURCE]
    team_candidates = filter_by_keywords(sport_raw, TEAM_KEYWORDS)
    pl_candidates = filter_by_keywords(non_echo_raw, PL_KEYWORDS)
    cl_candidates = filter_by_keywords(non_echo_raw, CL_KEYWORDS)
    print(f"  {len(team_candidates)} team/player items, {len(pl_candidates)} general PL, {len(cl_candidates)} general CL (after dedupe)")

    print("Fetching other-sports feeds...")
    other_sports_raw = collect(OTHER_SPORTS_FEEDS, cutoff)
    other_sports_raw = filter_unseen(other_sports_raw, seen)
    football_keywords = TEAM_KEYWORDS + PL_KEYWORDS + CL_KEYWORDS + ["football", "soccer"]
    other_sports_candidates = [it for it in other_sports_raw if it not in filter_by_keywords(other_sports_raw, football_keywords)]
    print(f"  {len(other_sports_candidates)} non-football items (from {len(other_sports_raw)} fetched)")

    print("Fetching CIES Football Observatory...")
    cies_raw = collect(CIES_FEEDS, cutoff)
    cies_raw = filter_unseen(cies_raw, seen)[:MAX_CIES_ITEMS]

    print("Fetching fixtures & results...")
    fixtures_data = build_fixtures_data(today_dublin, yesterday_dublin)

    print("Fetching weather...")
    weather_line = get_dublin_weather()

    print("Checking economic calendar...")
    econ_events = get_upcoming_econ_events(today_dublin)
    print(f"  {len(econ_events)} upcoming events within {ECON_EVENTS_HORIZON_DAYS} days")

    print("Scoring, categorizing, and writing breakdowns for news...")
    top_stories, category_sections, worth_knowing = build_news_sections(all_news_raw)
    print(f"  Top: {len(top_stories)} | Worth knowing: {len(worth_knowing)}")
    for cat in CATEGORIES:
        print(f"  {cat}: {len(category_sections[cat])}")

    print("Curating sport items with Claude...")
    team_curated = curate_sport_items(team_candidates, MAX_SPORT_ITEMS,
                                       "already filtered to Liverpool, Real Betis, Cork City FC, Troy Parrott, Mohamed Salah, and the Republic of Ireland national team")
    team_curated = cap_source_count(team_curated, LIVERPOOL_ECHO_SOURCE, MAX_ECHO_ITEMS)
    pl_curated = curate_sport_items(pl_candidates, MAX_PL_ITEMS, "general Premier League storylines, not tied to one club")
    cl_curated = curate_sport_items(cl_candidates, MAX_CL_ITEMS, "general Champions League storylines, not tied to one club")
    other_sports_curated = curate_sport_items(other_sports_candidates, MAX_OTHER_SPORTS_ITEMS, "key headlines from sports other than football - rugby, GAA, tennis, boxing, etc.")
    print(f"  kept {len(team_curated)} team items (Echo capped at {MAX_ECHO_ITEMS}), {len(pl_curated)} PL, {len(cl_curated)} CL, {len(other_sports_curated)} other sports")

    print("Writing sport blurbs...")
    for it in team_curated + pl_curated + cl_curated + other_sports_curated + cies_raw:
        it["blurb"] = blurb_for_sport_item(it)

    all_sport_items = team_curated + pl_curated + cl_curated + other_sports_curated + cies_raw

    print("Writing today's bulletin...")
    bulletin = write_bulletin(top_stories, category_sections, all_sport_items, fixtures_data)
    print(f"  bulletin has {len(bulletin)} bullets")

    html = render_html(bulletin, top_stories, category_sections, worth_knowing,
                        team_curated, pl_curated, cl_curated, other_sports_curated, cies_raw,
                        fixtures_data, edition_date, weather_line, econ_events, weekly_digest)

    print("Sending email...")
    send_email(html, subject=f"Morning Brief — {edition_date}")
    print("Done.")

    print("Updating dedupe state...")
    shown_items = top_stories + [it for items in category_sections.values() for it in items] + worth_knowing + all_sport_items
    mark_seen(shown_items, seen, today_dublin.isoformat())
    cutoff_date_str = (today_dublin - dt.timedelta(days=SEEN_RETENTION_DAYS)).isoformat()
    seen = prune_seen(seen, cutoff_date_str)
    save_seen_state(seen, SEEN_STORIES_PATH)
    print(f"  {len(seen)} stories now on file")

    print("Updating weekly digest history...")
    weekly_history = append_today_to_history(weekly_history, today_dublin.isoformat(), top_stories)
    history_cutoff = (today_dublin - dt.timedelta(days=WEEKLY_HISTORY_RETENTION_DAYS)).isoformat()
    weekly_history = prune_history(weekly_history, history_cutoff)
    save_weekly_history(weekly_history, WEEKLY_HISTORY_PATH)
    print(f"  {len(weekly_history)} days now on file")


if __name__ == "__main__":
    main()
