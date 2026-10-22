import io
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from league import penalty, parse_scores, ingest_rows, open_db, leaderboard, set_average, parse_duration

class LeagueTests(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.TemporaryDirectory()
        self.con = open_db(Path(self.t.name)/"test.db")
        self.cfg = {
            "start_date":"2026-10-11","conversation_id":"thread-1",
            "players":["A","B","C"],"aliases":{"A":["Alice"],"B":["Bob"],"C":["Charlie"]},
            "fallback_seconds":dict.fromkeys(["Queens","Tango","Zip","Mini Sudoku","Crossclimb","Wend","Patches"],120),
        }
    def tearDown(self):
        self.con.close()
        self.t.cleanup()
    def test_penalty_ceiling(self):
        self.assertEqual([penalty(n) for n in [8,9,20,60,120,200]], [9,11,23,68,135,225])
    def test_parse_multi_game(self):
        text = "Queens #803 | 7:18 with no hints\nWend #35 | 0:12 🌀\nMini Sudoku #335 | 2:40 and smarter than 90% of CEOs"
        self.assertEqual(parse_scores(text), [("Queens",803,438),("Wend",35,12),("Mini Sudoku",335,160)])
    def test_parse_number_first_turkish_share(self):
        text = "578. Zip | 0:15 🏁\n🏅 Bugün tüm oyuncuların içinde en iyi %25 arasındayım!\nlnkd.in/zip."
        self.assertEqual(parse_scores(text), [("Zip",578,15)])
        self.assertEqual(parse_scores("431. Mini Sudoku | 1:49 ✏️"), [("Mini Sudoku",431,109)])
    def test_parse_time_on_next_line(self):
        self.assertEqual(parse_scores("Tango #735\n0:17 🌗\nlnkd.in/tango."), [("Tango",735,17)])
    def test_parse_comment_glued_to_share(self):
        self.assertEqual(parse_scores("so closePatches #209 | 0:12 🧶"), [("Patches",209,12)])
    def test_parse_ignores_pinpoint_and_version_numbers(self):
        self.assertEqual(parse_scores("897. Pinpoint | 1 tahmin\nv1.578. Zip"), [])
    def test_missing_two_and_idempotence(self):
        example = {"CONVERSATION ID":"thread-1","FROM":"Alice","DATE":"2026-10-11 12:00:00 UTC", "CONTENT":"Queens #800 | 0:20"}
        self.assertEqual(ingest_rows(self.con,self.cfg,[example])["inserted"], 1)
        self.assertEqual(ingest_rows(self.con,self.cfg,[example])["duplicates"], 1)
        data = leaderboard(self.con,self.cfg,datetime(2026,10,12,12,0,tzinfo=timezone.utc))
        day = next(d for d in data["daily"] if d["date"]=="2026-10-11" and d["game"]=="Queens")
        self.assertEqual(day["players"]["A"]["seconds"],20)
        self.assertEqual(day["players"]["B"]["seconds"],135)
        self.assertEqual(day["players"]["C"]["seconds"],135)
    def test_weekday_reference_bootstrap(self):
        self.cfg["fallback_seconds"]["Queens"] = [9, 12, 16, 25, 40, 80, 120]
        rows = [
            {"CONVERSATION ID":"thread-1","FROM":"Alice","DATE":"2026-10-11 12:00:00 UTC", "CONTENT":"Queens #800 | 0:20"},
            {"CONVERSATION ID":"thread-1","FROM":"Bob","DATE":"2026-10-11 12:30:00 UTC", "CONTENT":"Queens #800 | 2:00"}
        ]
        ingest_rows(self.con,self.cfg,rows)
        day = next(d for d in leaderboard(self.con,self.cfg,datetime(2026,10,12,12,0,tzinfo=timezone.utc))["daily"]
                   if d["date"]=="2026-10-11" and d["game"]=="Queens")
        self.assertEqual(day["baseline"], 120)
        self.assertEqual(day["missing_penalty"], 135)

    def test_global_average_and_live_provisional_penalties(self):
        set_average(self.con,"Queens","2026-10-11",120)
        rows = [
            {"CONVERSATION ID":"thread-1","FROM":"Alice","DATE":"2026-10-11 12:00:00 UTC", "CONTENT":"Queens #800 | 0:20"},
            {"CONVERSATION ID":"thread-1","FROM":"Bob","DATE":"2026-10-11 12:00:30 UTC", "CONTENT":"Queens #800 | 0:20"},
        ]
        ingest_rows(self.con,self.cfg,rows)
        live = leaderboard(self.con,self.cfg,datetime(2026,10,11,12,30,tzinfo=timezone.utc))
        q = next(d for d in live["daily"] if d["date"]=="2026-10-11" and d["game"]=="Queens")
        self.assertFalse(q["final"])
        self.assertEqual(q["global_average"],120)
        self.assertEqual(q["players"]["C"],{"status":"provisional","seconds":135,"is_fastest":False})
        self.assertEqual(live["totals"]["Queens"]["A"]["beats"],0)
        self.assertEqual(q["players"]["A"]["seconds"],20)
        ingest_rows(self.con,self.cfg,[{"CONVERSATION ID":"thread-1","FROM":"Charlie","DATE":"2026-10-11 13:00:00 UTC", "CONTENT":"Queens #800 | 0:21"}])
        live2 = leaderboard(self.con,self.cfg,datetime(2026,10,11,13,30,tzinfo=timezone.utc))
        q = next(d for d in live2["daily"] if d["date"]=="2026-10-11" and d["game"]=="Queens")
        self.assertTrue(q["final"])
        self.assertEqual(q["missing_penalty"],0)
        self.assertEqual([live2["totals"]["Queens"][p]["beats"] for p in ("A","B","C")],[1,1,0])
        self.assertEqual([live2["totals"]["Queens"][p]["beaten"] for p in ("A","B","C")],[0,0,1])

    def test_offline_catchup_recalculates_penalty(self):
        now=datetime(2026,10,12,12,0,tzinfo=timezone.utc)
        day=lambda data: next(d for d in data["daily"] if d["date"]=="2026-10-11" and d["game"]=="Queens")
        original=day(leaderboard(self.con,self.cfg,now))
        self.assertEqual(original["players"]["A"]["status"],"missed")
        ingest_rows(self.con,self.cfg,[{"CONVERSATION ID":"thread-1","FROM":"Alice","DATE":"2026-10-11 15:00:00 UTC", "CONTENT":"Queens #800 | 0:20"}])
        after=day(leaderboard(self.con,self.cfg,now))
        self.assertEqual(after["players"]["A"]["seconds"],20)
        self.assertEqual(after["players"]["A"]["status"],"played")
        self.assertEqual(after["players"]["B"]["seconds"],135)

    def test_average_fallback_and_total_time(self):
        self.assertEqual(parse_duration("2:00"),120)
        self.assertEqual(parse_duration("135"),135)
        set_average(self.con,"Queens","2026-10-11",120)
        after=leaderboard(self.con,self.cfg,datetime(2026,10,12,12,0,tzinfo=timezone.utc))
        d=next(x for x in after["daily"] if x["date"]=="2026-10-11" and x["game"]=="Queens")
        self.assertEqual(d["missing_penalty"],135)
        self.assertEqual(sum(v["seconds"] for v in after["daily_overall"][0]["players"].values()),sum(x["players"][p]["seconds"] for x in after["daily"] if x["date"]=="2026-10-11" for p in self.cfg["players"]))

    def test_no_non_group_scores(self):
        example = {"CONVERSATION ID":"other","FROM":"Alice","DATE":"2026-10-11 12:00:00 UTC", "CONTENT":"Queens #800 | 0:20"}
        self.assertEqual(ingest_rows(self.con,self.cfg,[example]),{})
if __name__ == "__main__": unittest.main()

class NewSeasonBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.TemporaryDirectory()
        self.db=Path(self.t.name)/"official.db"
        self.con=open_db(self.db)
        # Copy of leagues.example.json with test players, so tests never depend on a real roster.
        leagues=json.loads((Path(__file__).parent/'leagues.example.json').read_text(encoding='utf-8'))
        leagues['friend_group'].update(me="You",conversation_id="friend-chat",players=["You","Friend A","Friend B"],
                                       aliases={"You":["Alice"],"Friend A":["Bob"],"Friend B":["Charlie"]})
        leagues['family_group'].update(me="You",players=["You","Sister"],aliases={"You":["Alice"],"Sister":["Sara"]})
        self.leagues=Path(self.t.name)/'leagues.json'
        self.leagues.write_text(json.dumps(leagues),encoding='utf-8')
        from league import load_league_config
        self.cfg=load_league_config(self.leagues,'friend_group')
    def tearDown(self):
        self.con.close();self.t.cleanup()
    def row(self, name, instant, text, thread="friend-chat"):
        return {"CONVERSATION ID":thread,"FROM":name,"DATE":instant,"CONTENT":text}
    def test_old_messages_not_imported_despite_new_sync(self):
        # Friday October 16 data is from the same conversation, but the league starts Monday.
        old=self.row("Alice","2026-10-16 12:00:00 UTC","Queens #800 | 0:04")
        newer=self.row("Alice","2026-10-19 12:00:00 UTC","Queens #803 | 0:08")
        counts=ingest_rows(self.con,self.cfg,[old,newer])
        self.assertEqual(counts["ignored_before_start"],1)
        self.assertEqual(counts["inserted"],1)
        rows=self.con.execute('SELECT puzzle_date,seconds FROM results').fetchall()
        self.assertEqual([(r[0],r[1]) for r in rows],[('2026-10-19',8)])
        report=leaderboard(self.con,self.cfg,datetime(2026,10,19,20,tzinfo=timezone.utc))
        self.assertEqual(report['totals']['Queens']['You']['played'],1)
        self.assertEqual(len(report['daily_overall']),1)
    def test_before_start_even_with_accidentally_stored_old_records(self):
        self.con.execute("""INSERT INTO results(game,puzzle_date,player,puzzle_number,seconds,msg_time,fingerprint)
          VALUES('Queens','2026-10-16','You',800,4,'2026-10-16T12:00:00+00:00','old')""")
        self.con.commit()
        report=leaderboard(self.con,self.cfg,datetime(2026,10,18,22,tzinfo=timezone.utc))
        self.assertEqual(report['overall']['You']['seconds'],0)
        self.assertEqual(report['daily'],[])
    def test_first_day_awaiting_zero_and_then_provisional(self):
        t=datetime(2026,10,19,13,tzinfo=timezone.utc)
        before=leaderboard(self.con,self.cfg,t)
        queens=next(x for x in before['daily'] if x['game']=='Queens')
        self.assertEqual(queens['players']['You']['status'],'awaiting')
        self.assertEqual(queens['players']['You']['seconds'],0)
        self.assertEqual(before['overall']['You']['pending'],0)
        ingest_rows(self.con,self.cfg,[self.row('Alice','2026-10-19 12:00:00 UTC','Queens #803 | 0:09')])
        after=leaderboard(self.con,self.cfg,t)
        queens=next(x for x in after['daily'] if x['game']=='Queens')
        self.assertEqual(queens['players']['Friend A']['status'],'provisional')
        self.assertGreater(queens['players']['Friend A']['seconds'],9)
    def test_2_separate_leagues_and_demo(self):
        from league import load_league_config, save_demo_score, seed_demo, demo_time
        family=load_league_config(self.leagues,'family_group',demo=True)
        friends=load_league_config(self.leagues,'friend_group',demo=True)
        self.assertEqual(len(family['players']),2)
        self.assertEqual(len(friends['players']),3)
        self.assertEqual(family['start_date'],'2026-10-16')
        self.assertEqual(family['end_date'],'2026-10-18')
        demo_db=open_db(Path(self.t.name)/'demo.db')
        seed_demo(demo_db,family)
        sample=leaderboard(demo_db,family,demo_time('after'))
        self.assertEqual(len(sample['daily']),21)
        self.assertTrue(all(d['final'] for d in sample['daily']))
        self.assertIn('Sister',sample['overall'])
        self.assertNotIn('Sister',leaderboard(self.con,self.cfg,datetime(2026,10,19,14,tzinfo=timezone.utc))['overall'])
        demo_db.close()
    def test_penalty_uses_global_average_no_outside_start(self):
        from league import set_average
        set_average(self.con,'Queens','2026-10-19',120)
        ingest_rows(self.con,self.cfg,[self.row('Alice','2026-10-19 12:00:00 UTC','Queens #803 | 0:20'),self.row('Bob','2026-10-19 13:00:00 UTC','Queens #803 | 0:20')])
        q=next(x for x in leaderboard(self.con,self.cfg,datetime(2026,10,19,14,tzinfo=timezone.utc))['daily'] if x['game']=='Queens')
        self.assertEqual(q['missing_penalty'],135)
        self.assertEqual(q['players']['Friend B']['status'],'provisional')

class ChangelogTests(unittest.TestCase):
    """Member Changelog events identify senders by person URN only."""
    def setUp(self):
        self.t = tempfile.TemporaryDirectory()
        self.con = open_db(Path(self.t.name)/"official.db")
        self.group = {"start_date":"2026-10-19","conversation_id":"group","me":"A","players":["A","B","C"],
                      "aliases":{"A":["Alice"],"B":["Bob"],"C":["Charlie"]},
                      "fallback_seconds":dict.fromkeys(["Queens","Tango","Zip","Mini Sudoku","Crossclimb","Wend","Patches"],120)}
        self.pair = dict(self.group, conversation_id="pair", players=["A","B"], aliases={"A":["Alice"],"B":["Bob"]})
    def tearDown(self):
        self.con.close(); self.t.cleanup()
    def ev(self, n, author, thread, text, when=datetime(2026,10,19,12,tzinfo=timezone.utc)):
        ms = int(when.timestamp()*1000)
        return {"id":n,"owner":"urn:li:person:ME","actor":author,"resourceName":"messages","method":"CREATE","processedAt":ms+500,
                "activity":{"id":f"msg-{n}","author":author,"thread":"urn:li:messagingThread:"+thread,"createdAt":ms,"content":{"fallback":text}}}
    def results(self):
        return sorted((r["player"],r["game"],r["seconds"]) for r in self.con.execute("SELECT * FROM results"))
    def test_owner_known_and_two_player_chat_infers_other(self):
        from league import ingest_changelog
        stats = ingest_changelog(self.con, self.pair, [self.ev(1,"urn:li:person:SIS","pair","Zip #581 | 0:27"),
                                                       self.ev(2,"urn:li:person:ME","pair","581. Zip | 0:15")])
        self.assertEqual(stats["unknown_sender"], 0)
        self.assertEqual(self.results(), [("A","Zip",15),("B","Zip",27)])
    def test_group_sender_waits_until_mapped(self):
        from league import ingest_changelog, map_sender
        stats = ingest_changelog(self.con, self.group, [self.ev(1,"urn:li:person:X","group","Queens #902 | 0:41")])
        self.assertEqual(stats["unknown_sender"], 1)
        self.assertEqual(self.results(), [])
        self.assertEqual(leaderboard(self.con,self.group,datetime(2026,10,19,13,tzinfo=timezone.utc))["unknown_senders"], 1)
        self.assertEqual(map_sender(self.con, self.group, "X", "C")["unknown_sender"], 0)
        self.assertEqual(self.results(), [("C","Queens",41)])
    def test_snapshot_row_teaches_sender(self):
        from league import ingest_changelog, learn_senders_from_rows, resolve_changelog
        ingest_changelog(self.con, self.group, [self.ev(1,"urn:li:person:Y","group","Tango #742 | 0:30")])
        snapshot = [{"CONVERSATION ID":"group","FROM":"Bob","DATE":"2026-10-19 12:00:01 UTC","CONTENT":"Tango #742 | 0:30"}]
        self.assertEqual(learn_senders_from_rows(self.con, self.group, snapshot), 1)
        resolve_changelog(self.con, self.group)
        self.assertEqual(self.results(), [("B","Tango",30)])
    def test_chat_text_other_chats_and_pre_season_are_not_scored(self):
        from league import ingest_changelog
        ingest_changelog(self.con, self.group, [
            self.ev(1,"urn:li:person:ME","group","Test"),
            self.ev(2,"urn:li:person:ME","other","Zip #580 | 0:09"),
            self.ev(3,"urn:li:person:ME","group","Zip #578 | 0:11", datetime(2026,10,17,17,tzinfo=timezone.utc))])
        stored = [r["content"] for r in self.con.execute("SELECT content FROM changelog_messages")]
        self.assertEqual(stored, ["Zip #578 | 0:11"])  # only score text from this chat is kept
        self.assertEqual(self.results(), [])            # and the start-date gate still applies

class DailyReportTests(unittest.TestCase):
    def test_report_ranks_day_and_season_and_ignores_later_days(self):
        from league import daily_report_text
        from datetime import date
        t = tempfile.TemporaryDirectory(); self.addCleanup(t.cleanup)
        con = open_db(Path(t.name)/"r.db"); self.addCleanup(con.close)
        cfg = {"name":"Test league","start_date":"2026-10-19","conversation_id":"c","players":["A","B"],
               "aliases":{"A":["Alice"],"B":["Bob"]},"fallback_seconds":dict.fromkeys(["Queens","Tango","Zip","Mini Sudoku","Crossclimb","Wend","Patches"],[10]*7)}
        row = lambda who, when, text: {"CONVERSATION ID":"c","FROM":who,"DATE":when,"CONTENT":text}
        ingest_rows(con, cfg, [row("Alice","2026-10-19 12:00:00 UTC","Queens #902 | 0:20"),
                               row("Bob","2026-10-19 13:00:00 UTC","Queens #902 | 0:30\nZip #581 | 0:05"),
                               row("Alice","2026-10-20 12:00:00 UTC","Queens #903 | 0:01")])
        text = daily_report_text(con, cfg, date(2026,10,19))
        self.assertIn("Test league · Mon 19 Oct · final", text)
        self.assertIn("👑 A 0:20", text)
        self.assertIn("🏁 B 0:05", text)
        self.assertIn("Season after 1 day:", text)
        # A: fastest Queens, beaten on Zip (missed). Games nobody completed give no beats or beaten.
        self.assertIn("🥇 A 1:32 · 1 beats, 1 beaten · 6 missed", text)
        self.assertIn("🥈 B 1:35 · 1 beats, 1 beaten · 5 missed", text)
        self.assertNotIn("0:01", text)  # the next day's score is not part of this report
        self.assertIn("season starts", daily_report_text(con, cfg, date(2026,10,18)))

class PuzzleNumberTests(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.TemporaryDirectory()
        self.con = open_db(Path(self.t.name)/"p.db")
        self.cfg = {"start_date":"2026-10-19","conversation_id":"c","players":["A","B"],"aliases":{"A":["Alice"],"B":["Bob"]},
                    "fallback_seconds":dict.fromkeys(["Queens","Tango","Zip","Mini Sudoku","Crossclimb","Wend","Patches"],[60]*7)}
    def tearDown(self):
        self.con.close(); self.t.cleanup()
    def test_late_share_of_yesterday_changes_nothing(self):
        # Queens #902 is Monday 19 Oct. At 09:01 in Germany (00:01 Pacific) Tuesday's puzzle is #903.
        rows = [{"CONVERSATION ID":"c","FROM":"Alice","DATE":"2026-10-20 07:01:00 UTC","CONTENT":"Queens #902 | 0:20"},
                {"CONVERSATION ID":"c","FROM":"Alice","DATE":"2026-10-20 15:00:00 UTC","CONTENT":"Queens #903 | 0:31"}]
        stats = ingest_rows(self.con, self.cfg, rows)
        self.assertEqual(stats, {"ignored_old_puzzle":1, "inserted":1})
        self.assertEqual([tuple(r) for r in self.con.execute("SELECT puzzle_date,puzzle_number,seconds FROM results")],
                         [("2026-10-20",903,31)])
    def test_bonus_puzzle_ignored(self):
        rows = [{"CONVERSATION ID":"c","FROM":"Bob","DATE":"2026-10-19 12:00:00 UTC","CONTENT":"Bonus puzzleQueens #1000027 | 1:14"}]
        self.assertEqual(ingest_rows(self.con, self.cfg, rows), {"ignored_bonus_puzzle":1})

class LiveReportTests(unittest.TestCase):
    def test_live_text_shows_today_so_far(self):
        from league import live_report_text
        t = tempfile.TemporaryDirectory(); self.addCleanup(t.cleanup)
        con = open_db(Path(t.name)/"l.db"); self.addCleanup(con.close)
        cfg = {"name":"Test league","start_date":"2026-10-19","conversation_id":"c","players":["A","B"],
               "aliases":{"A":["Alice"],"B":["Bob"]},"fallback_seconds":dict.fromkeys(["Queens","Tango","Zip","Mini Sudoku","Crossclimb","Wend","Patches"],[10]*7)}
        ingest_rows(con, cfg, [{"CONVERSATION ID":"c","FROM":"Alice","DATE":"2026-10-19 12:00:00 UTC","CONTENT":"Queens #902 | 0:20"}])
        text = live_report_text(con, cfg, datetime(2026,10,19,13,tzinfo=timezone.utc))
        self.assertIn("Test league · Mon 19 Oct · live", text)
        self.assertIn("1. A 0:20 · 1/7 played", text)
        self.assertIn("2. B 0:23 · 0/7 played", text)   # provisional penalty: ceil(20 * 9/8)
        self.assertIn("👑 A 0:20", text)
        self.assertIn("season starts", live_report_text(con, cfg, datetime(2026,10,18,13,tzinfo=timezone.utc)))

class LeagueGamesTests(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.TemporaryDirectory(); self.addCleanup(self.t.cleanup)
        self.cfg = {"name":"G","start_date":"2026-10-19","conversation_id":"c","players":["A","B"],"me":"A",
                    "aliases":{"A":["Alice"],"B":["Bob"]},"games":["Queens","Zip"],
                    "fallback_seconds":dict.fromkeys(["Queens","Tango","Zip","Mini Sudoku","Crossclimb","Wend","Patches"],[10]*7)}
    def test_games_outside_the_league_never_count_not_even_as_penalties(self):
        con = open_db(Path(self.t.name)/"g.db"); self.addCleanup(con.close)
        ingest_rows(con, self.cfg, [{"CONVERSATION ID":"c","FROM":"Alice","DATE":"2026-10-19 12:00:00 UTC",
                                     "CONTENT":"Queens #902 | 0:20\nMini Sudoku #434 | 1:00\nZip #581 | 0:05"},
                                    {"CONVERSATION ID":"c","FROM":"Bob","DATE":"2026-10-19 13:00:00 UTC","CONTENT":"Queens #902 | 0:30"}])
        data = leaderboard(con, self.cfg, datetime(2026,10,20,12,tzinfo=timezone.utc))
        self.assertEqual(data["games"], ["Queens","Zip"])
        self.assertEqual(sorted(data["totals"]), ["Queens","Zip"])
        self.assertEqual({d["game"] for d in data["daily"]}, {"Queens","Zip"})
        self.assertEqual(data["overall"]["A"]["seconds"], 25)            # Sudoku's 1:00 is ignored
        self.assertEqual(data["overall"]["B"]["seconds"], 30 + 12)       # missed Zip: ceil(max(5,10)*9/8); no Sudoku penalty
    def test_sync_makes_one_changelog_call_for_all_leagues_and_snapshot_weekly(self):
        from unittest import mock
        import league
        cons = [open_db(Path(self.t.name)/f"{k}.db") for k in ("x","y")]
        for c in cons: self.addCleanup(c.close)
        leagues = [(dict(self.cfg, league_key="x"), cons[0]), (dict(self.cfg, league_key="y", conversation_id="d"), cons[1])]
        with mock.patch.object(league, "api_changelog_events", return_value=iter([])) as feed, \
             mock.patch.object(league, "api_snapshot_pages", return_value=iter([])) as snap:
            league.sync_leagues(leagues, "token")
            feed.return_value = iter([])
            league.sync_leagues(leagues, "token")
        self.assertEqual(feed.call_count, 2)   # one per sync, shared by both leagues
        self.assertEqual(snap.call_count, 1)   # the second sync is within the week
