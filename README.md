# LinkedIn Games League

A private, local leaderboard for LinkedIn's daily puzzle games, played in a LinkedIn chat. It reads the score shares people post, adds up their times per game, and charges a penalty for skipped games. Several separate leagues (for example a friends group and a one-to-one family chat) can run side by side, plus a practice sandbox.

Requires Python 3.11 or newer. No Python packages, paid account or hosting provider. The dashboard runs on `127.0.0.1`, not on the public internet.

## Set up

```bash
cp leagues.example.json leagues.json
```

`leagues.json` holds your real roster and stays on your computer (it is in `.gitignore`). For each league:

- `conversation_id`: the LinkedIn conversation ID (find it with `find-chat`, below).
- `players`: names shown on the leaderboard.
- `aliases`: the **exact sender names** LinkedIn shows for each player.
- `me`: the player who owns the LinkedIn API token (used to recognise their messages in the live feed).
- `start_date`: first puzzle day of the season, in Pacific time.
- `fallback_seconds`: per-game reference times for Monday to Sunday, used for penalties until real averages are available.

League keys (`friend_group`, `family_group` in the example) are free to choose; pass them with `--league`. Without `--league` the first league in the file is used.

## Try the practice sandbox

```bash
python3 league.py --demo --league friend_group serve
```

Open **http://localhost:8765/**. If another program already uses port 8765, add `--port 8775` after `serve` and open that port instead. The sandbox uses fixed sample days (Friday 16 to Sunday 18 October 2026) with fake results and fake global averages; pick a day, or **After Sunday reset**, from the dropdown. Use **Simulate someone posting a score** to change a time and **Reset sample results** to start over.

Each league has its own sandbox, for example `python3 league.py --demo --league family_group serve --port 8766`. Run each server in its own Terminal window and press **Ctrl+C** to stop it.

`practice_preview.html` is a static mock leaderboard you can open without Python.

## Season start and the no-backfill rule

`start_date` means **midnight America/Los_Angeles**, the moment LinkedIn's daily puzzles reset (09:00 in Central Europe).

- Messages sent **before the start date (Pacific time) are ignored at import**, also when they are synchronised later.
- The leaderboard also ignores any pre-start records that end up in the database.
- Sandbox scores live in `demo_<league>.db`, real scores in `official_<league>.db`. They never overlap.

The puzzle day of a score is the Pacific date of the message, and a day closes at the reset. LinkedIn numbers each game's daily puzzle one higher per day, so a score only counts for its own day's puzzle:

