#!/usr/bin/env python3
"""LinkedIn Games league engine (supports leagues with 2+ players). Python 3.11+, stdlib only (tzdata needed on some Windows installs)."""
import argparse
import csv
import hashlib
import math
import html
import json
import os
import re
import sqlite3
import statistics
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, date, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

GAMES = ("Queens", "Tango", "Zip", "Mini Sudoku", "Crossclimb", "Wend", "Patches")
DB_LOCK = threading.RLock()
PT = ZoneInfo("America/Los_Angeles")
_GAME = r"(?P<game>Mini\s+Sudoku|Crossclimb|Queens|Tango|Zip|Wend|Patches)"
_TIME = r"(?P<time>\d{1,2}:\d{2}(?::\d{2})?|\d+\s*(?:s|sec|seconds)\b)"
# "Zip #367 | 0:05", or "Tango #735" with the time on the next line. No word boundary before
# the game: players sometimes type a comment straight into the share ("so closeTango #722 | 0:45").
GAME_RX = re.compile(
    _GAME + r"\s*(?:\#|No\.?\s*)(?P<number>\d+)\s*(?:[|:–\-]\s*)?" + _TIME,
    re.IGNORECASE,
)
# Turkish shares since 2026 put the puzzle number first: "578. Zip | 0:15".
GAME_RX_NUMBER_FIRST = re.compile(
    r"(?<![\w.])(?P<number>\d+)\.\s*" + _GAME + r"\s*[|:–\-]\s*" + _TIME,
    re.IGNORECASE,
)

def parse_scores(text):
    """Returns (game, puzzle number, time seconds). Ignores unrelated text and score marketing copy."""
    found = []
    for m in [*GAME_RX.finditer(text or ""), *GAME_RX_NUMBER_FIRST.finditer(text or "")]:
        game = next((g for g in GAMES if g.casefold().replace(" ", "") == re.sub(r"\s+", "", m["game"]).casefold()), None)
        if not game:
            continue
        s = m["time"].lower().replace("seconds", "").replace("sec", "").replace("s", "").strip()
        parts = [int(v) for v in s.split(":")]
        duration = (parts[0] * 60 + parts[1]) if len(parts) == 2 else (parts[0] * 3600 + parts[1] * 60 + parts[2]) if len(parts) == 3 else parts[0]
        if duration > 0:
            found.append((game, int(m["number"]), duration))
    return found

def parse_datetime(s):
    s = str(s or "").strip()
    if not s:
        raise ValueError("Missing DATE field")
    if s.endswith(" UTC"):
        s = s[:-4] + "+00:00"
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        # An example of LinkedIn's common export format.
        dt = datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt

def load_config(path):
    cfg = json.loads(Path(path).read_text(encoding="utf-8"))
    if set(cfg.get("players", [])) != set(cfg.get("aliases", {}).keys()):
        raise ValueError("Each member of players must have an entry in aliases")
    if len(cfg.get("players", [])) < 2 or len(set(cfg["players"])) != len(cfg["players"]):
        raise ValueError("Configure at least two distinct players")
    date.fromisoformat(cfg["start_date"])
    return cfg

