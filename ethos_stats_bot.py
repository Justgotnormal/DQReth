"""
Ethos Suite Drop-Stats Bot
===========================

Parses "VICTORY" and "RODIN ATTEMPT FAILED" embeds posted by the Ethos Suite
app bot, logs every run + item drop to SQLite, and serves a !combinedstats
command that reproduces the aggregate breakdown (item counts, tier %, spell
exclusion, Rodin win/loss record, average clear time).

RARITY MAPPING — PLEASE CONFIRM
---------------------------------
The colored circle before each item name turned out to be a plain unicode
emoji (🟣, 🔵, etc), not a custom Discord emoji. UNICODE_RARITY_MAP below
uses the standard game convention (white=Common, green=Uncommon, blue=Rare,
purple=Epic, orange=Legendary, red=Ultimate) — this is an ASSUMPTION based
on only two confirmed data points (🟣 on gear, 🔵 on a helmet). If your
in-game tier order is different, just edit the mapping — no need to touch
anything else in the file.

Any color not in the map falls back to "Unknown" and still gets logged and
counted, it just won't sort into a named tier until you add it.

Setup
-----
pip install discord.py
Set DISCORD_BOT_TOKEN as an environment variable.
Enable "Message Content Intent" for your bot in the Developer Portal.

Run
---
python ethos_stats_bot.py
"""

import os
import re
import sqlite3

import discord
from discord.ext import commands

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ETHOS_SUITE_BOT_ID = 1541811908141777018
DB_PATH = "ethos_drops.db"

# See "RARITY MAPPING — PLEASE CONFIRM" note above. Edit freely.
UNICODE_RARITY_MAP = {
    "⚪": "Common",
    "🟢": "Uncommon",
    "🔵": "Rare",
    "🟣": "Epic Gear",
    "🟠": "Legendary",
    "🔴": "Ultimate",
}

# Item/ability types that should be excluded from the gear breakdown,
# same as "spells" in the original stats format.
EXCLUDED_TYPES = {"spell", "ability"}

TIER_ORDER = ["Common", "Uncommon", "Rare", "Epic Gear", "Legendary", "Ultimate", "Unknown"]

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            outcome TEXT,               -- 'win' or 'loss'
            clear_seconds INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
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
    conn.commit()
    conn.close()