- A share of an earlier puzzle (for example yesterday's, posted a minute after the reset) is ignored. It neither changes the closed day nor counts as the new day.
- Bonus puzzles (numbered from 1,000,000) are ignored.

## Scoring

Seven divisions: **Queens, Tango, Zip, Mini Sudoku, Crossclimb, Wend, Patches**. Pinpoint is ignored because it is not timed.

- Raw accumulated seconds per game and in total. Lower is better.
- **Missing-game penalty**: `ceil(max(slowest posted time, LinkedIn daily global average if known, otherwise the fallback reference) × 9/8)`.
- While nobody has played a game on the current day, missing players show as "awaiting" with 0.
- Once somebody posts, absent players get a visible **provisional** penalty, replaced by their real time when they post. At midnight Pacific, remaining penalties become final.
- If everyone plays, there is no penalty.
- **Beats and beaten**, like goals for and against: in every finished game that at least one player completed, the fastest player gets a beat (ties: all of them) and everyone else gets a beaten. Fastest in Queens and Tango on a 7-game day means 2 beats, 5 beaten. The daily message shows each day's count and the season total. Daily overall wins are tracked separately.
- **Reference times**: set `fallback_seconds` to your group's typical time per game and weekday, so penalties stay proportionate (easy days tight, hard days larger). After three weeks of the season, the league's own same-weekday medians take over automatically.
- Standings are recomputed from stored scores on every page load, so a score imported late corrects the right day.

**IMPORTANT:** The fallback reference times in `leagues.example.json` are rough guesses, **not LinkedIn averages**. Agree on them with your players.

## Import scores

### From a LinkedIn data export (`messages.csv`)

```bash
python3 league.py --league friend_group find-chat --csv /path/to/messages.csv
python3 league.py --league friend_group import-csv /path/to/messages.csv
python3 league.py --league friend_group serve
```

### Automatically through LinkedIn's EU data-portability API

Members in the EU/EEA or Switzerland can read their own inbox through LinkedIn's free Member Data Portability API (Digital Markets Act). One member's token covers every chat that member is in.

1. Create an app at linkedin.com/developers/apps with the page **Member Data Portability (Member) Default Company**. Page verification is not needed.
2. On **Products**, request **Member Data Portability API (Member)**.
3. At https://www.linkedin.com/developers/tools/oauth create a token with scope `r_dma_portability_self_serve`.
4. Save it outside the repo (the importer reads `LINKEDIN_TOKEN` or this file):
   ```bash
   read -s "TOKEN?Paste token: " && printf '%s' "$TOKEN" > ~/.linkedin-league-token && chmod 600 ~/.linkedin-league-token && unset TOKEN
   ```

Then:

```bash
python3 league.py --league friend_group find-chat
python3 league.py --league friend_group sync-api
python3 league.py --league friend_group serve --port 8775 --poll-minutes 60
```

How the two API sources behave (checked with a real account in October 2026):

- The **INBOX snapshot** has the same columns as `messages.csv`, with sender names, but it is a copy made at consent time and did not refresh afterwards. It is used for history and catch-up.
- The **Member Changelog API** shows new messages, including other members' messages, within minutes and keeps 28 days. It names senders only by `urn:li:person:…` IDs. `sync-api` and `serve --poll-minutes` read it first.
- Sender IDs are learned automatically for the token holder (`me`), for the other person in a one-to-one chat, and from snapshot rows with the same chat, text and second. In a group chat, a new sender's scores wait (the dashboard shows a warning) until you map that sender once:
  ```bash
  python3 league.py --league friend_group senders
  python3 league.py --league friend_group map-sender urn:li:person:XXXX "Friend A"
  ```
- Only score messages from the league's own chat are stored, never other chat text.
- Recognised share formats: `Zip #367 | 0:05`, `Tango #735` with the time on the next line, the number-first `578. Zip | 0:15` used by some languages, and shares with a comment typed straight in front of the game name.

The API returns member data, **not** LinkedIn's game-wide daily averages. No browser-automation collector is included, because LinkedIn restricts accounts for unauthorised automation.

### Daily results message

`daily-report` writes a chat-ready summary of the last finished puzzle day (Pacific time) for every league: the day's ranking with medals, the fastest player per game, and the season table.

```bash
python3 league.py daily-report                      # print it
python3 league.py daily-report --sync --email --once  # sync, email it once per day
```

LinkedIn's API cannot send chat messages for you, so you paste the message into the chat yourself. Two ways to get it on your phone:

- **iPhone shortcut**: `--save-dir` writes `<name>.txt` (the message) and `<name>-chat.txt` (a link to the LinkedIn chat), where `<name>` is the league's optional `share_name` in `leagues.json`, else its key. With `--sync` it reads only the changelog, so a run at 09:01 Central European time (00:01 Pacific) finishes in seconds. Point it at `~/Library/Mobile Documents/iCloud~is~workflow~my~workflows/Documents/Games League` (shown on the iPhone as iCloud Drive › Shortcuts › Games League) and build a shortcut: *Get File* `Games League/<league>.txt` → *Copy to Clipboard* → *Get File* `Games League/<league>-chat.txt` → *Open URLs*. One tap copies the message and opens the chat.
- **Email**: `--email`. Email uses SMTP settings in `~/.games-league-mail.json` (`user`, `password`, optional `to`, `host`, `port`; for Gmail use an app password). Schedule the command shortly after the reset, at 00:05 Pacific; with `--once` it is safe to run it at both 08:05 and 09:05 Central European time to cover the weeks when Europe and the US change clocks on different dates.

### Global averages

If you collect LinkedIn's daily averages yourself, import them after the start:

```csv
game,puzzle_date,seconds
Queens,2026-10-19,120
Tango,2026-10-19,70
```

```bash
python3 league.py --league friend_group import-averages averages.csv
```

## Offline, recovery and costs

The data volumes are tiny. SQLite writes scores to disk, and the changelog keeps 28 days, so a Mac that was asleep or off catches up on the next sync. The dashboard itself is offline while the computer is off.

To view the leaderboard from a phone on the same Wi-Fi, add `--host 0.0.0.0` to `serve` and open `http://<computer-name>.local:<port>` (macOS may ask once whether Python may accept incoming connections). The official dashboard is read-only; the sandbox always stays on 127.0.0.1. On a public network, other people on that network could open it too.

Running locally costs nothing. There is no hosted or mobile version: do **not** expose the unauthenticated local server to the internet.

## Tests

```bash
python3 -m unittest -v test_league.py
```

Tests cover penalty rounding, share parsing, missing players, global averages, beats, delayed imports, the no-backfill rule, two- and three-player leagues, separate sandbox databases, and changelog sender matching.

## Files

- `league.py`: server, parser, scoring engine and import tools.
- `leagues.example.json`: example league configuration; copy it to `leagues.json` (not committed).
- `dashboard.html`: responsive dashboard with the practice form.
- `test_league.py`: automated tests.
- `practice_preview.html`: static example leaderboard.

Privacy: get your players' consent before storing their scores. Keep `leagues.json`, the databases and the token out of source control.
