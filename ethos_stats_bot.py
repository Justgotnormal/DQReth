"""
Dual-Source Drop-Stats Bot (Ethos Suite + Harvard Labs)
==========================================================

Tracks run results from TWO different game-notification bots at once:

- Harvard Labs: one message per run, already combining every account that
  took part (e.g. "VICTORY ... 2 accounts ... 6 drops" + a per-account
  breakdown). Rarity is given as a plain word ("Epic", "Common").
- Ethos Suite: one message PER PLAYER per run (so a 2-4 person Rodin fight
  arrives as several separate messages that need merging back into one
  run). Rarity is given as a colored emoji circle.

Each incoming message is routed to exactly ONE of the two parsers based on
its format — a Harvard Labs message always contains an "(@handle)" account
tag that an Ethos Suite message never has — so no message can ever be
double-counted by both parsers.

IMPORTANT CAVEAT — POSSIBLE DOUBLE-COUNTING ACROSS SOURCES
--------------------------------------------------------------
If these two bots are ever reporting on the SAME underlying game runs
(rather than genuinely separate farming activity), tracking both will
double your stats. This file has no way to detect that — it can only avoid
parsing one message twice, not tell whether two different messages describe
the same real-world event. Watch whether !combinedstats numbers look
roughly double what you'd expect; if so, you may want to track one source
only (the Harvard Labs section below is simpler and less error prone to
remove the other if needed).

Setup
-----
pip install discord.py
Set DISCORD_BOT_TOKEN as an environment variable.
Enable "Message Content Intent" for your bot in the Developer Portal.

Run
---
python ethos_stats_bot.py
"""

import asyncio
import os
import re
import sqlite3

import discord
from discord.ext import commands

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# Defaults to a local file for running on your own machine. On Railway,
# set the DB_PATH environment variable to your mounted volume's path
# (e.g. /data/ethos_drops.db) so data survives redeploys.
DB_PATH = os.environ.get("DB_PATH", "ethos_drops.db")

# Harvard Labs gives rarity as a plain word already — "Epic" is mapped to
# "Epic Gear" only to keep the same tier label used in the original
# reference stats format (and in rows already logged by the old parser).
RARITY_WORD_MAP = {
    "common": "Common",
    "uncommon": "Uncommon",
    "rare": "Rare",
    "epic": "Epic Gear",
    "legendary": "Legendary",
    "ultimate": "Ultimate",
}

# Ethos Suite gives rarity as a colored circle emoji instead of a word.
UNICODE_RARITY_MAP = {
    "⚪": "Common",
    "🟢": "Uncommon",
    "🔵": "Rare",
    "🟣": "Epic Gear",
    "🟠": "Legendary",
    "🔴": "Ultimate",
}

# Item/ability types that should be excluded from the gear breakdown,
# same as "spells" in the original stats format. Harvard Labs appears to
# use "Spell" directly as a type (e.g. "Gale Barrage (Spell)").
EXCLUDED_TYPES = {"spell", "ability"}

TIER_ORDER = ["Common", "Uncommon", "Rare", "Epic Gear", "Legendary", "Ultimate", "Unknown"]

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

# Every connection gets WAL mode (lets reads and writes coexist instead of
# locking the whole file) and a busy timeout (so a brief lock conflict waits
# and retries instead of raising immediately). Without these, several
# writes landing close together could make a write block — and because
# every DB call runs in a worker thread (see on_message), a blocked write
# only delays itself rather than freezing the whole bot's Discord connection,
# which is what caused an earlier "online but not recording" outage.
def _connect():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn

def init_db():
    conn = _connect()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            outcome TEXT,               -- 'win' or 'loss'
            clear_seconds INTEGER,
            player_name TEXT,
            is_rodin INTEGER DEFAULT 0,
            source TEXT DEFAULT 'ethos',   -- 'ethos' or 'harvard'
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # Migrations for databases created before these columns existed.
    for migration in (
        "ALTER TABLE runs ADD COLUMN is_rodin INTEGER DEFAULT 0",
        "ALTER TABLE runs ADD COLUMN source TEXT DEFAULT 'ethos'",
    ):
        try:
            conn.execute(migration)
        except sqlite3.OperationalError:
            pass  # column already exists
    conn.execute("""
        CREATE TABLE IF NOT EXISTS drops (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER,
            item_name TEXT,
            item_type TEXT,
            rarity TEXT,
            power REAL,
            potential_power REAL,
            FOREIGN KEY(run_id) REFERENCES runs(id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_created_at ON runs(created_at)")
    conn.commit()
    conn.close()

def log_run(outcome: str, clear_seconds, player_names, items, is_rodin: int = 0, source: str = "ethos") -> int:
    conn = _connect()
    cur = conn.execute(
        "INSERT INTO runs (outcome, clear_seconds, player_name, is_rodin, source) VALUES (?, ?, ?, ?, ?)",
        (outcome, clear_seconds, player_names, is_rodin, source),
    )
    run_id = cur.lastrowid
    for item in items:
        conn.execute(
            """INSERT INTO drops (run_id, item_name, item_type, rarity, power, potential_power)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (run_id, item["name"], item["type"], item["rarity"],
             item.get("power"), item.get("potential_power")),
        )
    conn.commit()
    conn.close()
    return run_id

# How long after an Ethos Suite result message to still consider a later one
# part of the same shared party attempt, rather than a brand new run.
# Harvard Labs never needs this — each of its messages already combines every
# account that took part into one complete run.
RUN_MERGE_WINDOW_SECONDS = 15

def find_recent_ethos_rodin_run(outcome: str):
    """Find an Ethos Suite Rodin run logged very recently with the same
    outcome — almost certainly the same shared party battle posted once per
    player. Returns (run_id, existing_player_names) or None. Scoped to
    source='ethos' so it can never merge with a Harvard Labs run."""
    conn = _connect()
    row = conn.execute(
        f"""SELECT id, player_name FROM runs
            WHERE is_rodin = 1 AND source = 'ethos' AND outcome = ?
              AND created_at >= datetime('now', '-{RUN_MERGE_WINDOW_SECONDS} seconds')
            ORDER BY id DESC LIMIT 1""",
        (outcome,),
    ).fetchone()
    conn.close()
    return row