def log_run(outcome: str, clear_seconds, items) -> int:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(
        "INSERT INTO runs (outcome, clear_seconds) VALUES (?, ?)",
        (outcome, clear_seconds),
    )
    run_id = cur.lastrowid
    for item in items:
        conn.execute(
            """INSERT INTO drops (run_id, item_name, item_type, rarity, power, potential_power)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (run_id, item["name"], item["type"], item["rarity"],
             item["power"], item["potential_power"]),
        )
    conn.commit()
    conn.close()
    return run_id

def fetch_runs():
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT outcome, clear_seconds FROM runs").fetchall()
    conn.close()
    return rows

def fetch_drops():
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT item_name, item_type, rarity FROM drops").fetchall()
    conn.close()
    return rows

# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

CUSTOM_EMOJI_PATTERN = re.compile(r"<a?:(\w+):(\d+)>")
UNICODE_EMOJI_PATTERN = re.compile("|".join(re.escape(e) for e in UNICODE_RARITY_MAP))
MD_STRIP_PATTERN = re.compile(r"[*_`]")

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

def parse_clear_time(text: str):
    """'04:04' -> 244 seconds. '2h 31m 58s' -> total seconds. Returns None if unparseable."""
    text = text.strip()
    if ":" in text:
        parts = [int(p) for p in text.split(":")]
        seconds = 0
        for p in parts:
            seconds = seconds * 60 + p
        return seconds
    total = 0
    for value, unit in re.findall(r"(\d+)\s*([hms])", text):
        value = int(value)
        total += {"h": 3600, "m": 60, "s": 1}[unit] * value
    return total or None

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

def parse_ethos_message(message: discord.Message):
    """
    Returns {"outcome": "win"/"loss", "clear_seconds": int|None, "items": [...]}
    or None if this message isn't a recognized Ethos Suite run result.
    """
    raw = get_full_text(message)
    if not raw:
        return None

    if re.search(r"\bVICTORY\b", raw, re.IGNORECASE):
        outcome = "win"
    elif re.search(r"\bFAILED\b", raw, re.IGNORECASE) or re.search(r"\bDEFEATED\b", raw, re.IGNORECASE):
        outcome = "loss"
    else:
        return None  # not a run-result message

    # Clear Time (win) or Fight Time (loss)
    time_match = re.search(r"(?:Clear Time|Fight Time)\s+`?([0-9:hms\s]+?)`?(?:\s|$|•)", raw)
    clear_seconds = parse_clear_time(time_match.group(1)) if time_match else None

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

    return {"outcome": outcome, "clear_seconds": clear_seconds, "items": items}

# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------

@bot.event
async def on_ready():
    init_db()
    print(f"Logged in as {bot.user}")

@bot.event
async def on_message(message: discord.Message):
    if message.author.id == ETHOS_SUITE_BOT_ID:
        parsed = parse_ethos_message(message)
        if parsed:
            log_run(parsed["outcome"], parsed["clear_seconds"], parsed["items"])

    await bot.process_commands(message)

# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

@bot.command(name="rawdump")
async def raw_dump(ctx: commands.Context):
    """Debug helper: prints the raw content of the message being replied to
    (or the most recent Ethos Suite message) so you can read real emoji codes."""
    target = None
    if ctx.message.reference:
        target = await ctx.channel.fetch_message(ctx.message.reference.message_id)
    else:
        async for msg in ctx.channel.history(limit=25):
            if msg.author.id == ETHOS_SUITE_BOT_ID:
                target = msg
                break

    if not target:
        await ctx.send("Couldn't find an Ethos Suite message to dump.")
        return

    raw = get_full_text(target)
    print("----- RAW MESSAGE DUMP -----")
    print(raw)
    print("-----------------------------")
    # Send in chunks so long dumps don't exceed Discord's message limit.
    for i in range(0, len(raw), 1900):
        await ctx.send(f"```\n{raw[i:i+1900]}\n```")

@bot.command(name="combinedstats", aliases=["combined"])
async def combined_stats(ctx: commands.Context):
    runs = fetch_runs()
    drops = fetch_drops()

    if not runs:
        await ctx.send("No runs logged yet.")
        return

    total_runs = len(runs)
    wins = sum(1 for r in runs if r[0] == "win")
    losses = sum(1 for r in runs if r[0] == "loss")
    win_rate = 100 * wins / total_runs if total_runs else 0

    clear_times = [r[1] for r in runs if r[1] is not None]
    avg_seconds = sum(clear_times) / len(clear_times) if clear_times else 0
    avg_m, avg_s = divmod(int(avg_seconds), 60)

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
        item_lines.append(f"{name:<24}{count:>7}  {pct:5.2f}%")

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
        tier_lines.append(f"{tier:<12}{count:>7}  {pct:5.2f}%")

    embed = discord.Embed(
        title="Dungeon Quest Farm Statistics — COMBINED RESULTS",
        color=discord.Color.dark_theme(),
    )
    embed.add_field(name="Total Runs", value=str(total_runs), inline=True)
    embed.add_field(name="Average Clear", value=f"{avg_m}m {avg_s}s", inline=True)
    embed.add_field(name="Total Item Rolls", value=str(total_item_rolls), inline=True)
    embed.add_field(name="Non-spell Gear Drops", value=str(total_non_spell), inline=True)

    if item_lines:
        embed.add_field(
            name="Item Breakdown (spells excluded)",
            value="```\n" + "\n".join(item_lines) + "\n```",
            inline=False,
        )
    if tier_lines:
        embed.add_field(
            name="Tier Breakdown",
            value="```\n" + "\n".join(tier_lines) + "\n```",
            inline=False,
        )

    spell_pct = (100 * len(spell_drops) / total_item_rolls) if total_item_rolls else 0
    embed.add_field(
        name="Excluded Spells",
        value=f"{len(spell_drops)} rolls ({spell_pct:.2f}%)",
        inline=False,
    )
    embed.add_field(
        name="Rodin Record",
        value=f"Wins: {wins} • Failed: {losses} • Attempts: {total_runs}\n"
              f"Win Rate: {win_rate:.2f}%",
        inline=False,
    )

    await ctx.send(embed=embed)

# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        raise SystemExit("Set the DISCORD_BOT_TOKEN environment variable first.")
    bot.run(token)