def open_db(path):
    con = sqlite3.connect(path, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("""CREATE TABLE IF NOT EXISTS results (
      game TEXT NOT NULL,
      puzzle_date TEXT NOT NULL,
      player TEXT NOT NULL,
      puzzle_number INTEGER NOT NULL,
      seconds INTEGER NOT NULL,
      msg_time TEXT NOT NULL,
      fingerprint TEXT NOT NULL,
      PRIMARY KEY (game,puzzle_date,player)
    )""")
    con.execute("CREATE INDEX IF NOT EXISTS historical ON results(game,puzzle_date)")
    con.execute("""CREATE TABLE IF NOT EXISTS daily_averages (
      game TEXT NOT NULL, puzzle_date TEXT NOT NULL, seconds INTEGER NOT NULL,
      source TEXT NOT NULL, entered_at TEXT NOT NULL,
      PRIMARY KEY(game,puzzle_date)
    )""")
    con.execute("""CREATE TABLE IF NOT EXISTS metadata (
      key TEXT PRIMARY KEY, value TEXT NOT NULL
    )""")
    # Changelog events name senders only by person URN. Score messages wait here until the URN is known.
    con.execute("""CREATE TABLE IF NOT EXISTS sender_urns (
      urn TEXT PRIMARY KEY, name TEXT NOT NULL, source TEXT NOT NULL, learned_at TEXT NOT NULL
    )""")
    con.execute("""CREATE TABLE IF NOT EXISTS changelog_messages (
      id TEXT PRIMARY KEY, author TEXT NOT NULL, sent TEXT NOT NULL, content TEXT NOT NULL
    )""")
    con.commit()
    return con

def canonical_player(raw, cfg):
    raw = str(raw or "").strip().casefold()
    for player, aliases in cfg["aliases"].items():
        if raw in [str(a).strip().casefold() for a in aliases]:
            return player
    return None

def set_average(con, game, puzzle_day, seconds, source="manual LinkedIn UI"):
    if game not in GAMES:
        raise ValueError(f"Unknown game: {game}")
    date.fromisoformat(puzzle_day)
    if not isinstance(seconds, int) or seconds <= 0:
        raise ValueError("Average must be a positive integer in seconds")
    with DB_LOCK:
        con.execute("""INSERT INTO daily_averages (game,puzzle_date,seconds,source,entered_at)
          VALUES(?,?,?,?,?) ON CONFLICT(game,puzzle_date) DO UPDATE SET
          seconds=excluded.seconds, source=excluded.source, entered_at=excluded.entered_at""",
          (game,puzzle_day,seconds,source,datetime.now(timezone.utc).isoformat()))
        con.commit()

def parse_duration(s):
    s = str(s).strip()
    if re.fullmatch(r"\d+", s):
        return int(s)
    parts = s.split(":")
    if len(parts) == 2 and all(p.isdigit() for p in parts):
        return int(parts[0])*60 + int(parts[1])
    if len(parts) == 3 and all(p.isdigit() for p in parts):
        return int(parts[0])*3600 + int(parts[1])*60 + int(parts[2])
    raise ValueError("Time must be seconds (120) or mm:ss (2:00)")

def ingest_rows(con, cfg, rows):
    stats = Counter()
    target = str(cfg.get("conversation_id", "")).strip()
    start = date.fromisoformat(cfg["start_date"])
    with DB_LOCK:
      for row in rows:
          if target and str(row.get("CONVERSATION ID", "")).strip() != target:
              continue
          player = canonical_player(row.get("FROM", ""), cfg)
          if not player:
              continue
          matches = parse_scores(str(row.get("CONTENT", "")))
          if not matches:
              continue
          try:
              sent = parse_datetime(row.get("DATE", ""))
          except ValueError:
              stats["invalid_date"] += len(matches)
              continue
          # Puzzle reset is 00:00 Pacific. DATE, not receipt date, determines the puzzle day.
          puzzle_date = sent.astimezone(PT).date()
          # HARD GATE: messages sent before the official start never enter the database.
          # This applies equally to delayed API syncs, initial CSV imports and backfills.
          if puzzle_date < start:
              stats["ignored_before_start"] += len(matches)
              continue
          puzzle_day = puzzle_date.isoformat()
          for game, puzzle_num, seconds in matches:
              data = f"{target}|{row.get('DATE')}|{row.get('FROM')}|{game}|{puzzle_num}|{seconds}"
              digest = hashlib.sha256(data.encode()).hexdigest()
              cur = con.execute("""INSERT OR IGNORE INTO results(game,puzzle_date,player,puzzle_number,seconds,msg_time,fingerprint)
                                   VALUES(?,?,?,?,?,?,?)""",
                                (game, puzzle_day, player, puzzle_num, seconds, sent.isoformat(), digest))
              stats["inserted" if cur.rowcount else "duplicates"] += 1
    with DB_LOCK:
        con.execute("INSERT OR REPLACE INTO metadata (key,value) VALUES ('last_import',?)",
                    (datetime.now(timezone.utc).isoformat(),))
        con.commit()
    return dict(stats)

TOKEN_FILE = Path.home() / ".linkedin-league-token"

def linkedin_token():
    """LINKEDIN_TOKEN env var, else the private token file. Empty string if neither exists."""
    token = os.environ.get("LINKEDIN_TOKEN", "").strip()
    if not token and TOKEN_FILE.exists():
        token = TOKEN_FILE.read_text(encoding="utf-8").strip()
    return token

def api_snapshot_pages(token):
    headers = {"Authorization": f"Bearer {token}", "Linkedin-Version": "202312", "Content-Type": "application/json"}
    for start in range(10000):
        url = "https://api.linkedin.com/rest/memberSnapshotData?" + urllib.parse.urlencode({"q":"criteria","domain":"INBOX","start":start})
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=50) as rsp:
                payload = json.load(rsp)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                if start == 0:
                    raise RuntimeError("LinkedIn INBOX snapshot is unavailable or still being prepared (HTTP 404). Retry later.") from exc
                break
            raise RuntimeError(f"LinkedIn HTTP {exc.code}. Check API permission, token, and rate limits.") from exc
        messages = [m for element in payload.get("elements", []) if element.get("snapshotDomain") == "INBOX"
                    for m in element.get("snapshotData", [])]
        if not messages:
            break
        yield from messages
        # API documentation says keep paging until 'No data found'.
    else:
        raise RuntimeError("Unexpectedly large snapshot; page safety limit reached")

