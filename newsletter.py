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

# News feeds. Category (Ireland/Business/Politics/Technology/World) is
# decided by Claude from content, not by which feed a story came from - so
# these groupings just control which feeds get fetched, not where a story
# ends up.
IRELAND_FEEDS = {
    "TheJournal.ie (VERIFY)": "https://www.thejournal.ie/feed/",
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
    "Sky Sports - Football": "https://www.skysports.com/rss/12040",
    "Liverpool Echo - LFC":  "https://www.liverpoolecho.co.uk/all-about/liverpool-fc/?service=rss",
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

def select_top_sport_items(items, limit):
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

# ---------------------------------------------------------------------------
# RENDERING
# ---------------------------------------------------------------------------

def render_html(bulletin, top_stories, category_sections, worth_knowing, sport_items, fixtures_data, edition_date):

    def section_header(text, size="16px"):
        return f"""<div style="font-family:Georgia,serif;font-size:{size};font-weight:700;text-transform:uppercase;border-bottom:2px solid #111;margin:26px 0 12px;padding-bottom:4px;">{text}</div>"""

    def full_story_block(it):
        b = it.get("breakdown", {})
        return f"""
        <div style="margin-bottom:22px;padding-bottom:18px;border-bottom:1px solid #ddd;">
          <div style="font-size:18px;font-weight:700;font-family:Georgia,serif;color:#111;">{it['title']}</div>
          <div style="font-size:12px;color:#777;margin:2px 0 8px;text-transform:uppercase;letter-spacing:0.03em;">{it['source']}</div>
          <div style="font-size:14px;color:#333;line-height:1.55;font-family:Georgia,serif;">
            <div style="margin-bottom:6px;"><b>What happened:</b> {b.get('what_happened','')}</div>
            <div style="margin-bottom:6px;"><b>Why it matters:</b> {b.get('why_it_matters','')}</div>
            <div><b>Watch next:</b> {b.get('watch_next','')}</div>
          </div>
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

    # --- Fixtures & Results: enlarged, prominent, right below the bulletin ---
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

    # --- Top stories ---
    top_html = "".join(full_story_block(it) for it in top_stories) or "<p style='color:#777;'>Nothing cleared the must-know bar today.</p>"

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
    sport_html = "".join(
        blurb_story_block(it["title"], it["source"], it.get("blurb", it["summary"][:300]), it["link"]) for it in sport_items
    ) or "<p style='color:#777;'>Nothing new on Liverpool, Real Betis, Cork City, Troy Parrott, or the Boys in Green today.</p>"

    return f"""<!DOCTYPE html>
<html><body style="margin:0;padding:0;background:#f2efe9;">
<div style="max-width:640px;margin:0 auto;background:#fff;padding:28px 24px;">
  <div style="text-align:center;border-bottom:4px double #111;padding-bottom:10px;margin-bottom:18px;">
    <div style="font-family:Georgia,serif;font-size:30px;font-weight:900;letter-spacing:0.02em;">THE MORNING BRIEF</div>
    <div style="font-size:12px;color:#555;text-transform:uppercase;letter-spacing:0.08em;margin-top:4px;">{edition_date}</div>
  </div>

  {bulletin_html}
  {fixtures_html}

  {section_header(TOP_LABEL, size="18px")}
  {top_html}

  {category_html}

  {section_header(WORTH_KNOWING_LABEL)}
  {worth_html}

  {section_header("\U0001F3C6 Sport")}
  {sport_html}

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
    print("Fetching technology feeds...")
    tech_raw = collect(TECH_FEEDS, cutoff)
    all_news_raw = ireland_raw + world_raw + tech_raw
    print(f"  {len(all_news_raw)} total news items collected")

    print("Fetching sport feeds...")
    sport_raw = collect(SPORT_FEEDS, cutoff)
    sport_filtered = filter_by_keywords(sport_raw, TEAM_KEYWORDS)
    print(f"  {len(sport_filtered)} sport items matched your keywords")

    print("Fetching fixtures & results...")
    fixtures_data = build_fixtures_data(today_dublin, yesterday_dublin)

    print("Scoring, categorizing, and writing breakdowns for news...")
    top_stories, category_sections, worth_knowing = build_news_sections(all_news_raw)
    print(f"  Top: {len(top_stories)} | Worth knowing: {len(worth_knowing)}")
    for cat in CATEGORIES:
        print(f"  {cat}: {len(category_sections[cat])}")

    print("Curating sport items with Claude...")
    sport_curated = select_top_sport_items(sport_filtered, MAX_SPORT_ITEMS)
    print(f"  kept {len(sport_curated)} of {len(sport_filtered)} sport items")

    print("Writing sport blurbs...")
    for it in sport_curated:
        it["blurb"] = blurb_for_sport_item(it)

    print("Writing today's bulletin...")
    bulletin = write_bulletin(top_stories, category_sections, sport_curated, fixtures_data)
    print(f"  bulletin has {len(bulletin)} bullets")

    html = render_html(bulletin, top_stories, category_sections, worth_knowing, sport_curated, fixtures_data, edition_date)

    print("Sending email...")
    send_email(html, subject=f"Morning Brief — {edition_date}")
    print("Done.")


if __name__ == "__main__":
    main()