def merge_into_run(run_id: int, existing_player_names, new_player_names, items):
    conn = _connect()
    existing = existing_player_names.split(",") if existing_player_names else []
    new = new_player_names.split(",") if new_player_names else []
    merged = list(dict.fromkeys([*existing, *new]))
    conn.execute("UPDATE runs SET player_name = ? WHERE id = ?", (",".join(merged), run_id))
    for item in items:
        conn.execute(
            """INSERT INTO drops (run_id, item_name, item_type, rarity, power, potential_power)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (run_id, item["name"], item["type"], item["rarity"],
             item.get("power"), item.get("potential_power")),
        )
    conn.commit()
    conn.close()

def fetch_runs():
    conn = _connect()
    rows = conn.execute("SELECT outcome, clear_seconds, player_name, is_rodin FROM runs").fetchall()
    conn.close()
    return rows

def fetch_drops():
    conn = _connect()
    rows = conn.execute("SELECT item_name, item_type, rarity FROM drops").fetchall()
    conn.close()
    return rows

def reset_db():
    conn = _connect()
    conn.execute("DELETE FROM drops")
    conn.execute("DELETE FROM runs")
    conn.commit()
    conn.close()

# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

# Strip Discord markdown (bold **, backtick code spans) before parsing so
# regexes don't have to account for it. A screenshot can't show whether
# these characters are literally present, so stripping them is cheap
# insurance either way.
MD_STRIP_PATTERN = re.compile(r"[*`]")

# Ethos Suite specific: its rarity circle is a plain unicode emoji, but a
# custom Discord emoji (<:name:id>) is also matched just in case and treated
# as "Unknown" rarity rather than silently dropping the item.
CUSTOM_EMOJI_PATTERN = re.compile(r"<a?:(\w+):(\d+)>")
UNICODE_EMOJI_PATTERN = re.compile("|".join(re.escape(e) for e in UNICODE_RARITY_MAP))

NUM_SUFFIX = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}

def parse_number(text: str) -> float:
    """Turn '57.05b', '14.13k', '1,018' etc into a plain float."""
    text = text.strip().replace(",", "")
    match = re.match(r"^([\-\d.]+)\s*([kmbKMB]?)$", text)
    if not match:
        return 0.0
    value, suffix = match.groups()
    value = float(value)
    if suffix.lower() in NUM_SUFFIX:
        value *= NUM_SUFFIX[suffix.lower()]
    return value

# "Varity (@DQRTESTING321123) - 3 drops" or "... - Lv 214 - 3 drops"
ACCOUNT_HEADER_PATTERN = re.compile(
    r"([^\n(]+?)\s*\(@([\w.]+)\)\s*-\s*(?:Lv\s*\d+\s*-\s*)?(\d+)\s*drops?",
    re.IGNORECASE,
)

# "Epic - Gale Barrage (Spell), Jotunn Mage Armor (Armour)"
RARITY_LINE_PATTERN = re.compile(
    r"^\s*(Common|Uncommon|Rare|Epic|Legendary|Ultimate)\s*-\s*(.+)$",
    re.IGNORECASE | re.MULTILINE,
)

# "Gale Barrage (Spell)" within a rarity line's comma-separated item list
ITEM_TYPE_PATTERN = re.compile(r"([^,()]+?)\s*\(([^)]+)\)")

def get_full_text(message: discord.Message) -> str:
    if message.embeds:
        embed = message.embeds[0]
        parts = [embed.title or "", embed.description or ""]
        for field in embed.fields:
            parts.append(field.name or "")
            parts.append(field.value or "")
        if embed.footer and embed.footer.text:
            parts.append(embed.footer.text)
        return "\n".join(parts)
    return message.content

def parse_clear_time(text: str):
    """'4:35' / '02:22' -> seconds. Returns None if unparseable."""
    text = text.strip()
    if ":" not in text:
        return None
    try:
        parts = [int(p) for p in text.split(":")]
    except ValueError:
        return None
    seconds = 0
    for p in parts:
        seconds = seconds * 60 + p
    return seconds

def parse_harvard_message(raw: str):
    """
    Returns {"outcome": "win"/"loss", "clear_seconds": int|None,
    "player_names": "handle1,handle2", "items": [...], "is_rodin": bool}
    or None if this message isn't a recognized Harvard Labs run result.

    Each Harvard Labs message already represents ONE complete run across
    every account that took part — unlike the old per-player Ethos Suite
    messages, nothing here needs to be merged with another message.
    """
    clean = MD_STRIP_PATTERN.sub("", raw)
    upper = clean.upper()

    is_rodin = "RODIN" in upper
    if is_rodin and "FAILED" in upper:
        outcome = "loss"
    elif "VICTORY" in upper or "CLEARED" in upper:
        outcome = "win"
    else:
        return None  # not a run-result message

    # Everything before the first account header ("Name (@handle) - ...")
    # is the summary/header area: outcome badge, time, accounts, drops,
    # deaths, haul value, best-drop callout. Restricting these lookups to
    # that header segment avoids accidentally matching a stray number
    # inside an account's own drop list further down.
    first_header_match = ACCOUNT_HEADER_PATTERN.search(clean)
    header_segment = clean[:first_header_match.start()] if first_header_match else clean

    time_match = re.search(r"\b(\d{1,2}:\d{2})\b", header_segment)
    clear_seconds = parse_clear_time(time_match.group(1)) if time_match else None

    # Find every account's header line, then slice out the text between
    # each one (up to the next header, "Session", the footer, or the end)
    # as that account's item list.
    headers = list(ACCOUNT_HEADER_PATTERN.finditer(clean))
    player_names = []
    items = []

    for i, h in enumerate(headers):
        handle = h.group(2).strip()
        if handle:
            player_names.append(handle)

        segment_start = h.end()
        segment_end = headers[i + 1].start() if i + 1 < len(headers) else len(clean)
        segment = clean[segment_start:segment_end]

        # Cut the segment off at "Session" or the footer timestamp if present,
        # so trailing fields after the last account never get misread as items.
        for stop_marker in (r"\bSession\b", r"\bToday at\b", r"\bYesterday at\b"):
            stop_match = re.search(stop_marker, segment, re.IGNORECASE)
            if stop_match:
                segment = segment[:stop_match.start()]

        for rarity_match in RARITY_LINE_PATTERN.finditer(segment):
            rarity_word = rarity_match.group(1).lower()
            rarity = RARITY_WORD_MAP.get(rarity_word, "Unknown")
            rest = rarity_match.group(2)

            for item_match in ITEM_TYPE_PATTERN.finditer(rest):
                name = item_match.group(1).strip().strip(",").strip()
                item_type = item_match.group(2).strip()
                if name:
                    items.append({
                        "name": name,
                        "type": item_type,
                        "rarity": rarity,
                        "power": None,
                        "potential_power": None,
                    })

    # Lightweight sanity check, visible in Railway's deploy logs, not Discord —
    # helps catch a format drift without needing !rawdump every time.
    accounts_expected = re.search(r"(\d+)\s*accounts?", header_segment, re.IGNORECASE)
    drops_expected = re.search(r"(\d+)\s*drops?", header_segment, re.IGNORECASE)
    if accounts_expected and int(accounts_expected.group(1)) != len(player_names):
        print(f"[parse_harvard_message] WARNING: expected {accounts_expected.group(1)} accounts, "
              f"parsed {len(player_names)}")
    if drops_expected and int(drops_expected.group(1)) != len(items):
        print(f"[parse_harvard_message] WARNING: expected {drops_expected.group(1)} drops, "
              f"parsed {len(items)}")

    return {
        "outcome": outcome,
        "clear_seconds": clear_seconds,
        "player_names": ",".join(player_names) if player_names else None,
        "items": items,
        "is_rodin": is_rodin,
    }

def parse_ethos_message(raw: str):
    """
    Returns {"outcome": "win"/"loss", "clear_seconds": int|None,
    "player_names": "name", "items": [...], "is_rodin": bool}
    or None if this message isn't a recognized Ethos Suite run result.

    Unlike Harvard Labs, each Ethos Suite message covers only ONE player —
    a multi-person Rodin fight arrives as several separate messages that
    on_message() merges back together via find_recent_ethos_rodin_run().
    """
    if not raw:
        return None

    # "VICTORY" and "RODIN DEFEATED" both mean the player won (Rodin was
    # defeated BY the player) — only "ATTEMPT FAILED" is a loss. Only the
    # RODIN-titled messages represent a shared party battle that can be
    # split across several messages; plain VICTORY runs are solo and never merged.
    if re.search(r"\bVICTORY\b", raw, re.IGNORECASE):
        outcome = "win"
        is_rodin = False
    elif re.search(r"RODIN DEFEATED", raw, re.IGNORECASE):
        outcome = "win"
        is_rodin = True
    elif re.search(r"ATTEMPT FAILED", raw, re.IGNORECASE):
        outcome = "loss"
        is_rodin = True
    else:
        return None  # not a run-result message

    # Clear Time (win) or Fight Time (loss)
    time_match = re.search(r"(?:Clear Time|Fight Time)\s+`?([0-9:hms\s]+?)`?(?:\s|$|•)", raw)
    clear_seconds = parse_clear_time(time_match.group(1)) if time_match else None

    # Player name appears as a Discord spoiler tag, e.g. ||idontlikehaqer||
    player_match = re.search(r"\|\|(.+?)\|\|", raw)
    player_name = player_match.group(1).strip() if player_match else None

    items = []
    if "No drops captured" not in raw:
        for line in raw.splitlines():
            unicode_match = UNICODE_EMOJI_PATTERN.search(line)
            custom_match = None if unicode_match else CUSTOM_EMOJI_PATTERN.search(line)

            # Item drop lines always start with a rarity emoji. Any line
            # without one (Session/Lv/Gold stat lines, etc.) is not a drop —
            # skip it, even if it happens to contain parentheses.
            if not unicode_match and not custom_match:
                continue

            rarity = UNICODE_RARITY_MAP.get(unicode_match.group(0), "Unknown") if unicode_match else "Unknown"

            clean_line = UNICODE_EMOJI_PATTERN.sub("", line)
            clean_line = CUSTOM_EMOJI_PATTERN.sub("", clean_line)
            clean_line = MD_STRIP_PATTERN.sub("", clean_line).strip()

            # Power/potential numbers are optional — abilities often have none.
            item_match = re.match(
                r"^(?P<name>.+?)\s*\((?P<type>[^)]+)\)\s*"
                r"(?:(?P<power>[\d,.]+[kmbKMB]?)\s*\((?P<potential>[\d,.]+[kmbKMB]?)\))?$",
                clean_line,
            )
            if item_match:
                power_str = item_match.group("power")
                potential_str = item_match.group("potential")
                items.append({
                    "name": item_match.group("name").strip(),
                    "type": item_match.group("type").strip(),
                    "rarity": rarity,
                    "power": parse_number(power_str) if power_str else None,
                    "potential_power": parse_number(potential_str) if potential_str else None,
                })

    return {
        "outcome": outcome,
        "clear_seconds": clear_seconds,
        "player_names": player_name,
        "items": items,
        "is_rodin": is_rodin,
    }

def parse_any_message(message: discord.Message):
    """
    Dispatches to exactly ONE of the two parsers, never both — so a single
    message can never be double-counted. Harvard Labs messages always
    contain an "(@handle)" account tag that Ethos Suite messages never do,
    which makes the two formats mutually exclusive to detect.

    Returns the parsed dict (with an added "source" key, "harvard" or
    "ethos") or None if the message isn't a recognized run result from
    either bot.
    """
    raw = get_full_text(message)
    if not raw:
        return None

    clean_for_check = MD_STRIP_PATTERN.sub("", raw)
    if ACCOUNT_HEADER_PATTERN.search(clean_for_check):
        parsed = parse_harvard_message(raw)
        if parsed:
            parsed["source"] = "harvard"
        return parsed

    parsed = parse_ethos_message(raw)
    if parsed:
        parsed["source"] = "ethos"
    return parsed

# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------

@bot.event
async def on_ready():
    await asyncio.to_thread(init_db)
    await bot.tree.sync()
    print(f"Logged in as {bot.user}")

@bot.event
async def on_message(message: discord.Message):
    # Recognize run-result messages by content, not by a specific bot/webhook
    # ID — an ID-based check broke twice before (the ID can silently change
    # on reinstall/reauthorization). parse_any_message() already requires
    # specific wording per source, so this stays specific without needing an
    # ID at all. Skip the bot's own messages so it can never parse its own
    # !combinedstats output.
    if message.author.id != bot.user.id:
        parsed = parse_any_message(message)
        if parsed:
            # DB calls run in a worker thread, not on the bot's event loop,
            # so a slow write can only delay itself, never freeze Discord.
            if parsed["source"] == "ethos" and parsed["is_rodin"]:
                # Ethos Suite posts one message per player for a shared Rodin
                # fight — merge messages that land within the same short
                # window instead of logging each as its own run.
                existing = await asyncio.to_thread(find_recent_ethos_rodin_run, parsed["outcome"])
                if existing:
                    existing_run_id, existing_player_names = existing
                    await asyncio.to_thread(
                        merge_into_run, existing_run_id, existing_player_names,
                        parsed["player_names"], parsed["items"],
                    )
                else:
                    await asyncio.to_thread(
                        log_run, parsed["outcome"], parsed["clear_seconds"], parsed["player_names"],
                        parsed["items"], 1, "ethos",
                    )
            else:
                # Harvard Labs messages are always already complete, and
                # non-Rodin Ethos VICTORY messages are solo — neither ever merges.
                await asyncio.to_thread(
                    log_run, parsed["outcome"], parsed["clear_seconds"], parsed["player_names"],
                    parsed["items"], int(parsed["is_rodin"]), parsed["source"],
                )

    await bot.process_commands(message)

# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

@bot.hybrid_command(name="rawdump", description="Debug: show the raw text of a recent run-result message")
async def raw_dump(ctx: commands.Context):
    """Debug helper: prints the raw content of the message being replied to
    (or the most recent recognized run-result message) so the parser can be
    checked/tightened against the real text."""
    target = None
    # Replying-to-a-message only makes sense for prefix invocation; slash
    # commands have no message to reply to, so just grab the latest one.
    message_ref = getattr(ctx, "message", None)
    if message_ref and message_ref.reference:
        target = await ctx.channel.fetch_message(message_ref.reference.message_id)
    else:
        async for msg in ctx.channel.history(limit=25):
            if msg.author.id == bot.user.id:
                continue
            if parse_any_message(msg):
                target = msg
                break

    if not target:
        await ctx.send("Couldn't find a VICTORY/RODIN message in the last 25 messages here.")
        return

    raw = get_full_text(target)
    print("----- RAW MESSAGE DUMP -----")
    print(raw)
    print("-----------------------------")
    # Send in chunks so long dumps don't exceed Discord's message limit.
    for i in range(0, len(raw), 1900):
        await ctx.send(f"```\n{raw[i:i+1900]}\n```")

def _fetch_recent_runs(count: int):
    conn = _connect()
    rows = conn.execute(
        """SELECT id, outcome, clear_seconds, player_name, is_rodin, source, created_at
           FROM runs ORDER BY id DESC LIMIT ?""",
        (count,),
    ).fetchall()
    conn.close()
    return rows

@bot.hybrid_command(name="listruns", description="Debug: show the most recently logged runs, raw")
async def list_runs(ctx: commands.Context, count: int = 10):
    rows = await asyncio.to_thread(_fetch_recent_runs, count)

    if not rows:
        await ctx.send("No runs logged yet.")
        return

    lines = ["id  outcome  seconds  rodin  source   created_at            player_name"]
    for r in rows:
        run_id, outcome, seconds, player_name, is_rodin, source, created_at = r
        lines.append(
            f"{run_id:<4}{outcome:<9}{str(seconds):<9}{is_rodin:<7}{(source or ''):<9}{created_at:<22}{player_name}"
        )

    text = "\n".join(lines)
    for i in range(0, len(text), 1900):
        await ctx.send(f"```\n{text[i:i+1900]}\n```")

@bot.hybrid_command(name="resetstats", description="Admin only: permanently wipe all logged runs and drops")
@commands.has_permissions(administrator=True)
async def reset_stats(ctx: commands.Context, confirm: str = None):
    """Wipes all logged runs and item drops. Requires: !resetstats confirm"""
    if confirm != "confirm":
        await ctx.send(
            "⚠️ This will **permanently delete all logged runs and drops**. "
            "Run `!resetstats confirm` if you're sure."
        )
        return

    await asyncio.to_thread(reset_db)
    await ctx.send("✅ All stats have been reset. Logging starts fresh from here.")

@reset_stats.error
async def reset_stats_error(ctx: commands.Context, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("Only server admins can reset stats.")
    else:
        raise error

@bot.hybrid_command(name="combinedstats", aliases=["combined"], description="Show the combined drop-farming stats")
async def combined_stats(ctx: commands.Context):
    runs = await asyncio.to_thread(fetch_runs)
    drops = await asyncio.to_thread(fetch_drops)

    if not runs:
        await ctx.send("No runs logged yet.")
        return

    normal_runs = [r for r in runs if not r[3]]
    rodin_runs = [r for r in runs if r[3]]

    total_runs = len(normal_runs)
    clear_times = [r[1] for r in normal_runs if r[1] is not None]
    avg_seconds = sum(clear_times) / len(clear_times) if clear_times else 0
    avg_m, avg_s = divmod(int(avg_seconds), 60)

    rodin_attempts = len(rodin_runs)
    wins = sum(1 for r in rodin_runs if r[0] == "win")
    losses = sum(1 for r in rodin_runs if r[0] == "loss")
    win_rate = 100 * wins / rodin_attempts if rodin_attempts else 0

    distinct_users = set()
    for r in runs:
        if r[2]:
            for name in r[2].split(","):
                name = name.strip()
                if name:
                    distinct_users.add(name)
    user_count = len(distinct_users)

    total_item_rolls = len(drops)
    non_spell_drops = [d for d in drops if d[1].lower() not in EXCLUDED_TYPES]
    spell_drops = [d for d in drops if d[1].lower() in EXCLUDED_TYPES]
    total_non_spell = len(non_spell_drops)

    # Item breakdown
    item_counts = {}
    for name, _type, _rarity in non_spell_drops:
        item_counts[name] = item_counts.get(name, 0) + 1

    item_lines = []
    for name, count in sorted(item_counts.items(), key=lambda x: -x[1]):
        pct = 100 * count / total_non_spell if total_non_spell else 0
        item_lines.append(f"{name:<24}{count:>6}  {pct:5.2f}%")

    # Valhalla items are identified by name prefix, not a distinct type field.
    # "Epic Valhalla Breakdown" is specifically the EPIC-rarity drops of those
    # items — not every Valhalla item regardless of rarity — so both the name
    # prefix and rarity must match.
    valhalla_epic_drops = [
        d for d in non_spell_drops
        if d[0].lower().startswith("valhalla") and d[2] == "Epic Gear"
    ]
    valhalla_counts = {}
    for name, _type, _rarity in valhalla_epic_drops:
        valhalla_counts[name] = valhalla_counts.get(name, 0) + 1
    valhalla_lines = []
    for name, count in sorted(valhalla_counts.items(), key=lambda x: -x[1]):
        pct = 100 * count / total_non_spell if total_non_spell else 0
        valhalla_lines.append(f"{name:<24}{count:>6}  {pct:5.2f}%")

    # Tier breakdown
    tier_counts = {}
    for _name, _type, rarity in non_spell_drops:
        tier_counts[rarity] = tier_counts.get(rarity, 0) + 1

    tier_lines = []
    for tier in TIER_ORDER:
        if tier not in tier_counts:
            continue
        count = tier_counts[tier]
        pct = 100 * count / total_non_spell if total_non_spell else 0
        tier_lines.append(f"{tier:<12}{count:>6}  {pct:5.2f}%")

    spell_pct = (100 * len(spell_drops) / total_item_rolls) if total_item_rolls else 0

    title_suffix = f" ACROSS {user_count} USERS" if user_count else ""
    footer_suffix = f" across {user_count} users" if user_count else ""

    lines = []
    lines.append(f"**Total Runs:** `{total_runs:,}`")
    lines.append(f"**Average Clear:** `{avg_m}m {avg_s}s`")
    lines.append(f"**Total Item Rolls:** `{total_item_rolls:,}`")
    lines.append(f"**Non-spell Gear Drops:** `{total_non_spell:,}`")
    lines.append("")

    if item_lines:
        lines.append("**Item Breakdown (spells excluded)**")
        lines.append("```\n" + "\n".join(item_lines) + "\n```")

    if valhalla_lines:
        lines.append("**Epic Valhalla Breakdown**")
        lines.append("```\n" + "\n".join(valhalla_lines) + "\n```")

    if tier_lines:
        lines.append("**Tier Breakdown**")
        lines.append("```\n" + "\n".join(tier_lines) + "\n```")

    lines.append("**Excluded Spells**")
    lines.append(f"`{len(spell_drops):,}` rolls (`{spell_pct:.2f}%`)")
    lines.append("")

    if rodin_attempts:
        lines.append("**Rodin Record**")
        lines.append(f"Wins: `{wins}` • Failed: `{losses}` • Attempts: `{rodin_attempts}`")
        lines.append(f"Win Rate: `{win_rate:.2f}%`")

    embed = discord.Embed(
        title=f"Dungeon Quest Farm Statistics — COMBINED RESULTS{title_suffix}",
        description="\n".join(lines),
        color=discord.Color.dark_theme(),
    )
    embed.set_footer(text=f"Combined stats{footer_suffix} • Type !combined or !combinedstats")

    await ctx.send(embed=embed)

# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        raise SystemExit("Set the DISCORD_BOT_TOKEN environment variable first.")
    bot.run(token)