THREAD_PREFIX = "urn:li:messagingThread:"

def api_changelog_events(token, start_ms=None):
    """Member Changelog events (last 28 days, near-live), oldest first. start_ms is inclusive."""
    headers = {"Authorization": f"Bearer {token}", "Linkedin-Version": "202312", "Content-Type": "application/json"}
    seen = set()
    for _ in range(1000):
        params = {"q": "memberAndApplication", "count": 50}
        if start_ms:
            params["startTime"] = start_ms
        req = urllib.request.Request("https://api.linkedin.com/rest/memberChangeLogs?" + urllib.parse.urlencode(params), headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=50) as rsp:
                elements = json.load(rsp).get("elements", [])
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"LinkedIn changelog HTTP {exc.code}. Check API permission, token, and rate limits.") from exc
        new = [e for e in elements if e.get("id") not in seen]
        seen.update(e.get("id") for e in elements)
        yield from new
        # LinkedIn recommends the latest processedAt as the next (inclusive) startTime.
        if len(elements) < 50 or not new:
            return
        start_ms = max(e["processedAt"] for e in elements)

def remember_sender(con, urn, name, source, override=False):
    with DB_LOCK:
        con.execute(f"""INSERT INTO sender_urns(urn,name,source,learned_at) VALUES(?,?,?,?)
          ON CONFLICT(urn) DO {'UPDATE SET name=excluded.name,source=excluded.source,learned_at=excluded.learned_at' if override else 'NOTHING'}""",
          (urn, name, source, datetime.now(timezone.utc).isoformat()))
        con.commit()

def ingest_changelog(con, cfg, events):
    """Stores score messages from this league's chat (nothing else), then counts those whose sender is known."""
    stats = Counter()
    thread = THREAD_PREFIX + str(cfg.get("conversation_id", "")).strip()
    me = cfg.get("me")
    cursor = 0
    with DB_LOCK:
        for e in events:
            cursor = max(cursor, e.get("processedAt") or 0)
            a = e.get("activity") or {}
            if e.get("resourceName") != "messages" or e.get("method") != "CREATE" or a.get("thread") != thread:
                continue
            text = (a.get("content") or {}).get("fallback") or ""
            if not parse_scores(text):
                continue
            if me and e.get("owner"):
                # The event owner is always the token holder.
                remember_sender(con, e["owner"], cfg["aliases"][me][0], "token owner")
            sent = datetime.fromtimestamp(a["createdAt"] / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            cur = con.execute("INSERT OR IGNORE INTO changelog_messages(id,author,sent,content) VALUES(?,?,?,?)",
                              (a.get("id") or e.get("resourceId"), a.get("author") or e.get("actor"), sent, text))
            stats["score_messages" if cur.rowcount else "seen_before"] += 1
        if cursor:
            con.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES('changelog_cursor',?)", (str(cursor),))
        con.commit()
    stats.update(resolve_changelog(con, cfg))
    return dict(stats)

def resolve_changelog(con, cfg):
    """Turns stored changelog score messages into results for every sender whose URN is known."""
    with DB_LOCK:
        known = {r["urn"]: r["name"] for r in con.execute("SELECT urn,name FROM sender_urns")}
        msgs = [dict(r) for r in con.execute("SELECT * FROM changelog_messages")]
    me = cfg.get("me")
    if me and len(cfg["players"]) == 2 and any(canonical_player(n, cfg) == me for n in known.values()):
        # One-to-one chat: any sender who is not the token holder is the other player.
        other = cfg["aliases"][next(p for p in cfg["players"] if p != me)][0]
        for m in msgs:
            if m["author"] not in known:
                remember_sender(con, m["author"], other, "two-player chat")
                known[m["author"]] = other
    rows = [{"CONVERSATION ID": cfg["conversation_id"], "FROM": known[m["author"]], "DATE": m["sent"], "CONTENT": m["content"]}
            for m in msgs if m["author"] in known]
    stats = {f"results_{k}": v for k, v in (ingest_rows(con, cfg, rows) if rows else {}).items()}
    stats["unknown_sender"] = sum(1 for m in msgs if m["author"] not in known)
    return stats

def learn_senders_from_rows(con, cfg, rows):
    """Snapshot rows carry names, changelog messages carry URNs: same chat, text and second means same sender."""
    with DB_LOCK:
        pending = [dict(r) for r in con.execute(
            "SELECT * FROM changelog_messages WHERE author NOT IN (SELECT urn FROM sender_urns)")]
    if not pending:
        return 0
    by_text = defaultdict(list)
    for r in rows:
        if str(r.get("CONVERSATION ID", "")).strip() == cfg["conversation_id"] and canonical_player(r.get("FROM"), cfg):
            try:
                by_text[str(r.get("CONTENT", "")).strip()].append((parse_datetime(r.get("DATE")), r["FROM"]))
            except ValueError:
                pass
    learned = 0
    for m in pending:
        sent = parse_datetime(m["sent"])
        names = {name for dt, name in by_text.get(m["content"].strip(), []) if abs((dt - sent).total_seconds()) <= 5}
        if len(names) == 1:
            remember_sender(con, m["author"], names.pop(), "matched snapshot")
            learned += 1
    return learned

def map_sender(con, cfg, urn, name):
    """Manual URN → player mapping, for group chats before the snapshot has caught up."""
    urn = urn if urn.startswith("urn:li:person:") else "urn:li:person:" + urn
    alias = cfg["aliases"][name][0] if name in cfg["players"] else name
    if not canonical_player(alias, cfg):
        raise ValueError(f"{name!r} is not a player or LinkedIn name in this league")
    remember_sender(con, urn, alias, "manual", override=True)
    return resolve_changelog(con, cfg)

def sync_linkedin(con, cfg, token):
    """Live changelog first, then the full INBOX snapshot (catch-up, and it teaches sender URNs)."""
    with DB_LOCK:
        row = con.execute("SELECT value FROM metadata WHERE key='changelog_cursor'").fetchone()
    stats = {"changelog": ingest_changelog(con, cfg, api_changelog_events(token, int(row[0]) if row else None))}
    try:
        snapshot = list(api_snapshot_pages(token))
    except RuntimeError as exc:
        stats["snapshot_error"] = str(exc)
        return stats
    stats["snapshot"] = ingest_rows(con, cfg, snapshot)
    stats["learned_senders"] = learn_senders_from_rows(con, cfg, snapshot)
    stats["resolved"] = resolve_changelog(con, cfg)
    return stats

def import_csv(con, cfg, filename):
    with open(filename, encoding="utf-8-sig", newline="") as f:
        # Skips optional preamble before LinkedIn's CSV header.
        lines = f.readlines()
        first = next((i for i, line in enumerate(lines) if line.startswith("CONVERSATION ID,")), None)
        if first is None:
            raise RuntimeError("Couldn't find the LinkedIn messages.csv header")
        return ingest_rows(con, cfg, csv.DictReader(lines[first:]))

def list_conversations(rows):
    counts, titles = Counter(), {}
    for row in rows:
        scores = parse_scores(str(row.get("CONTENT", "")))
        if not scores:
            continue
        cid = str(row.get("CONVERSATION ID", ""))
        counts[cid] += len(scores)
        titles[cid] = str(row.get("CONVERSATION TITLE", ""))
    for cid, count in counts.most_common(25):
        print(f"{count:4} score results | conversation ID={cid} | title={titles[cid]}")
    if not counts:
        print("No recognizable game scores. Verify the export/API includes your group conversation and share text.")

def historical_reference(rows, game, day, fallback):
    """Median of last 8 same-weekday daily medians, with per-weekday bootstrap values."""
    prior = defaultdict(list)
    for r in rows:
        d = date.fromisoformat(r["puzzle_date"])
        if r["game"] == game and d < day and d.weekday() == day.weekday():
            prior[d].append(r["seconds"])
    dates = sorted(prior, reverse=True)[:8]
    if len(dates) < 3:
        return int(fallback[day.weekday()] if isinstance(fallback, list) else fallback)
    return max(1, int(statistics.median([statistics.median(prior[d]) for d in dates])))

def penalty(baseline):
    """ceil(1.125 * baseline), exactly, without floating point inaccuracies."""
    return (9 * baseline + 7) // 8

def leaderboard(con, cfg, now=None):
    """Pure rebuild from persisted results. Backfilled messages recalculate old standings automatically.

    Current puzzle day uses provisional penalties in projected totals. A game is settled
    when the Pacific day ended OR all three scores have arrived.
    """
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(PT).date()
    start = date.fromisoformat(cfg["start_date"])
    with DB_LOCK:
        rows = [dict(r) for r in con.execute("SELECT * FROM results WHERE puzzle_date >= ?", (start.isoformat(),))]
        averages = {(r["game"],r["puzzle_date"]):dict(r)
                    for r in con.execute("SELECT * FROM daily_averages WHERE puzzle_date >= ?", (start.isoformat(),))}
        meta = {r["key"]:r["value"] for r in con.execute("SELECT * FROM metadata")}
        unknown_senders = con.execute("""SELECT COUNT(*) FROM changelog_messages
          WHERE author NOT IN (SELECT urn FROM sender_urns)""").fetchone()[0]
    indexed = {(r["game"], r["puzzle_date"], r["player"]): r for r in rows}
    totals = {game:{p:{"seconds":0,"played":0,"missed":0,"pending":0,
                        "beats":0,"beaten":0} for p in cfg["players"]} for game in GAMES}
    daily = []
    days_overall = []
    last_day = min(today, date.fromisoformat(cfg.get("end_date", "9999-12-31")))
    for offset in range(max(0, (last_day-start).days+1)):
        day = start + timedelta(days=offset)
        daystr = day.isoformat()
        is_past = day < today
        daily_sum = {p:0 for p in cfg["players"]}
        daily_finished = True
        day_missed_count = {p:0 for p in cfg["players"]}
        for game in GAMES:
            submitted = [indexed[(game,daystr,p)] for p in cfg["players"]
                         if (game,daystr,p) in indexed]
            global_avg = averages.get((game,daystr))
            historical = historical_reference(rows, game, day, cfg["fallback_seconds"][game])
            # Global average is preferable to the historical fallback when supplied.
            reference = global_avg["seconds"] if global_avg else historical
            baseline = max([reference] + [r["seconds"] for r in submitted])
            missed_seconds = penalty(baseline)
            game_settled = is_past or len(submitted)==len(cfg["players"])
            if not game_settled:
                daily_finished = False
            players = {}
            numbers = {r["puzzle_number"] for r in submitted}
            inconsistent = len(numbers) > 1
            fastest = min((r["seconds"] for r in submitted), default=None)
            for p in cfg["players"]:
                r = indexed.get((game,daystr,p))
                if r:
                    seconds = r["seconds"]
                    status = "played"
                    totals[game][p]["played"] += 1
                else:
                    # While the day is open and nobody has played, do not impose
                    # provisional penalties prematurely. Once somebody posts, show them.
                    waiting = not is_past and not submitted
                    seconds = 0 if waiting else missed_seconds
                    status = "awaiting" if waiting else "missed" if is_past else "provisional"
                    if not waiting:
                        totals[game][p]["missed" if is_past else "pending"] += 1
                    if is_past:
                        day_missed_count[p] += 1
                totals[game][p]["seconds"] += seconds
                daily_sum[p] += seconds
                players[p] = {"status":status,"seconds":seconds,
                              "is_fastest": bool(r and r["seconds"] == fastest)}
                if game_settled and fastest is not None:
                    if r and r["seconds"] == fastest:
                        totals[game][p]["beats"] += 1
                    else:
                        totals[game][p]["beaten"] += 1
            daily.append({"date":daystr,"game":game,"final":game_settled,
                          "baseline":baseline,"reference":historical,
                          "global_average":global_avg["seconds"] if global_avg else None,
                          "average_source":global_avg["source"] if global_avg else None,
                          "missing_penalty": missed_seconds if len(submitted)<len(cfg["players"]) else 0,
                          "players":players,"puzzle_numbers_conflict":inconsistent})
        winner = min(daily_sum.values()) if daily_sum else None
        any_result = any((g,daystr,p) in indexed for g in GAMES for p in cfg["players"])
        days_overall.append({"date":daystr,"final":daily_finished,
                             "players":{p:{"seconds":daily_sum[p],
                                           "missed":day_missed_count[p],
                                           "is_fastest":bool(any_result and daily_sum[p]==winner)}
                                        for p in cfg["players"]}})
    overall = {}
    for p in cfg["players"]:
        overall[p] = {"seconds":sum(totals[g][p]["seconds"] for g in GAMES),
                      "played":sum(totals[g][p]["played"] for g in GAMES),
                      "missed":sum(totals[g][p]["missed"] for g in GAMES),
                      "pending":sum(totals[g][p]["pending"] for g in GAMES),
                      "beats":sum(totals[g][p]["beats"] for g in GAMES),
                      "beaten":sum(totals[g][p]["beaten"] for g in GAMES),
                      "daily_wins":sum(int(d["final"] and d["players"][p]["is_fastest"]) for d in days_overall)}
    return {"start_date":start.isoformat(), "today_pt":today.isoformat(),
            "generated_at":now.isoformat(), "last_import":meta.get("last_import"),
            "league_name":cfg.get("name","Games League"),
            "players":cfg["players"],"totals":totals,"overall":overall,
            "daily":daily,"daily_overall":days_overall,
            "league_key":cfg.get("league_key"),"unknown_senders":unknown_senders}

def dashboard_template():
    return (Path(__file__).parent / "dashboard.html").read_text(encoding="utf-8")


def make_html(con, cfg, now=None):
    payload = json.dumps(leaderboard(con, cfg, now=now), ensure_ascii=False).replace("<", "\\u003c")
    return dashboard_template().replace("__DATA__", payload)

def load_league_config(leagues_path, league_key=None, demo=False):
    path = Path(leagues_path)
    if not path.exists():
        raise SystemExit(f"{path} not found. Copy leagues.example.json to {path.name} and fill in your league.")
    leagues = json.loads(path.read_text(encoding="utf-8"))
    league_key = league_key or next(iter(leagues))
    if league_key not in leagues:
        raise ValueError(f"Unknown league {league_key!r}. Available: {', '.join(leagues)}")
    cfg = dict(leagues[league_key])
    if demo:
        cfg["start_date"] = "2026-10-16"  # Friday practice day, distinct database
        cfg["name"] = cfg["name"] + " • SANDBOX"
        cfg["end_date"] = "2026-10-18"
    if len(cfg["players"]) < 2 or set(cfg["players"]) != set(cfg["aliases"]):
        raise ValueError("Each league requires at least two players with aliases")
    if cfg.get("me") is not None and cfg["me"] not in cfg["players"]:
        raise ValueError("'me' (the LinkedIn token holder) must be one of the players")
    cfg["league_key"] = league_key
    return cfg

DEMO_DAYS = ["2026-10-16", "2026-10-17", "2026-10-18"]

def demo_puzzle_number(game, day):
    return 800 + GAMES.index(game)*500 + (date.fromisoformat(day)-date(2026,10,16)).days

def save_demo_score(con, cfg, game, day, player, seconds):
    if game not in GAMES or player not in cfg["players"] or day not in DEMO_DAYS or not 1 <= seconds <= 86400:
        raise ValueError("Invalid demo score, date or player")
    msg_time = datetime.combine(date.fromisoformat(day), datetime.min.time(), tzinfo=PT).replace(hour=11).astimezone(timezone.utc).isoformat()
    with DB_LOCK:
        con.execute("""INSERT INTO results(game,puzzle_date,player,puzzle_number,seconds,msg_time,fingerprint)
          VALUES(?,?,?,?,?,?,?) ON CONFLICT(game,puzzle_date,player) DO UPDATE SET
          seconds=excluded.seconds,msg_time=excluded.msg_time,fingerprint=excluded.fingerprint""",
          (game, day, player, demo_puzzle_number(game,day), seconds, msg_time, "demo"))
        con.commit()

def seed_demo(con, cfg):
    """Fixture data, deterministic and completely separate from official databases."""
    with DB_LOCK:
        con.execute("DELETE FROM results")
        con.execute("DELETE FROM daily_averages")
        con.execute("DELETE FROM metadata")
        con.commit()
    players = cfg["players"]
    # Two illustrative practice days with deliberately different difficulties and absences.
    fixtures = {
      "2026-10-16": {
        "Queens":([8,10, None],11),"Tango":([21,None,18],23),
        "Zip":([9,11,12],10),"Mini Sudoku":([37,43,None],45),
        "Crossclimb":([48,52,63],55),"Wend":([32,35,None],42),
        "Patches":([26,None,30],33),
      },
      "2026-10-17": {
        "Queens":([20,114,None],120),"Tango":([72,64,80],88),
        "Zip":([16,None,19],22),"Mini Sudoku":([103,120,None],135),
        "Crossclimb":([130,None,141],164),"Wend":([92,105,None],118),
        "Patches":([76,70,81],85),
      },
      "2026-10-18": {
        "Queens":([20,None,None],120),"Tango":([None,65,None],110),
        "Zip":([None,None,None],60),"Mini Sudoku":([None,None,None],155),
        "Crossclimb":([None,None,None],185),"Wend":([None,None,None],175),
        "Patches":([None,None,None],145),
      },
    }
    for day, gs in fixtures.items():
        for game,(times,avg) in gs.items():
            set_average(con,game,day,avg,source="synthetic demo, NOT LinkedIn")
            for p,seconds in zip(players,times):
                if seconds is not None:
                    save_demo_score(con,cfg,game,day,p,seconds)
    with DB_LOCK:
        con.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES ('last_import',?)", ("DEMO ONLY",))
        con.commit()

def demo_time(value):
    # After 10AM PT, which means all demo submissions for that day have occurred.
    if value in DEMO_DAYS:
        d = date.fromisoformat(value)
        return datetime(d.year,d.month,d.day,12,tzinfo=PT)
    if value == "after":
        return datetime(2026,10,19,0,1,tzinfo=PT)
    return datetime.now(timezone.utc)

def serve(con, cfg, port, interval=0, demo=False):
    if interval and not demo:
        def worker():
            while True:
                try:
                    if linkedin_token():
                        s = sync_linkedin(con, cfg, linkedin_token())
                        print(datetime.now(timezone.utc).isoformat(timespec="seconds"), "API sync", s, flush=True)
                except Exception as exc:
                    print("API sync error:", exc, file=sys.stderr, flush=True)
                time.sleep(interval*60)
        threading.Thread(target=worker, daemon=True).start()
    class Handler(BaseHTTPRequestHandler):
        def reply(self, code, content, mime="application/json; charset=utf-8"):
            blob=content.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type",mime)
            self.send_header("Content-Length",str(len(blob)))
            self.send_header("Cache-Control","no-store")
            self.send_header("X-Content-Type-Options","nosniff")
            self.end_headers()
            self.wfile.write(blob)
        def do_GET(self):
            url=urllib.parse.urlsplit(self.path)
            if url.path not in ("/", "/index.html"):
                self.send_error(404)
                return
            qs=urllib.parse.parse_qs(url.query)
            asof=qs.get("asof",[""])[0] if demo else ""
            if asof and asof not in DEMO_DAYS+["after"]:
                self.send_error(400,"Invalid demo date")
                return
            content=make_html(con, cfg, demo_time(asof) if asof else None)
            content=content.replace("__DEMO__", "true" if demo else "false")
            content=content.replace("__ASOF__", json.dumps(asof))
            self.reply(200,content,"text/html; charset=utf-8")
        def do_POST(self):
            if not demo: self.send_error(404); return
            if self.path not in ("/demo/score","/demo/average","/demo/reset"):
                self.send_error(404);return
            try:
                size=int(self.headers.get("Content-Length","0"))
                if not 0 <= size <= 4096: raise ValueError("Request too large")
                obj=json.loads(self.rfile.read(size).decode()) if size else {}
                if self.path == "/demo/reset":
                    seed_demo(con,cfg)
                elif self.path == "/demo/score":
                    save_demo_score(con,cfg,str(obj["game"]),str(obj["date"]),str(obj["player"]),parse_duration(obj["seconds"]))
                else:
                    game,day=str(obj["game"]),str(obj["date"])
                    if day not in DEMO_DAYS: raise ValueError("Date outside demo")
                    set_average(con,game,day,parse_duration(obj["seconds"]),source="demo adjustment")
                self.reply(200,json.dumps({"ok":True}))
            except (KeyError,ValueError,TypeError,json.JSONDecodeError) as exc:
                self.reply(400,json.dumps({"ok":False,"error":str(exc)}))
        def log_message(self, format_, *args):
            pass
    print(f"Local dashboard: http://localhost:{port} (demo={demo})", flush=True)
    print("Bound to localhost only. Never expose unauthenticated dashboard to the Internet.", flush=True)
    ThreadingHTTPServer(("127.0.0.1",port),Handler).serve_forever()

def main():
    ap=argparse.ArgumentParser(description="LinkedIn Games League, two separate leagues + sandbox")
    ap.add_argument("--leagues",default=str(Path(__file__).parent/"leagues.json"))
    ap.add_argument("--league",default=None,help="League key from leagues.json (default: the first one)")
    ap.add_argument("--demo",action="store_true",help="Use independent sandbox data, never the official results")
    ap.add_argument("--db",default=None)
    subs=ap.add_subparsers(dest="command",required=True)
    imp=subs.add_parser("import-csv",help="Import LinkedIn messages.csv, excluding messages before official start")
    imp.add_argument("file")
    subs.add_parser("sync-api",help="Import from the EU data-portability API: live changelog plus INBOX snapshot")
    subs.add_parser("senders",help="Show which LinkedIn person IDs are mapped to players, and unknown ones")
    ms=subs.add_parser("map-sender",help="Map a LinkedIn person ID (from 'senders') to a player")
    ms.add_argument("urn")
    ms.add_argument("player")
    lc=subs.add_parser("find-chat",help="List conversations containing recognized scores")
    lc.add_argument("--csv",help="Read messages.csv instead of API")
    avg=subs.add_parser("set-average",help="Set known LinkedIn global average, optional input")
    avg.add_argument("game",choices=GAMES)
    avg.add_argument("date")
    avg.add_argument("time")
    avgfile=subs.add_parser("import-averages",help="Import averages CSV: game,puzzle_date,seconds")
    avgfile.add_argument("file")
    report=subs.add_parser("report",help="Write an offline report")
    report.add_argument("--output",default="leaderboard.html")
    srv=subs.add_parser("serve",help="Serve local dashboard")
    srv.add_argument("--port",type=int,default=8765)
    srv.add_argument("--poll-minutes",type=int,default=0)
    subs.add_parser("demo-reset",help="Reset all synthetic scores in this demo league")
    args=ap.parse_args()
    cfg=load_league_config(args.leagues,args.league,args.demo)
    args.league=cfg["league_key"]
    if args.demo and args.command in ("import-csv","sync-api","find-chat"):
        ap.error("The demo cannot import actual LinkedIn messages. Omit --demo.")
    if args.command=="find-chat":
        if args.csv:
            with open(args.csv,encoding="utf-8-sig",newline="") as f:
                lines=f.readlines(); first=next((i for i,ln in enumerate(lines) if ln.startswith("CONVERSATION ID,")),None)
                if first is None: raise RuntimeError("LinkedIn CSV header not found")
                list_conversations(csv.DictReader(lines[first:]))
        else:
            token=linkedin_token()
            if not token: raise RuntimeError(f"Set LINKEDIN_TOKEN, save it to {TOKEN_FILE}, or pass --csv")
            list_conversations(api_snapshot_pages(token))
        return
    base=Path(__file__).parent
    db=Path(args.db) if args.db else base/(f"{'demo' if args.demo else 'official'}_{args.league}.db")
    con=open_db(db)
    if args.demo and args.command in ("serve","report"):
        if con.execute("SELECT COUNT(*) FROM results").fetchone()[0]==0:
            seed_demo(con,cfg)
    if args.command=="demo-reset":
        if not args.demo: ap.error("demo-reset requires --demo")
        seed_demo(con,cfg)
        print(f"Reset synthetic demo database {db}")
    elif args.command=="import-csv":
        print(import_csv(con,cfg,args.file))
    elif args.command=="sync-api":
        token=linkedin_token()
        if not token: raise RuntimeError(f"Set LINKEDIN_TOKEN or save it to {TOKEN_FILE}")
        print(sync_linkedin(con,cfg,token))
    elif args.command=="senders":
        known={r["urn"]:r for r in con.execute("SELECT * FROM sender_urns")}
        for urn,r in known.items():
            print(f"{urn}  →  {canonical_player(r['name'],cfg) or r['name']}  ({r['source']})")
        pending=con.execute("""SELECT author,COUNT(*) n,MAX(sent) last,content FROM changelog_messages
          WHERE author NOT IN (SELECT urn FROM sender_urns) GROUP BY author""").fetchall()
        for r in pending:
            print(f"UNKNOWN {r['author']}: {r['n']} score messages, latest {r['last']}: {r['content'].splitlines()[0]!r}")
        if pending:
            print(f"Map one with: python3 league.py --league {args.league} map-sender <urn> <player>  (players: {', '.join(cfg['players'])})")
        elif not known:
            print("No score messages from the changelog yet.")
    elif args.command=="map-sender":
        print(map_sender(con,cfg,args.urn,args.player))
    elif args.command=="set-average":
        if date.fromisoformat(args.date)<date.fromisoformat(cfg["start_date"]):
            raise ValueError("Averages before the league start are ignored")
        set_average(con,args.game,args.date,parse_duration(args.time))
        print("Saved global average")
    elif args.command=="import-averages":
        n=0
        with open(args.file,encoding="utf-8-sig",newline="") as f:
            for r in csv.DictReader(f):
                if date.fromisoformat(r["puzzle_date"])>=date.fromisoformat(cfg["start_date"]):
                    set_average(con,r["game"],r["puzzle_date"],parse_duration(r["seconds"]),source="imported averages CSV")
                    n+=1
        print(f"Imported {n} global averages after official start")
    elif args.command=="report":
        Path(args.output).write_text(make_html(con,cfg).replace("__DEMO__","true" if args.demo else "false").replace("__ASOF__",'""'),encoding="utf-8")
        print(f"Saved {args.output}")
    elif args.command=="serve":
        serve(con,cfg,args.port,args.poll_minutes,args.demo)

if __name__=="__main__":
    main()
