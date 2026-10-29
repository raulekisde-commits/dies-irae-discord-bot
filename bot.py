# ================== BOT ==================
import discord
from discord.ext import commands, tasks
from discord import app_commands
import os
import time
import re
import io
import aiohttp
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field
from typing import List, Optional
from dotenv import load_dotenv
import asyncio
import random
import sqlite3
import json
from decimal import Decimal
from economy import setup_economy
from bets import setup_bets
from guildshop import setup_guildshop
from games import setup_games
from rankings import setup_rankings
try:
    from utcclock import setup_utcclock
except Exception:
    import traceback as _tb
    _imp_err = _tb.format_exc()
    async def setup_utcclock(*_a, _imp_err=_imp_err):
        raise RuntimeError('No se pudo importar utcclock.py (¿falta subirlo al repo?):\n' + _imp_err)
try:
    from splits import setup_splits
except Exception:
    import traceback as _tb
    _imp_err = _tb.format_exc()
    async def setup_splits(*_a, _imp_err=_imp_err):
        raise RuntimeError('No se pudo importar splits.py (¿falta subirlo al repo?):\n' + _imp_err)
try:
    from deposits import setup_deposits
except Exception:
    import traceback as _tb
    _imp_err = _tb.format_exc()
    async def setup_deposits(*_a, _imp_err=_imp_err):
        raise RuntimeError('No se pudo importar deposits.py (¿falta subirlo al repo?):\n' + _imp_err)
try:
    from casino import setup_casino
except Exception:
    import traceback as _tb
    _imp_err = _tb.format_exc()
    async def setup_casino(*_a, _imp_err=_imp_err):
        raise RuntimeError('No se pudo importar casino.py (¿falta subirlo al repo?):\n' + _imp_err)
try:
    from duels import setup_duels
except Exception:
    import traceback as _tb
    _imp_err = _tb.format_exc()
    async def setup_duels(*_a, _imp_err=_imp_err):
        raise RuntimeError('No se pudo importar duels.py (¿falta subirlo al repo?):\n' + _imp_err)
try:
    from panels import setup_panels
except Exception:
    import traceback as _tb
    _imp_err = _tb.format_exc()
    async def setup_panels(*_a, _imp_err=_imp_err):
        raise RuntimeError('No se pudo importar panels.py (¿falta subirlo al repo?):\n' + _imp_err)

# ================== TOKEN (env o /root/discordbot/.env) ==================
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    env_path = "token.env"
    if os.path.exists(env_path):
        load_dotenv(dotenv_path=env_path)
        TOKEN = os.getenv("DISCORD_TOKEN")

if not TOKEN:
    raise RuntimeError("Falta DISCORD_TOKEN en variables de entorno o en /root/discordbot/.env")

# ================== BASE DE DATOS PERSISTENTE ==================
# Por defecto queda junto a bot.py. Se puede cambiar con BOT_DB_PATH.
DB_PATH = os.getenv("BOT_DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_data.sqlite3"))


def db_execute(query: str, params: tuple = ()):
    """Ejecuta una escritura SQLite en una conexión corta y segura."""
    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(query, params)
        conn.commit()


def db_fetchall(query: str, params: tuple = ()):
    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(query, params).fetchall()


def db_ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str):
    """Agrega una columna solo si falta (migración segura, conserva los datos)."""
    cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if cols and column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def init_database():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS giveaways (
                message_id INTEGER PRIMARY KEY,
                prize TEXT NOT NULL,
                end_at TEXT NOT NULL,
                channel_id INTEGER NOT NULL,
                creator_id INTEGER NOT NULL,
                guild_id INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS giveaway_entrants (
                message_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                PRIMARY KEY (message_id, user_id),
                FOREIGN KEY (message_id) REFERENCES giveaways(message_id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS giveaway_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id INTEGER NOT NULL,
                guild_id INTEGER,
                channel_id INTEGER,
                prize TEXT NOT NULL,
                winners_requested INTEGER NOT NULL,
                entrants_count INTEGER NOT NULL,
                valid_count INTEGER NOT NULL,
                winners_json TEXT NOT NULL,
                outcome TEXT NOT NULL,
                finished_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS timers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                material TEXT NOT NULL,
                tier TEXT NOT NULL,
                map_name TEXT NOT NULL,
                end_at TEXT NOT NULL,
                created_by_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                warned_30 INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS timer_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                category TEXT NOT NULL,
                material TEXT NOT NULL,
                tier TEXT,
                mapa TEXT NOT NULL,
                spawn_at TEXT NOT NULL,
                created_by INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS timer_panels (
                guild_id INTEGER NOT NULL,
                category TEXT NOT NULL,
                channel_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                PRIMARY KEY (guild_id, category)
            );
            CREATE TABLE IF NOT EXISTS venta_panels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                name_key TEXT NOT NULL,
                total INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                message_id INTEGER,
                created_by INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE (guild_id, name_key)
            );
            CREATE TABLE IF NOT EXISTS venta_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                panel_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                amount INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (panel_id) REFERENCES venta_panels(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS ticket_images (
                user_id INTEGER PRIMARY KEY,
                url TEXT NOT NULL,
                filename TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS split_log_ids (
                panel_message_id INTEGER PRIMARY KEY,
                log_message_id INTEGER NOT NULL
            );
        """)
        # Migraciones aditivas: nunca borran ni recrean datos existentes.
        db_ensure_column(conn, "giveaways", "winners_count", "INTEGER NOT NULL DEFAULT 1")
        db_ensure_column(conn, "timer_entries", "fixed", "INTEGER NOT NULL DEFAULT 0")
        db_ensure_column(conn, "venta_panels", "stock_added", "INTEGER NOT NULL DEFAULT 0")
        conn.commit()


init_database()

# ================== CONFIG ==================
GUILD_ID = 1257878770841288724
CATEGORY_ID = 1257902293609742346
LOG_CHANNEL_ID = 1462207061902496037

RECRUITER_ROLE_ID = 1257896905099444354
CONTENT_CREATOR_ROLE_ID = 1258562816512884808     # puede usar /cta (iniciar CTAs)
FLEX_ROLE_ID = 1558522576743964762                # puede anotarse en cualquier posición de una CTA, sin importar los roles pedidos
MIEMBRO_ROLE_ID = 1257896455860129822
TANK_ROLE_ID = 1260755129754189854
HEALER_ROLE_ID = 1260755151296266331
SUPP_ROLE_ID = 1260755342472646656
DPS_ROLE_ID = 1260755289062248458
BATTLE_MOUNT_ROLE_ID = 1469369363739181087

PUBLIC_ROLE_ID = 1266805315547041902
STAFF_ROLE_ID = 1257896709246423083

COOLDOWN_SECONDS = 60

# ✅ Timers
TIMERS_ROLE_ID = 1462515835326169159
TIMERS_ROLE_NAME_FALLBACK = "timers"

TIMER_ALERT_CHANNEL_ID = 1462184630835740732
TIMER_ALERT_MINUTES_BEFORE = 60

TIMERS_ALERT_ROLE_ID = 1258562816512884808

TIMERS_BOARD_CHANNEL_ID = 1462516135361777875
TIMERS_BOARD_TITLE = "⏱ Timers Activos"
timers_board_message_id: Optional[int] = None

MATERIALS_NORMAL = {"fibra", "cuero", "mineral", "madera"}
MATERIALS_SPECIAL = {"vortex", "core"}
MATERIALS_NO_TIER = {"dragon"}   # materiales que se publican sin tier

# Iconos centralizados por material (se usan en el anuncio de /timeradd)
MATERIAL_ICONS = {
    "fibra": "🌿",
    "cuero": "🐄",
    "mineral": "🪨",
    "madera": "🪵",
    "vortex": "🌀",
    "core": "💎",
    "dragon": "🐉",
}
DEFAULT_MATERIAL_ICON = "📦"

TIERS_NORMAL = {"4.4", "5.4", "6.4", "7.4", "8.4"}
TIERS_SPECIAL = {"common", "rare", "epic", "legendary"}

TIER_LABELS_SPECIAL = {
    "common": "Common (Verde)",
    "rare": "Rare (Azul)",
    "epic": "Epic (Violeta)",
    "legendary": "Legendary (Amarillo)",
}

ALBIONBB_REGION = os.getenv("ALBIONBB_REGION", "eu")

ALBIONBB_BASE_URL = f"https://api.albionbb.com/{ALBIONBB_REGION}"
# Plantel de la guild en Albion (API oficial del juego). El ID es el mismo que aparece en la URL de AlbionBB.
ALBION_GUILD_NAME = os.getenv("ALBION_GUILD_NAME", "Smogg")
ALBION_GUILD_ID = os.getenv("ALBION_GUILD_ID", "FkCFeONGTeqETPtZhJEubg")
_ALBION_GAMEINFO_BY_REGION = {
    "eu": "https://gameinfo-ams.albiononline.com",
    "west": "https://gameinfo.albiononline.com", "us": "https://gameinfo.albiononline.com",
    "east": "https://gameinfo-sgp.albiononline.com", "asia": "https://gameinfo-sgp.albiononline.com",
}
# Smogg figura en el servidor de Américas (Europa y Asia devuelven la guild vacía/inexistente).
ALBION_GAMEINFO_BASE = os.getenv("ALBION_GAMEINFO_BASE", "https://gameinfo.albiononline.com")
ALBION_TIMEOUT_SECONDS = 45
ALBION_RETRIES = 2
ALBION_GAMEINFO_ALL = ["https://gameinfo.albiononline.com", "https://gameinfo-ams.albiononline.com",
                       "https://gameinfo-sgp.albiononline.com"]
ALBION_ROSTER_CACHE_SECONDS = 120
_albion_roster_cache: dict = {"at": 0.0, "names": set()}
ALBIONBB_BATTLE_URL = f"{ALBIONBB_BASE_URL}/battles"
ALBIONBB_KILLS_URL = f"{ALBIONBB_BASE_URL}/battles/kills"
# ================== FOCO DONOR TICKETS (NUEVO) ==================
FOCO_CATEGORY_ID = 1468340293571973273        
FOCO_NOTIFY_USER_ID = 350688193903067140       # a quien se etiqueta cuando se abre un ticket de Foco Donor (en vez del Staff)
FOCO_LOG_CHANNEL_ID = 1468345144502915313      

FOCO_TOPIC_PREFIX = "FOCO_DONOR"

ticket_images = {}         
active_applications = {}
cooldowns = {}

# NUEVO: foco tickets
active_foco_tickets = {}   # user_id -> channel_id (ayuda rápida, pero además validamos por topic)

intents = discord.Intents.default()
intents.members = True
intents.message_content = True
intents.guilds = True

GUILD_OBJ = discord.Object(id=GUILD_ID)

async def _report_startup_errors(bot):
    """Cuando el bot está listo, te manda por DM (al dueño de la app) los errores de arranque."""
    try:
        await bot.wait_until_ready()
        if not STARTUP_ERRORS:
            return
        owner = (await bot.application_info()).owner
        txt = "⚠️ **Errores al arrancar el bot:**\n" + "\n".join(f"**{n}**\n```{e[-900:]}```" for n, e in STARTUP_ERRORS)
        for i in range(0, len(txt), 1900):
            await owner.send(txt[i:i + 1900])
    except Exception as e:
        print("No pude enviar el reporte de errores:", e)



STARTUP_ERRORS = []


async def _safe_setup(name, fn, *args):
    """Corre un setup_*; si falla, NO frena a los demás: guarda el error en startup_errors.log y en STARTUP_ERRORS."""
    import traceback as _tb
    try:
        await fn(*args)
        print(f"✅ setup {name} OK")
    except Exception:
        err = _tb.format_exc()
        STARTUP_ERRORS.append((name, err))
        print(f"❌ setup {name} FALLÓ:\n{err}")
        try:
            with open("startup_errors.log", "a", encoding="utf-8") as _f:
                _f.write(f"[{datetime.now(timezone.utc).isoformat()}] setup {name}\n{err}\n")
        except Exception:
            pass



class MyBot(commands.Bot):
    async def setup_hook(self):
        self.http_session = aiohttp.ClientSession()
        # Wuju Coins: se carga ANTES del sync para que sus comandos slash se registren
        try:
            await _safe_setup("economy", setup_economy, self, DB_PATH, GUILD_ID, MIEMBRO_ROLE_ID, STAFF_ROLE_ID)
            await _safe_setup("bets", setup_bets, self, DB_PATH, GUILD_ID)
            await _safe_setup("guildshop", setup_guildshop, self, DB_PATH, GUILD_ID)
            await _safe_setup("games", setup_games, self, DB_PATH, GUILD_ID)
            await _safe_setup("rankings", setup_rankings, self, DB_PATH, GUILD_ID)
            await _safe_setup("deposits", setup_deposits, self, DB_PATH, GUILD_ID)
            await _safe_setup("casino", setup_casino, self, DB_PATH, GUILD_ID)
            await _safe_setup("duels", setup_duels, self, DB_PATH, GUILD_ID)
            await _safe_setup("panels", setup_panels, self, GUILD_ID)
            await _safe_setup("splits", setup_splits, self, DB_PATH, GUILD_ID)
            await _safe_setup("utcclock", setup_utcclock, self, DB_PATH, GUILD_ID)
            self.loop.create_task(_report_startup_errors(self))
            print("✅ Wuju Coins y apuestas cargados")
        except Exception as e:
            print("❌ Error cargando Wuju Coins:", e)
        try:
            await self.tree.sync(guild=GUILD_OBJ)
            print("✅ Slash commands sincronizados (guild) [setup_hook]")
        except Exception as e:
            print("❌ Error sync slash commands [setup_hook]:", e)

    async def close(self):
        if hasattr(self, "http_session"):
            await self.http_session.close()
        await super().close()
        
bot = MyBot(command_prefix="!", intents=intents)

# ================== TIMERS DATA ==================
@dataclass
class TimerItem:
    material: str
    tier: str
    map_name: str
    end_at: datetime
    created_by_id: int
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    warned_30: bool = False
    db_id: Optional[int] = None

timers: List[TimerItem] = []

# ================== SORTEOS ==================
GIVEAWAY_MAX_WINNERS = 25
# Qué hacer si hay menos participantes válidos que ganadores pedidos:
#   False -> no se sortea nada y se avisa (no se inventan ni se completan ganadores).
#   True  -> se sortea entre todos los participantes válidos y se aclara que fueron menos.
GIVEAWAY_ALLOW_PARTIAL = False


@dataclass
class GiveawayItem:
    prize: str
    end_at: datetime
    channel_id: int
    message_id: int
    creator_id: int
    entrants: set[int] = field(default_factory=set)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    winners_count: int = 1
    finished: bool = False

giveaways: dict[int, GiveawayItem] = {}
giveaways_panel_tasks: dict = {}      # message_id -> tarea que actualiza el contador del panel
giveaways_panel_dirty: set = set()
GIVEAWAY_PANEL_REFRESH_SECONDS = 5  # message_id -> GiveawayItem


_GIVEAWAY_UPSERT_SQL = """INSERT INTO giveaways(message_id, prize, end_at, channel_id, creator_id, guild_id, winners_count)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(message_id) DO UPDATE SET prize=excluded.prize, end_at=excluded.end_at,
             channel_id=excluded.channel_id, creator_id=excluded.creator_id, guild_id=excluded.guild_id,
             winners_count=excluded.winners_count"""


def _giveaway_row_values(g: GiveawayItem, guild_id: int):
    return (g.message_id, g.prize, g.end_at.astimezone(timezone.utc).isoformat(),
            g.channel_id, g.creator_id, guild_id, int(g.winners_count))


def save_giveaway(g: GiveawayItem, guild_id: int = GUILD_ID):
    db_execute(_GIVEAWAY_UPSERT_SQL, _giveaway_row_values(g, guild_id))


def save_giveaway_entrant(message_id: int, user_id: int, g: Optional[GiveawayItem] = None,
                          guild_id: int = GUILD_ID):
    """Guarda un participante.

    Si se pasa `g`, la fila del sorteo se asegura en la misma transacción antes de insertar al
    participante. Así nunca falla por falta de la fila padre (FOREIGN KEY) cuando el sorteo
    existe en memoria, y la base se autorrepara."""
    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        if g is not None:
            conn.execute(_GIVEAWAY_UPSERT_SQL, _giveaway_row_values(g, guild_id))
        conn.execute("INSERT OR IGNORE INTO giveaway_entrants(message_id, user_id) VALUES (?, ?)",
                     (message_id, user_id))
        conn.commit()


def remove_giveaway(message_id: int):
    giveaways.pop(message_id, None)
    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("DELETE FROM giveaway_entrants WHERE message_id = ?", (message_id,))
        conn.execute("DELETE FROM giveaways WHERE message_id = ?", (message_id,))
        conn.commit()


def record_giveaway_result(g: GiveawayItem, guild_id: int, valid_count: int,
                           winners: list[int], outcome: str):
    """Guarda el resultado para poder verificarlo después (quién ganó y entre cuántos)."""
    db_execute(
        """INSERT INTO giveaway_history(message_id, guild_id, channel_id, prize, winners_requested,
                                         entrants_count, valid_count, winners_json, outcome, finished_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (g.message_id, guild_id, g.channel_id, g.prize, int(g.winners_count), len(g.entrants),
         int(valid_count), json.dumps([str(w) for w in winners]), outcome,
         datetime.now(timezone.utc).isoformat()),
    )


def pick_giveaway_winners(eligible, count: int, rng=None) -> list[int]:
    """Elige `count` ganadores distintos, exclusivamente entre `eligible`.

    Devuelve [] si no hay suficientes participantes (nunca completa con usuarios inventados)."""
    unique = list(dict.fromkeys(int(u) for u in eligible))
    if count < 1 or len(unique) < count:
        return []
    rng = rng or random.SystemRandom()
    return rng.sample(unique, count)


def save_timer(t: TimerItem):
    values = (t.material, t.tier, t.map_name, t.end_at.astimezone(timezone.utc).isoformat(),
              t.created_by_id, t.created_at.astimezone(timezone.utc).isoformat(), int(t.warned_30))
    if t.db_id is None:
        with sqlite3.connect(DB_PATH, timeout=30) as conn:
            cur = conn.execute(
                "INSERT INTO timers(material, tier, map_name, end_at, created_by_id, created_at, warned_30) VALUES (?, ?, ?, ?, ?, ?, ?)",
                values,
            )
            t.db_id = cur.lastrowid
            conn.commit()
    else:
        db_execute(
            "UPDATE timers SET material=?, tier=?, map_name=?, end_at=?, created_by_id=?, created_at=?, warned_30=? WHERE id=?",
            values + (t.db_id,),
        )


def delete_timer(t: TimerItem):
    if t.db_id is not None:
        db_execute("DELETE FROM timers WHERE id = ?", (t.db_id,))


def save_ticket_image(user_id: int, url: str, filename: str):
    db_execute(
        "INSERT INTO ticket_images(user_id, url, filename) VALUES (?, ?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET url=excluded.url, filename=excluded.filename",
        (user_id, url, filename),
    )
    ticket_images[user_id] = {"url": url, "filename": filename}


def delete_ticket_image(user_id: int):
    ticket_images.pop(user_id, None)
    db_execute("DELETE FROM ticket_images WHERE user_id = ?", (user_id,))


def save_split_log_mapping(panel_message_id: int, log_message_id: int):
    split_log_ids[panel_message_id] = log_message_id
    db_execute(
        "INSERT INTO split_log_ids(panel_message_id, log_message_id) VALUES (?, ?) "
        "ON CONFLICT(panel_message_id) DO UPDATE SET log_message_id=excluded.log_message_id",
        (panel_message_id, log_message_id),
    )


def restore_persistent_state(guild: discord.Guild):
    """Restaura sorteos, participantes, timers y referencias locales desde SQLite."""
    giveaways.clear()
    for row in db_fetchall("SELECT * FROM giveaways"):
        try:
            end_at = datetime.fromisoformat(row["end_at"])
            if end_at.tzinfo is None:
                end_at = end_at.replace(tzinfo=timezone.utc)
            g = GiveawayItem(
                prize=row["prize"], end_at=end_at.astimezone(timezone.utc),
                channel_id=int(row["channel_id"]), message_id=int(row["message_id"]),
                creator_id=int(row["creator_id"]),
                winners_count=max(1, int(row["winners_count"] or 1)),
            )
            g.entrants = {int(r["user_id"]) for r in db_fetchall(
                "SELECT user_id FROM giveaway_entrants WHERE message_id = ?", (g.message_id,)
            )}
            giveaways[g.message_id] = g
        except Exception as exc:
            print(f"⚠️ No pude restaurar el sorteo {row['message_id']}: {exc}")

    timers.clear()
    for row in db_fetchall("SELECT * FROM timers ORDER BY end_at"):
        try:
            end_at = datetime.fromisoformat(row["end_at"])
            created_at = datetime.fromisoformat(row["created_at"])
            if end_at.tzinfo is None:
                end_at = end_at.replace(tzinfo=timezone.utc)
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            timers.append(TimerItem(
                material=row["material"], tier=row["tier"], map_name=row["map_name"],
                end_at=end_at.astimezone(timezone.utc), created_by_id=int(row["created_by_id"]),
                created_at=created_at.astimezone(timezone.utc), warned_30=bool(row["warned_30"]),
                db_id=int(row["id"]),
            ))
        except Exception as exc:
            print(f"⚠️ No pude restaurar un timer guardado: {exc}")

    ticket_images.clear()
    for row in db_fetchall("SELECT user_id, url, filename FROM ticket_images"):
        ticket_images[int(row["user_id"])] = {"url": row["url"], "filename": row["filename"]}

    split_log_ids.clear()
    for row in db_fetchall("SELECT panel_message_id, log_message_id FROM split_log_ids"):
        split_log_ids[int(row["panel_message_id"])] = int(row["log_message_id"])

    # Los canales con tickets abiertos ya existen en Discord: reconstruir los índices en memoria.
    active_applications.clear()
    active_foco_tickets.clear()
    for channel in guild.text_channels:
        topic = channel.topic or ""
        if topic.startswith(f"{RECRUIT_TOPIC_PREFIX}|"):
            uid = parse_recruit_topic(topic).get("uid")
            if uid:
                active_applications[int(uid)] = channel.id
        elif topic.startswith(f"{FOCO_TOPIC_PREFIX}|"):
            uid = parse_foco_topic(topic).get("uid")
            if uid:
                active_foco_tickets[int(uid)] = channel.id


def start_giveaway_task(guild_id: int, g: GiveawayItem):
    tasks_by_id = getattr(bot, "_giveaway_tasks", None)
    if tasks_by_id is None:
        tasks_by_id = {}
        bot._giveaway_tasks = tasks_by_id
    current = tasks_by_id.get(g.message_id)
    if current is not None and not current.done():
        return
    tasks_by_id[g.message_id] = asyncio.create_task(run_giveaway_flow(guild_id, g))

def is_staff_member(member: discord.Member) -> bool:
    staff_role = member.guild.get_role(STAFF_ROLE_ID)
    return (staff_role is not None) and (staff_role in member.roles)

def build_giveaway_embed(g: GiveawayItem, winners: Optional[list[int]] = None,
                         finished: bool = False, note: Optional[str] = None) -> discord.Embed:
    end_unix = int(g.end_at.timestamp())
    emb = discord.Embed(
        title="🕳️ Sorteo del topo" if not finished else "🕳️ Sorteo finalizado",
        description="Tocá el botón para anotarte." if not finished else "Este sorteo ya terminó.",
        color=discord.Color.gold() if not finished else discord.Color.dark_grey(),
    )
    emb.add_field(name="🎁 Premio", value=f"`{g.prize}`", inline=False)
    emb.add_field(name="🏆 Ganadores", value=str(g.winners_count), inline=True)
    if finished:
        emb.add_field(name="👥 Anotados", value=str(len(g.entrants)), inline=True)
        if winners:
            emb.add_field(name="🎉 Ganadores del sorteo",
                          value="\n".join(f"{i}. <@{uid}>" for i, uid in enumerate(winners, start=1)),
                          inline=False)
        elif note:
            emb.add_field(name="Resultado", value=note, inline=False)
    else:
        emb.add_field(name="⏳ Termina", value=f"<t:{end_unix}:t> (<t:{end_unix}:R>)", inline=False)
        emb.add_field(name="👥 Anotados", value=str(len(g.entrants)), inline=True)
    emb.set_footer(text="Smogg Sorteos System")
    return emb

async def _giveaway_channel(guild: discord.Guild, g: GiveawayItem):
    """Devuelve el canal del sorteo (canal de texto, hilo, etc.) o None si no se puede acceder."""
    ch = guild.get_channel(g.channel_id) or guild.get_thread(g.channel_id)
    if ch is None:
        try:
            ch = await guild.fetch_channel(g.channel_id)
        except Exception:
            return None
    if not hasattr(ch, "send") or not hasattr(ch, "fetch_message"):
        return None
    return ch

async def _giveaway_panel_worker(g: GiveawayItem):
    while True:
        await asyncio.sleep(GIVEAWAY_PANEL_REFRESH_SECONDS)
        giveaways_panel_dirty.discard(g.message_id)
        if g.finished or g.message_id not in giveaways:
            return
        try:
            ch = bot.get_channel(g.channel_id)
            if ch is not None and hasattr(ch, "get_partial_message"):
                await ch.get_partial_message(g.message_id).edit(embed=build_giveaway_embed(g))
        except Exception as exc:
            print(f"Sorteo {g.message_id}: no pude actualizar el contador: {exc}")
        if g.message_id not in giveaways_panel_dirty:
            return


def giveaway_request_panel_refresh(g: GiveawayItem):
    """Actualiza el número de anotados del panel. Agrupa muchos clics en una sola edición cada pocos
    segundos para no pasarse del límite de ediciones de Discord."""
    task = giveaways_panel_tasks.get(g.message_id)
    if task is not None and not task.done():
        giveaways_panel_dirty.add(g.message_id)
        return
    giveaways_panel_tasks[g.message_id] = asyncio.create_task(_giveaway_panel_worker(g))


async def finish_giveaway_message(ch, g: GiveawayItem, winners: Optional[list[int]],
                                  note: Optional[str] = None):
    """Deshabilita el botón y deja visible el resultado en el mensaje original del sorteo."""
    try:
        msg = await ch.fetch_message(g.message_id)
    except Exception:
        return
    view = GiveawayJoinView()
    for child in view.children:
        child.disabled = True
    try:
        await msg.edit(embed=build_giveaway_embed(g, winners=winners, finished=True, note=note), view=view)
    except Exception:
        pass

async def resolve_valid_entrants(guild: discord.Guild, entrants) -> list[int]:
    """Participantes válidos: siguen en el servidor y no son bots."""
    valid = []
    for uid in entrants:
        member = guild.get_member(uid)
        if member is None:
            try:
                member = await guild.fetch_member(uid)
            except Exception:
                member = None
        if member is not None and not member.bot:
            valid.append(uid)
    return valid

async def run_giveaway_flow(guild_id: int, g: GiveawayItem):
    # Esperar hasta el final
    now = datetime.now(timezone.utc)
    delay = (g.end_at - now).total_seconds()
    if delay > 0:
        await asyncio.sleep(delay)

    guild = bot.get_guild(guild_id)
    if not guild:
        # No borrar el registro persistente por un fallo temporal de caché/conexión.
        return

    g.finished = True      # frena las actualizaciones del contador para no pisar el resultado final
    ch = await _giveaway_channel(guild, g)
    if ch is None:
        # Se conserva en SQLite para reintentar en la próxima reconexión.
        g.finished = False
        return

    async with g.lock:
        entrants = list(g.entrants)

    valid = await resolve_valid_entrants(guild, entrants)
    wanted = max(1, int(g.winners_count))
    no_pings = discord.AllowedMentions.none()

    if not valid:
        note = "No hubo participantes válidos."
        record_giveaway_result(g, guild_id, 0, [], "no_entrants")
        await finish_giveaway_message(ch, g, None, note)
        try:
            await ch.send(f"📭 Sorteo de `{g.prize}` cancelado: no se anotó nadie.", allowed_mentions=no_pings)
        except Exception:
            pass
        remove_giveaway(g.message_id)
        return

    count = wanted
    partial = False
    if len(valid) < wanted:
        if not GIVEAWAY_ALLOW_PARTIAL:
            note = f"Se pidieron {wanted} ganadores pero solo hay {len(valid)} participantes válidos."
            record_giveaway_result(g, guild_id, len(valid), [], "insufficient")
            await finish_giveaway_message(ch, g, None, note)
            try:
                await ch.send(
                    f"⚠️ No se pudo sortear `{g.prize}`: se pidieron **{wanted}** ganadores y solo hay "
                    f"**{len(valid)}** participantes válidos. <@{g.creator_id}>, creá el sorteo de nuevo "
                    f"con menos ganadores si querés repetirlo.",
                    allowed_mentions=discord.AllowedMentions(users=[discord.Object(id=g.creator_id)],
                                                             roles=False, everyone=False),
                )
            except Exception:
                pass
            remove_giveaway(g.message_id)
            return
        count = len(valid)
        partial = True

    winners = pick_giveaway_winners(valid, count)
    outcome = "partial" if partial else "ok"
    record_giveaway_result(g, guild_id, len(valid), winners, outcome)
    await finish_giveaway_message(ch, g, winners)

    lines = [f"🏆 **¡Sorteo finalizado!** Premio: `{g.prize}`", ""]
    lines += [f"{i}. <@{uid}>" for i, uid in enumerate(winners, start=1)]
    lines.append("")
    lines.append(f"Participantes válidos: **{len(valid)}**.")
    if partial:
        lines.append(f"Se pidieron {wanted} ganadores, pero solo había {len(winners)} participantes válidos.")
    text = "\n".join(lines)
    allowed = discord.AllowedMentions(users=[discord.Object(id=u) for u in winners], roles=False, everyone=False)
    sent = False
    for _attempt in range(2):
        try:
            await ch.send(text, allowed_mentions=allowed)
            sent = True
            break
        except Exception:
            await asyncio.sleep(2)
    if not sent:
        print(f"⚠️ Sorteo {g.message_id}: no pude anunciar el resultado; queda guardado en giveaway_history.")
    remove_giveaway(g.message_id)


# ---------- UTILIDADES ----------
async def send_log(guild: discord.Guild, message: str):
    channel = guild.get_channel(LOG_CHANNEL_ID)
    if channel is None:
        try:
            channel = await guild.fetch_channel(LOG_CHANNEL_ID)
        except Exception:
            return
    try:
        await channel.send(message)
    except Exception:
        pass

async def create_transcript(channel: discord.TextChannel):
    messages = []
    async for msg in channel.history(limit=200, oldest_first=True):
        content = msg.content if msg.content else ""
        messages.append(f"[{msg.author}] {content}")
    return "\n".join(messages)

def staff_only():
    async def predicate(ctx: commands.Context):
        if ctx.guild is None:
            return False
        staff_role = ctx.guild.get_role(STAFF_ROLE_ID)
        if staff_role is None:
            await ctx.reply("❌ No encontré el rol de Staff. Avisale a un administrador.")
            return False
        if staff_role not in ctx.author.roles:
            await ctx.reply("❌ Solo los miembros con el rol **Staff** pueden usar este comando.")
            return False
        return True
    return commands.check(predicate)

def _get_timers_role(guild: discord.Guild) -> Optional[discord.Role]:
    if TIMERS_ROLE_ID and TIMERS_ROLE_ID != 0:
        return guild.get_role(TIMERS_ROLE_ID)
    return discord.utils.get(guild.roles, name=TIMERS_ROLE_NAME_FALLBACK)

def parse_duration_hhmm(s: str) -> Optional[tuple[int, int]]:
    if ":" not in s:
        return None
    parts = s.split(":")
    if len(parts) != 2:
        return None
    try:
        h = int(parts[0])
        m = int(parts[1])
    except ValueError:
        return None
    if h < 0 or m < 0 or m > 59:
        return None
    return h, m

def fmt_utc(dt: datetime) -> str:
    now = datetime.now(timezone.utc)
    if dt.date() != now.date():
        return dt.strftime("%H:%M UTC (%d/%m)")
    return dt.strftime("%H:%M UTC")

def time_left_str(end_at: datetime) -> str:
    now = datetime.now(timezone.utc)
    delta = end_at - now
    total_seconds = int(delta.total_seconds())
    if total_seconds <= 0:
        return "0m"
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"

def is_timers_member(member: discord.Member) -> bool:
    role = _get_timers_role(member.guild)
    if role is None:
        return False
    return role in member.roles

# ✅ helper anti-40060
async def respond_ephemeral(interaction: discord.Interaction, content: str):
    try:
        if interaction.response.is_done():
            return await interaction.followup.send(content, ephemeral=True)
        return await interaction.response.send_message(content, ephemeral=True)
    except Exception:
        try:
            return await interaction.followup.send(content, ephemeral=True)
        except Exception:
            return

def safe_channel_name(user_name: str, user_id: int) -> str:
    base = f"postulacion-{user_name}-{user_id}".lower()
    base = re.sub(r"[^a-z0-9\-]", "-", base)
    base = re.sub(r"-{2,}", "-", base).strip("-")
    return base[:90]

def _format_timer_line(t: TimerItem) -> str:
    # Mostrar tier lindo si es special
    tier_txt = TIER_LABELS_SPECIAL.get(t.tier, f"T{t.tier}")
    return f"• 🧱 **{t.material.title()}** | ⭐ **{tier_txt}** | 🗺️ **{t.map_name}** → 🕒 **{fmt_utc(t.end_at)}** (en {time_left_str(t.end_at)})"

async def ensure_timers_board_message(guild: discord.Guild):
    global timers_board_message_id

    ch = guild.get_channel(TIMERS_BOARD_CHANNEL_ID)
    if ch is None:
        try:
            ch = await guild.fetch_channel(TIMERS_BOARD_CHANNEL_ID)
        except Exception:
            return
    if not isinstance(ch, discord.TextChannel):
        return

    # 1) Buscar en pins un mensaje del bot con el título esperado
    try:
        pins = await ch.pins()
        for m in pins:
            if m.author.id == bot.user.id and m.embeds:
                emb = m.embeds[0]
                if emb.title == TIMERS_BOARD_TITLE:
                    timers_board_message_id = m.id
                    return
    except Exception:
        pass

    # 2) Si no existe, crear uno nuevo y pinearlo
    embed = discord.Embed(title=TIMERS_BOARD_TITLE, description="(cargando...)", color=discord.Color.blurple())
    try:
        m = await ch.send(embed=embed)
        try:
            await m.pin(reason="Timers board message")
        except Exception:
            pass
        timers_board_message_id = m.id
    except Exception:
        return

async def update_timers_board(guild: discord.Guild):
    global timers_board_message_id

    ch = guild.get_channel(TIMERS_BOARD_CHANNEL_ID)
    if ch is None:
        try:
            ch = await guild.fetch_channel(TIMERS_BOARD_CHANNEL_ID)
        except Exception:
            return
    if not isinstance(ch, discord.TextChannel):
        return

    if timers_board_message_id is None:
        await ensure_timers_board_message(guild)

    if timers_board_message_id is None:
        return

    # Ordenar por tiempo
    sorted_timers = sorted(timers, key=lambda x: x.end_at)

    if not sorted_timers:
        desc = "📭 No hay timers activos."
    else:
        lines = [_format_timer_line(t) for t in sorted_timers]
        desc = "\n".join(lines)
        if len(desc) > 3900:
            desc = desc[:3900] + "\n…"

    embed = discord.Embed(
        title=TIMERS_BOARD_TITLE,
        description=desc,
        color=discord.Color.blurple(),
        timestamp=datetime.now(timezone.utc)
    )

    try:
        msg = await ch.fetch_message(timers_board_message_id)
        await msg.edit(embed=embed)
    except Exception:
        # si se borró el mensaje, recrear
        timers_board_message_id = None
        await ensure_timers_board_message(guild)
        if timers_board_message_id:
            try:
                msg = await ch.fetch_message(timers_board_message_id)
                await msg.edit(embed=embed)
            except Exception:
                pass

RECRUIT_TOPIC_PREFIX = "RECRUIT"
def make_recruit_topic(user_id: int) -> str:
    return f"{RECRUIT_TOPIC_PREFIX}|uid={user_id}"
def parse_recruit_topic(topic: Optional[str]) -> dict:
    if not topic or not topic.startswith(f"{RECRUIT_TOPIC_PREFIX}|"):
        return {}
    data = {}
    for p in topic.split("|")[1:]:
        if "=" in p:
            k, v = p.split("=", 1)
            data[k.strip()] = v.strip()
    return data

async def download_as_file(url: str, filename: str) -> Optional[discord.File]:
    try:
        async with bot.http_session.get(url) as resp:
            if resp.status != 200:
                return None
            data = await resp.read()
        return discord.File(fp=io.BytesIO(data), filename=filename)
    except Exception:
        return None
NO_MENTIONS = discord.AllowedMentions.none()
# ================== FOCO DONOR HELPERS (NUEVO) ==================
def _sanitize_topic_value(s: str, max_len: int = 200) -> str:
    s = (s or "").strip()
    s = s.replace("|", "/").replace("\n", " ").replace("\r", " ")
    s = re.sub(r"\s{2,}", " ", s)
    if len(s) > max_len:
        s = s[:max_len - 1] + "…"
    return s

def make_foco_topic(user_id: int, foco: str, item_spec: str) -> str:
    foco_v = _sanitize_topic_value(foco, 60)
    item_v = _sanitize_topic_value(item_spec, 300)
    return f"{FOCO_TOPIC_PREFIX}|uid={user_id}|foco={foco_v}|item={item_v}"

def parse_foco_topic(topic: Optional[str]) -> dict:
    data = {}
    if not topic:
        return data
    if not topic.startswith(f"{FOCO_TOPIC_PREFIX}|"):
        return data
    parts = topic.split("|")
    for p in parts[1:]:
        if "=" in p:
            k, v = p.split("=", 1)
            data[k.strip()] = v.strip()
    return data

def safe_foco_channel_name(display_name: str) -> str:
    # pedido: "Apodo - Foco Donor" (en canal, sin espacios -> guiones)
    base = f"{display_name}-foco-donor".lower()
    base = re.sub(r"[^a-z0-9\-]", "-", base)
    base = re.sub(r"-{2,}", "-", base).strip("-")
    return base[:90]

def find_open_foco_channel(guild: discord.Guild, user_id: int) -> Optional[discord.TextChannel]:
    # Busca por topic (sirve incluso si el bot reinicia)
    uid_str = str(user_id)
    for ch in guild.text_channels:
        if not ch.topic:
            continue
        if ch.topic.startswith(f"{FOCO_TOPIC_PREFIX}|uid={uid_str}|"):
            return ch
        # formato normal: FOCO_DONOR|uid=...|...
        if ch.topic.startswith(f"{FOCO_TOPIC_PREFIX}|"):
            info = parse_foco_topic(ch.topic)
            if info.get("uid") == uid_str:
                return ch
    return None

async def send_foco_log(guild: discord.Guild, message: str):
    target_id = FOCO_LOG_CHANNEL_ID if FOCO_LOG_CHANNEL_ID and FOCO_LOG_CHANNEL_ID != 0 else LOG_CHANNEL_ID
    channel = guild.get_channel(target_id)
    if channel is None:
        try:
            channel = await guild.fetch_channel(target_id)
        except Exception:
            return
    try:
        await channel.send(message)
    except Exception:
        pass

def get_foco_category(guild: discord.Guild) -> Optional[discord.CategoryChannel]:
    foco_cat_id = FOCO_CATEGORY_ID if FOCO_CATEGORY_ID and FOCO_CATEGORY_ID != 0 else CATEGORY_ID
    return discord.utils.get(guild.categories, id=foco_cat_id)


# ---------- TIMERS HOUSEKEEPING ----------
@tasks.loop(seconds=30)
async def timers_housekeeping():
    now = datetime.now(timezone.utc)

    expired = [t for t in timers if now >= t.end_at]
    for t in expired:
        try:
            timers.remove(t)
            delete_timer(t)
        except ValueError:
            pass

    if not TIMER_ALERT_CHANNEL_ID or TIMER_ALERT_CHANNEL_ID == 0:
        return

    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return

    channel = guild.get_channel(TIMER_ALERT_CHANNEL_ID)
    if channel is None:
        try:
            channel = await guild.fetch_channel(TIMER_ALERT_CHANNEL_ID)
        except Exception:
            return

    if expired:
        await update_timers_board(guild)

    for t in timers:
        if t.warned_30:
            continue
        seconds_left = (t.end_at - now).total_seconds()
        if 0 < seconds_left <= (TIMER_ALERT_MINUTES_BEFORE * 60):
            t.warned_30 = True
            save_timer(t)
            
            tier_txt = TIER_LABELS_SPECIAL.get(t.tier, f"T{t.tier}")

            alert_role_mention = f"<@&{TIMERS_ALERT_ROLE_ID}>"
            msg = (
                f"{alert_role_mention}\n"
                f"**Las inteadas de daitza no se pagan solas, armen la party**\n"
                f"⏰ **Faltan {TIMER_ALERT_MINUTES_BEFORE} min**\n"
                f"🧱 **{t.material.title()}** | ⭐ **{tier_txt}**\n"
                f"🗺️ **{t.map_name}**\n"
                f"🕒 Sale a **{fmt_utc(t.end_at)}**"
            )

            try:
                await channel.send(msg)
            except Exception:
                pass

@timers_housekeeping.before_loop
async def before_timers_housekeeping():
    await bot.wait_until_ready()

# ================== TIMER POSTS (/timeradd) ==================
TIMER_POST_CHANNEL_ID = 1462184630835740732
TIMER_POST_DELETE_AFTER_MINUTES = 15
ARGENTINA_TZ = timezone(timedelta(hours=-3))   # UTC-3 fijo (Argentina no usa horario de verano)

# Lee el timestamp de "Falta" del propio mensaje (formato nuevo con ** y formato anterior sin **)
_TIMER_POST_TS_RE = re.compile(r"Falta:\**\s*<t:(\d+):R>")
_CLOCK_HHMM_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*$")


def parse_clock_hhmm(s: str) -> Optional[tuple[int, int]]:
    """'16:10' / '6:05' -> (16, 10) / (6, 5). Hora de reloj válida (0-23 : 0-59) o None."""
    match = _CLOCK_HHMM_RE.match(s or "")
    if not match:
        return None
    h, m = int(match.group(1)), int(match.group(2))
    if h > 23 or m > 59:
        return None
    return h, m


def next_utc_occurrence(h: int, m: int, now: Optional[datetime] = None) -> datetime:
    """Próxima vez (UTC) que el reloj marque h:m. Si ya pasó hoy, es mañana (cruza medianoche sin problemas)."""
    now = now or datetime.now(timezone.utc)
    target = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target


# ---- Paneles de timers: 3 mensajes fijos (Nodos · Vortex y Cores · Dragones) que se editan solos ----
TIMER_CATEGORY_ORDER = ("nodos", "vortex", "dragones")
TIMER_CATEGORY_TITLES = {"nodos": "🌿 Nodos", "vortex": "🌀 Vortex y Cores", "dragones": "🐉 Dragones"}
TIMER_MATERIAL_NAMES = {"fibra": "Fibra", "cuero": "Cuero", "mineral": "Mineral", "madera": "Madera",
                        "vortex": "Vortex", "core": "Core", "dragon": "Dragón"}
TIMER_KEEP_MINUTES = 20          # un timer se borra del panel 20 minutos después de la hora en que salió
TIMER_PANEL_MAX_LINES = 30
TIMER_MAPA_MAX = 60
_timer_panel_lock = asyncio.Lock()


def timer_category(material: str) -> str:
    if material in MATERIALS_NO_TIER:
        return "dragones"
    return "vortex" if material in MATERIALS_SPECIAL else "nodos"


def timer_entry_line(entry: dict) -> str:
    """'🌿 7.4 Fibra - Wetgrave Marsh - en 2 horas 18:12 UTC - @quien'.
    El «en 2 horas» (o «hace 2 horas») es el timestamp de Discord y se actualiza solo en pantalla;
    la hora fija va siempre al lado."""
    spawn = _timer_parse_dt(entry["spawn_at"])
    material = entry["material"]
    tier = entry.get("tier")
    tier_text = TIER_LABELS_SPECIAL.get(tier, tier) if tier else ""
    name = TIMER_MATERIAL_NAMES.get(material, str(material).title())
    label = " ".join(x for x in (tier_text, name) if x)
    icon = MATERIAL_ICONS.get(material, DEFAULT_MATERIAL_ICON)
    when = f"<t:{int(spawn.timestamp())}:R> {spawn.strftime('%H:%M')} UTC"
    who = f" - <@{entry['created_by']}>" if entry.get("created_by") else ""
    return f"{icon} {label} - {entry['mapa']} - {when}{who}"


def _timer_parse_dt(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def timer_build_embed(category: str, entries: list) -> discord.Embed:
    ordered = sorted(entries, key=lambda e: (e["spawn_at"], e["id"]))[-TIMER_PANEL_MAX_LINES:]
    lines = [timer_entry_line(e) for e in ordered]
    while len("\n".join(lines)) > 3900 and len(lines) > 1:
        lines.pop(0)                       # se descartan primero los más viejos
    embed = discord.Embed(title=TIMER_CATEGORY_TITLES[category],
                          description="\n".join(lines) if lines else "📭 No hay timers por ahora.",
                          color=discord.Color.blurple())
    embed.set_footer(text=f"Ordenado por hora de aparición · horario UTC · se borran {TIMER_KEEP_MINUTES} min después de salir")
    return embed


def timer_add_entry(guild_id: int, material: str, tier: Optional[str], mapa: str, spawn_at: datetime, user_id: int,
                    fixed: bool = False) -> int:
    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        cur = conn.execute(
            "INSERT INTO timer_entries(guild_id,category,material,tier,mapa,spawn_at,created_by,created_at,fixed) VALUES (?,?,?,?,?,?,?,?,?)",
            (guild_id, timer_category(material), material, tier, mapa, spawn_at.astimezone(timezone.utc).isoformat(),
             user_id, datetime.now(timezone.utc).isoformat(), 1 if fixed else 0))
        conn.commit()
        return int(cur.lastrowid)


def timer_list_entries(guild_id: int, category: str) -> list:
    return [dict(r) for r in db_fetchall(
        "SELECT * FROM timer_entries WHERE guild_id=? AND category=? ORDER BY spawn_at, id", (guild_id, category))]


def timer_purge_expired(guild_id: int, now: Optional[datetime] = None) -> set:
    """Borra los timers que salieron hace más de TIMER_KEEP_MINUTES. Devuelve las categorías afectadas."""
    now = now or datetime.now(timezone.utc)
    limit = (now - timedelta(minutes=TIMER_KEEP_MINUTES)).isoformat()
    rows = db_fetchall("SELECT DISTINCT category FROM timer_entries WHERE guild_id=? AND spawn_at<?", (guild_id, limit))
    if rows:
        db_execute("DELETE FROM timer_entries WHERE guild_id=? AND spawn_at<?", (guild_id, limit))
    return {r["category"] for r in rows}


def timer_find_entries(guild_id: int, material: str, mapa: str, clock: tuple, tier: Optional[str] = None) -> list:
    """Timers que coinciden con material, mapa (sin distinguir mayúsculas), hora UTC (HH, MM) y, si se indica, tier."""
    wanted = (mapa or "").strip().casefold()
    found = []
    for e in db_fetchall("SELECT * FROM timer_entries WHERE guild_id=? AND material=? ORDER BY spawn_at, id", (guild_id, material)):
        e = dict(e)
        spawn = _timer_parse_dt(e["spawn_at"])
        if e["mapa"].strip().casefold() == wanted and (spawn.hour, spawn.minute) == clock and (tier is None or e.get("tier") == tier):
            found.append(e)
    return found


async def timer_panel_refresh(guild: discord.Guild, only: Optional[set] = None):
    """Actualiza (o crea, si no existen) los paneles de timers. Los mensajes se editan, nunca se duplican."""
    async with _timer_panel_lock:
        channel = guild.get_channel(TIMER_POST_CHANNEL_ID)
        if channel is None:
            try:
                channel = await guild.fetch_channel(TIMER_POST_CHANNEL_ID)
            except Exception:
                channel = None
        if channel is None or not hasattr(channel, "send"):
            raise RuntimeError("No encontré el canal de timers.")
        for category in TIMER_CATEGORY_ORDER:
            if only is not None and category not in only:
                continue
            embed = timer_build_embed(category, timer_list_entries(guild.id, category))
            row = db_fetchall("SELECT message_id FROM timer_panels WHERE guild_id=? AND category=?", (guild.id, category))
            if row:
                try:
                    await channel.get_partial_message(int(row[0]["message_id"])).edit(embed=embed, view=None)   # view=None: saca el botón de las versiones anteriores
                    continue
                except discord.NotFound:
                    pass                      # lo borraron: se vuelve a crear abajo
            sent = await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
            db_execute("INSERT INTO timer_panels(guild_id,category,channel_id,message_id) VALUES (?,?,?,?) "
                       "ON CONFLICT(guild_id,category) DO UPDATE SET channel_id=excluded.channel_id, message_id=excluded.message_id",
                       (guild.id, category, channel.id, sent.id))


class TimerDeleteView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🗑️ Eliminar", style=discord.ButtonStyle.danger, custom_id="timer_post_delete")
    async def delete_post(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return await respond_ephemeral(interaction, "❌ Solo disponible en el servidor.")

        if not (is_timers_member(interaction.user) or is_staff_member(interaction.user)):
            return await respond_ephemeral(interaction, "❌ No tenés permiso para eliminar este timer.")

        try:
            await interaction.message.delete()
        except discord.NotFound:
            pass
        except Exception:
            return await respond_ephemeral(interaction, "❌ No pude eliminar el mensaje.")

        await respond_ephemeral(interaction, "🗑️ Timer eliminado.")


@tasks.loop(seconds=60)
async def timer_posts_cleanup():
    """Borra los posts de /timeradd 15 min después de la hora indicada.
    Lee la hora del propio mensaje, así funciona aunque el bot se reinicie."""
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return

    channel = guild.get_channel(TIMER_POST_CHANNEL_ID)
    if channel is None:
        try:
            channel = await guild.fetch_channel(TIMER_POST_CHANNEL_ID)
        except Exception:
            return
    if not isinstance(channel, discord.TextChannel):
        return

    # Paneles nuevos: se quitan los timers vencidos y se actualiza solo lo que cambió.
    try:
        changed = timer_purge_expired(guild.id)
        if changed:
            await timer_panel_refresh(guild, changed)
    except Exception as exc:
        print(f"timers: error en la limpieza de paneles: {exc}")

    # Mensajes sueltos del sistema anterior (uno por timer): se siguen borrando 15 min después de la hora.
    limit_unix = int(datetime.now(timezone.utc).timestamp()) - TIMER_POST_DELETE_AFTER_MINUTES * 60

    try:
        async for msg in channel.history(limit=100):
            if bot.user is None or msg.author.id != bot.user.id:
                continue
            match = _TIMER_POST_TS_RE.search(msg.content or "")
            if not match:
                continue
            if int(match.group(1)) <= limit_unix:
                try:
                    await msg.delete()
                except Exception:
                    pass
    except Exception:
        pass

@timer_posts_cleanup.before_loop
async def before_timer_posts_cleanup():
    await bot.wait_until_ready()


# ================== SLASH COMMANDS TIMERS ==================
@bot.tree.command(name="timeradd", description="Agregar timer (solo rol Timers)", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(
    material="Material",
    mapa="Nombre del mapa",
    tiempo="Horario UTC en que sale (HH:MM) ej 16:10",
    tier="Tier (obligatorio salvo para Dragon)",
)
@app_commands.choices(
    material=[
        app_commands.Choice(name="Fibra", value="fibra"),
        app_commands.Choice(name="Cuero", value="cuero"),
        app_commands.Choice(name="Mineral", value="mineral"),
        app_commands.Choice(name="Madera", value="madera"),
        app_commands.Choice(name="Vortex", value="vortex"),
        app_commands.Choice(name="Core", value="core"),
        app_commands.Choice(name="Dragon", value="dragon"),
    ],
    tier=[
        app_commands.Choice(name="4.4", value="4.4"),
        app_commands.Choice(name="5.4", value="5.4"),
        app_commands.Choice(name="6.4", value="6.4"),
        app_commands.Choice(name="7.4", value="7.4"),
        app_commands.Choice(name="8.4", value="8.4"),
        app_commands.Choice(name="Common (Verde)", value="common"),
        app_commands.Choice(name="Rare (Azul)", value="rare"),
        app_commands.Choice(name="Epic (Violeta)", value="epic"),
        app_commands.Choice(name="Legendary (Amarillo)", value="legendary"),
    ],
)
async def timeradd_slash(
    interaction: discord.Interaction,
    material: app_commands.Choice[str],
    mapa: str,
    tiempo: str,
    tier: Optional[app_commands.Choice[str]] = None,
):
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        return await respond_ephemeral(interaction, "❌ Solo disponible en el servidor.")

    if not is_timers_member(interaction.user):
        return await respond_ephemeral(interaction, "❌ Solo el rol **timers** puede usar este comando.")

    mapa = mapa.strip()
    if not mapa:
        return await respond_ephemeral(interaction, "❌ El nombre del mapa no puede estar vacío.")

    mat = material.value
    tr: Optional[str] = tier.value if tier is not None else None

    # Validación material/tier (Dragon no lleva tier; si lo mandan se ignora)
    if mat in MATERIALS_NO_TIER:
        tr = None
    elif tr is None:
        return await respond_ephemeral(
            interaction,
            "❌ Falta el **tier**. Solo **Dragon** se puede publicar sin tier."
        )
    elif mat in MATERIALS_SPECIAL:
        if tr not in TIERS_SPECIAL:
            return await respond_ephemeral(
                interaction,
                "❌ Para **Vortex/Core** el tier tiene que ser: Common/Rare/Epic/Legendary."
            )
    else:
        if tr not in TIERS_NORMAL:
            return await respond_ephemeral(
                interaction,
                "❌ Para **Fibra/Cuero/Mineral/Madera** el tier tiene que ser: 4.4/5.4/6.4/7.4/8.4."
            )

    clock = parse_clock_hhmm(tiempo)
    if clock is None:
        return await respond_ephemeral(
            interaction,
            "❌ Tiempo inválido. Usá el horario UTC en formato `HH:MM` (00:00 a 23:59), ej: `16:10`."
        )

    target_utc = next_utc_occurrence(*clock)

    if len(mapa) > TIMER_MAPA_MAX:
        return await respond_ephemeral(interaction, f"❌ El nombre del mapa es muy largo (máximo {TIMER_MAPA_MAX} caracteres).")

    await interaction.response.defer(ephemeral=True)

    timer_add_entry(interaction.guild.id, mat, tr, mapa, target_utc, interaction.user.id)
    category = timer_category(mat)
    try:
        await timer_panel_refresh(interaction.guild, {category})      # edita el panel de esa categoría (lo crea si falta)
    except discord.Forbidden:
        return await interaction.followup.send("⚠️ Guardé el timer, pero no tengo permisos para escribir en el canal de timers.", ephemeral=True)
    except Exception as exc:
        print(f"timers: no pude actualizar los paneles: {exc}")
        return await interaction.followup.send("⚠️ Guardé el timer, pero no pude actualizar los paneles. Revisá el canal de timers.", ephemeral=True)

    await interaction.followup.send(f"✅ Timer agregado al panel **{TIMER_CATEGORY_TITLES[category]}**.", ephemeral=True)

@bot.tree.command(name="deltimer", description="Eliminar un timer cargado con /timeradd (completá sus mismos datos)", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(
    material="Material del timer",
    mapa="Nombre del mapa (el mismo que pusiste)",
    tiempo="Horario UTC del timer (HH:MM) ej 16:10",
    tier="Tier (opcional; ayuda si hay dos timers iguales con distinto tier)",
)
@app_commands.choices(
    material=[
        app_commands.Choice(name="Fibra", value="fibra"),
        app_commands.Choice(name="Cuero", value="cuero"),
        app_commands.Choice(name="Mineral", value="mineral"),
        app_commands.Choice(name="Madera", value="madera"),
        app_commands.Choice(name="Vortex", value="vortex"),
        app_commands.Choice(name="Core", value="core"),
        app_commands.Choice(name="Dragon", value="dragon"),
    ],
    tier=[
        app_commands.Choice(name="4.4", value="4.4"),
        app_commands.Choice(name="5.4", value="5.4"),
        app_commands.Choice(name="6.4", value="6.4"),
        app_commands.Choice(name="7.4", value="7.4"),
        app_commands.Choice(name="8.4", value="8.4"),
        app_commands.Choice(name="Common (Verde)", value="common"),
        app_commands.Choice(name="Rare (Azul)", value="rare"),
        app_commands.Choice(name="Epic (Violeta)", value="epic"),
        app_commands.Choice(name="Legendary (Amarillo)", value="legendary"),
    ],
)
async def deltimer_slash(
    interaction: discord.Interaction,
    material: app_commands.Choice[str],
    mapa: str,
    tiempo: str,
    tier: Optional[app_commands.Choice[str]] = None,
):
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        return await respond_ephemeral(interaction, "❌ Solo disponible en el servidor.")
    if not (is_timers_member(interaction.user) or is_staff_member(interaction.user)):
        return await respond_ephemeral(interaction, "❌ Solo el rol **timers** o el Staff pueden usar este comando.")
    clock = parse_clock_hhmm(tiempo)
    if clock is None:
        return await respond_ephemeral(interaction, "❌ Tiempo inválido. Usá el horario UTC en formato `HH:MM` (00:00 a 23:59), ej: `16:10`.")

    mat = material.value
    tr = None if mat in MATERIALS_NO_TIER else (tier.value if tier is not None else None)
    matches = timer_find_entries(interaction.guild.id, mat, mapa, clock, tr)
    if not matches:
        return await respond_ephemeral(
            interaction,
            "❌ No encontré un timer con esos datos. Tienen que coincidir el material, el mapa y la hora UTC "
            "tal como aparecen en el panel.")
    db_execute(f"DELETE FROM timer_entries WHERE id IN ({','.join('?' * len(matches))})", tuple(e["id"] for e in matches))
    category = timer_category(mat)
    await interaction.response.defer(ephemeral=True)
    try:
        await timer_panel_refresh(interaction.guild, {category})
    except Exception as exc:
        print(f"timers: no pude actualizar el panel: {exc}")
        return await interaction.followup.send("⚠️ Eliminé el timer, pero no pude actualizar el panel.", ephemeral=True)
    plural = "" if len(matches) == 1 else f" ({len(matches)} timers iguales)"
    await interaction.followup.send(f"🗑️ Timer eliminado del panel **{TIMER_CATEGORY_TITLES[category]}**{plural}.", ephemeral=True)


@deltimer_slash.autocomplete("mapa")
async def deltimer_mapa_autocomplete(interaction: discord.Interaction, current: str):
    if interaction.guild is None:
        return []
    rows = db_fetchall("SELECT DISTINCT mapa FROM timer_entries WHERE guild_id=? ORDER BY mapa", (interaction.guild.id,))
    cur = (current or "").casefold()
    return [app_commands.Choice(name=r["mapa"][:100], value=r["mapa"][:100]) for r in rows if cur in r["mapa"].casefold()][:25]


@bot.tree.command(name="sorteo", description="Crear sorteo (solo Staff)", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(
    premio="Qué se sortea (texto libre)",
    tiempo="Duración en H:M (ej: 0:30, 2:00, 6:10)",
    ganadores=f"Cantidad de ganadores (1 a {GIVEAWAY_MAX_WINNERS}). Por defecto 1",
)
async def sorteo_slash(interaction: discord.Interaction, premio: str, tiempo: str,
                       ganadores: app_commands.Range[int, 1, GIVEAWAY_MAX_WINNERS] = 1):
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        return await respond_ephemeral(interaction, "❌ Este comando solo funciona dentro del servidor.")

    if not is_staff_member(interaction.user):
        return await respond_ephemeral(interaction, "❌ Solo el **Staff** puede crear sorteos.")

    if not premio or not premio.strip():
        return await respond_ephemeral(interaction, "❌ El premio no puede estar vacío.")

    dur = parse_duration_hhmm(tiempo.strip())
    if dur is None:
        return await respond_ephemeral(
            interaction,
            "❌ Tiempo inválido.\nUsá `H:M` (ej: `0:30`, `2:00`, `6:10`)."
        )

    h, m = dur
    if h == 0 and m == 0:
        return await respond_ephemeral(interaction, "❌ La duración tiene que ser mayor a `0:00`.")
    end_at = datetime.now(timezone.utc) + timedelta(hours=h, minutes=m)

    await interaction.response.defer(ephemeral=True)

    temp_g = GiveawayItem(
        prize=premio.strip(),
        end_at=end_at,
        channel_id=interaction.channel.id,
        message_id=0,
        creator_id=interaction.user.id,
        winners_count=int(ganadores),
    )

    try:
        msg = await interaction.channel.send(embed=build_giveaway_embed(temp_g), view=GiveawayJoinView())
    except Exception:
        return await interaction.followup.send(
            "❌ No pude publicar el sorteo. Revisá que el bot pueda enviar mensajes en este canal.",
            ephemeral=True)

    temp_g.message_id = msg.id
    # Primero se guarda en la base y recién después en memoria: si el guardado falla, no queda
    # un sorteo publicado que no existe en SQLite (causa del error FOREIGN KEY al anotarse).
    try:
        save_giveaway(temp_g, interaction.guild.id)
    except Exception as exc:
        print(f"❌ No pude guardar el sorteo {msg.id}: {exc}")
        try:
            await msg.delete()
        except Exception:
            pass
        return await interaction.followup.send(
            "❌ No pude guardar el sorteo en la base de datos, así que lo cancelé. Avisale a un administrador.",
            ephemeral=True)

    giveaways[msg.id] = temp_g

    # Arrancar flujo en background (se vuelve a iniciar desde SQLite tras un reinicio)
    start_giveaway_task(interaction.guild.id, temp_g)

    await interaction.followup.send(
        f"✅ Sorteo creado. Premio: `{temp_g.prize}` · Ganadores: {temp_g.winners_count} · "
        f"Termina: <t:{int(end_at.timestamp())}:R>",
        ephemeral=True
    )

def staff_only_slash():
    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return False
        return is_staff_member(interaction.user)
    return app_commands.check(predicate)


@bot.tree.command(
    name="list_role",
    description="(Staff) Lista los miembros (apodo del server) que tienen un rol",
    guild=discord.Object(id=GUILD_ID)
)
@staff_only_slash()
@app_commands.describe(rol="Rol del que querés listar miembros")
async def list_role_slash(interaction: discord.Interaction, rol: discord.Role):
    if interaction.guild is None:
        return await respond_ephemeral(interaction, "❌ Solo disponible en el servidor.")

    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild

    try:
        await guild.chunk(cache=True)
    except Exception:
        pass

    members = [m for m in guild.members if rol in m.roles]

    if not members:
        return await interaction.followup.send(f"📭 No hay nadie con el rol **{rol.name}**.", ephemeral=True)

    lines = [m.nick or m.display_name or m.name for m in members]
    lines.sort(key=lambda s: s.lower())

    text = "\n".join(lines)
    header = f"Rol: {rol.name} ({len(lines)} miembros)\n" + ("-" * 32) + "\n"
    full = header + text

    if len(full) > 1800:
        file = discord.File(fp=io.BytesIO(full.encode("utf-8")), filename=f"list_role_{rol.name}.txt")
        return await interaction.followup.send(
            content=f"📄 Lista para **{rol.name}** ({len(lines)}):",
            file=file,
            ephemeral=True
        )

    await interaction.followup.send(f"```{full}```", ephemeral=True)

@list_role_slash.error
async def list_role_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        return await respond_ephemeral(interaction, "❌ Solo **Staff** puede usar `/list_role`.")
    return await respond_ephemeral(interaction, "❌ Error ejecutando el comando.")

@bot.tree.command(
    name="delrole",
    description="(Staff) Elimina un rol (sin tagear a nadie)",
    guild=discord.Object(id=GUILD_ID)
)
@staff_only_slash()
@app_commands.describe(rol="Rol que querés borrar")
async def delrole_slash(interaction: discord.Interaction, rol: discord.Role):
    if interaction.guild is None:
        return await respond_ephemeral(interaction, "❌ Solo disponible en el servidor.")

    # Bloqueos de seguridad
    if rol.id == STAFF_ROLE_ID or rol.managed:
        return await respond_ephemeral(interaction, "❌ No se puede eliminar ese rol (Staff o rol administrado).")

    # Evitar borrar @everyone
    if interaction.guild.default_role and rol.id == interaction.guild.default_role.id:
        return await respond_ephemeral(interaction, "❌ No se puede eliminar @everyone.")

    # Intentar borrar
    try:
        role_name = rol.name
        await rol.delete(reason=f"Eliminado por {interaction.user} (Staff)")

        # Ephemeral + sin mentions
        if interaction.response.is_done():
            await interaction.followup.send(
                f"🗑️ Rol **{role_name}** eliminado correctamente.",
                ephemeral=True,
                allowed_mentions=NO_MENTIONS
            )
        else:
            await interaction.response.send_message(
                f"🗑️ Rol **{role_name}** eliminado correctamente.",
                ephemeral=True,
                allowed_mentions=NO_MENTIONS
            )

    except discord.Forbidden:
        return await respond_ephemeral(interaction, "❌ No pude eliminar el rol. Me falta el permiso **Gestionar roles** o el rol está por encima del mío.")
    except Exception:
        return await respond_ephemeral(interaction, "❌ Ocurrió un error inesperado al eliminar el rol.")

# ---------- COMANDOS STAFF ----------
@bot.command(name="addroll-list")
@staff_only()
@commands.guild_only()
async def addroll_list(ctx: commands.Context, *, args: str = None):
    if not args:
        return await ctx.reply("Uso: `!addroll-list NombreDelRol @Usuario1 @Usuario2 ...` (o `@Rol @Usuario1 @Usuario2 ...` para usar un rol que ya existe)")

    mentioned_members = ctx.message.mentions
    if not mentioned_members:
        return await ctx.reply("Tenés que mencionar al menos 1 usuario.\nUso: `!addroll-list NombreDelRol @Usuario1 @Usuario2 ...`")

    role_name = args
    for m in mentioned_members:
        role_name = role_name.replace(m.mention, "").strip()
    # Si se etiquetó un rol existente (@Rol), se usa ese rol: no se crea uno nuevo.
    tagged_roles = [r for r in ctx.message.role_mentions if r.id != ctx.guild.id]
    for r in tagged_roles:
        role_name = role_name.replace(r.mention, "").strip()
    role_name = " ".join(role_name.split())

    if len(tagged_roles) > 1:
        return await ctx.reply("Etiquetá un solo rol por comando.")
    role = tagged_roles[0] if tagged_roles else None

    if role is None and not role_name:
        return await ctx.reply("Falta el nombre del rol.\nUso: `!addroll-list NombreDelRol @Usuario1 @Usuario2 ...`")

    if role is None:
        role = discord.utils.get(ctx.guild.roles, name=role_name)

    if role is None:
        try:
            role = await ctx.guild.create_role(name=role_name, reason=f"Rol creado automáticamente por {ctx.author} (Staff)")
        except discord.Forbidden:
            return await ctx.reply("❌ No pude crear el rol. Me falta el permiso **Gestionar roles** o el rol está por encima del mío.")
        except Exception:
            return await ctx.reply("❌ Ocurrió un error inesperado al crear el rol.")

    ok, fail = 0, 0
    for member in mentioned_members:
        try:
            await member.add_roles(role, reason=f"Asignado por {ctx.author} (Staff)")
            ok += 1
        except Exception:
            fail += 1

    await ctx.reply(f"✅ Rol **{role.name}** asignado. OK: {ok} | Fallos: {fail}")

# ---------- RECRUIT VIEW ----------
class RecruitView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return False

        recruiter_role = interaction.guild.get_role(RECRUITER_ROLE_ID)
        if recruiter_role is None or recruiter_role not in interaction.user.roles:
            await respond_ephemeral(interaction, "❌ Solo reclutadores pueden usar estos botones.")
            return False

        # Validar que el canal sea un ticket válido
        ch = interaction.channel
        if not isinstance(ch, discord.TextChannel):
            await respond_ephemeral(interaction, "❌ Esto solo funciona en un canal de ticket.")
            return False

        info = parse_recruit_topic(ch.topic)
        if not info.get("uid"):
            await respond_ephemeral(interaction, "❌ No pude identificar al postulante de este ticket (puede ser un ticket viejo o mal creado).")
            return False

        return True

    async def _get_applicant(self, interaction: discord.Interaction) -> Optional[discord.Member]:
        ch = interaction.channel
        if not isinstance(ch, discord.TextChannel) or interaction.guild is None:
            return None

        info = parse_recruit_topic(ch.topic)
        uid = info.get("uid")
        if not uid:
            return None

        member = interaction.guild.get_member(int(uid))
        if member is None:
            try:
                member = await interaction.guild.fetch_member(int(uid))
            except Exception:
                member = None
        return member

    async def accept_player(self, interaction: discord.Interaction, role_id: int, role_name: str):
        member = await self._get_applicant(interaction)
        if member is None:
            return await respond_ephemeral(interaction, "❌ No encontré al postulante (se fue del servidor o su ID no es válido).")

        role = interaction.guild.get_role(role_id)
        member_role = interaction.guild.get_role(MIEMBRO_ROLE_ID)
        public_role = interaction.guild.get_role(PUBLIC_ROLE_ID)

        if role is None or member_role is None:
            return await respond_ephemeral(interaction, "❌ Los roles del bot no están bien configurados. Avisale a un administrador.")

        try:
            await member.add_roles(member_role, role, reason=f"Aceptado como {role_name}")
            if public_role and public_role in member.roles:
                await member.remove_roles(public_role, reason="Aceptado: se quita rol Public")
        except discord.Forbidden:
            return await respond_ephemeral(interaction, "❌ No tengo permisos para asignar roles. Revisá que mi rol esté por encima de los roles a asignar.")
        except Exception:
            return await respond_ephemeral(interaction, "❌ Ocurrió un error inesperado al asignar los roles.")

        try:
            await interaction.channel.send(f"✅ {member.mention} aceptado como **{role_name}** en **Smogg** ⚔️")
        except Exception:
            pass

        # LOG
        try:
            log_channel = interaction.guild.get_channel(LOG_CHANNEL_ID) or await interaction.guild.fetch_channel(LOG_CHANNEL_ID)
        except Exception:
            log_channel = None

        if log_channel:
            embed = discord.Embed(
                title="✅ POSTULACIÓN ACEPTADA",
                color=discord.Color.green(),
                timestamp=datetime.now(timezone.utc)
            )
            embed.add_field(name="👤 Postulante", value=member.mention, inline=True)
            embed.add_field(name="🧑‍💼 Reclutador", value=interaction.user.mention, inline=True)
            embed.add_field(name="🎭 Rol asignado", value=role_name, inline=False)
            embed.add_field(name="📍 Ticket", value=interaction.channel.name, inline=False)

            # Imagen: la guardás por uid
            img_data = ticket_images.get(member.id)
            try:
                if img_data and img_data.get("url"):
                    filename = img_data.get("filename", "image.png")
                    file = await download_as_file(img_data["url"], filename)
                    if file:
                        embed.set_image(url=f"attachment://{filename}")
                        await log_channel.send(embed=embed, file=file)
                    else:
                        await log_channel.send(embed=embed)
                        await log_channel.send(img_data["url"])
                else:
                    await log_channel.send(embed=embed)
            except Exception:
                pass

        # Cleanup (aunque reinicies, esto solo limpia memoria)
        active_applications.pop(member.id, None)
        delete_ticket_image(member.id)

        try:
            await interaction.channel.delete(reason=f"Postulación aceptada por {interaction.user}")
        except Exception:
            pass

    async def reject_player(self, interaction: discord.Interaction):
        member = await self._get_applicant(interaction)
        if member:
            try:
                await member.send("❌ Tu postulación en **Smogg** fue rechazada. Podés volver a aplicar más adelante.")
            except Exception:
                pass

        try:
            await send_log(interaction.guild, f"❌ **RECHAZADO** {member} | Ticket: #{interaction.channel.name}")
        except Exception:
            pass

        if member:
            active_applications.pop(member.id, None)
            delete_ticket_image(member.id)

        try:
            await interaction.channel.delete(reason=f"Postulación rechazada por {interaction.user}")
        except Exception:
            pass

    async def close_ticket(self, interaction: discord.Interaction):
        member = await self._get_applicant(interaction)
        transcript = ""
        try:
            transcript = await create_transcript(interaction.channel)
        except Exception:
            pass

        try:
            await send_log(
                interaction.guild,
                f"🔒 **POSTULACIÓN CERRADA** {member}\n```\n{transcript[:1800]}\n```"
            )
        except Exception:
            pass

        if member:
            active_applications.pop(member.id, None)
            delete_ticket_image(member.id)

        try:
            await interaction.channel.delete(reason=f"Ticket cerrado por {interaction.user}")
        except Exception:
            pass

    # BOTONES (con custom_id fijo => persistente)
    @discord.ui.button(label="✔ Miembro", style=discord.ButtonStyle.success, custom_id="recruit_accept_miembro")
    async def miembro(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        await self.accept_player(interaction, MIEMBRO_ROLE_ID, "Miembro")

    @discord.ui.button(label="🛡 Tank", style=discord.ButtonStyle.primary, custom_id="recruit_accept_tank")
    async def tank(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        await self.accept_player(interaction, TANK_ROLE_ID, "Tank")

    @discord.ui.button(label="✨ Healer", style=discord.ButtonStyle.primary, custom_id="recruit_accept_healer")
    async def healer(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        await self.accept_player(interaction, HEALER_ROLE_ID, "Healer")

    @discord.ui.button(label="🧙 Support", style=discord.ButtonStyle.primary, custom_id="recruit_accept_supp")
    async def supp(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        await self.accept_player(interaction, SUPP_ROLE_ID, "Support")

    @discord.ui.button(label="⚔ DPS", style=discord.ButtonStyle.primary, custom_id="recruit_accept_dps")
    async def dps(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        await self.accept_player(interaction, DPS_ROLE_ID, "DPS")

    @discord.ui.button(label="🐎 Battle Mount", style=discord.ButtonStyle.primary, custom_id="recruit_accept_bm")
    async def battle_mount(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        await self.accept_player(interaction, BATTLE_MOUNT_ROLE_ID, "Battle Mount")

    @discord.ui.button(label="❌ Rechazar", style=discord.ButtonStyle.secondary, custom_id="recruit_reject")
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        await self.reject_player(interaction)

    @discord.ui.button(label="🔒 Cerrar Postulación", style=discord.ButtonStyle.danger, custom_id="recruit_close")
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        await self.close_ticket(interaction)


# ================== FOCO DONOR SYSTEM ==================
class FocoDonorModal(discord.ui.Modal, title="Foco Donor"):
    foco = discord.ui.TextInput(
        label="¿Cuánto foco tenés actualmente?",
        placeholder="Ej: 30000",
        required=True,
        max_length=60
    )
    item_spec = discord.ui.TextInput(
        label="¿Qué ítem podés craftear y con qué spec?",
        placeholder="Ej: Hellion Jacket - Spec 100",
        required=True,
        style=discord.TextStyle.paragraph,
        max_length=300
    )

    def __init__(self, opener: discord.Member):
        super().__init__(timeout=None)
        self.opener = opener

    async def on_submit(self, interaction: discord.Interaction):
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return await respond_ephemeral(interaction, "❌ Solo disponible en el servidor.")

        guild = interaction.guild
        user_id = interaction.user.id

        # Anti-duplicado (incluye reinicios)
        existing = find_open_foco_channel(guild, user_id)
        if existing is not None:
            return await respond_ephemeral(interaction, f"❌ Ya tenés un ticket de **Foco Donor** abierto: {existing.mention}")

        category = get_foco_category(guild)
        if category is None:
            return await respond_ephemeral(
                interaction,
                "❌ No encontré la categoría de tickets de Foco Donor. Avisale a un administrador."
            )

        # Crear canal
        display_name = interaction.user.display_name
        channel_name = safe_foco_channel_name(display_name)

        bot_member = guild.get_member(bot.user.id) if bot.user else None
        staff_role = guild.get_role(STAFF_ROLE_ID)

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True),
        }
        if staff_role:
            overwrites[staff_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True)
        if bot_member:
            overwrites[bot_member] = discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True)

        # La persona que recibe el aviso tiene que poder ver el canal.
        notify_member = guild.get_member(FOCO_NOTIFY_USER_ID)
        if notify_member is None:
            try:
                notify_member = await guild.fetch_member(FOCO_NOTIFY_USER_ID)
            except Exception:
                notify_member = None
        if notify_member is not None:
            overwrites[notify_member] = discord.PermissionOverwrite(view_channel=True, send_messages=True)

        topic = make_foco_topic(user_id, str(self.foco.value), str(self.item_spec.value))

        try:
            channel = await guild.create_text_channel(
                name=channel_name,
                category=category,
                overwrites=overwrites,
                topic=topic
            )
        except discord.Forbidden:
            return await respond_ephemeral(interaction, "❌ No tengo permisos para crear canales ni configurar sus permisos.")
        except Exception:
            return await respond_ephemeral(interaction, "❌ No pude crear el canal del ticket.")

        active_foco_tickets[user_id] = channel.id

        # Mensaje inicial en el canal
        embed = discord.Embed(
            title="💠 Foco Donor",
            description=(
                f"**Foco declarado:** `{self.foco.value}`\n"
                f"**Item + spec:** `{self.item_spec.value}`\n\n"
                "Un Staff va a revisar tu donación."
            ),
            color=discord.Color.blurple()
        )
        embed.set_footer(text="Smogg Foco Donor System")

        await channel.send(
            content=f"{interaction.user.mention} <@{FOCO_NOTIFY_USER_ID}>",
            embed=embed,
            view=FocoTicketActionView(),
            allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
        )

        await respond_ephemeral(interaction, f"✅ Ticket creado: {channel.mention}")

class FocoTicketActionView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return False
        staff_role = interaction.guild.get_role(STAFF_ROLE_ID)
        if staff_role is None or staff_role not in interaction.user.roles:
            await respond_ephemeral(interaction, "❌ Solo **Staff** puede aceptar/rechazar donaciones.")
            return False
        return True

    @discord.ui.button(
        label="✅ Cerrar Exitoso",
        style=discord.ButtonStyle.success,
        custom_id="foco_ticket_success"
    )
    async def foco_success(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)

        ch = interaction.channel
        if not isinstance(ch, discord.TextChannel) or interaction.guild is None:
            return await respond_ephemeral(interaction, "❌ Este canal no es válido para esta acción.")

        info = parse_foco_topic(ch.topic)
        uid = info.get("uid")
        foco = info.get("foco", "N/D")
        item = info.get("item", "N/D")

        if not uid:
            return await respond_ephemeral(interaction, "❌ No pude leer los datos del ticket.")

        member = interaction.guild.get_member(int(uid))
        if member is None:
            try:
                member = await interaction.guild.fetch_member(int(uid))
            except Exception:
                member = None

        if member:
            # Log de foco (tag a la persona + cantidad)
            await send_foco_log(
                interaction.guild,
                f"✅ **FOCO DONADO (OK)** {member.mention} → **{foco}** foco | Item: **{item}**"
            )

            try:
                await ch.send(f"✅ Donación aprobada. Gracias {member.mention} 💚")
            except Exception:
                pass

            active_foco_tickets.pop(member.id, None)

        try:
            await ch.delete(reason=f"Foco donor ticket cerrado exitoso por {interaction.user}")
        except Exception:
            pass

        await respond_ephemeral(interaction, "✅ Ticket cerrado como exitoso.")

    @discord.ui.button(
        label="❌ Rechazar Donación",
        style=discord.ButtonStyle.danger,
        custom_id="foco_ticket_reject"
    )
    async def foco_reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)

        ch = interaction.channel
        if not isinstance(ch, discord.TextChannel) or interaction.guild is None:
            return await respond_ephemeral(interaction, "❌ Este canal no es válido para esta acción.")

        info = parse_foco_topic(ch.topic)
        uid = info.get("uid")
        item = info.get("item", "ese ítem")

        if not uid:
            return await respond_ephemeral(interaction, "❌ No pude leer los datos del ticket.")

        member = interaction.guild.get_member(int(uid))
        if member is None:
            try:
                member = await interaction.guild.fetch_member(int(uid))
            except Exception:
                member = None

        if member:
            try:
                await member.send(
                    "❌ Donación rechazada.\n"
                    f"En este momento no necesitamos craftear **{item}**."
                )
            except Exception:
                pass

            try:
                await ch.send(f"❌ Donación rechazada. Le avisé por DM a {member.mention}.")
            except Exception:
                pass

            active_foco_tickets.pop(member.id, None)

        try:
            await ch.delete(reason=f"Foco donor ticket rechazado por {interaction.user}")
        except Exception:
            pass

        await respond_ephemeral(interaction, "✅ Ticket rechazado y cerrado.")

class FocoPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="💠 Abrir Foco Donor",
        style=discord.ButtonStyle.success,
        custom_id="open_foco_donor"
    )
    async def open_foco(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return await respond_ephemeral(interaction, "❌ Solo disponible en el servidor.")

        user_id = interaction.user.id
        now = time.time()

        # Anti-spam cooldown
        if user_id in cooldowns and (now - cooldowns[user_id] < COOLDOWN_SECONDS):
            return await respond_ephemeral(interaction, "⏳ Esperá un momento antes de volver a intentar.")
        cooldowns[user_id] = now

        # Anti-duplicado (incluye reinicios)
        existing = find_open_foco_channel(interaction.guild, user_id)
        if existing is not None:
            return await respond_ephemeral(interaction, f"❌ Ya tenés un ticket de **Foco Donor** abierto: {existing.mention}")

        # Abrir modal con 2 preguntas antes de crear el canal
        try:
            await interaction.response.send_modal(FocoDonorModal(interaction.user))
        except Exception:
            return await respond_ephemeral(interaction, "❌ No pude abrir el formulario. Probá de nuevo.")

class GiveawayJoinView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🎟️ Anotarme", style=discord.ButtonStyle.success, custom_id="giveaway_join_btn")
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return await respond_ephemeral(interaction, "❌ Solo disponible en el servidor.")

        msg = interaction.message
        if msg is None:
            return await respond_ephemeral(interaction, "❌ No pude leer el mensaje del sorteo.")

        g = giveaways.get(msg.id)
        if g is None:
            return await respond_ephemeral(interaction, "❌ Este sorteo ya terminó o no está activo.")

        if datetime.now(timezone.utc) >= g.end_at:
            return await respond_ephemeral(interaction, "⏰ Llegaste tarde, el sorteo ya terminó.")

        if interaction.user.bot:
            return await respond_ephemeral(interaction, "❌ Los bots no pueden participar.")

        try:
            async with g.lock:
                if interaction.user.id in g.entrants:
                    return await respond_ephemeral(interaction, "✅ Ya estabas anotado.")
                # Guardar primero; solo si se guardó se agrega a la memoria.
                save_giveaway_entrant(g.message_id, interaction.user.id, g=g, guild_id=interaction.guild.id)
                g.entrants.add(interaction.user.id)
        except sqlite3.Error as exc:
            print(f"❌ No pude guardar la inscripción al sorteo {g.message_id}: {exc}")
            return await respond_ephemeral(
                interaction, "❌ No pude guardar tu inscripción. Probá de nuevo en unos segundos.")

        # El contador del panel se actualiza agrupado (una edición cada pocos segundos).
        giveaway_request_panel_refresh(g)
        return await respond_ephemeral(interaction, "✅ Listo, quedaste anotado.")


# Limpieza si borran el canal manualmente (por si acaso)
@bot.event
async def on_guild_channel_delete(channel: discord.abc.GuildChannel):
    try:
        if not isinstance(channel, discord.TextChannel) or not channel.topic:
            return

        # FOCO
        if channel.topic.startswith(f"{FOCO_TOPIC_PREFIX}|"):
            info = parse_foco_topic(channel.topic)
            uid = info.get("uid")
            if uid:
                active_foco_tickets.pop(int(uid), None)

        # RECRUIT
        if channel.topic.startswith(f"{RECRUIT_TOPIC_PREFIX}|"):
            info = parse_recruit_topic(channel.topic)
            uid = info.get("uid")
            if uid:
                active_applications.pop(int(uid), None)
                delete_ticket_image(int(uid))

    except Exception:
        pass

# ---------- PANEL VIEW (RECLUTAMIENTO) ----------
class PanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="⚡ Abrir Postulación", style=discord.ButtonStyle.success, custom_id="open_postulacion")
    async def open_application(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.guild is None:
            return

        user_id = interaction.user.id
        now = time.time()

        if user_id in active_applications:
            return await respond_ephemeral(interaction, "❌ Ya tenés una postulación activa.")

        if user_id in cooldowns and (now - cooldowns[user_id] < COOLDOWN_SECONDS):
            return await respond_ephemeral(interaction, "⏳ Esperá un momento antes de volver a intentar.")

        cooldowns[user_id] = now
        await interaction.response.defer(ephemeral=True)

        guild = interaction.guild
        category = discord.utils.get(guild.categories, id=CATEGORY_ID)
        recruiter_role = guild.get_role(RECRUITER_ROLE_ID)

        if category is None:
            return await interaction.followup.send("❌ No encontré la categoría de postulaciones. Avisale a un administrador.", ephemeral=True)

        if recruiter_role is None:
            return await interaction.followup.send("❌ No encontré el rol de reclutador. Avisale a un administrador.", ephemeral=True)

        channel_name = safe_channel_name(interaction.user.name, interaction.user.id)
        bot_member = guild.get_member(bot.user.id) if bot.user else None

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True),
            recruiter_role: discord.PermissionOverwrite(view_channel=True, send_messages=True),
        }
        if bot_member:
            overwrites[bot_member] = discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True)

        topic = make_recruit_topic(user_id)

        channel = await guild.create_text_channel(
            name=channel_name,
            category=category,
            overwrites=overwrites,
            topic=topic
        )

        active_applications[user_id] = channel.id

        embed = discord.Embed(
            title="⚔️ Reclutamiento Smogg",
            description=(
                "**Enviá lo siguiente:**\n\n"
                "📸 Screenshot perfil Albion\n"
                "🎥 POVs jugando el rol con el que postulaste\n"
                "⚔ Rol ZvZ\n"
                "🕒 Horarios\n"
                "📜 Gremios anteriores en los que estuviste\n\n"
                "Un reclutador revisará tu postulación."
            ),
            color=discord.Color.gold()
        )

        recruiter_mention = recruiter_role.mention if recruiter_role else "@Reclutadores"

        await channel.send(
            content=f"{interaction.user.mention} {recruiter_mention}",
            embed=embed,
            view=RecruitView()
        )

        await send_log(guild, f"📥 **NUEVA POSTULACIÓN** {interaction.user} → {channel.mention}")
        await interaction.followup.send(f"✅ Postulación creada: {channel.mention}", ephemeral=True)

# ---------- COMANDO PANEL (RECLUTAMIENTO) ----------
@bot.command()
@commands.has_permissions(administrator=True)
async def panel(ctx: commands.Context):
    embed = discord.Embed(
        title="⚔️ Smogg Reclutamiento",
        description="Presioná el botón para abrir tu **postulación oficial**.",
        color=discord.Color.orange()
    )
    embed.set_footer(text="Albion Online Recruitment System")
    await ctx.send(embed=embed, view=PanelView())

# ---------- COMANDO PANEL (FOCO DONOR) NUEVO ----------
@bot.command(name="panel_foco")
@commands.has_permissions(administrator=True)
async def panel_foco(ctx: commands.Context):
    embed = discord.Embed(
        title="Foco Donor",
        description="Presioná el botón para donar foco a la guild.",
        color=discord.Color.blurple()
    )
    embed.set_footer(text="Smogg Foco Donor System")
    await ctx.send(embed=embed, view=FocoPanelView())


async def _publish_admin_panel(interaction: discord.Interaction, embed: discord.Embed, view: discord.ui.View):
    """Versión slash de !panel / !panel_foco: solo administradores; el panel se publica en el canal actual."""
    if interaction.guild is None or not isinstance(interaction.user, discord.Member) or not interaction.user.guild_permissions.administrator:
        return await respond_ephemeral(interaction, "❌ Este comando es solo para administradores.")
    channel = interaction.channel
    if channel is None or not hasattr(channel, "send"):
        return await respond_ephemeral(interaction, "❌ No puedo publicar en este canal.")
    try:
        await channel.send(embed=embed, view=view)
    except (discord.Forbidden, discord.HTTPException) as exc:
        return await respond_ephemeral(interaction, f"❌ No pude publicar el panel: {exc}")
    await respond_ephemeral(interaction, "✅ Panel publicado.")


@bot.tree.command(name="panel-reclutamiento", description="(Admin) Publica el panel de reclutamiento en este canal", guild=GUILD_OBJ)
@app_commands.default_permissions(administrator=True)
async def panel_slash(interaction: discord.Interaction):
    embed = discord.Embed(
        title="⚔️ Smogg Reclutamiento",
        description="Presioná el botón para abrir tu **postulación oficial**.",
        color=discord.Color.orange()
    )
    embed.set_footer(text="Albion Online Recruitment System")
    await _publish_admin_panel(interaction, embed, PanelView())


@bot.tree.command(name="panel_foco", description="(Admin) Publica el panel de Foco Donor en este canal", guild=GUILD_OBJ)
@app_commands.default_permissions(administrator=True)
async def panel_foco_slash(interaction: discord.Interaction):
    embed = discord.Embed(
        title="Foco Donor",
        description="Presioná el botón para donar foco a la guild.",
        color=discord.Color.blurple()
    )
    embed.set_footer(text="Smogg Foco Donor System")
    await _publish_admin_panel(interaction, embed, FocoPanelView())


# ============================================================
# FUNCIONES AUXILIARES
# ============================================================

def normalizar_nombre(nombre: str) -> str:
    """
    Normaliza un nombre para comparar ignorando MAYÚSCULAS/minúsculas.

    NO elimina:
    - espacios
    - guiones
    - números
    - símbolos
    - caracteres especiales

    Ejemplos:

    Carlos == carlos       -> True
    CARLOS == Carlos       -> True
    Carlos123 == Carlos    -> False
    Carlitos == Carlos     -> False
    """

    return nombre.casefold()


def extraer_ids(texto: str) -> list[str]:
    """
    Acepta:

    1461052597
    1461052597,1461051559,1461048629

    https://albionbb.com/battles/1461052597

    https://albionbb.com/battles/multi?ids=1461052597,1461051559,1461048629

    y devuelve únicamente los IDs.
    """

    texto = texto.strip()

    # Si es una URL con ?ids=
    match = re.search(r"[?&]ids=([^&\s]+)", texto, re.IGNORECASE)

    if match:
        texto = match.group(1)

    # Extraer todos los números
    ids = re.findall(r"\d+", texto)

    # Eliminar duplicados manteniendo el orden
    resultado = []

    for battle_id in ids:
        if battle_id not in resultado:
            resultado.append(battle_id)

    return resultado


async def obtener_jugadores_batalla_albion(
    session: aiohttp.ClientSession,
    battle_id: str,
) -> list[dict[str, str]]:
    """
    Obtiene los participantes de UNA batalla desde el endpoint de detalle
    de AlbionBB: /battles/{id}.

    AlbionBB puede cambiar ligeramente la estructura JSON, por eso el
    extractor acepta las variantes habituales de Name/GuildName/Guild.
    """
    url = f"{ALBIONBB_BATTLE_URL}/{battle_id}"

    async with session.get(url) as response:
        if response.status != 200:
            raise RuntimeError(
                f"AlbionBB batalla {battle_id}: HTTP {response.status}"
            )
        data = await response.json(content_type=None)

    encontrados: dict[tuple[str, str], dict[str, str]] = {}

    def extraer(obj):
        if isinstance(obj, dict):
            nombre = obj.get("Name") or obj.get("name")

            guild_name = obj.get("GuildName") or obj.get("guildName")
            guild_obj = obj.get("Guild") or obj.get("guild")

            if not guild_name and isinstance(guild_obj, dict):
                guild_name = guild_obj.get("Name") or guild_obj.get("name")
            elif not guild_name and isinstance(guild_obj, str):
                guild_name = guild_obj

            if isinstance(nombre, str) and isinstance(guild_name, str) and guild_name.strip():
                clave = (normalizar_nombre(nombre), normalizar_nombre(guild_name))
                encontrados.setdefault(
                    clave,
                    {"name": nombre, "guild": guild_name}
                )

            for value in obj.values():
                extraer(value)

        elif isinstance(obj, list):
            for value in obj:
                extraer(value)

    extraer(data)
    return list(encontrados.values())


async def obtener_jugadores_albion(
    battle_ids: list[str],
) -> dict[str, dict[str, str]]:
    """
    Obtiene los participantes de todas las batallas solicitadas.

    Primero usa /battles/{id}, que es la fuente adecuada para obtener la
    tabla de participantes de una batalla. Si una batalla no devuelve
    participantes, usa /battles/kills como respaldo.

    El resultado se indexa por nombre normalizado y elimina duplicados.
    """
    timeout = aiohttp.ClientTimeout(total=45)
    jugadores: dict[str, dict[str, str]] = {}

    async with aiohttp.ClientSession(timeout=timeout) as session:
        for battle_id in battle_ids:
            try:
                participantes = await obtener_jugadores_batalla_albion(
                    session, battle_id
                )
            except Exception as error:
                print(f"⚠️ Error obteniendo detalle de batalla {battle_id}: {error}")
                participantes = []

            for jugador in participantes:
                clave = normalizar_nombre(jugador["name"])
                jugadores.setdefault(clave, jugador)

        # Fallback: si el detalle no entregó jugadores, intentamos el endpoint
        # de kills para no dejar el comando inutilizable ante cambios de API.
        if not jugadores:
            ids = ",".join(battle_ids)
            async with session.get(ALBIONBB_KILLS_URL, params={"ids": ids}) as response:
                if response.status != 200:
                    raise RuntimeError(
                        f"AlbionBB fallback kills: HTTP {response.status}"
                    )
                data = await response.json(content_type=None)

            if not isinstance(data, list):
                raise RuntimeError("La respuesta de AlbionBB no tiene el formato esperado.")

            def agregar_participante(jugador):
                if not isinstance(jugador, dict):
                    return

                nombre = jugador.get("Name") or jugador.get("name")
                guild = jugador.get("GuildName") or jugador.get("guildName")

                if not nombre or not guild:
                    return

                clave = normalizar_nombre(nombre)
                jugadores.setdefault(
                    clave,
                    {"name": nombre, "guild": guild}
                )

            for evento in data:
                agregar_participante(evento.get("Killer", {}))
                agregar_participante(evento.get("Victim", {}))
                for participante in evento.get("Participants", []) or []:
                    agregar_participante(participante)
                for participante in evento.get("GroupMembers", []) or []:
                    agregar_participante(participante)

    return jugadores


# ============================================================
# MATCHING DISCORD
# ============================================================

def construir_indice_discord(
    guild: discord.Guild
) -> dict[str, list[discord.Member]]:

    """
    Construye un índice:

    nombre -> miembros Discord

    Busca tanto:
    - nickname/apodo
    - username

    Todo comparado ignorando mayúsculas.
    """

    indice = {}

    for member in guild.members:

        # ----------------------------------------------------
        # Username
        # ----------------------------------------------------

        username = normalizar_nombre(member.name)

        indice.setdefault(username, [])
        indice[username].append(member)

        # ----------------------------------------------------
        # Nickname / apodo
        # ----------------------------------------------------

        if member.nick:

            nickname = normalizar_nombre(member.nick)

            indice.setdefault(nickname, [])
            indice[nickname].append(member)

    return indice


# ============================================================
# SLASH COMMAND: ROL DESDE ALBIONBB
# ============================================================

@bot.tree.command(
    name="rolalbion",
    description="Crea un rol y lo asigna a participantes de AlbionBB.",
    guild=GUILD_OBJ,
)
@app_commands.describe(
    ids="IDs o link de AlbionBB. Ej: 1461052597,1461051559,1461048629",
    guild="Nombre exacto de la guild de Albion. Ej: Smogg",
    rol="Nombre del rol que se creará. Ej: Smogg ZvZ"
)
async def rolalbion(
    interaction: discord.Interaction,
    ids: str,
    guild: str,
    rol: str,
):
    """Crea un rol y lo asigna mediante coincidencia exacta de nombres."""
    await interaction.response.defer(ephemeral=True)

    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        return await interaction.followup.send(
            "❌ Este comando solo puede usarse dentro del servidor.",
            ephemeral=True,
        )

    if not interaction.user.guild_permissions.manage_roles:
        return await interaction.followup.send(
            "❌ Necesitás el permiso **Gestionar roles** para usar este comando.",
            ephemeral=True,
        )

    battle_ids = extraer_ids(ids)
    if not battle_ids:
        return await interaction.followup.send(
            "❌ No encontré ningún ID de batalla.\n\n"
            "Ejemplo:\n"
            "`/rolalbion ids:1461052597,1461051559,1461048629 guild:Smogg rol:Smogg ZvZ`",
            ephemeral=True,
        )

    if len(battle_ids) > 50:
        return await interaction.followup.send(
            "❌ Podés consultar como máximo 50 batallas por comando.",
            ephemeral=True,
        )

    guild_name = guild.strip()
    role_name = rol.strip()
    if not guild_name or not role_name:
        return await interaction.followup.send(
            "❌ La guild y el nombre del rol no pueden estar vacíos.",
            ephemeral=True,
        )

    # Obtener participantes reales de las batallas.
    try:
        jugadores = await obtener_jugadores_albion(battle_ids)
    except Exception as error:
        print(f"❌ /rolalbion AlbionBB: {error!r}")
        return await interaction.followup.send(
            "❌ No pude obtener los participantes de AlbionBB.\n"
            "Revisá los IDs y que AlbionBB esté disponible.",
            ephemeral=True,
        )

    guild_key = normalizar_nombre(guild_name)
    jugadores_guild = []

    for datos in jugadores.values():
        if normalizar_nombre(datos["guild"]) == guild_key:
            jugadores_guild.append(datos["name"])

    # Deduplicar jugadores que aparecieron en varias batallas.
    jugadores_guild = list(dict.fromkeys(jugadores_guild))

    if not jugadores_guild:
        return await interaction.followup.send(
            f"❌ No encontré participantes de **{guild_name}** en las "
            f"{len(battle_ids)} batallas consultadas.",
            ephemeral=True,
        )

    # Discord necesita la lista de miembros para poder comparar apodos.
    try:
        await interaction.guild.chunk(cache=True)
    except Exception:
        pass

    indice_discord: dict[str, dict[int, discord.Member]] = {}

    for member in interaction.guild.members:
        # Username actual de Discord.
        username_key = normalizar_nombre(member.name)
        indice_discord.setdefault(username_key, {})[member.id] = member

        # Apodo dentro del servidor.
        if member.nick:
            nickname_key = normalizar_nombre(member.nick)
            indice_discord.setdefault(nickname_key, {})[member.id] = member

    encontrados: list[tuple[str, discord.Member]] = []
    no_encontrados: list[str] = []
    ambiguos: list[tuple[str, list[discord.Member]]] = []

    for jugador in jugadores_guild:
        candidatos = list(indice_discord.get(normalizar_nombre(jugador), {}).values())

        if len(candidatos) == 0:
            no_encontrados.append(jugador)
        elif len(candidatos) == 1:
            encontrados.append((jugador, candidatos[0]))
        else:
            ambiguos.append((jugador, candidatos))

    # Crear o reutilizar el rol.
    role = discord.utils.get(interaction.guild.roles, name=role_name)

    if role is None:
        try:
            role = await interaction.guild.create_role(
                name=role_name,
                reason=f"Creado por /rolalbion para la guild {guild_name}",
            )
        except discord.Forbidden:
            return await interaction.followup.send(
                "❌ No puedo crear roles. Necesito **Gestionar roles**.",
                ephemeral=True,
            )
        except discord.HTTPException as error:
            return await interaction.followup.send(
                f"❌ Discord rechazó la creación del rol: `{error}`",
                ephemeral=True,
            )

    bot_member = interaction.guild.me
    if bot_member is None or role >= bot_member.top_role:
        return await interaction.followup.send(
            "❌ El rol está por encima o al mismo nivel que el rol del bot.\n"
            "Mové el rol del bot por encima del rol que querés asignar.",
            ephemeral=True,
        )

    asignados = []
    ya_tenian = []
    errores = []

    for jugador, member in encontrados:
        try:
            if role in member.roles:
                ya_tenian.append((jugador, member))
            else:
                await member.add_roles(
                    role,
                    reason=f"Participación AlbionBB - {guild_name}",
                )
                asignados.append((jugador, member))
        except discord.Forbidden:
            errores.append((jugador, member))
        except discord.HTTPException as error:
            print(f"⚠️ Error asignando {role_name} a {member}: {error}")
            errores.append((jugador, member))

    # Resultado compacto. Los detalles se mandan en mensajes efímeros separados
    # para no superar el límite de 2000 caracteres de Discord.
    resumen = (
        "## ⚔️ Rol AlbionBB\n"
        f"**Guild:** `{guild_name}`\n"
        f"**Batallas:** `{len(battle_ids)}`\n"
        f"**Jugadores de la guild:** `{len(jugadores_guild)}`\n"
        f"**Encontrados:** `{len(encontrados)}`\n"
        f"**Asignados ahora:** `{len(asignados)}`\n"
        f"**Ya tenían el rol:** `{len(ya_tenian)}`\n"
        f"**No encontrados:** `{len(no_encontrados)}`\n"
        f"**Ambiguos:** `{len(ambiguos)}`\n"
        f"**Errores:** `{len(errores)}`\n\n"
        f"**Rol:** {role.mention}"
    )

    await interaction.followup.send(
        resumen,
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions(roles=True),
    )

    detalles: list[str] = []
    detalles.extend(f"✓ `{j}` → {m.display_name}" for j, m in asignados)
    detalles.extend(f"↪ `{j}` → {m.display_name} (ya lo tenía)" for j, m in ya_tenian)
    detalles.extend(f"✗ `{j}` → NO ENCONTRADO" for j in no_encontrados)
    detalles.extend(
        f"⚠ `{j}` → {', '.join(m.display_name for m in miembros)}"
        for j, miembros in ambiguos
    )
    detalles.extend(f"🚫 `{j}` → {m.display_name} (sin permisos)" for j, m in errores)

    if detalles:
        for inicio in range(0, len(detalles), 25):
            bloque = "\n".join(detalles[inicio:inicio + 25])
            await interaction.followup.send(
                bloque,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )

# ============================================================
# TAB SELL / CALCULAR SPLIT
# ============================================================
SPLIT_PANEL_CHANNEL_ID = 1557432820257923183
SPLIT_LOG_CHANNEL_ID = 1557434386775932969
SPLIT_SELLER_ROLE_IDS = {1257896709246423083, 1257896977220501557}
SPLIT_PING_ROLE_ID = 1468314531825713338
SPLIT_DEFAULT_DISCOUNT = 15.0

split_log_ids: dict[int, int] = {}   # panel_message_id -> log_message_id
split_locks: dict[int, asyncio.Lock] = {}


def fmt_num(n: float) -> str:
    """1234567.0 -> '1,234,567' | 15.5 -> '15.5'"""
    if float(n).is_integer():
        return f"{int(n):,}"
    return f"{n:,.2f}".rstrip("0").rstrip(".")


def build_split_log(
    panel_url: str, city: str, tab: str, creator_id: int,
    total: float, reparacion: float, pct: float, tax: float, precio_venta: float,
) -> str:
    return (
        f"Link al mensaje: {panel_url}\n"
        f"City: {city}\n"
        f"Nombre tab: {tab}\n"
        f"Creador de la tab: <@{creator_id}>\n"
        f"Vendedor: \n"
        f"Comprador: \n"
        f"Total: {fmt_num(total)}\n"
        f"Reparacion: {fmt_num(reparacion)}\n"
        f"Tax venta: {fmt_num(pct)}% ({fmt_num(tax)})\n"
        f"Precio de venta: {fmt_num(precio_venta)}"
    )


async def _get_channel(guild: discord.Guild, channel_id: int):
    ch = guild.get_channel(channel_id)
    if ch is None:
        try:
            ch = await guild.fetch_channel(channel_id)
        except Exception:
            return None
    return ch


async def find_split_log_message(guild: discord.Guild, panel_msg: discord.Message) -> Optional[discord.Message]:
    log_ch = await _get_channel(guild, SPLIT_LOG_CHANNEL_ID)
    if not isinstance(log_ch, discord.TextChannel):
        return None

    log_id = split_log_ids.get(panel_msg.id)
    if log_id:
        try:
            return await log_ch.fetch_message(log_id)
        except Exception:
            pass

    # Fallback (por si el bot reinició): buscar por el link del panel
    needle = f"Link al mensaje: {panel_msg.jump_url}"
    try:
        async for m in log_ch.history(limit=500):
            if m.author.id == bot.user.id and m.content.startswith(needle):
                save_split_log_mapping(panel_msg.id, m.id)
                return m
    except Exception:
        pass
    return None


class SplitBuyerModal(discord.ui.Modal, title="Marcar tab como vendido"):
    comprador = discord.ui.TextInput(
        label="¿A quién se lo vendiste?",
        placeholder="Nombre del comprador",
        min_length=1,
        max_length=100,
        required=True,
    )

    def __init__(self, panel_msg: discord.Message):
        super().__init__()
        self.panel_msg = panel_msg

    async def on_submit(self, interaction: discord.Interaction):
        guild = interaction.guild
        buyer = self.comprador.value.strip()
        if not buyer:
            return await respond_ephemeral(interaction, "❌ Tenés que indicar el comprador.")

        await interaction.response.defer(ephemeral=True)
        lock = split_locks.setdefault(self.panel_msg.id, asyncio.Lock())

        async with lock:
            # Releer el panel para saber si alguien ya lo vendió
            try:
                panel = await self.panel_msg.channel.fetch_message(self.panel_msg.id)
            except Exception:
                return await interaction.followup.send("❌ No pude leer el panel.", ephemeral=True)

            if "Vendido por" in (panel.content or ""):
                return await interaction.followup.send("❌ Este tab ya fue marcado como vendido.", ephemeral=True)

            log_msg = await find_split_log_message(guild, panel)
            if log_msg is None:
                return await interaction.followup.send("❌ No encontré el registro de este tab en el canal de logs.", ephemeral=True)

            # Editar log: completar Vendedor y Comprador
            new_content = re.sub(
                r"^Vendedor:.*$", lambda _m: f"Vendedor: {interaction.user.mention}",
                log_msg.content, count=1, flags=re.MULTILINE,
            )
            new_content = re.sub(
                r"^Comprador:.*$", lambda _m: f"Comprador: {buyer}",
                new_content, count=1, flags=re.MULTILINE,
            )
            try:
                await log_msg.edit(content=new_content, allowed_mentions=discord.AllowedMentions.none())
            except Exception:
                return await interaction.followup.send("❌ No pude editar el registro.", ephemeral=True)

            # Editar panel: marcar vendido, agregar comprador y deshabilitar botón
            try:
                await panel.edit(
                    content=(
                        f"{panel.content}\n\n"
                        f"✅ **Vendido por** {interaction.user.display_name}\n"
                        f"Vendido a: {buyer}"
                    ),
                    view=SplitSoldView(disabled=True),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except Exception:
                pass

        await interaction.followup.send("✅ Registrado como vendido.", ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        try:
            await respond_ephemeral(interaction, "❌ Ocurrió un error al registrar la venta.")
        except Exception:
            pass


class SplitSoldView(discord.ui.View):
    def __init__(self, disabled: bool = False):
        super().__init__(timeout=None)
        if disabled:
            for child in self.children:
                child.disabled = True

    @discord.ui.button(label="💰 Vendido", style=discord.ButtonStyle.success, custom_id="split_sold_btn")
    async def sold(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return await respond_ephemeral(interaction, "❌ Solo disponible en el servidor.")

        user_role_ids = {r.id for r in interaction.user.roles}
        if not (user_role_ids & SPLIT_SELLER_ROLE_IDS):
            return await respond_ephemeral(interaction, "❌ No tenés permiso para marcar este tab como vendido.")

        msg = interaction.message
        if msg is None:
            return await respond_ephemeral(interaction, "❌ No pude leer el panel.")

        if "Vendido por" in (msg.content or ""):
            return await respond_ephemeral(interaction, "❌ Este tab ya fue marcado como vendido.")

        await interaction.response.send_modal(SplitBuyerModal(msg))


@bot.tree.command(
    name="tabsell",
    description="Calcula el precio de venta de un tab y crea el panel.",
    guild=GUILD_OBJ,
)
@app_commands.describe(
    nombre_tab="Nombre del tab",
    city="Ciudad donde se vende",
    total="Total",
    reparacion="Costo de reparación",
    descuento="Porcentaje de tax de venta (opcional, por defecto 15)",
)
async def tabsell(
    interaction: discord.Interaction,
    nombre_tab: str,
    city: str,
    total: float,
    reparacion: float,
    descuento: Optional[app_commands.Range[float, 0, 100]] = None,
):
    if interaction.guild is None:
        return await respond_ephemeral(interaction, "❌ Solo disponible en el servidor.")

    await interaction.response.defer(ephemeral=True)

    panel_ch = await _get_channel(interaction.guild, SPLIT_PANEL_CHANNEL_ID)
    if not isinstance(panel_ch, discord.TextChannel):
        return await interaction.followup.send("❌ No encontré el canal de paneles.", ephemeral=True)

    log_ch = await _get_channel(interaction.guild, SPLIT_LOG_CHANNEL_ID)
    if not isinstance(log_ch, discord.TextChannel):
        return await interaction.followup.send("❌ No encontré el canal de logs.", ephemeral=True)

    nombre_tab = nombre_tab.strip()
    city = city.strip()
    pct = SPLIT_DEFAULT_DISCOUNT if descuento is None else descuento

    tax = round(total * pct / 100)
    precio_venta = round(total - tax - reparacion)

    content = (
        f"<@&{SPLIT_PING_ROLE_ID}>\n"
        "Tab Sell\n"
        f"Nombre tab: {nombre_tab}\n"
        f"City: {city}\n"
        f"Precio de venta (-{fmt_num(pct)}%): {fmt_num(precio_venta)}"
    )

    try:
        panel_msg = await panel_ch.send(
            content,
            view=SplitSoldView(),
            allowed_mentions=discord.AllowedMentions(roles=True),
        )
    except discord.Forbidden:
        return await interaction.followup.send("❌ No tengo permisos para escribir en el canal de paneles.", ephemeral=True)
    except Exception:
        return await interaction.followup.send("❌ No pude crear el panel.", ephemeral=True)

    # Log creado al ejecutar el comando
    log_text = build_split_log(
        panel_msg.jump_url, city, nombre_tab, interaction.user.id,
        total, reparacion, pct, tax, precio_venta,
    )
    try:
        log_msg = await log_ch.send(log_text, allowed_mentions=discord.AllowedMentions.none())
        save_split_log_mapping(panel_msg.id, log_msg.id)
    except Exception:
        await interaction.followup.send(
            "⚠️ Panel creado pero no pude escribir el registro en el canal de logs.",
            ephemeral=True,
        )

    thread_warning = ""
    try:
        await panel_msg.create_thread(name=nombre_tab[:100] or "Tab Sell")
    except Exception:
        thread_warning = "\n⚠️ No pude crear el hilo (revisá el permiso **Crear hilos públicos**)."

    await interaction.followup.send(
        f"✅ Panel creado: {panel_msg.jump_url}\n"
        f"Total {fmt_num(total)} - {fmt_num(pct)}% ({fmt_num(tax)}) - reparación {fmt_num(reparacion)}\n"
        f"Precio de venta: **{fmt_num(precio_venta)}**"
        f"{thread_warning}",
        ephemeral=True,
    )


# ============================================================
# /calcularsplit (solo Staff)
# ============================================================
SPLIT_CALC_DEFAULT_SALE_TAX = 15.0
SPLIT_CALC_DEFAULT_GUILD_TAX = 5.0
SPLIT_CALC_MAX_NUMBERS = 50
_SPLIT_CALC_SEPARATORS_RE = re.compile(r"[\s+;]+")
_SPLIT_CALC_NUMBER_RE = re.compile(r"^\d{1,15}(?:\.\d{1,4})?$")


def parse_number_sum(text: str) -> Optional[float]:
    """'1000000 500 23333' -> 1023833.0

    Los números se separan con espacios (también se aceptan '+' y ';').
    La coma NO es separador: '1,000,000' es inválido en vez de sumarse mal en silencio.
    Devuelve None si no hay números, hay demasiados o algún valor no es válido.
    """
    tokens = [t for t in _SPLIT_CALC_SEPARATORS_RE.split((text or "").strip()) if t]
    if not tokens or len(tokens) > SPLIT_CALC_MAX_NUMBERS:
        return None
    total = Decimal(0)
    for token in tokens:
        if not _SPLIT_CALC_NUMBER_RE.match(token):
            return None
        total += Decimal(token)
    return float(total)


def calc_split_final(total: float, sale_tax: float, guild_tax: float, reparacion: float, bolsitas: float = 0.0) -> int:
    """Split final = total - (total × (tax venta + tax guild) / 100) - reparación + bolsitas.
    Las bolsitas se suman al final: no pagan tax ni se restan."""
    return round(total - (total * (sale_tax + guild_tax) / 100) - reparacion + bolsitas)


def build_split_calc_panel(
    total: float, reparacion: float, bolsitas: float,
    sale_tax: float, guild_tax: float, split_final: float,
) -> str:
    return (
        "Split:\n"
        f"Total: {fmt_num(total)}\n"
        f"Reparacion: {fmt_num(reparacion)}\n"
        f"Bolsitas: {fmt_num(bolsitas)}\n"
        f"Tax de venta: {fmt_num(sale_tax)}%\n"
        f"Tax de guild: {fmt_num(guild_tax)}%\n\n"
        f"Split final: {fmt_num(split_final)}"
    )


@bot.tree.command(
    name="calcularsplit",
    description="(Staff) Calcula el split y publica el panel en este canal.",
    guild=GUILD_OBJ,
)
@staff_only_slash()
@app_commands.describe(
    total="Total. Podés poner varios números separados por espacio. Ej: 1000000 500 23333",
    reparacion="Reparación. Podés poner varios números separados por espacio. Ej: 15000 2500",
    bolsitas="Bolsitas (opcional, por defecto 0). Se suman al split final",
    tax_de_venta="Tax de venta en % (opcional, por defecto 15)",
    tax_de_guild="Tax de guild en % (opcional, por defecto 5)",
)
async def calcularsplit_slash(
    interaction: discord.Interaction,
    total: str,
    reparacion: str,
    bolsitas: Optional[app_commands.Range[float, 0.0, 1e15]] = None,
    tax_de_venta: Optional[app_commands.Range[float, 0, 100]] = None,
    tax_de_guild: Optional[app_commands.Range[float, 0, 100]] = None,
):
    if interaction.guild is None:
        return await respond_ephemeral(interaction, "❌ Solo disponible en el servidor.")

    total_sum = parse_number_sum(total)
    if total_sum is None:
        return await respond_ephemeral(
            interaction,
            "❌ **Total** inválido. Usá solo números separados por espacios, ej: `1000000 500 23333` "
            "(sin comas ni puntos de miles)."
        )

    rep_sum = parse_number_sum(reparacion)
    if rep_sum is None:
        return await respond_ephemeral(
            interaction,
            "❌ **Reparación** inválida. Usá solo números separados por espacios, ej: `15000 2500` "
            "(sin comas ni puntos de miles)."
        )

    bag = 0.0 if bolsitas is None else bolsitas
    sale_tax = SPLIT_CALC_DEFAULT_SALE_TAX if tax_de_venta is None else tax_de_venta
    guild_tax = SPLIT_CALC_DEFAULT_GUILD_TAX if tax_de_guild is None else tax_de_guild

    if sale_tax + guild_tax > 100:
        return await respond_ephemeral(interaction, "❌ La suma de **tax de venta** y **tax de guild** no puede superar 100%.")

    final = calc_split_final(total_sum, sale_tax, guild_tax, rep_sum, bag)

    # Mensaje público en el canal donde se ejecutó el comando
    await interaction.response.send_message(
        build_split_calc_panel(total_sum, rep_sum, bag, sale_tax, guild_tax, final),
        allowed_mentions=discord.AllowedMentions.none(),
    )

@calcularsplit_slash.error
async def calcularsplit_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        return await respond_ephemeral(interaction, "❌ Solo **Staff** puede usar `/calcularsplit`.")
    return await respond_ephemeral(interaction, "❌ Error ejecutando el comando.")


# ================== SISTEMA CTA SMOGG ==================
# Las composiciones, CTA y reservas se guardan en SQLite usando la base existente.
import json
import copy
import contextlib

CTA_EMOJIS_DEFAULT = {
    "Golem": "🔨", "Heavy Mace": "🛡️", "Monarch": "🛡️", "GA": "🛡️",
    "Carving": "🔮", "Shadowcaller": "🔮", "Rootbound": "🔮", "Locus": "🔮",
    "Hallow Fall": "✨", "Exalted": "✨", "1h Nature": "✨", "Longbow": "🏹",
    "Spiked": "🐻", "Realmbraker": "✨", "Dawnsong": "🏹", "HoJ": "🛡️",
}
CTA_BRAWL_SLOTS = [
    (1, "Golem"), (2, "Heavy Mace"), (3, "Monarch"), (4, "GA"),
    (5, "Carving"), (6, "Shadowcaller"), (7, "Rootbound"), (8, "Locus"),
    (9, "Hallow Fall"), (10, "Hallow Fall"), (11, "Exalted"), (12, "1h Nature"),
    (13, "Longbow"), (14, "Longbow"), (15, "Longbow"), (16, "Longbow"),
    (17, "Spiked"), (18, "Realmbraker"), (19, "Dawnsong"), (20, "HoJ"),
]


def cta_slot_label(s: dict) -> str:
    """Nombre de la posición, con emoji solo si la composición tiene uno cargado a mano."""
    emoji = (s.get("emoji") or "").strip()
    return f"{emoji} {s['role_name']}" if emoji else str(s["role_name"])


def cta_db_init():
    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS cta_compositions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            name TEXT NOT NULL COLLATE NOCASE,
            created_by INTEGER NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(guild_id, name)
        );
        CREATE TABLE IF NOT EXISTS cta_slots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            composition_id INTEGER NOT NULL,
            party_no INTEGER NOT NULL DEFAULT 1,
            position_no INTEGER NOT NULL,
            role_name TEXT NOT NULL,
            emoji TEXT NOT NULL DEFAULT '',
            access_mode TEXT NOT NULL DEFAULT 'any',
            role_ids_json TEXT NOT NULL DEFAULT '[]',
            access_match TEXT NOT NULL DEFAULT 'any',
            UNIQUE(composition_id, party_no, position_no),
            FOREIGN KEY(composition_id) REFERENCES cta_compositions(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS cta_party_access (
            composition_id INTEGER NOT NULL,
            party_no INTEGER NOT NULL,
            access_mode TEXT NOT NULL DEFAULT 'any',
            role_ids_json TEXT NOT NULL DEFAULT '[]',
            access_match TEXT NOT NULL DEFAULT 'any',
            PRIMARY KEY(composition_id, party_no),
            FOREIGN KEY(composition_id) REFERENCES cta_compositions(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS cta_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            composition_id INTEGER,
            composition_name TEXT NOT NULL,
            snapshot_json TEXT NOT NULL,
            channel_id INTEGER NOT NULL,
            message_id INTEGER,
            thread_id INTEGER,
            created_by INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            closes_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            closed_at TEXT,
            FOREIGN KEY(composition_id) REFERENCES cta_compositions(id) ON DELETE SET NULL
        );
        CREATE TABLE IF NOT EXISTS cta_signups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cta_id INTEGER NOT NULL,
            slot_key TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            assigned_by INTEGER,
            override_reason TEXT,
            signed_at TEXT NOT NULL,
            UNIQUE(cta_id, slot_key),
            UNIQUE(cta_id, user_id),
            FOREIGN KEY(cta_id) REFERENCES cta_runs(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS cta_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            cta_id INTEGER,
            actor_id INTEGER NOT NULL,
            action TEXT NOT NULL,
            details TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS cta_settings (
            guild_id INTEGER PRIMARY KEY,
            channel_id INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_cta_runs_status ON cta_runs(guild_id, status, closes_at);
        CREATE INDEX IF NOT EXISTS idx_cta_signups_cta ON cta_signups(cta_id);
        """)
        # Migración aditiva: ids de los mensajes del tablero en el hilo de cada CTA.
        db_ensure_column(conn, "cta_runs", "board_message_ids_json", "TEXT NOT NULL DEFAULT '[]'")
        db_ensure_column(conn, "cta_runs", "panel_extra_ids_json", "TEXT NOT NULL DEFAULT '[]'")
        db_ensure_column(conn, "cta_compositions", "builds_url", "TEXT")
        db_ensure_column(conn, "cta_compositions", "legacy_first", "INTEGER NOT NULL DEFAULT 0")
        # Rol creado al cerrar la CTA: NULL = pendiente, -1 = en proceso, 0 = no se pudo, otro = id del rol.
        db_ensure_column(conn, "cta_runs", "role_id", "INTEGER")
        # Composiciones «track»: al cerrar la inscripción generan un split (/transparencia) en vez de crear un rol.
        db_ensure_column(conn, "cta_compositions", "track", "INTEGER NOT NULL DEFAULT 0")
        db_ensure_column(conn, "cta_runs", "track", "INTEGER NOT NULL DEFAULT 0")
        db_ensure_column(conn, "cta_runs", "tab", "TEXT")
        db_ensure_column(conn, "cta_runs", "city", "TEXT")
        # Split de la CTA track: NULL = pendiente, -1 = en proceso, 0 = no se pudo crear, otro = no usado (el split guarda source_cta_id).
        db_ensure_column(conn, "cta_runs", "split_state", "INTEGER")
        # El ⚔️ era el emoji automático: ya no se agrega, así que se limpia de las posiciones existentes.
        conn.execute("UPDATE cta_slots SET emoji='' WHERE emoji='⚔️' OR emoji='⚔'")
        # Las restricciones por rol ahora son siempre «alcanza con tener el rol»: se unifica lo guardado.
        conn.execute("UPDATE cta_slots SET access_match='any' WHERE access_match<>'any'")
        conn.execute("UPDATE cta_party_access SET access_match='any' WHERE access_match<>'any'")
        conn.commit()


cta_db_init()


def cta_now_iso():
    return datetime.now(timezone.utc).isoformat()


def cta_audit(guild_id: int, actor_id: int, action: str, cta_id=None, details=""):
    db_execute("INSERT INTO cta_audit(guild_id, cta_id, actor_id, action, details, created_at) VALUES (?, ?, ?, ?, ?, ?)",
               (guild_id, cta_id, actor_id, action, str(details)[:1500], cta_now_iso()))


def cta_get_composition(guild_id: int, name: str):
    rows = db_fetchall("SELECT * FROM cta_compositions WHERE guild_id=? AND name=? COLLATE NOCASE", (guild_id, name.strip()))
    if not rows:
        return None
    comp = dict(rows[0])
    comp["slots"] = [dict(r) for r in db_fetchall("SELECT * FROM cta_slots WHERE composition_id=? ORDER BY party_no, position_no", (comp["id"],))]
    for slot in comp["slots"]:
        slot["role_ids"] = json.loads(slot.pop("role_ids_json", "[]"))
    return comp


def cta_ensure_brawl(guild_id: int, creator_id: int):
    if cta_get_composition(guild_id, "brawl"):
        return
    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        cur = conn.execute("INSERT OR IGNORE INTO cta_compositions(guild_id,name,created_by,updated_at) VALUES (?,?,?,?)",
                           (guild_id, "brawl", creator_id, cta_now_iso()))
        row = conn.execute("SELECT id FROM cta_compositions WHERE guild_id=? AND name='brawl' COLLATE NOCASE", (guild_id,)).fetchone()
        if not row:
            return
        cid = row[0]
        for pos, role_name in CTA_BRAWL_SLOTS:
            conn.execute("INSERT OR IGNORE INTO cta_slots(composition_id,party_no,position_no,role_name,emoji) VALUES (?,?,?,?,?)",
                         (cid, 1, pos, role_name, ""))
        conn.commit()


def cta_slot_key(slot: dict) -> str:
    return f"{int(slot['party_no'])}:{int(slot['position_no'])}"


def cta_run_get(run_id: int):
    rows = db_fetchall("SELECT * FROM cta_runs WHERE id=?", (run_id,))
    return dict(rows[0]) if rows else None


def cta_get_signups(run_id: int):
    return [dict(r) for r in db_fetchall("SELECT * FROM cta_signups WHERE cta_id=? ORDER BY signed_at", (run_id,))]


def cta_user_has_role(member: discord.Member, role_ids: list[int], mode: str) -> bool:
    member_roles = {r.id for r in member.roles}
    required = {int(x) for x in role_ids}
    if not required:
        return True
    return bool(required & member_roles)      # alcanza con tener uno de los roles pedidos


CTA_LEGACY_ROLE_ID = 1465833385804038427
CTA_LEGACY_MINUTES = 5


def cta_legacy_window_end(run: dict, snapshot: dict):
    """Fin de la ventana en que la Party 1 es solo para Legacy. None si la composición no tiene la opción."""
    if not snapshot.get("legacy_first"):
        return None
    return _cta_parse_dt(run["created_at"]) + timedelta(minutes=CTA_LEGACY_MINUTES)


cta_legacy_tasks: set = set()


async def cta_legacy_refresh_later(run_id: int, delay: float):
    """Cuando termina la ventana Legacy, actualiza el panel para sacar el aviso."""
    try:
        await asyncio.sleep(delay)
        run = cta_run_get(run_id)
        if run and run["status"] == "open":
            await cta_refresh_panel(bot, run_id)
    except Exception as exc:
        print(f"CTA #{run_id}: no pude actualizar el panel al terminar la ventana Legacy: {exc}")


def cta_legacy_error(member, slot: dict, snapshot: dict, run: dict, now: Optional[datetime] = None) -> Optional[str]:
    """Primeros minutos de la CTA: la Party 1 es solo para el rol Legacy (se suma a los demás permisos)."""
    end = cta_legacy_window_end(run, snapshot)
    if end is None or int(slot["party_no"]) != 1:
        return None
    now = now or datetime.now(timezone.utc)
    if now >= end:
        return None
    if any(r.id == CTA_LEGACY_ROLE_ID for r in getattr(member, "roles", [])):
        return None
    return (f"⏳ Durante los primeros {CTA_LEGACY_MINUTES} minutos la **Party 1** es solo para <@&{CTA_LEGACY_ROLE_ID}>. "
            f"Se abre para todos <t:{int(end.timestamp())}:R>. Mientras tanto podés anotarte en las otras parties.")


def cta_access_error(member: discord.Member, slot: dict, guild: discord.Guild) -> Optional[str]:
    if any(r.id == FLEX_ROLE_ID for r in member.roles):
        return None          # rol Flex: puede anotarse en lo que quiera
    party_restriction = slot.get("party_access") or {"mode": "any", "role_ids": [], "match": "any"}
    checks = [
        (party_restriction.get("mode", "any"), party_restriction.get("role_ids", []), party_restriction.get("match", "any"), "la party"),
        (slot.get("access_mode", "any"), slot.get("role_ids", []), slot.get("access_match", "any"), "esta posición"),
    ]
    for mode, role_ids, match, where in checks:
        if mode != "roles" or not role_ids:
            continue
        if not cta_user_has_role(member, role_ids, match):
            names = [guild.get_role(int(rid)).mention if guild.get_role(int(rid)) else f"rol eliminado ({rid})" for rid in role_ids]
            condition = "el rol" if len(names) == 1 else "uno de estos roles"
            return f"No podés anotarte en {where}: necesitás {condition} {' '.join(names)}."
    return None


CTA_BUILDS_URL_MAX = 500


def cta_clean_builds_url(text: Optional[str]) -> Optional[str]:
    """Valida el link de builds. Vacío -> None (quitar el link). Lanza ValueError si no sirve."""
    t = (text or "").strip().strip("<>").strip()
    if not t or t.casefold() in {"quitar", "borrar", "ninguno", "none", "-"}:
        return None
    if len(t) > CTA_BUILDS_URL_MAX:
        raise ValueError(f"El link es demasiado largo (máximo {CTA_BUILDS_URL_MAX} caracteres).")
    if re.search(r"\s", t) or not re.fullmatch(r"https?://[^\s/$.?#][^\s]*\.[^\s]+", t, re.I):
        raise ValueError("El link no es válido. Tiene que empezar con `https://` y no llevar espacios. Ej: `https://mi-web.com/builds`")
    return t.replace("(", "%28").replace(")", "%29")


def cta_snapshot_from_comp(comp: dict):
    party_rows = db_fetchall("SELECT party_no, access_mode, role_ids_json, access_match FROM cta_party_access WHERE composition_id=?", (comp["id"],))
    party_access = {int(r["party_no"]): {"mode": r["access_mode"], "role_ids": json.loads(r["role_ids_json"] or "[]"), "match": r["access_match"]} for r in party_rows}
    slots = []
    for s in comp["slots"]:
        slots.append({
            "party_no": int(s["party_no"]), "position_no": int(s["position_no"]),
            "role_name": s["role_name"], "emoji": s["emoji"],
            "access_mode": s.get("access_mode", "any"), "role_ids": s.get("role_ids", []),
            "access_match": s.get("access_match", "any"),
            "party_access": party_access.get(int(s["party_no"]), {"mode": "any", "role_ids": [], "match": "any"})
        })
    return {"name": comp["name"], "slots": slots, "party_access": party_access, "builds_url": comp.get("builds_url") or None,
            "legacy_first": bool(comp.get("legacy_first"))}


# ================== CTA: reglas, numeración global y tablero en el hilo ==================
CTA_MIN_MINUTES = 10
CTA_DEFAULT_MINUTES = 40
CTA_MAX_MINUTES = 23 * 60 + 59
CTA_MAX_SLOTS = 250
CTA_BOARD_CHAR_BUDGET = 5200      # el límite de Discord es 6000 caracteres de embeds por mensaje
CTA_NAME_RE = re.compile(r"[\w -]{2,40}", re.UNICODE)
CTA_LEAVE_WORDS = {"salir", "cancelar", "baja", "liberar", "-"}
CTA_NO_PINGS = discord.AllowedMentions.none()


def cta_duration_help() -> str:
    return (f"Usá `HH:MM` (por ejemplo `00:{CTA_DEFAULT_MINUTES}`) o solo minutos "
            f"(por ejemplo `{CTA_DEFAULT_MINUTES}`, `45m` o `1h30`). Mínimo {CTA_MIN_MINUTES} minutos.")


def cta_parse_duration(text: Optional[str]) -> int:
    """Convierte el texto del comando en minutos.

    Sin texto devuelve la duración predeterminada (40 min). Lanza ValueError con un mensaje
    listo para mostrar al usuario si el formato es inválido o si es menor al mínimo (10 min)."""
    if text is None or not str(text).strip():
        return CTA_DEFAULT_MINUTES
    t = str(text).strip().lower()
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", t)
    if m:
        hours, minutes = int(m.group(1)), int(m.group(2))
        if minutes > 59:
            raise ValueError("Los minutos van de 00 a 59. " + cta_duration_help())
    else:
        m = re.fullmatch(r"(?:(\d{1,2})\s*h(?:s|oras?)?)?\s*(?:(\d{1,4})\s*(?:m|min|mins|minutos?)?)?", t)
        if not m or not (m.group(1) or m.group(2)):
            raise ValueError("No entendí la duración. " + cta_duration_help())
        hours, minutes = int(m.group(1) or 0), int(m.group(2) or 0)
    total = hours * 60 + minutes
    if total < CTA_MIN_MINUTES:
        raise ValueError(f"La duración mínima es de {CTA_MIN_MINUTES} minutos. " + cta_duration_help())
    if total > CTA_MAX_MINUTES:
        raise ValueError("La duración máxima es de 23:59. " + cta_duration_help())
    return total


def cta_number_slots(slots) -> list:
    """Numeración global: devuelve [(número, slot)] correlativo entre todas las parties.

    Se calcula siempre a partir de la composición real, ordenando por party y posición, así que
    no hay huecos ni repetidos aunque las parties tengan cantidades distintas de posiciones."""
    ordered = sorted(slots, key=lambda s: (int(s["party_no"]), int(s["position_no"])))
    return [(i, s) for i, s in enumerate(ordered, start=1)]


def cta_slot_by_number(slots, number: int):
    for n, s in cta_number_slots(slots):
        if n == number:
            return s
    return None


def cta_number_of_slot_key(slots, slot_key: str) -> Optional[int]:
    for n, s in cta_number_slots(slots):
        if cta_slot_key(s) == slot_key:
            return n
    return None


def cta_parse_thread_command(text: Optional[str]):
    """Interpreta lo que se escribe en el hilo.

    `21` o `#21` -> ("join", 21) · `-21` -> ("leave", 21) · `salir` / `cancelar` / `-` -> ("leave", None)
    · otro texto -> None."""
    t = (text or "").strip().lower()
    if not t:
        return None
    m = re.fullmatch(r"#?\s*(\d{1,3})", t)
    if m:
        return ("join", int(m.group(1)))
    m = re.fullmatch(r"[-−–]\s*#?\s*(\d{1,3})", t)
    if m:
        return ("leave", int(m.group(1)))
    if t in CTA_LEAVE_WORDS:
        return ("leave", None)
    return None


def _cta_parse_dt(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@contextlib.contextmanager
def db_immediate():
    """Transacción SQLite con bloqueo de escritura inmediato (evita carreras entre inscripciones)."""
    conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()


def cta_try_signup(run_id: int, user_id: int, slot_key: str):
    """Anota o mueve a un jugador de forma atómica. Devuelve (código, dato).

    Códigos: closed · same · occupied(id del ocupante) · locked(clave actual) · moved(clave anterior) · joined."""
    with db_immediate() as conn:
        run = conn.execute("SELECT status, closes_at FROM cta_runs WHERE id=?", (run_id,)).fetchone()
        if not run or run[0] != "open" or _cta_parse_dt(run[1]) <= datetime.now(timezone.utc):
            return ("closed", None)
        existing = conn.execute("SELECT slot_key, assigned_by FROM cta_signups WHERE cta_id=? AND user_id=?",
                                (run_id, user_id)).fetchone()
        if existing and existing[0] == slot_key:
            return ("same", None)
        occupant = conn.execute("SELECT user_id FROM cta_signups WHERE cta_id=? AND slot_key=?",
                                (run_id, slot_key)).fetchone()
        if occupant:
            return ("occupied", int(occupant[0]))
        if existing:
            if existing[1] is not None:
                return ("locked", existing[0])
            conn.execute("UPDATE cta_signups SET slot_key=?, signed_at=? WHERE cta_id=? AND user_id=?",
                         (slot_key, cta_now_iso(), run_id, user_id))
            return ("moved", existing[0])
        conn.execute("INSERT INTO cta_signups(cta_id,slot_key,user_id,signed_at) VALUES (?,?,?,?)",
                     (run_id, slot_key, user_id, cta_now_iso()))
        return ("joined", None)


def cta_try_leave(run_id: int, user_id: int):
    """Libera la plaza del jugador. Devuelve (código, clave): closed · none · left."""
    with db_immediate() as conn:
        run = conn.execute("SELECT status, closes_at FROM cta_runs WHERE id=?", (run_id,)).fetchone()
        if not run or run[0] != "open" or _cta_parse_dt(run[1]) <= datetime.now(timezone.utc):
            return ("closed", None)
        existing = conn.execute("SELECT slot_key FROM cta_signups WHERE cta_id=? AND user_id=?",
                                (run_id, user_id)).fetchone()
        if not existing:
            return ("none", None)
        conn.execute("DELETE FROM cta_signups WHERE cta_id=? AND user_id=?", (run_id, user_id))
        return ("left", existing[0])


def cta_set_party_roles(composition_id: int, party_no: int, role_ids: list):
    """Rol general de una party: sin ese rol nadie puede anotarse en ninguna posición de la party
    (además de los roles propios de cada posición). Lista vacía = party abierta."""
    db_execute("INSERT INTO cta_party_access(composition_id,party_no,access_mode,role_ids_json,access_match) VALUES (?,?,?,?,'any') "
               "ON CONFLICT(composition_id,party_no) DO UPDATE SET access_mode=excluded.access_mode, role_ids_json=excluded.role_ids_json, access_match='any'",
               (composition_id, party_no, "roles" if role_ids else "any", json.dumps([str(r) for r in role_ids])))
    db_execute("UPDATE cta_compositions SET updated_at=? WHERE id=?", (cta_now_iso(), composition_id))


def cta_is_organizer(member) -> bool:
    """Staff / reclutadores / quien gestiona el servidor (mismo criterio que /cta_asignar)."""
    perms = getattr(member, "guild_permissions", None)
    return bool((perms is not None and perms.manage_guild) or any(r.id in {STAFF_ROLE_ID, RECRUITER_ROLE_ID} for r in member.roles))


def cta_staff_assign(run_id: int, target_id: int, slot_key: str, actor_id: int):
    """El Staff pone a un jugador en una posición (o lo mueve si ya estaba en otra). Devuelve (código, dato):
    closed · same · occupied(id del ocupante) · moved(clave anterior) · assigned."""
    with db_immediate() as conn:
        run = conn.execute("SELECT status, closes_at FROM cta_runs WHERE id=?", (run_id,)).fetchone()
        if not run or run[0] != "open" or _cta_parse_dt(run[1]) <= datetime.now(timezone.utc):
            return ("closed", None)
        existing = conn.execute("SELECT slot_key FROM cta_signups WHERE cta_id=? AND user_id=?", (run_id, target_id)).fetchone()
        if existing and existing[0] == slot_key:
            return ("same", None)
        occupant = conn.execute("SELECT user_id FROM cta_signups WHERE cta_id=? AND slot_key=?", (run_id, slot_key)).fetchone()
        if occupant:
            return ("occupied", int(occupant[0]))
        if existing:
            conn.execute("UPDATE cta_signups SET slot_key=?, assigned_by=?, signed_at=? WHERE cta_id=? AND user_id=?",
                         (slot_key, actor_id, cta_now_iso(), run_id, target_id))
            return ("moved", existing[0])
        conn.execute("INSERT INTO cta_signups(cta_id,slot_key,user_id,assigned_by,signed_at) VALUES (?,?,?,?,?)",
                     (run_id, slot_key, target_id, actor_id, cta_now_iso()))
        return ("assigned", None)


def cta_release_slot(run_id: int, slot_key: str, only_user: Optional[int] = None):
    """Libera una posición por su clave. Con only_user, solo si la ocupa esa persona.
    Devuelve (código, dato): closed · empty · other(id del ocupante) · removed(id del jugador sacado)."""
    with db_immediate() as conn:
        run = conn.execute("SELECT status, closes_at FROM cta_runs WHERE id=?", (run_id,)).fetchone()
        if not run or run[0] != "open" or _cta_parse_dt(run[1]) <= datetime.now(timezone.utc):
            return ("closed", None)
        occupant = conn.execute("SELECT user_id FROM cta_signups WHERE cta_id=? AND slot_key=?", (run_id, slot_key)).fetchone()
        if not occupant:
            return ("empty", None)
        uid = int(occupant[0])
        if only_user is not None and uid != only_user:
            return ("other", uid)
        conn.execute("DELETE FROM cta_signups WHERE cta_id=? AND slot_key=?", (run_id, slot_key))
        return ("removed", uid)


def cta_slot_restricted(slot: dict) -> bool:
    return (slot.get("access_mode") == "roles" and bool(slot.get("role_ids"))) or \
           ((slot.get("party_access") or {}).get("mode") == "roles" and bool((slot.get("party_access") or {}).get("role_ids")))


def cta_party_specs(slots, assigned: Optional[dict] = None, show_roles: bool = False, color=None) -> list:
    """Genera una lista de 'specs' (título + descripción) por party, con numeración global.

    Si `assigned` es un dict slot_key -> user_id se muestra quién ocupa cada posición."""
    by_party: dict = {}
    for n, s in cta_number_slots(slots):
        by_party.setdefault(int(s["party_no"]), []).append((n, s))
    specs = []
    for party_no in sorted(by_party):
        items = by_party[party_no]
        first, last = items[0][0], items[-1][0]
        lines = []
        pa = items[0][1].get("party_access") or {}
        if pa.get("mode") == "roles" and pa.get("role_ids"):
            roles = " ".join(f"<@&{r}>" for r in pa["role_ids"])
            lines.append(f"🛡️ Para anotarte en esta party necesitás: {roles}")
        for n, s in items:
            mark = " 🔒" if cta_slot_restricted(s) else ""
            extra = ""
            if show_roles and s.get("access_mode") == "roles" and s.get("role_ids"):
                extra = " · " + " ".join(f"<@&{r}>" for r in s["role_ids"])
            if assigned is not None:
                uid = assigned.get(cta_slot_key(s))
                who = f"<@{uid}>" if uid else "*libre*"
                lines.append(f"**{n}.** {cta_slot_label(s)} — {who}{mark}")
            else:
                lines.append(f"**{n}.** {cta_slot_label(s)}{mark}{extra}")
        label = f"PARTY {party_no} · posiciones {first}–{last}" if first != last else f"PARTY {party_no} · posición {first}"
        chunk, size, part = [], 0, 0
        for line in lines:
            if chunk and size + len(line) + 1 > 3800:
                part += 1
                specs.append({"title": label if part == 1 else f"{label} (cont.)",
                              "description": "\n".join(chunk), "color": color})
                chunk, size = [], 0
            chunk.append(line)
            size += len(line) + 1
        if chunk:
            part += 1
            specs.append({"title": label if part == 1 else f"{label} (cont.)",
                          "description": "\n".join(chunk), "color": color})
    return specs


def cta_spec_chars(spec: dict) -> int:
    return len(spec.get("title") or "") + len(spec.get("description") or "") + len(spec.get("footer") or "")


def cta_pack_specs(specs: list, budget: int = CTA_BOARD_CHAR_BUDGET) -> list:
    """Agrupa specs en mensajes respetando 10 embeds y el presupuesto de caracteres por mensaje."""
    groups, current, used = [], [], 0
    for spec in specs:
        size = cta_spec_chars(spec)
        if current and (len(current) >= 10 or used + size > budget):
            groups.append(current)
            current, used = [], 0
        current.append(spec)
        used += size
    if current:
        groups.append(current)
    return groups


def cta_spec_to_embed(spec: dict) -> discord.Embed:
    emb = discord.Embed(title=spec.get("title"), description=spec.get("description"),
                        color=spec.get("color") or discord.Color.blurple())
    if spec.get("footer"):
        emb.set_footer(text=spec["footer"])
    return emb


def cta_run_context(run: dict):
    """Devuelve (snapshot, {slot_key: user_id}) de una CTA."""
    snapshot = json.loads(run["snapshot_json"])
    assigned = {s["slot_key"]: int(s["user_id"]) for s in cta_get_signups(run["id"])}
    return snapshot, assigned


def cta_board_specs(run: dict) -> list:
    """Embeds del panel principal: encabezado (estado + contador de anotados) y una tarjeta por party."""
    snapshot, assigned = cta_run_context(run)
    is_open = run["status"] == "open"
    color = discord.Color.green() if is_open else discord.Color.dark_grey()
    total = len(snapshot["slots"])
    occupied = sum(1 for s in snapshot["slots"] if cta_slot_key(s) in assigned)
    if is_open:
        closes = _cta_parse_dt(run["closes_at"])
        thread_ref = f"<#{run['thread_id']}>" if run.get("thread_id") else "el hilo de esta CTA"
        status = (f"🟢 **Inscripciones abiertas** · cierran <t:{int(closes.timestamp())}:R>\n"
                  f"Para anotarte, escribí el número de tu posición en {thread_ref}.")
    else:
        status = "🔒 **Inscripciones cerradas** · esta es la composición final"
    builds_line = f"\n🔗 [Builds]({snapshot['builds_url']})" if snapshot.get("builds_url") else ""
    legacy_line = ""
    legacy_end = cta_legacy_window_end(run, snapshot) if is_open else None
    if legacy_end is not None and datetime.now(timezone.utc) < legacy_end:
        legacy_line = (f"\n⏳ **Party 1 solo para <@&{CTA_LEGACY_ROLE_ID}>** hasta las <t:{int(legacy_end.timestamp())}:t> "
                       f"(<t:{int(legacy_end.timestamp())}:R>)")
    header = {"title": f"⚔️ SMOGG · CTA {run['composition_name'].upper()} · #{run['id']}",
              "description": f"{status}\n👥 **Anotados: {occupied}/{total}**{builds_line}{legacy_line}", "color": color}
    specs = [header] + cta_party_specs(snapshot["slots"], assigned=assigned, color=color)
    specs[-1] = dict(specs[-1], footer=f"Anotados: {occupied}/{total} · CTA #{run['id']}")
    return specs


def cta_panel_groups(run: dict) -> list:
    """Divide los embeds del panel en mensajes (máx. 10 embeds y ~5200 caracteres cada uno)."""
    return [[cta_spec_to_embed(spec) for spec in group] for group in cta_pack_specs(cta_board_specs(run))]


def cta_instructions_text(run: dict, total: int) -> str:
    closes = _cta_parse_dt(run["closes_at"])
    return (
        f"📋 **Inscripción a la CTA {run['composition_name']} (#{run['id']})**\n"
        f"• Escribí **el número** de la posición que querés. Ejemplo: `21`.\n"
        f"• Hay posiciones del **1 al {total}** y los números siguen entre parties.\n"
        f"• Para cambiar de posición, escribí otro número libre.\n"
        f"• Staff: etiquetá a alguien y escribí el número (`@persona 21`) para anotarlo, o `@persona -21` para sacarlo.\n"
        f"• Para salir, escribí `-` y tu número (ejemplo: `-21`) o `salir`.\n"
        f"• Las posiciones con 🔒 piden ciertos roles.\n"
        + (f"• ⏳ Los primeros {CTA_LEGACY_MINUTES} minutos la **Party 1** es solo para el rol Legacy.\n"
           if json.loads(run["snapshot_json"]).get("legacy_first") else "") +
        f"• Las inscripciones cierran <t:{int(closes.timestamp())}:R>.\n"
        f"• La lista de parties y el contador de anotados están en el panel principal, arriba del hilo. Se actualiza sola.\n"
        f"• Al cerrar, todos los anotados reciben un rol nuevo de esta CTA."
    )


cta_board_tasks: dict = {}
cta_board_dirty: set = set()
cta_board_locks: dict = {}


async def cta_get_thread(run: dict):
    if not run.get("thread_id"):
        return None
    tid = int(run["thread_id"])
    guild = bot.get_guild(int(run["guild_id"]))
    thread = (guild.get_thread(tid) if guild else None) or bot.get_channel(tid)
    if thread is None:
        try:
            thread = await bot.fetch_channel(tid)
        except Exception:
            return None
    return thread


async def cta_refresh_board(run_id: int):
    """Mantiene la lista de parties en el CANAL: si no entra en el panel, continúa en mensajes siguientes
    (se editan, no se duplican). Además limpia el tablero viejo que antes vivía en el hilo."""
    lock = cta_board_locks.setdefault(run_id, asyncio.Lock())
    async with lock:
        run = cta_run_get(run_id)
        if not run:
            return
        # Tablero viejo en el hilo: se borra una sola vez. El [0] queda como marca de "hilo preparado".
        try:
            legacy = [int(x) for x in json.loads(run.get("board_message_ids_json") or "[]")]
        except Exception:
            legacy = []
        if legacy != [0]:
            thread = await cta_get_thread(run) if run.get("thread_id") else None
            for old_id in legacy:
                if old_id and thread is not None:
                    try:
                        await thread.get_partial_message(old_id).delete()
                    except Exception:
                        pass
            db_execute("UPDATE cta_runs SET board_message_ids_json='[0]' WHERE id=?", (run_id,))
        if not run.get("message_id"):
            return
        guild = bot.get_guild(int(run["guild_id"]))
        channel = (guild.get_channel(int(run["channel_id"])) if guild else None) or bot.get_channel(int(run["channel_id"]))
        if channel is None:
            return
        groups = cta_panel_groups(run)[1:]           # el primer grupo va en el panel principal
        try:
            ids = [int(x) for x in json.loads(run.get("panel_extra_ids_json") or "[]")]
        except Exception:
            ids = []
        new_ids = []
        for i, embeds in enumerate(groups):
            msg_id = None
            if i < len(ids):
                try:
                    await channel.get_partial_message(ids[i]).edit(content=None, embeds=embeds)
                    msg_id = ids[i]
                except discord.NotFound:
                    msg_id = None
            if msg_id is None:
                sent = await channel.send(embeds=embeds, allowed_mentions=CTA_NO_PINGS)
                msg_id = sent.id
            new_ids.append(msg_id)
        for old_id in ids[len(groups):]:
            try:
                await channel.get_partial_message(old_id).delete()
            except Exception:
                pass
        if new_ids != ids:
            db_execute("UPDATE cta_runs SET panel_extra_ids_json=? WHERE id=?", (json.dumps(new_ids), run_id))


async def cta_refresh_panel(bot_obj, run_id: int):
    """Actualiza el panel principal del canal: estado, contador, lista de parties y botones."""
    run = cta_run_get(run_id)
    if not run or not run.get("message_id"):
        return
    guild = bot_obj.get_guild(int(run["guild_id"]))
    if not guild:
        return
    channel = guild.get_channel(int(run["channel_id"]))
    if channel is None:
        try:
            channel = await bot_obj.fetch_channel(int(run["channel_id"]))
        except Exception:
            return
    thread_url = f"https://discord.com/channels/{run['guild_id']}/{run['thread_id']}" if run.get("thread_id") else None
    try:
        msg = await channel.fetch_message(int(run["message_id"]))
        await msg.edit(embeds=cta_panel_groups(run)[0],
                       view=CTAPanelView(run_id=int(run_id), closed=run["status"] != "open", thread_url=thread_url),
                       allowed_mentions=CTA_NO_PINGS)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
        print(f"CTA #{run_id}: no se pudo actualizar el panel: {exc}")


async def _cta_board_worker(run_id: int, delay: float):
    while True:
        await asyncio.sleep(delay)
        cta_board_dirty.discard(run_id)
        try:
            await cta_refresh_board(run_id)
            await cta_refresh_panel(bot, run_id)
        except Exception as exc:
            print(f"CTA #{run_id}: error actualizando tablero: {exc}")
        if run_id not in cta_board_dirty:
            break


def cta_request_board_refresh(run_id: int, delay: float = 1.5):
    """Pide actualizar tablero y panel. Agrupa varias inscripciones seguidas en una sola edición."""
    task = cta_board_tasks.get(run_id)
    if task is not None and not task.done():
        cta_board_dirty.add(run_id)
        return
    cta_board_tasks[run_id] = asyncio.create_task(_cta_board_worker(run_id, delay))


async def cta_close_run(run_id: int, reason="timeout"):
    run = cta_run_get(run_id)
    if not run or run["status"] != "open":
        return
    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        cur = conn.execute("UPDATE cta_runs SET status='closed', closed_at=? WHERE id=? AND status='open'",
                           (cta_now_iso(), run_id))
        changed = cur.rowcount
        conn.commit()
    if not changed:
        return
    cta_audit(int(run["guild_id"]), int(run["created_by"]), "cta_closed", run_id, reason)
    try:
        await cta_refresh_board(run_id)
    except Exception as exc:
        print(f"CTA #{run_id}: no se pudo actualizar el tablero final: {exc}")
    await cta_refresh_panel(bot, run_id)
    thread = await cta_get_thread(run)
    if thread is not None:
        try:
            await thread.send("🔒 Las inscripciones de esta CTA están cerradas. La composición final quedó en el panel principal.",
                              allowed_mentions=CTA_NO_PINGS)
        except Exception:
            pass
    if run.get("track"):
        # CTA con track: nunca se crea el rol (role_id=0 también evita que el reintento al reiniciar lo cree); se genera el split.
        db_execute("UPDATE cta_runs SET role_id=0 WHERE id=? AND role_id IS NULL", (run_id,))
        task = asyncio.create_task(cta_create_track_split(run_id))
    else:
        task = asyncio.create_task(cta_assign_signup_role(run_id))
    cta_role_tasks.add(task)
    task.add_done_callback(cta_role_tasks.discard)


cta_role_tasks: set = set()


async def cta_create_track_split(run_id: int):
    """Al cerrar una CTA con track: crea el split con la gente anotada (misma lógica que /transparencia).

    Se reclama de forma atómica (split_state -1) para que nunca se cree dos veces; el split además guarda
    source_cta_id, así que aunque se reintente no se duplica."""
    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        cur = conn.execute("UPDATE cta_runs SET split_state=-1 WHERE id=? AND status='closed' AND track=1 AND split_state IS NULL", (run_id,))
        claimed = cur.rowcount
        conn.commit()
    if not claimed:
        return
    run = cta_run_get(run_id)
    thread = await cta_get_thread(run)

    async def say(text: str):
        if thread is not None:
            try:
                await thread.send(text, allowed_mentions=CTA_NO_PINGS)
            except Exception:
                pass

    cog = bot.get_cog("SplitsCog")
    if cog is None:
        db_execute("UPDATE cta_runs SET split_state=NULL WHERE id=?", (run_id,))     # se reintenta al reconectar
        return
    user_ids = [int(r["user_id"]) for r in db_fetchall("SELECT user_id FROM cta_signups WHERE cta_id=? ORDER BY signed_at", (run_id,))]
    title = f"CTA {run['composition_name']} #{run_id}"
    try:
        split, msg, error = await cog.create_from_cta(cta_id=run_id, title=title, tab=run.get("tab"), city=run.get("city"),
                                                      user_ids=user_ids, creator_id=int(run["created_by"]))
    except Exception as exc:
        print(f"CTA #{run_id}: error generando el split: {exc!r}")
        split, msg, error = None, None, "❌ Falló la creación del split."
    if split is None:
        db_execute("UPDATE cta_runs SET split_state=0 WHERE id=?", (run_id,))
        await say(f"⚠️ No pude generar el split de esta CTA. {error or ''} Podés crearlo a mano con `/transparencia`.".strip())
        return
    db_execute("UPDATE cta_runs SET split_state=?  WHERE id=?", (int(split.id), run_id))
    cta_audit(int(run["guild_id"]), int(run["created_by"]), "cta_split_created", run_id, f"split={split.id}; anotados={len(user_ids)}")
    link = getattr(msg, "jump_url", None) if msg is not None else None
    await say(f"📊 Generé el split de esta CTA con **{len(user_ids)}** anotado(s)" + (f": {link}" if link else ".") +
              " No se creó ningún rol. Si te anotás ahora, el organizador decide si te suma al split.")


def cta_role_name(run: dict) -> str:
    return f"CTA {run['composition_name']} #{run['id']}"[:100]


async def cta_assign_signup_role(run_id: int):
    """Al cerrar la CTA crea un rol nuevo y se lo asigna a todos los jugadores anotados.

    Se reclama de forma atómica (role_id -1) para que nunca se cree dos veces el mismo rol."""
    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        cur = conn.execute("UPDATE cta_runs SET role_id=-1 WHERE id=? AND status='closed' AND role_id IS NULL", (run_id,))
        claimed = cur.rowcount
        conn.commit()
    if not claimed:
        return
    run = cta_run_get(run_id)
    thread = await cta_get_thread(run)

    async def say(text: str):
        if thread is not None:
            try:
                await thread.send(text, allowed_mentions=CTA_NO_PINGS)
            except Exception:
                pass

    user_ids = [int(r["user_id"]) for r in db_fetchall("SELECT user_id FROM cta_signups WHERE cta_id=? ORDER BY signed_at", (run_id,))]
    if not user_ids:
        db_execute("UPDATE cta_runs SET role_id=0 WHERE id=?", (run_id,))
        await say("No se anotó nadie, así que no se creó ningún rol.")
        return
    guild = bot.get_guild(int(run["guild_id"]))
    if guild is None:
        db_execute("UPDATE cta_runs SET role_id=NULL WHERE id=?", (run_id,))   # se reintenta al reconectar
        return
    try:
        role = await guild.create_role(name=cta_role_name(run), mentionable=False,
                                       reason=f"CTA #{run_id} cerrada: rol para los jugadores anotados")
    except discord.Forbidden:
        db_execute("UPDATE cta_runs SET role_id=0 WHERE id=?", (run_id,))
        await say("⚠️ No pude crear el rol de la CTA: me falta el permiso **Gestionar roles**.")
        return
    except discord.HTTPException as exc:
        db_execute("UPDATE cta_runs SET role_id=0 WHERE id=?", (run_id,))
        print(f"CTA #{run_id}: no pude crear el rol: {exc}")
        await say("⚠️ Discord no me dejó crear el rol de la CTA.")
        return
    db_execute("UPDATE cta_runs SET role_id=? WHERE id=?", (role.id, run_id))
    assigned, missing, failed = 0, 0, 0
    for uid in user_ids:
        member = guild.get_member(uid)
        if member is None:
            try:
                member = await guild.fetch_member(uid)
            except Exception:
                member = None
        if member is None:
            missing += 1
            continue
        try:
            await member.add_roles(role, reason=f"Anotado en la CTA #{run_id}")
            assigned += 1
        except discord.HTTPException as exc:
            failed += 1
            print(f"CTA #{run_id}: no pude dar el rol a {uid}: {exc}")
    cta_audit(int(run["guild_id"]), int(run["created_by"]), "cta_role_assigned", run_id, f"role={role.id}; assigned={assigned}; missing={missing}; failed={failed}")
    text = f"🎖️ Creé el rol {role.mention} y se lo asigné a **{assigned}** de {len(user_ids)} jugadores anotados."
    if missing:
        text += f" {missing} ya no están en el servidor."
    if failed:
        text += f" ⚠️ Fallé con {failed}: revisá que mi rol esté por encima del rol creado y que tenga **Gestionar roles**."
    await say(text)


async def cta_ensure_thread_and_board(run_id: int):
    """Recupera una CTA abierta tras un reinicio: hilo, instrucciones y tablero."""
    run = cta_run_get(run_id)
    if not run or run["status"] != "open":
        return
    if not run.get("thread_id") and run.get("message_id"):
        guild = bot.get_guild(int(run["guild_id"]))
        channel = guild.get_channel(int(run["channel_id"])) if guild else None
        if channel is not None and hasattr(channel, "get_partial_message"):
            try:
                thread = await channel.get_partial_message(int(run["message_id"])).create_thread(
                    name=f"CTA {run['composition_name']} #{run_id}"[:100], auto_archive_duration=1440)
                db_execute("UPDATE cta_runs SET thread_id=? WHERE id=?", (thread.id, run_id))
            except (discord.Forbidden, discord.HTTPException) as exc:
                print(f"CTA #{run_id}: no pude crear el hilo al recuperar: {exc}")
                return
        run = cta_run_get(run_id)
    if not run.get("thread_id"):
        return
    thread = await cta_get_thread(run)
    if thread is not None and hasattr(thread, "join"):
        try:
            await thread.join()      # asegura que el bot siga recibiendo los mensajes del hilo
        except discord.HTTPException:
            pass
    ids = json.loads(run.get("board_message_ids_json") or "[]")
    if not ids:
        thread = await cta_get_thread(run)
        if thread is not None:
            snapshot = json.loads(run["snapshot_json"])
            try:
                await thread.send(cta_instructions_text(run, len(snapshot["slots"])), allowed_mentions=CTA_NO_PINGS)
            except Exception as exc:
                print(f"CTA #{run_id}: no pude publicar las instrucciones: {exc}")
    await cta_refresh_board(run_id)
    await cta_refresh_panel(bot, run_id)


async def cta_reply_short(message: discord.Message, text: str, seconds: int = 12):
    """Respuesta breve que se borra sola, para no llenar el hilo."""
    try:
        await message.reply(text, mention_author=False, delete_after=seconds, allowed_mentions=CTA_NO_PINGS)
    except discord.HTTPException:
        try:
            await message.channel.send(text, delete_after=seconds, allowed_mentions=CTA_NO_PINGS)
        except discord.HTTPException:
            pass


async def cta_ack_ok(message: discord.Message, fallback_text: str):
    """Confirma con una reacción ✅ (sin mensajes extra). Si no se puede reaccionar, responde breve."""
    try:
        await message.add_reaction("✅")
    except discord.HTTPException:
        await cta_reply_short(message, fallback_text, 8)


async def cta_handle_thread_message(message: discord.Message) -> bool:
    """Procesa los mensajes escritos en el hilo de una CTA. Devuelve True si el hilo pertenece a una CTA."""
    rows = db_fetchall("SELECT * FROM cta_runs WHERE thread_id=?", (message.channel.id,))
    if not rows:
        return False
    run = dict(rows[0])
    if not isinstance(message.author, discord.Member):
        return True
    mentions = [m for m in message.mentions if not getattr(m, "bot", False) and m.id != message.author.id]
    command = cta_parse_thread_command(re.sub(r"<@!?\d+>", "", message.content or "") if mentions else message.content)
    if command is None:
        return True
    action, number = command
    if run["status"] != "open":
        if run.get("track") and action == "join" and not mentions and run["status"] == "closed":
            # CTA con track: quien se anota tarde queda como solicitud en el hilo del split (el organizador decide).
            cog = bot.get_cog("SplitsCog")
            state = None
            if cog is not None:
                try:
                    state = await cog.register_late_request(cta_id=int(run["id"]), user_id=message.author.id, slot_no=number)
                except Exception as exc:
                    print(f"CTA #{run['id']}: no pude registrar al anotado tardío: {exc!r}")
            texts = {
                "new": "🔒 Las inscripciones ya cerraron. Le avisé al organizador en el hilo del split: él decide si te suma.",
                "pending": "⏳ Ya avisé que querés sumarte; esperá la decisión del organizador.",
                "listed": "✅ Ya estás en la lista del split.",
                "accepted": "✅ Ya te sumaron al split.",
                "rejected": "🔒 El organizador ya decidió no sumarte a este split.",
            }
            await cta_reply_short(message, texts.get(state, "🔒 Las inscripciones de esta CTA ya están cerradas."), 12)
            return True
        await cta_reply_short(message, "🔒 Las inscripciones de esta CTA ya están cerradas.", 8)
        return True
    run_id = int(run["id"])
    member = message.author
    snapshot = json.loads(run["snapshot_json"])

    if mentions:                                   # el Staff etiqueta a alguien: «@persona 21» o «@persona -21»
        return await cta_handle_staff_command(message, run, snapshot, mentions, action, number)

    if action == "leave" and number is not None:   # «-21»: sacarte de la posición 21
        slot = cta_slot_by_number(snapshot["slots"], number)
        if slot is None:
            await cta_reply_short(message, f"La posición {number} no existe. Elegí un número del 1 al {len(snapshot['slots'])}.")
            return True
        organizer = cta_is_organizer(member)
        code, other = cta_release_slot(run_id, cta_slot_key(slot), only_user=None if organizer else member.id)
        if code == "closed":
            await cta_reply_short(message, "🔒 Las inscripciones de esta CTA ya están cerradas.", 8)
        elif code == "empty":
            await cta_reply_short(message, f"La posición {number} ya está libre.", 8)
        elif code == "other":
            await cta_reply_short(message, f"La posición {number} no es tuya, está ocupada por <@{other}>.")
        else:
            cta_audit(int(run["guild_id"]), member.id, "cancel_signup" if other == member.id else "staff_remove_signup",
                      run_id, f"{cta_slot_key(slot)}; member={other}")
            cta_request_board_refresh(run_id)
            await cta_ack_ok(message, f"Liberaste la posición {number}." if other == member.id
                             else f"Saqué a <@{other}> de la posición {number}.")
        return True

    if action == "leave":
        code, key = cta_try_leave(run_id, member.id)
        if code == "closed":
            await cta_reply_short(message, "🔒 Las inscripciones de esta CTA ya están cerradas.", 8)
        elif code == "none":
            await cta_reply_short(message, "No estás anotado en esta CTA.", 8)
        else:
            snapshot = json.loads(run["snapshot_json"])
            old_no = cta_number_of_slot_key(snapshot["slots"], key)
            cta_audit(int(run["guild_id"]), member.id, "cancel_signup", run_id, key)
            cta_request_board_refresh(run_id)
            await cta_ack_ok(message, f"Liberaste la posición {old_no}.")
        return True

    total = len(snapshot["slots"])
    slot = cta_slot_by_number(snapshot["slots"], number)
    if slot is None:
        await cta_reply_short(message, f"La posición {number} no existe. Elegí un número del 1 al {total}.")
        return True
    error = cta_legacy_error(member, slot, snapshot, run) or cta_access_error(member, slot, message.guild)
    if error:
        await cta_reply_short(message, error)
        return True
    try:
        code, extra = cta_try_signup(run_id, member.id, cta_slot_key(slot))
    except sqlite3.Error as exc:
        print(f"CTA #{run_id}: error guardando inscripción: {exc}")
        await cta_reply_short(message, "No pude guardar tu inscripción. Probá de nuevo en unos segundos.")
        return True
    role_label = f"{slot['role_name']} (Party {slot['party_no']})"
    if code == "closed":
        await cta_reply_short(message, "🔒 Las inscripciones de esta CTA ya están cerradas.", 8)
    elif code == "same":
        await cta_reply_short(message, f"Ya estás anotado en la posición {number}.", 8)
    elif code == "occupied":
        await cta_reply_short(message, f"La posición {number} ya está ocupada por <@{extra}>. Elegí otra.")
    elif code == "locked":
        await cta_reply_short(message, "Un organizador te asignó tu plaza. Pedile a él que la cambie.")
    elif code == "moved":
        old_no = cta_number_of_slot_key(snapshot["slots"], extra)
        cta_audit(int(run["guild_id"]), member.id, "move_signup", run_id, f"{extra}->{cta_slot_key(slot)}")
        cta_request_board_refresh(run_id)
        await cta_reply_short(message, f"Pasaste de la posición {old_no} a la {number} · {role_label}.", 10)
    else:
        cta_audit(int(run["guild_id"]), member.id, "signup", run_id, cta_slot_key(slot))
        cta_request_board_refresh(run_id)
        await cta_ack_ok(message, f"Quedaste anotado en la posición {number} · {role_label}.")
    return True


async def cta_handle_staff_command(message, run: dict, snapshot: dict, mentions: list, action: str, number):
    """Comandos del Staff en el hilo: «@persona 21» la pone en la posición 21 (o la mueve) y
    «@persona -21» (o «@persona -» / «@persona salir») la saca. El Staff puede asignar aunque la posición
    pida roles; queda anotado en la auditoría."""
    if not cta_is_organizer(message.author):
        await cta_reply_short(message, "Solo el Staff puede anotar o sacar a otras personas.", 8)
        return True
    if len(mentions) > 1:
        await cta_reply_short(message, "Etiquetá a una sola persona por mensaje.", 8)
        return True
    run_id, target = int(run["id"]), mentions[0]
    slots = snapshot["slots"]

    if action == "join":
        slot = cta_slot_by_number(slots, number)
        if slot is None:
            await cta_reply_short(message, f"La posición {number} no existe. Elegí un número del 1 al {len(slots)}.")
            return True
        code, extra = cta_staff_assign(run_id, target.id, cta_slot_key(slot), message.author.id)
        label = f"{slot['role_name']} (Party {slot['party_no']})"
        if code == "closed":
            await cta_reply_short(message, "🔒 Las inscripciones de esta CTA ya están cerradas.", 8)
        elif code == "same":
            await cta_reply_short(message, f"{target.mention} ya está en la posición {number}.", 8)
        elif code == "occupied":
            await cta_reply_short(message, f"La posición {number} ya está ocupada por <@{extra}>. Sacalo primero con `-{number}`.")
        else:
            restricted = ""
            if isinstance(target, discord.Member) and cta_access_error(target, slot, message.guild):
                restricted = "; override=sin los roles pedidos"
            cta_audit(int(run["guild_id"]), message.author.id, "manual_assign_thread" if code == "assigned" else "manual_move_thread",
                      run_id, f"member={target.id}; slot={cta_slot_key(slot)}{restricted}")
            cta_request_board_refresh(run_id)
            verb = "Pasé a" if code == "moved" else "Anoté a"
            await cta_ack_ok(message, f"{verb} {target.mention} en la posición {number} · {label}.")
        return True

    # action == "leave"
    if number is not None:
        slot = cta_slot_by_number(slots, number)
        if slot is None:
            await cta_reply_short(message, f"La posición {number} no existe. Elegí un número del 1 al {len(slots)}.")
            return True
        code, other = cta_release_slot(run_id, cta_slot_key(slot), only_user=target.id)
        if code == "closed":
            await cta_reply_short(message, "🔒 Las inscripciones de esta CTA ya están cerradas.", 8)
        elif code == "empty":
            await cta_reply_short(message, f"La posición {number} ya está libre.", 8)
        elif code == "other":
            await cta_reply_short(message, f"{target.mention} no está en la posición {number}; la ocupa <@{other}>.")
        else:
            cta_audit(int(run["guild_id"]), message.author.id, "staff_remove_signup", run_id, f"member={target.id}; slot={cta_slot_key(slot)}")
            cta_request_board_refresh(run_id)
            await cta_ack_ok(message, f"Saqué a {target.mention} de la posición {number}.")
        return True
    code, key = cta_try_leave(run_id, target.id)
    if code == "closed":
        await cta_reply_short(message, "🔒 Las inscripciones de esta CTA ya están cerradas.", 8)
    elif code == "none":
        await cta_reply_short(message, f"{target.mention} no está anotado en esta CTA.", 8)
    else:
        cta_audit(int(run["guild_id"]), message.author.id, "staff_remove_signup", run_id, f"member={target.id}; slot={key}")
        cta_request_board_refresh(run_id)
        await cta_ack_ok(message, f"Saqué a {target.mention} de la posición {cta_number_of_slot_key(slots, key)}.")
    return True


class CTAPanelView(discord.ui.View):
    """Panel del canal. La inscripción se hace escribiendo el número en el hilo; acá quedan el
    acceso al hilo y la opción de cancelar la plaza. Los botones viejos siguen funcionando."""

    def __init__(self, run_id: Optional[int] = None, closed: bool = False, thread_url: Optional[str] = None):
        super().__init__(timeout=None)
        self.run_id = run_id
        self.closed = closed
        if thread_url:
            self.add_item(discord.ui.Button(label="Ir al hilo para anotarte", emoji="💬",
                                            style=discord.ButtonStyle.link, url=thread_url))
        cancel = discord.ui.Button(label="Cancelar mi plaza", emoji="↩️", style=discord.ButtonStyle.secondary, custom_id="cta:cancel" if run_id is None else f"cta:cancel:{run_id}", disabled=closed)
        refresh = discord.ui.Button(label="Actualizar panel", emoji="🔄", style=discord.ButtonStyle.primary, custom_id="cta:refresh" if run_id is None else f"cta:refresh:{run_id}")
        cancel.callback = self.cancel_callback
        refresh.callback = self.refresh_callback
        self.add_item(cancel)
        self.add_item(refresh)

    def _resolve_run_id(self, interaction: discord.Interaction):
        if self.run_id:
            return self.run_id
        msg_id = interaction.message.id if interaction.message else None
        if not msg_id:
            return None
        rows = db_fetchall("SELECT id FROM cta_runs WHERE message_id=?", (msg_id,))
        return int(rows[0]["id"]) if rows else None

    async def cancel_callback(self, interaction: discord.Interaction):
        run_id = self._resolve_run_id(interaction)
        if not run_id:
            return await interaction.response.send_message("No encontré esta CTA.", ephemeral=True)
        code, key = cta_try_leave(run_id, interaction.user.id)
        if code == "closed":
            return await interaction.response.send_message("Esta CTA ya está cerrada.", ephemeral=True)
        if code == "none":
            return await interaction.response.send_message("No tenés una plaza en esta CTA.", ephemeral=True)
        run = cta_run_get(run_id)
        cta_audit(int(run["guild_id"]), interaction.user.id, "cancel_signup", run_id, key)
        await interaction.response.send_message("Tu plaza quedó libre.", ephemeral=True)
        cta_request_board_refresh(run_id)

    async def refresh_callback(self, interaction: discord.Interaction):
        run_id = self._resolve_run_id(interaction)
        if not run_id:
            return await interaction.response.send_message("No encontré esta CTA.", ephemeral=True)
        await interaction.response.send_message("Panel actualizado.", ephemeral=True)
        cta_request_board_refresh(run_id, delay=0.2)


class CTAAddSlotModal(discord.ui.Modal, title="Agregar posición"):
    party_no = discord.ui.TextInput(label="Número de party", placeholder="1", default="1", max_length=3)
    position_no = discord.ui.TextInput(label="Lugar dentro de la party", placeholder="1", max_length=3)
    role_name = discord.ui.TextInput(label="Nombre del rol o build", placeholder="Ej. Longbow", max_length=80)
    emoji = discord.ui.TextInput(label="Emoji (opcional)", placeholder="Vacío = sin emoji", default="", max_length=30, required=False)

    def __init__(self, composition_id: int, guild_id: int, actor_id: int, party_default: int = 1, position_default: int = 1):
        super().__init__()
        self.composition_id, self.guild_id, self.actor_id = composition_id, guild_id, actor_id
        self.party_no.default = str(party_default)
        self.position_no.default = str(position_default)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            party = int(str(self.party_no.value).strip())
            pos = int(str(self.position_no.value).strip())
            role = str(self.role_name.value).strip()
            emoji = str(self.emoji.value).strip()
            if party < 1 or pos < 1 or party > 25 or pos > 25:
                raise ValueError
        except ValueError:
            return await interaction.response.send_message("La party y el lugar tienen que ser números entre 1 y 25.", ephemeral=True)
        if not role:
            return await interaction.response.send_message("El nombre del rol no puede estar vacío.", ephemeral=True)
        count = db_fetchall("SELECT COUNT(*) AS n FROM cta_slots WHERE composition_id=? AND party_no=?", (self.composition_id, party))[0]["n"]
        if pos > count + 1:
            return await interaction.response.send_message(f"Para no dejar huecos, el próximo lugar de la Party {party} es el {count + 1}.", ephemeral=True)
        try:
            with db_immediate() as conn:
                # Si el lugar está ocupado, corre hacia abajo las posiciones siguientes de esa party.
                conn.execute("UPDATE cta_slots SET position_no=-(position_no+1) WHERE composition_id=? AND party_no=? AND position_no>=?", (self.composition_id, party, pos))
                conn.execute("UPDATE cta_slots SET position_no=-position_no WHERE composition_id=? AND party_no=? AND position_no<0", (self.composition_id, party))
                conn.execute("INSERT INTO cta_slots(composition_id,party_no,position_no,role_name,emoji) VALUES (?,?,?,?,?)", (self.composition_id, party, pos, role, emoji))
                conn.execute("UPDATE cta_compositions SET updated_at=? WHERE id=?", (cta_now_iso(), self.composition_id))
        except sqlite3.IntegrityError:
            return await interaction.response.send_message("No pude agregar la posición porque ese lugar ya existe. Probá con otro.", ephemeral=True)
        cta_audit(self.guild_id, self.actor_id, "composition_add_slot", details=f"comp={self.composition_id}; party={party}; pos={pos}; role={role}")
        await interaction.response.send_message(f"✅ Agregué **{role}** en la Party **{party}**. Los números se actualizaron solos.", ephemeral=True)


class CTAEditSlotModal(discord.ui.Modal, title="Editar posición"):
    role_name = discord.ui.TextInput(label="Nombre del rol/build", max_length=80)
    emoji = discord.ui.TextInput(label="Emoji", max_length=30, required=False)

    def __init__(self, composition_id: int, slot: dict, guild_id: int, actor_id: int):
        super().__init__()
        self.composition_id, self.slot, self.guild_id, self.actor_id = composition_id, slot, guild_id, actor_id
        self.role_name.default = slot["role_name"]
        self.emoji.default = slot["emoji"] or ""

    async def on_submit(self, interaction: discord.Interaction):
        role = str(self.role_name.value).strip()
        emoji = str(self.emoji.value).strip()
        if not role:
            return await interaction.response.send_message("El nombre del rol no puede quedar vacío.", ephemeral=True)
        db_execute("UPDATE cta_slots SET role_name=?, emoji=? WHERE id=? AND composition_id=?", (role, emoji, self.slot["id"], self.composition_id))
        db_execute("UPDATE cta_compositions SET updated_at=? WHERE id=?", (cta_now_iso(), self.composition_id))
        cta_audit(self.guild_id, self.actor_id, "composition_edit_slot", details=f"comp={self.composition_id}; slot={self.slot['id']}; role={role}")
        await interaction.response.send_message("✅ Posición actualizada.", ephemeral=True)


class CTAAccessRoleView(discord.ui.View):
    def __init__(self, composition_id: int, slot: dict, guild_id: int, actor_id: int):
        super().__init__(timeout=180)
        self.composition_id, self.slot, self.guild_id, self.actor_id = composition_id, slot, guild_id, actor_id
        self.add_item(CTAAccessRoleSelect(self))


class CTAAccessRoleSelect(discord.ui.RoleSelect):
    def __init__(self, parent_view: CTAAccessRoleView):
        super().__init__(placeholder="Rol necesario para anotarse (vacío = abierto a todos)", min_values=0, max_values=25)
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        role_ids = [str(r.id) for r in self.values]
        match = "any"
        db_execute("UPDATE cta_slots SET access_mode=?, role_ids_json=?, access_match=? WHERE id=?", ("roles" if role_ids else "any", json.dumps(role_ids), match, self.parent_view.slot["id"]))
        db_execute("UPDATE cta_compositions SET updated_at=? WHERE id=?", (cta_now_iso(), self.parent_view.composition_id))
        cta_audit(self.parent_view.guild_id, self.parent_view.actor_id, "composition_access", details=f"slot={self.parent_view.slot['id']}; roles={','.join(role_ids)}; match={match}")
        await interaction.response.send_message("✅ Restricción guardada. Solo podrán anotarse quienes tengan ese rol." if role_ids else "✅ Posición abierta para todos.", ephemeral=True)


class CTAPartyAccessView(discord.ui.View):
    def __init__(self, composition: dict, parties: list[int], actor_id: int, selected: Optional[int] = None):
        super().__init__(timeout=180)
        self.composition, self.parties, self.actor_id = composition, parties, actor_id
        self.selected_party = selected if selected in parties else parties[0]
        self.match = "any"
        party_select = discord.ui.Select(placeholder="Party", options=[discord.SelectOption(label=f"Party {p}", value=str(p), default=(p == self.selected_party)) for p in parties[:25]])
        party_select.callback = self.select_party
        self.role_select = discord.ui.RoleSelect(placeholder="Rol necesario para anotarse en esta party", min_values=0, max_values=25)
        self.role_select.callback = self.save_roles
        self.add_item(party_select); self.add_item(self.role_select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.actor_id:
            await interaction.response.send_message("Este editor pertenece a otro administrador.", ephemeral=True)
            return False
        return True

    async def select_party(self, interaction: discord.Interaction):
        self.selected_party = int(interaction.data["values"][0])
        await interaction.response.send_message(f"Elegiste la Party {self.selected_party}. Ahora elegí el rol necesario.", ephemeral=True)

    async def save_roles(self, interaction: discord.Interaction):
        role_ids = [str(r.id) for r in self.role_select.values]
        cta_set_party_roles(self.composition["id"], self.selected_party, role_ids)
        cta_audit(self.composition["guild_id"], self.actor_id, "composition_party_access", details=f"comp={self.composition['id']}; party={self.selected_party}; roles={','.join(role_ids)}; match={self.match}")
        await interaction.response.send_message(f"✅ Restricción guardada para la Party {self.selected_party}. {'Roles: ' + ', '.join(r.mention for r in self.role_select.values) if role_ids else 'La party queda abierta para todos.'}", ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


class CTAMoveSlotModal(discord.ui.Modal, title="Mover / reordenar posición"):
    party_no = discord.ui.TextInput(label="Party destino", placeholder="1", max_length=3)
    position_no = discord.ui.TextInput(label="Posición destino", placeholder="1", max_length=3)

    def __init__(self, composition: dict, slot: dict, actor_id: int):
        super().__init__()
        self.composition, self.slot, self.actor_id = composition, slot, actor_id
        self.party_no.default = str(slot["party_no"])
        self.position_no.default = str(slot["position_no"])

    async def on_submit(self, interaction: discord.Interaction):
        try:
            dest_party, dest_pos = int(str(self.party_no.value)), int(str(self.position_no.value))
            if dest_party < 1 or dest_pos < 1:
                raise ValueError
        except ValueError:
            return await interaction.response.send_message("La party y la posición tienen que ser números positivos.", ephemeral=True)
        comp_id = self.composition["id"]
        slot_id = self.slot["id"]
        with sqlite3.connect(DB_PATH, timeout=30) as conn:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("BEGIN IMMEDIATE")
            source_party = int(self.slot["party_no"])
            source_rows = conn.execute("SELECT id FROM cta_slots WHERE composition_id=? AND party_no=? ORDER BY position_no", (comp_id, source_party)).fetchall()
            dest_rows = conn.execute("SELECT id FROM cta_slots WHERE composition_id=? AND party_no=? ORDER BY position_no", (comp_id, dest_party)).fetchall()
            source_ids = [int(r[0]) for r in source_rows if int(r[0]) != int(slot_id)]
            dest_ids = [int(r[0]) for r in dest_rows if int(r[0]) != int(slot_id)]
            target_list = source_ids if source_party == dest_party else dest_ids
            if dest_pos > len(target_list) + 1:
                conn.rollback()
                return await interaction.response.send_message(f"La posición destino máxima es {len(target_list) + 1}.", ephemeral=True)
            if source_party == dest_party:
                target_list.insert(dest_pos - 1, int(slot_id))
                conn.execute("UPDATE cta_slots SET position_no=-id WHERE composition_id=? AND party_no=?", (comp_id, source_party))
                for new_pos, sid in enumerate(target_list, start=1):
                    conn.execute("UPDATE cta_slots SET position_no=?, party_no=? WHERE id=?", (new_pos, dest_party, sid))
            else:
                conn.execute("UPDATE cta_slots SET position_no=-id WHERE composition_id=? AND party_no IN (?,?)", (comp_id, source_party, dest_party))
                dest_ids.insert(dest_pos - 1, int(slot_id))
                conn.execute("UPDATE cta_slots SET party_no=?, position_no=-id WHERE id=?", (dest_party, slot_id))
                for new_pos, sid in enumerate(source_ids, start=1):
                    conn.execute("UPDATE cta_slots SET position_no=? WHERE id=?", (new_pos, sid))
                for new_pos, sid in enumerate(dest_ids, start=1):
                    conn.execute("UPDATE cta_slots SET party_no=?, position_no=? WHERE id=?", (dest_party, new_pos, sid))
            conn.execute("UPDATE cta_compositions SET updated_at=? WHERE id=?", (cta_now_iso(), comp_id))
            conn.commit()
        cta_audit(self.composition["guild_id"], self.actor_id, "composition_move_slot", details=f"slot={slot_id}; party={dest_party}; position={dest_pos}")
        await interaction.response.send_message(f"✅ Posición movida a Party {dest_party}, posición {dest_pos}.", ephemeral=True)


def cta_active_runs_for_composition(composition_id: int) -> list:
    rows = db_fetchall("SELECT id FROM cta_runs WHERE composition_id=? AND status='open'", (composition_id,))
    return [int(r["id"]) for r in rows]


def cta_active_notice(composition_id: int) -> str:
    runs = cta_active_runs_for_composition(composition_id)
    if not runs:
        return ""
    ids = ", ".join(f"#{r}" for r in runs[:5])
    return (f"\nℹ️ Hay {len(runs)} CTA activa(s) con esta composición ({ids}). Los cambios solo valen para "
            f"las CTA nuevas: las inscripciones actuales no se modifican.")


async def cta_send_spec_groups(interaction: discord.Interaction, header: str, specs: list):
    """Envía una composición como embeds efímeros, repartidos en varios mensajes si hace falta."""
    groups = cta_pack_specs(specs) or [[]]
    for index, group in enumerate(groups):
        embeds = [cta_spec_to_embed(spec) for spec in group]
        content = header if index == 0 else None
        if interaction.response.is_done():
            await interaction.followup.send(content=content, embeds=embeds, ephemeral=True, allowed_mentions=CTA_NO_PINGS)
        else:
            await interaction.response.send_message(content=content, embeds=embeds, ephemeral=True, allowed_mentions=CTA_NO_PINGS)


CTA_PARTY_MAX_SLOTS = 25


def cta_plan_party_edit(old_slots: list, new_lines: list) -> dict:
    """Decide qué pasa con cada posición al reescribir la lista completa de una party.
    old_slots: dicts con id, role_name, emoji, en orden. new_lines: textos nuevos, en orden.
    1) Una línea idéntica a una posición actual la conserva tal cual (con su emoji y restricciones de rol).
    2) Las líneas que quedan sin pareja se emparejan en orden con las posiciones que quedaron sin línea:
       es un renombre y conserva las restricciones de rol.
    3) Líneas de sobra = posiciones nuevas. Posiciones de sobra = eliminadas.
    Devuelve {"items": [("keep"|"rename"|"new", slot_id|None, texto)], "deleted": [ids]}"""
    by_label: dict = {}
    for s_ in old_slots:
        by_label.setdefault(cta_slot_label(s_), []).append(int(s_["id"]))
    items: list = []
    unmatched_idx = []
    used = set()
    for i, line in enumerate(new_lines):
        pool = by_label.get(line)
        if pool:
            sid = pool.pop(0); used.add(sid)
            items.append(["keep", sid, line])
        else:
            items.append(None); unmatched_idx.append(i)
    leftover_old = [int(s_["id"]) for s_ in old_slots if int(s_["id"]) not in used]
    for n, i in enumerate(unmatched_idx):
        if n < len(leftover_old):
            items[i] = ["rename", leftover_old[n], new_lines[i][:80]]
        else:
            items[i] = ["new", None, new_lines[i][:80]]
    deleted = leftover_old[len(unmatched_idx):]
    return {"items": [tuple(x) for x in items], "deleted": deleted}


def cta_apply_party_edit(composition_id: int, party: int, new_lines: list) -> dict:
    """Reescribe la party completa en una sola transacción. Devuelve los conteos del cambio."""
    with db_immediate() as conn:
        rows = conn.execute("SELECT id, role_name, emoji FROM cta_slots WHERE composition_id=? AND party_no=? ORDER BY position_no",
                            (composition_id, party)).fetchall()
        old = [{"id": r[0], "role_name": r[1], "emoji": r[2]} for r in rows]
        plan = cta_plan_party_edit(old, new_lines)
        conn.execute("UPDATE cta_slots SET position_no=-id WHERE composition_id=? AND party_no=?", (composition_id, party))
        for sid in plan["deleted"]:
            conn.execute("DELETE FROM cta_slots WHERE id=?", (sid,))
        final_ids = []
        for n, (kind, sid, text) in enumerate(plan["items"]):
            if kind == "rename":
                conn.execute("UPDATE cta_slots SET role_name=?, emoji='' WHERE id=?", (text, sid))
            elif kind == "new":
                sid = conn.execute("INSERT INTO cta_slots(composition_id,party_no,position_no,role_name,emoji) VALUES (?,?,?,?,'')",
                                   (composition_id, party, -(10 ** 6 + n), text)).lastrowid
            final_ids.append(sid)
        for pos, sid in enumerate(final_ids, start=1):
            conn.execute("UPDATE cta_slots SET position_no=? WHERE id=?", (pos, sid))
        conn.execute("UPDATE cta_compositions SET updated_at=? WHERE id=?", (cta_now_iso(), composition_id))
    kinds = [k for k, _, _ in plan["items"]]
    return {"keep": kinds.count("keep"), "rename": kinds.count("rename"), "new": kinds.count("new"), "deleted": len(plan["deleted"])}


class CTAEditPartyModal(discord.ui.Modal):
    lines = discord.ui.TextInput(label="Posiciones (una por línea)", style=discord.TextStyle.paragraph, max_length=4000)

    def __init__(self, composition_id: int, guild_id: int, actor_id: int, party: int, slots: list):
        super().__init__(title=f"Editar Party {party}"[:45])
        self.composition_id, self.guild_id, self.actor_id, self.party = composition_id, guild_id, actor_id, party
        self.lines.default = "\n".join(cta_slot_label(s_) for s_ in slots)[:4000]

    async def on_submit(self, interaction: discord.Interaction):
        names = [ln.strip() for ln in str(self.lines.value).splitlines() if ln.strip()]
        if not names:
            return await interaction.response.send_message("La lista quedó vacía. Para sacar posiciones borrá solo esas líneas (o usá «Eliminar posición»).", ephemeral=True)
        if len(names) > CTA_PARTY_MAX_SLOTS:
            return await interaction.response.send_message(f"Una party admite hasta {CTA_PARTY_MAX_SLOTS} posiciones y escribiste {len(names)}.", ephemeral=True)
        try:
            res = cta_apply_party_edit(self.composition_id, self.party, names)
        except sqlite3.Error as exc:
            print(f"compo: no pude reescribir la party: {exc}")
            return await interaction.response.send_message("❌ No pude guardar los cambios. No se modificó nada, probá de nuevo.", ephemeral=True)
        cta_audit(self.guild_id, self.actor_id, "composition_edit_party", details=f"comp={self.composition_id}; party={self.party}; {res}")
        parts = [f"{res['keep']} sin cambios"]
        if res["rename"]: parts.append(f"{res['rename']} renombrada(s)")
        if res["new"]: parts.append(f"{res['new']} nueva(s)")
        if res["deleted"]: parts.append(f"{res['deleted']} eliminada(s)")
        await interaction.response.send_message(
            f"✅ Party {self.party} actualizada: " + ", ".join(parts) + ". Los números se actualizaron solos.\n"
            "Las posiciones que solo renombraste mantienen sus roles permitidos; las nuevas quedan abiertas."
            + cta_active_notice(self.composition_id), ephemeral=True)


class CTABulkAddModal(discord.ui.Modal, title="Agregar varias posiciones"):
    party_no = discord.ui.TextInput(label="Party", placeholder="1", default="1", max_length=3)
    lines = discord.ui.TextInput(label="Una posición por línea", style=discord.TextStyle.paragraph,
                                 placeholder="Longbow\nHallow Fall\n🛡️ Tank", max_length=3000)

    def __init__(self, composition_id: int, guild_id: int, actor_id: int, party_default: int = 1):
        super().__init__()
        self.composition_id, self.guild_id, self.actor_id = composition_id, guild_id, actor_id
        self.party_no.default = str(party_default)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            party = int(str(self.party_no.value).strip())
            if not 1 <= party <= 25:
                raise ValueError
        except ValueError:
            return await interaction.response.send_message("La party tiene que ser un número entre 1 y 25.", ephemeral=True)
        names = [ln.strip() for ln in str(self.lines.value).splitlines() if ln.strip()]
        if not names:
            return await interaction.response.send_message("Escribí al menos una posición.", ephemeral=True)
        with db_immediate() as conn:
            count = conn.execute("SELECT COUNT(*) FROM cta_slots WHERE composition_id=? AND party_no=?",
                                 (self.composition_id, party)).fetchone()[0]
            if count + len(names) > 25:
                return await interaction.response.send_message(
                    f"Una party admite hasta 25 posiciones. La Party {party} tiene {count} y querés agregar {len(names)}.", ephemeral=True)
            for offset, name in enumerate(names, start=1):
                role = name[:80]
                conn.execute("INSERT INTO cta_slots(composition_id,party_no,position_no,role_name,emoji) VALUES (?,?,?,?,?)",
                             (self.composition_id, party, count + offset, role, ""))
            conn.execute("UPDATE cta_compositions SET updated_at=? WHERE id=?", (cta_now_iso(), self.composition_id))
        cta_audit(self.guild_id, self.actor_id, "composition_bulk_add", details=f"comp={self.composition_id}; party={party}; n={len(names)}")
        await interaction.response.send_message(f"✅ Agregué {len(names)} posiciones a la Party {party}.", ephemeral=True)


class CTACompositionEditorView(discord.ui.View):
    def __init__(self, composition: dict, actor_id: int, party_filter: Optional[int] = None):
        super().__init__(timeout=300)
        self.composition = composition
        self.actor_id = actor_id
        self.party_filter = party_filter
        parties = sorted({int(s["party_no"]) for s in composition["slots"]})
        if parties and party_filter is None:
            self.party_filter = parties[0]
        if len(parties) > 1:
            pselect = discord.ui.Select(placeholder="Elegí una party para editar", options=[discord.SelectOption(label=f"Party {p}", value=str(p), default=(p == self.party_filter)) for p in parties[:25]])
            pselect.callback = self.select_party
            self.add_item(pselect)
        numbering = {s["id"]: n for n, s in cta_number_slots(composition["slots"])}
        visible_slots = [s for s in composition["slots"] if self.party_filter is None or int(s["party_no"]) == int(self.party_filter)]
        self.selected_slot_id = visible_slots[0]["id"] if visible_slots else None
        options = [discord.SelectOption(label=f"#{numbering[s['id']]} · P{s['party_no']} · {s['role_name']}"[:100], value=str(s["id"]), default=(s["id"] == self.selected_slot_id)) for s in visible_slots[:25]]
        if options:
            select = discord.ui.Select(placeholder="Elegí una posición para editar", options=options)
            select.callback = self.select_slot
            self.add_item(select)
        add = discord.ui.Button(label="Agregar posición", style=discord.ButtonStyle.success, emoji="➕")
        bulk = discord.ui.Button(label="Agregar varias", style=discord.ButtonStyle.success, emoji="📋")
        edit_party = discord.ui.Button(label="Editar party completa", style=discord.ButtonStyle.primary, emoji="📝", disabled=not options)
        edit = discord.ui.Button(label="Editar nombre/icono", style=discord.ButtonStyle.primary, emoji="✏️", disabled=not options)
        access = discord.ui.Button(label="Restringir por roles", style=discord.ButtonStyle.secondary, emoji="🔒", disabled=not options)
        delete = discord.ui.Button(label="Eliminar posición", style=discord.ButtonStyle.danger, emoji="🗑️", disabled=not options)
        preview = discord.ui.Button(label="Ver composición", style=discord.ButtonStyle.secondary, emoji="👁️")
        party_access = discord.ui.Button(label="Restringir party", style=discord.ButtonStyle.secondary, emoji="🛡️")
        move = discord.ui.Button(label="Mover / reordenar", style=discord.ButtonStyle.primary, emoji="↕️", disabled=not options)
        add.callback = self.add_slot
        bulk.callback = self.bulk_add
        edit_party.callback = self.edit_party
        edit.callback = self.edit_slot
        access.callback = self.access_slot
        delete.callback = self.delete_slot
        preview.callback = self.preview
        party_access.callback = self.configure_party_access
        move.callback = self.move_slot
        for item in (add, bulk, edit_party, edit, access, delete, preview, party_access, move):
            self.add_item(item)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.actor_id:
            await interaction.response.send_message("Este editor lo abrió otra persona.", ephemeral=True)
            return False
        if not interaction.user.guild_permissions.manage_guild and not any(r.id == STAFF_ROLE_ID for r in getattr(interaction.user, "roles", [])):
            await interaction.response.send_message("No tenés permisos para editar composiciones.", ephemeral=True)
            return False
        return True

    async def select_party(self, interaction: discord.Interaction):
        party = int(interaction.data["values"][0])
        fresh = cta_get_composition(int(self.composition["guild_id"]), self.composition["name"])
        await interaction.response.edit_message(content=f"**Editor de composición: {fresh['name']} · Party {party}**\nElegí una posición y después una acción.", view=CTACompositionEditorView(fresh, self.actor_id, party_filter=party))

    async def select_slot(self, interaction: discord.Interaction):
        self.selected_slot_id = int(interaction.data["values"][0])
        await interaction.response.defer()

    def _current_slot(self):
        fresh = cta_get_composition(int(self.composition["guild_id"]), self.composition["name"])
        return next((s for s in fresh["slots"] if s["id"] == self.selected_slot_id), None) if fresh else None

    async def add_slot(self, interaction: discord.Interaction):
        party = int(self.party_filter or 1)
        count = db_fetchall("SELECT COUNT(*) AS n FROM cta_slots WHERE composition_id=? AND party_no=?", (self.composition["id"], party))[0]["n"]
        await interaction.response.send_modal(CTAAddSlotModal(self.composition["id"], self.composition["guild_id"], interaction.user.id, party_default=party, position_default=int(count) + 1))

    async def bulk_add(self, interaction: discord.Interaction):
        await interaction.response.send_modal(CTABulkAddModal(self.composition["id"], self.composition["guild_id"], interaction.user.id, party_default=int(self.party_filter or 1)))

    async def edit_party(self, interaction: discord.Interaction):
        party = int(self.party_filter or 1)
        fresh = cta_get_composition(int(self.composition["guild_id"]), self.composition["name"])
        slots = [s_ for s_ in (fresh["slots"] if fresh else []) if int(s_["party_no"]) == party]
        slots.sort(key=lambda s_: int(s_["position_no"]))
        if not slots:
            return await interaction.response.send_message("Esa party todavía no tiene posiciones. Usá «Agregar varias».", ephemeral=True)
        await interaction.response.send_modal(CTAEditPartyModal(self.composition["id"], self.composition["guild_id"], interaction.user.id, party, slots))

    async def edit_slot(self, interaction: discord.Interaction):
        slot = self._current_slot()
        if not slot:
            return await interaction.response.send_message("Elegí primero una posición.", ephemeral=True)
        await interaction.response.send_modal(CTAEditSlotModal(self.composition["id"], slot, self.composition["guild_id"], interaction.user.id))

    async def access_slot(self, interaction: discord.Interaction):
        slot = self._current_slot()
        if not slot:
            return await interaction.response.send_message("Elegí primero una posición.", ephemeral=True)
        await interaction.response.send_message(f"Elegí los roles permitidos para **P{slot['party_no']} · {slot['role_name']}**. Si no elegís ninguno, la posición queda abierta para todos.", view=CTAAccessRoleView(self.composition["id"], slot, self.composition["guild_id"], interaction.user.id), ephemeral=True)

    async def move_slot(self, interaction: discord.Interaction):
        slot = self._current_slot()
        if not slot:
            return await interaction.response.send_message("Elegí primero una posición.", ephemeral=True)
        await interaction.response.send_modal(CTAMoveSlotModal(self.composition, slot, interaction.user.id))

    async def delete_slot(self, interaction: discord.Interaction):
        slot = self._current_slot()
        if not slot:
            return await interaction.response.send_message("Elegí primero una posición.", ephemeral=True)
        view = CTAConfirmDeleteSlotView(self.composition, slot, interaction.user.id)
        await interaction.response.send_message(f"¿Eliminar **P{slot['party_no']} · {slot['role_name']}**? Las posiciones siguientes de esa party se renumeran.", view=view, ephemeral=True)

    async def configure_party_access(self, interaction: discord.Interaction):
        fresh = cta_get_composition(int(self.composition["guild_id"]), self.composition["name"])
        parties = sorted({int(s["party_no"]) for s in fresh["slots"]}) if fresh else []
        if not parties:
            return await interaction.response.send_message("Agregá al menos una posición antes de restringir una party.", ephemeral=True)
        await interaction.response.send_message(f"Elegí el **rol general** de la **Party {self.party_filter}** (o cambiá de party en el primer menú). Sin ese rol nadie se puede anotar en la party; las posiciones con su propio rol lo siguen pidiendo además. Sin elegir ningún rol, la party queda abierta.", view=CTAPartyAccessView(self.composition, parties, interaction.user.id, selected=self.party_filter), ephemeral=True)

    async def preview(self, interaction: discord.Interaction):
        comp = cta_get_composition(self.composition["guild_id"], self.composition["name"])
        if not comp:
            return await interaction.response.send_message("No encontré la composición.", ephemeral=True)
        specs = cta_party_specs(cta_snapshot_from_comp(comp)["slots"], show_roles=True)
        await cta_send_spec_groups(interaction, f"**Composición: {comp['name']}** · {len(comp['slots'])} posiciones" if comp["slots"] else f"**{comp['name']}** todavía no tiene posiciones.", specs)


class CTAConfirmDeleteSlotView(discord.ui.View):
    def __init__(self, composition: dict, slot: dict, actor_id: int):
        super().__init__(timeout=60)
        self.composition, self.slot, self.actor_id = composition, slot, actor_id
        yes = discord.ui.Button(label="Confirmar eliminación", style=discord.ButtonStyle.danger)
        no = discord.ui.Button(label="Cancelar", style=discord.ButtonStyle.secondary)
        yes.callback = self.confirm
        no.callback = self.cancel
        self.add_item(yes); self.add_item(no)

    async def confirm(self, interaction: discord.Interaction):
        if interaction.user.id != self.actor_id:
            return await interaction.response.send_message("Solo quien abrió la confirmación puede usarla.", ephemeral=True)
        with db_immediate() as conn:
            conn.execute("DELETE FROM cta_slots WHERE id=? AND composition_id=?", (self.slot["id"], self.composition["id"]))
            rows = conn.execute("SELECT id FROM cta_slots WHERE composition_id=? AND party_no=? ORDER BY position_no", (self.composition["id"], self.slot["party_no"])).fetchall()
            for new_pos, (sid,) in enumerate(rows, start=1):
                conn.execute("UPDATE cta_slots SET position_no=? WHERE id=?", (new_pos, sid))
            conn.execute("UPDATE cta_compositions SET updated_at=? WHERE id=?", (cta_now_iso(), self.composition["id"]))
        cta_audit(self.composition["guild_id"], interaction.user.id, "composition_delete_slot", details=f"slot={self.slot['id']}")
        await interaction.response.edit_message(content="✅ Posición eliminada. La numeración se actualizó sola.", view=None)

    async def cancel(self, interaction: discord.Interaction):
        if interaction.user.id != self.actor_id:
            return await interaction.response.send_message("Solo quien abrió la confirmación puede usarla.", ephemeral=True)
        await interaction.response.edit_message(content="Operación cancelada.", view=None)


def cta_valid_composition_name(name: str) -> bool:
    return bool(CTA_NAME_RE.fullmatch(name or ""))


class CTACompositionGroup(app_commands.Group):
    def __init__(self):
        super().__init__(name="compo", description="Administrar composiciones de Smogg")

    async def _admin_check(self, interaction: discord.Interaction):
        member = interaction.user
        return isinstance(member, discord.Member) and (member.guild_permissions.manage_guild or any(r.id == STAFF_ROLE_ID for r in member.roles))

    @app_commands.command(name="crear", description="Crear una composición vacía")
    @app_commands.describe(nombre="Nombre único de la composición")
    async def crear(self, interaction: discord.Interaction, nombre: str):
        if not await self._admin_check(interaction):
            return await interaction.response.send_message("Solo el Staff o los administradores pueden crear composiciones.", ephemeral=True)
        name = nombre.strip()
        if not cta_valid_composition_name(name):
            return await interaction.response.send_message("Usá un nombre de 2 a 40 caracteres: letras, números, espacios, guiones o guion bajo.", ephemeral=True)
        try:
            db_execute("INSERT INTO cta_compositions(guild_id,name,created_by,updated_at) VALUES (?,?,?,?)", (interaction.guild_id, name, interaction.user.id, cta_now_iso()))
        except sqlite3.IntegrityError:
            return await interaction.response.send_message("Ya existe una composición con ese nombre.", ephemeral=True)
        cta_audit(interaction.guild_id, interaction.user.id, "composition_create", details=name)
        await interaction.response.send_message(f"✅ Composición **{name}** creada. Agregale posiciones con `/compo editar`.", ephemeral=True)

    @app_commands.command(name="listar", description="Listar composiciones guardadas")
    async def listar(self, interaction: discord.Interaction):
        rows = db_fetchall("""SELECT c.name, c.track, COUNT(s.id) AS n, COUNT(DISTINCT s.party_no) AS p
                              FROM cta_compositions c LEFT JOIN cta_slots s ON s.composition_id=c.id
                              WHERE c.guild_id=? GROUP BY c.id ORDER BY c.name COLLATE NOCASE""", (interaction.guild_id,))
        lines = [f"• **{r['name']}** · {r['p']} parties · {r['n']} posiciones" + (" · 📊 track" if r["track"] else "") for r in rows]
        await interaction.response.send_message("**Composiciones guardadas**\n" + ("\n".join(lines) if lines else "Todavía no hay composiciones."), ephemeral=True)

    @app_commands.command(name="ver", description="Ver una composición guardada")
    @app_commands.describe(nombre="Nombre de la composición")
    async def ver(self, interaction: discord.Interaction, nombre: str):
        comp = cta_get_composition(interaction.guild_id, nombre)
        if not comp:
            return await interaction.response.send_message("No existe esa composición. Mirá las guardadas con `/compo listar`.", ephemeral=True)
        if not comp["slots"]:
            return await interaction.response.send_message(f"**{comp['name']}** todavía no tiene posiciones.", ephemeral=True)
        specs = cta_party_specs(cta_snapshot_from_comp(comp)["slots"], show_roles=True)
        builds = f"\n🔗 [Builds]({comp['builds_url']})" if comp.get("builds_url") else ""
        legacy = f"\n⏳ Prioridad Legacy: la Party 1 es solo para el rol Legacy los primeros {CTA_LEGACY_MINUTES} min de la CTA." if comp.get("legacy_first") else ""
        await cta_send_spec_groups(interaction, f"**Composición: {comp['name']}** · {len(comp['slots'])} posiciones{builds}{legacy}", specs)

    @app_commands.command(name="editar", description="Abrir el editor interactivo de una composición")
    @app_commands.describe(nombre="Nombre de la composición")
    async def editar(self, interaction: discord.Interaction, nombre: str):
        if not await self._admin_check(interaction):
            return await interaction.response.send_message("No tenés permisos para editar composiciones.", ephemeral=True)
        comp = cta_get_composition(interaction.guild_id, nombre)
        if not comp:
            return await interaction.response.send_message("No existe esa composición. Podés crearla con `/compo crear`.", ephemeral=True)
        await interaction.response.send_message(f"**Editor de composición: {comp['name']}**\nElegí una posición y después una acción. Los cambios se guardan al instante." + cta_active_notice(comp["id"]), view=CTACompositionEditorView(comp, interaction.user.id), ephemeral=True)

    @app_commands.command(name="duplicar", description="Duplicar una composición")
    @app_commands.describe(nombre="Composición original", nuevo_nombre="Nombre de la copia")
    async def duplicar(self, interaction: discord.Interaction, nombre: str, nuevo_nombre: str):
        if not await self._admin_check(interaction):
            return await interaction.response.send_message("No tenés permisos para duplicar composiciones.", ephemeral=True)
        comp = cta_get_composition(interaction.guild_id, nombre)
        if not comp:
            return await interaction.response.send_message("No existe la composición original.", ephemeral=True)
        new_name = nuevo_nombre.strip()
        if not cta_valid_composition_name(new_name):
            return await interaction.response.send_message("Usá un nombre de 2 a 40 caracteres: letras, números, espacios, guiones o guion bajo.", ephemeral=True)
        try:
            with db_immediate() as conn:
                cur = conn.execute("INSERT INTO cta_compositions(guild_id,name,created_by,updated_at,builds_url,legacy_first) VALUES (?,?,?,?,?,?)", (interaction.guild_id, new_name, interaction.user.id, cta_now_iso(), comp.get("builds_url"), int(bool(comp.get("legacy_first")))))
                new_id = cur.lastrowid
                for s in comp["slots"]:
                    conn.execute("INSERT INTO cta_slots(composition_id,party_no,position_no,role_name,emoji,access_mode,role_ids_json,access_match) VALUES (?,?,?,?,?,?,?,?)", (new_id, s["party_no"], s["position_no"], s["role_name"], s["emoji"], s["access_mode"], json.dumps(s["role_ids"]), s["access_match"]))
                conn.execute("""INSERT INTO cta_party_access(composition_id,party_no,access_mode,role_ids_json,access_match)
                                SELECT ?, party_no, access_mode, role_ids_json, access_match FROM cta_party_access WHERE composition_id=?""", (new_id, comp["id"]))
        except sqlite3.IntegrityError:
            return await interaction.response.send_message("Ya existe una composición con ese nombre.", ephemeral=True)
        cta_audit(interaction.guild_id, interaction.user.id, "composition_duplicate", details=f"{comp['name']} -> {new_name}")
        await interaction.response.send_message(f"✅ **{comp['name']}** se duplicó como **{new_name}** (con sus restricciones de roles).", ephemeral=True)

    @app_commands.command(name="renombrar", description="Cambiar el nombre de una composición")
    @app_commands.describe(nombre="Nombre actual", nuevo_nombre="Nombre nuevo")
    async def renombrar(self, interaction: discord.Interaction, nombre: str, nuevo_nombre: str):
        if not await self._admin_check(interaction):
            return await interaction.response.send_message("No tenés permisos para renombrar composiciones.", ephemeral=True)
        comp = cta_get_composition(interaction.guild_id, nombre)
        if not comp:
            return await interaction.response.send_message("No existe esa composición.", ephemeral=True)
        new_name = nuevo_nombre.strip()
        if not cta_valid_composition_name(new_name):
            return await interaction.response.send_message("Usá un nombre de 2 a 40 caracteres: letras, números, espacios, guiones o guion bajo.", ephemeral=True)
        try:
            db_execute("UPDATE cta_compositions SET name=?, updated_at=? WHERE id=?", (new_name, cta_now_iso(), comp["id"]))
        except sqlite3.IntegrityError:
            return await interaction.response.send_message("Ya existe una composición con ese nombre.", ephemeral=True)
        cta_audit(interaction.guild_id, interaction.user.id, "composition_rename", details=f"{comp['name']} -> {new_name}")
        await interaction.response.send_message(f"✅ **{comp['name']}** ahora se llama **{new_name}**. Las CTA ya publicadas conservan su nombre.", ephemeral=True)

    @app_commands.command(name="rol_party", description="Rol general de una party: sin ese rol no se puede anotar nadie en ella")
    @app_commands.describe(nombre="Nombre de la composición", party="Número de party", rol="Rol necesario (vacío = quitar la restricción)")
    async def rol_party(self, interaction: discord.Interaction, nombre: str, party: app_commands.Range[int, 1, 25], rol: Optional[discord.Role] = None):
        if not await self._admin_check(interaction):
            return await interaction.response.send_message("No tenés permisos para editar composiciones.", ephemeral=True)
        comp = cta_get_composition(interaction.guild_id, nombre)
        if not comp:
            return await interaction.response.send_message("No existe esa composición. Mirá las guardadas con `/compo listar`.", ephemeral=True)
        if party not in {int(s["party_no"]) for s in comp["slots"]}:
            return await interaction.response.send_message(f"La composición **{comp['name']}** no tiene una Party {party}.", ephemeral=True)
        cta_set_party_roles(comp["id"], party, [rol.id] if rol else [])
        cta_audit(interaction.guild_id, interaction.user.id, "composition_party_access", details=f"comp={comp['id']}; party={party}; roles={rol.id if rol else ''}")
        if rol:
            msg = f"✅ Para anotarse en la **Party {party}** de **{comp['name']}** hace falta {rol.mention}. Las posiciones con su propio rol lo siguen pidiendo además."
        else:
            msg = f"✅ La **Party {party}** de **{comp['name']}** quedó abierta para todos."
        await interaction.response.send_message(msg, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="legacy", description=f"Activar o desactivar la prioridad Legacy (Party 1 solo para Legacy los primeros {CTA_LEGACY_MINUTES} min)")
    @app_commands.describe(nombre="Nombre de la composición", activar=f"true = la Party 1 es solo para Legacy los primeros {CTA_LEGACY_MINUTES} minutos de cada /cta; false = sin prioridad")
    async def legacy(self, interaction: discord.Interaction, nombre: str, activar: bool):
        if not await self._admin_check(interaction):
            return await interaction.response.send_message("No tenés permisos para editar composiciones.", ephemeral=True)
        comp = cta_get_composition(interaction.guild_id, nombre)
        if not comp:
            return await interaction.response.send_message("No existe esa composición. Mirá las guardadas con `/compo listar`.", ephemeral=True)
        db_execute("UPDATE cta_compositions SET legacy_first=?, updated_at=? WHERE id=?", (int(activar), cta_now_iso(), comp["id"]))
        cta_audit(interaction.guild_id, interaction.user.id, "composition_legacy", details=f"{comp['name']}: {'activada' if activar else 'desactivada'}")
        if activar:
            msg = (f"✅ **{comp['name']}**: en las CTA nuevas, los primeros {CTA_LEGACY_MINUTES} minutos la **Party 1** es solo para <@&{CTA_LEGACY_ROLE_ID}>. "
                   "Después se abre para todos con las reglas de siempre.")
        else:
            msg = f"✅ **{comp['name']}**: sin prioridad Legacy."
        await interaction.response.send_message(msg + cta_active_notice(comp["id"]), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="track", description="Activar o desactivar el track: al cerrar la inscripción se genera un split con los anotados")
    @app_commands.describe(nombre="Nombre de la composición",
                           activar="true = cada /cta con esta composición pide Tab y City y, al cerrar, crea el split (sin crear rol); false = sin track")
    async def track(self, interaction: discord.Interaction, nombre: str, activar: bool):
        if not await self._admin_check(interaction):
            return await interaction.response.send_message("No tenés permisos para editar composiciones.", ephemeral=True)
        comp = cta_get_composition(interaction.guild_id, nombre)
        if not comp:
            return await interaction.response.send_message("No existe esa composición. Mirá las guardadas con `/compo listar`.", ephemeral=True)
        db_execute("UPDATE cta_compositions SET track=?, updated_at=? WHERE id=?", (int(activar), cta_now_iso(), comp["id"]))
        cta_audit(interaction.guild_id, interaction.user.id, "composition_track", details=f"{comp['name']}: {'activado' if activar else 'desactivado'}")
        if activar:
            msg = (f"✅ **{comp['name']}** ahora tiene **track**: en cada `/cta` hay que indicar **Tab** y **City**, y al cerrar la inscripción "
                   "se genera el split (como `/transparencia`) con la gente anotada. En esas CTA no se crea ningún rol. "
                   "Quien se anote después del cierre aparece en el hilo del split para que vos decidas si lo sumás.")
        else:
            msg = f"✅ **{comp['name']}**: sin track. Las CTA nuevas vuelven a crear el rol al cerrar."
        await interaction.response.send_message(msg + cta_active_notice(comp["id"]), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="builds", description="Poner o quitar el link de builds de una composición")
    @app_commands.describe(nombre="Nombre de la composición", link="Link a la web de builds (https://...). Vacío o «quitar» = sacar el link")
    async def builds(self, interaction: discord.Interaction, nombre: str, link: Optional[str] = None):
        if not await self._admin_check(interaction):
            return await interaction.response.send_message("No tenés permisos para editar composiciones.", ephemeral=True)
        comp = cta_get_composition(interaction.guild_id, nombre)
        if not comp:
            return await interaction.response.send_message("No existe esa composición. Mirá las guardadas con `/compo listar`.", ephemeral=True)
        try:
            url = cta_clean_builds_url(link)
        except ValueError as exc:
            return await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
        db_execute("UPDATE cta_compositions SET builds_url=?, updated_at=? WHERE id=?", (url, cta_now_iso(), comp["id"]))
        cta_audit(interaction.guild_id, interaction.user.id, "composition_builds", details=f"{comp['name']}: {url or 'quitado'}")
        if url:
            msg = f"✅ **{comp['name']}** ahora tiene el link [Builds]({url}). Aparece en el panel de las CTA nuevas que uses con esta composición."
        else:
            msg = f"✅ Saqué el link de builds de **{comp['name']}**."
        await interaction.response.send_message(msg, ephemeral=True, suppress_embeds=True)

    @app_commands.command(name="eliminar", description="Eliminar una composición guardada")
    @app_commands.describe(nombre="Nombre de la composición")
    async def eliminar(self, interaction: discord.Interaction, nombre: str):
        if not await self._admin_check(interaction):
            return await interaction.response.send_message("No tenés permisos para eliminar composiciones.", ephemeral=True)
        comp = cta_get_composition(interaction.guild_id, nombre)
        if not comp:
            return await interaction.response.send_message("No existe esa composición.", ephemeral=True)
        view = CTAConfirmDeleteCompositionView(comp, interaction.user.id)
        await interaction.response.send_message(f"¿Eliminar **{comp['name']}**? Las CTA ya iniciadas conservan su copia y sus inscripciones." + cta_active_notice(comp["id"]), view=view, ephemeral=True)


class CTAConfirmDeleteCompositionView(discord.ui.View):
    def __init__(self, comp: dict, actor_id: int):
        super().__init__(timeout=60); self.comp, self.actor_id = comp, actor_id
        yes = discord.ui.Button(label="Eliminar composición", style=discord.ButtonStyle.danger)
        no = discord.ui.Button(label="Cancelar", style=discord.ButtonStyle.secondary)
        yes.callback = self.confirm; no.callback = self.cancel
        self.add_item(yes); self.add_item(no)
    async def confirm(self, interaction: discord.Interaction):
        if interaction.user.id != self.actor_id:
            return await interaction.response.send_message("Solo quien inició la operación puede confirmarla.", ephemeral=True)
        db_execute("DELETE FROM cta_compositions WHERE id=?", (self.comp["id"],))
        cta_audit(self.comp["guild_id"], interaction.user.id, "composition_delete", details=self.comp["name"])
        await interaction.response.edit_message(content="✅ Composición eliminada. Las CTA ya publicadas conservan su copia.", view=None)
    async def cancel(self, interaction: discord.Interaction):
        if interaction.user.id != self.actor_id:
            return await interaction.response.send_message("Solo quien inició la operación puede cancelarla.", ephemeral=True)
        await interaction.response.edit_message(content="Operación cancelada.", view=None)


compo_group = CTACompositionGroup()
bot.tree.add_command(compo_group, guild=GUILD_OBJ)


async def cta_name_autocomplete(interaction: discord.Interaction, current: str):
    rows = db_fetchall("SELECT name FROM cta_compositions WHERE guild_id=? AND name LIKE ? COLLATE NOCASE ORDER BY name COLLATE NOCASE LIMIT 25",
                       (interaction.guild_id, f"%{current.strip()}%"))
    return [app_commands.Choice(name=r["name"][:100], value=r["name"]) for r in rows]


@bot.tree.command(name="cta", description="Publicar una CTA usando una composición guardada", guild=GUILD_OBJ)
@app_commands.describe(nombre="Nombre de la composición",
                       tiempo=f"Duración de las inscripciones: HH:MM o minutos (mínimo {CTA_MIN_MINUTES}, por defecto {CTA_DEFAULT_MINUTES} minutos)",
                       tab="Tab (obligatorio si la composición tiene track)",
                       city="City (obligatorio si la composición tiene track)")
async def cta_command(interaction: discord.Interaction, nombre: str, tiempo: Optional[str] = None,
                      tab: Optional[str] = None, city: Optional[str] = None):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return await interaction.response.send_message("Este comando solo funciona dentro del servidor.", ephemeral=True)
    member = interaction.user
    if not (member.guild_permissions.manage_guild or any(r.id in {STAFF_ROLE_ID, RECRUITER_ROLE_ID, CONTENT_CREATOR_ROLE_ID} for r in member.roles)):
        return await interaction.response.send_message("No tenés permisos para iniciar una CTA.", ephemeral=True)
    try:
        minutes = cta_parse_duration(tiempo)
    except ValueError as exc:
        return await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
    if isinstance(interaction.channel, discord.Thread):
        return await interaction.response.send_message("Usá `/cta` en un canal de texto, no dentro de un hilo.", ephemeral=True)
    comp = cta_get_composition(interaction.guild_id, nombre)
    if not comp and nombre.casefold() == "brawl":
        cta_ensure_brawl(interaction.guild_id, interaction.user.id)
        comp = cta_get_composition(interaction.guild_id, nombre)
    if not comp:
        return await interaction.response.send_message(f"No existe la composición **{nombre}**. Mirá las guardadas con `/compo listar`.", ephemeral=True)
    if not comp["slots"]:
        return await interaction.response.send_message("Esta composición no tiene posiciones. Agregalas con `/compo editar`.", ephemeral=True)
    if len(comp["slots"]) > CTA_MAX_SLOTS:
        return await interaction.response.send_message(f"Una CTA no puede tener más de {CTA_MAX_SLOTS} posiciones.", ephemeral=True)
    tab_v = " ".join((tab or "").split()) or None
    city_v = " ".join((city or "").split()) or None
    if len(tab_v or "") > 60 or len(city_v or "") > 60:
        return await interaction.response.send_message("❌ Tab y City pueden tener hasta 60 caracteres.", ephemeral=True)
    track_v = int(bool(comp.get("track")))
    if track_v and not (tab_v and city_v):
        return await interaction.response.send_message(
            f"❌ La composición **{comp['name']}** tiene track: tenés que indicar **tab** y **city**. "
            f"Ej: `/cta nombre:{comp['name']} tab:Tab 3 city:Caerleon`", ephemeral=True)
    now = datetime.now(timezone.utc)
    close_at = now + timedelta(minutes=minutes)
    snapshot = cta_snapshot_from_comp(comp)
    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        cur = conn.execute("INSERT INTO cta_runs(guild_id,composition_id,composition_name,snapshot_json,channel_id,created_by,created_at,closes_at,status,track,tab,city) VALUES (?,?,?,?,?,?,?,?, 'open',?,?,?)",
                           (interaction.guild_id, comp["id"], comp["name"], json.dumps(snapshot, ensure_ascii=False), interaction.channel_id, interaction.user.id, now.isoformat(), close_at.isoformat(), track_v, tab_v, city_v))
        run_id = cur.lastrowid
        conn.commit()
    run = cta_run_get(run_id)
    message = None
    try:
        await interaction.response.send_message(embeds=cta_panel_groups(run)[0], view=CTAPanelView(run_id=run_id), allowed_mentions=CTA_NO_PINGS)
        message = await interaction.original_response()
        db_execute("UPDATE cta_runs SET message_id=? WHERE id=?", (message.id, run_id))
        thread = await message.create_thread(name=f"CTA {comp['name']} #{run_id}"[:100], auto_archive_duration=1440)
        db_execute("UPDATE cta_runs SET thread_id=? WHERE id=?", (thread.id, run_id))
        await thread.send(cta_instructions_text(cta_run_get(run_id), len(snapshot["slots"])), allowed_mentions=CTA_NO_PINGS)
        await cta_refresh_board(run_id)
        await cta_refresh_panel(bot, run_id)
        if snapshot.get("legacy_first"):
            task = asyncio.create_task(cta_legacy_refresh_later(run_id, CTA_LEGACY_MINUTES * 60 + 2))
            cta_legacy_tasks.add(task); task.add_done_callback(cta_legacy_tasks.discard)
        cta_audit(interaction.guild_id, interaction.user.id, "cta_created", run_id, f"composition={comp['name']}; closes={close_at.isoformat()}; minutes={minutes}")
    except Exception as exc:
        print(f"Error publicando CTA #{run_id}: {exc}")
        db_execute("UPDATE cta_runs SET status='error' WHERE id=?", (run_id,))
        text = ("❌ No pude crear la CTA. Revisá que el bot tenga los permisos «Crear hilos públicos», "
                "«Enviar mensajes en hilos» y «Enviar mensajes» en este canal.")
        if message is not None:
            try:
                await message.edit(content=text, embed=None, view=None)
            except Exception:
                pass
        elif not interaction.response.is_done():
            await interaction.response.send_message(text, ephemeral=True)
        else:
            try:
                await interaction.followup.send(text, ephemeral=True)
            except Exception:
                pass
        return


cta_command.autocomplete("nombre")(cta_name_autocomplete)


@bot.tree.command(name="cta_activas", description="Consultar las CTA activas", guild=GUILD_OBJ)
async def cta_activas(interaction: discord.Interaction):
    rows = db_fetchall("SELECT id, composition_name, closes_at, channel_id, thread_id FROM cta_runs WHERE guild_id=? AND status='open' ORDER BY closes_at", (interaction.guild_id,))
    if not rows:
        return await interaction.response.send_message("No hay CTA activas.", ephemeral=True)
    lines = []
    for r in rows:
        closes = _cta_parse_dt(r["closes_at"])
        where = f"<#{r['thread_id']}>" if r["thread_id"] else f"<#{r['channel_id']}>"
        lines.append(f"• CTA **#{r['id']} — {r['composition_name']}** · cierra <t:{int(closes.timestamp())}:R> · {where}")
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@bot.tree.command(name="cta_cerrar", description="Cerrar una CTA antes de tiempo", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(cta_id="Número de la CTA (figura en el panel y en /cta_activas)")
async def cta_cerrar(interaction: discord.Interaction, cta_id: int):
    if not isinstance(interaction.user, discord.Member) or not (interaction.user.guild_permissions.manage_guild or any(r.id in {STAFF_ROLE_ID, RECRUITER_ROLE_ID} for r in interaction.user.roles)):
        return await interaction.response.send_message("No tenés permisos para cerrar CTA.", ephemeral=True)
    run = cta_run_get(cta_id)
    if not run or int(run["guild_id"]) != interaction.guild_id or run["status"] != "open":
        return await interaction.response.send_message("No encontré una CTA abierta con ese número.", ephemeral=True)
    await interaction.response.send_message(f"CTA **#{cta_id}** cerrada. La composición final queda publicada en el hilo.", ephemeral=True)
    await cta_close_run(cta_id, reason=f"closed_by:{interaction.user.id}")


@bot.tree.command(name="cta_asignar", description="Asignar a un jugador a una posición de una CTA", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(cta_id="Número de la CTA", miembro="Jugador", numero="Número de posición (el mismo que se usa en el hilo)", excepcion="Asignar aunque no cumpla los roles pedidos", motivo="Motivo (obligatorio si usás excepción)")
async def cta_asignar(interaction: discord.Interaction, cta_id: int, miembro: discord.Member, numero: app_commands.Range[int, 1, CTA_MAX_SLOTS], excepcion: bool = False, motivo: Optional[str] = None):
    if not isinstance(interaction.user, discord.Member) or not (interaction.user.guild_permissions.manage_guild or any(r.id in {STAFF_ROLE_ID, RECRUITER_ROLE_ID} for r in interaction.user.roles)):
        return await interaction.response.send_message("No tenés permisos para asignar posiciones.", ephemeral=True)
    run = cta_run_get(cta_id)
    if not run or int(run["guild_id"]) != interaction.guild_id or run["status"] != "open":
        return await interaction.response.send_message("La CTA no existe o ya está cerrada.", ephemeral=True)
    snapshot = json.loads(run["snapshot_json"])
    slot = cta_slot_by_number(snapshot["slots"], numero)
    if not slot:
        return await interaction.response.send_message(f"La posición {numero} no existe. Esta CTA tiene posiciones del 1 al {len(snapshot['slots'])}.", ephemeral=True)
    error = cta_access_error(miembro, slot, interaction.guild)
    if error and not excepcion:
        return await interaction.response.send_message(error + " Si querés asignarlo igual, marcá `excepcion` e indicá el motivo.", ephemeral=True)
    if excepcion and not (motivo and motivo.strip()):
        return await interaction.response.send_message("Para usar una excepción tenés que indicar el motivo.", ephemeral=True)
    key = cta_slot_key(slot)
    try:
        with db_immediate() as conn:
            if conn.execute("SELECT 1 FROM cta_signups WHERE cta_id=? AND slot_key=?", (cta_id, key)).fetchone():
                return await interaction.response.send_message(f"La posición {numero} ya está ocupada. Liberala primero con `/cta_liberar`.", ephemeral=True)
            if conn.execute("SELECT 1 FROM cta_signups WHERE cta_id=? AND user_id=?", (cta_id, miembro.id)).fetchone():
                return await interaction.response.send_message(f"{miembro.mention} ya está anotado en esta CTA. Liberalo primero con `/cta_liberar`.", ephemeral=True, allowed_mentions=CTA_NO_PINGS)
            conn.execute("INSERT INTO cta_signups(cta_id,slot_key,user_id,assigned_by,override_reason,signed_at) VALUES (?,?,?,?,?,?)", (cta_id, key, miembro.id, interaction.user.id, motivo.strip() if excepcion and motivo else None, cta_now_iso()))
    except sqlite3.IntegrityError:
        return await interaction.response.send_message("La posición se ocupó justo ahora. Probá de nuevo.", ephemeral=True)
    cta_audit(interaction.guild_id, interaction.user.id, "manual_assign_override" if excepcion else "manual_assign", cta_id, f"member={miembro.id}; slot={key}; reason={motivo or ''}")
    cta_request_board_refresh(cta_id)
    await interaction.response.send_message(f"✅ {miembro.mention} quedó asignado a la posición {numero} ({slot['role_name']}, Party {slot['party_no']}).", ephemeral=True, allowed_mentions=CTA_NO_PINGS)


@bot.tree.command(name="cta_liberar", description="Liberar la posición de un jugador en una CTA", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(cta_id="Número de la CTA", miembro="Jugador")
async def cta_liberar(interaction: discord.Interaction, cta_id: int, miembro: discord.Member):
    if not isinstance(interaction.user, discord.Member) or not (interaction.user.guild_permissions.manage_guild or any(r.id in {STAFF_ROLE_ID, RECRUITER_ROLE_ID} for r in interaction.user.roles)):
        return await interaction.response.send_message("No tenés permisos para liberar posiciones.", ephemeral=True)
    run = cta_run_get(cta_id)
    if not run or int(run["guild_id"]) != interaction.guild_id or run["status"] != "open":
        return await interaction.response.send_message("La CTA no existe o ya está cerrada.", ephemeral=True)
    rows = db_fetchall("SELECT slot_key FROM cta_signups WHERE cta_id=? AND user_id=?", (cta_id, miembro.id))
    if not rows:
        return await interaction.response.send_message("Ese jugador no está anotado en esta CTA.", ephemeral=True)
    db_execute("DELETE FROM cta_signups WHERE cta_id=? AND user_id=?", (cta_id, miembro.id))
    cta_audit(interaction.guild_id, interaction.user.id, "admin_release", cta_id, f"member={miembro.id}; slot={rows[0]['slot_key']}")
    cta_request_board_refresh(cta_id)
    await interaction.response.send_message(f"Liberé la posición de {miembro.mention}.", ephemeral=True, allowed_mentions=CTA_NO_PINGS)


@bot.tree.command(name="cta_historial", description="Ver las últimas CTA cerradas", guild=discord.Object(id=GUILD_ID))
async def cta_historial(interaction: discord.Interaction):
    rows = db_fetchall("SELECT id, composition_name, created_at, closed_at, status FROM cta_runs WHERE guild_id=? AND status IN ('closed','error') ORDER BY id DESC LIMIT 10", (interaction.guild_id,))
    if not rows:
        return await interaction.response.send_message("Todavía no hay CTA cerradas.", ephemeral=True)
    lines = []
    for r in rows:
        created = _cta_parse_dt(r["created_at"])
        state = "cerrada" if r["status"] == "closed" else "con error"
        lines.append(f"• **#{r['id']} {r['composition_name']}** · iniciada <t:{int(created.timestamp())}:R> · {state}")
    await interaction.response.send_message("**Últimas CTA**\n" + "\n".join(lines), ephemeral=True)


@tasks.loop(seconds=15)
async def cta_housekeeping():
    """Cierra automáticamente las CTA cuyo tiempo terminó (también las que vencieron con el bot apagado)."""
    rows = db_fetchall("SELECT id, closes_at FROM cta_runs WHERE status='open'")
    now = datetime.now(timezone.utc)
    for row in rows:
        try:
            if _cta_parse_dt(row["closes_at"]) <= now:
                await cta_close_run(int(row["id"]), reason="timeout")
        except Exception as exc:
            print(f"Error en housekeeping CTA #{row['id']}: {exc}")


@cta_housekeeping.before_loop
async def before_cta_housekeeping():
    await bot.wait_until_ready()


@bot.event
async def on_member_update(before: discord.Member, after: discord.Member):
    # Si pierde un rol exigido por una posición ya ocupada, avisar al organizador; no expulsarlo automáticamente.
    if before.roles == after.roles or not after.guild:
        return
    rows = db_fetchall("SELECT s.cta_id, s.slot_key, r.created_by, r.channel_id, r.thread_id, r.snapshot_json FROM cta_signups s JOIN cta_runs r ON r.id=s.cta_id WHERE s.user_id=? AND r.guild_id=? AND r.status='open'", (after.id, after.guild.id))
    for row in rows:
        try:
            snapshot = json.loads(row["snapshot_json"])
            slot = next((x for x in snapshot["slots"] if cta_slot_key(x) == row["slot_key"]), None)
            if not slot or not cta_access_error(after, slot, after.guild):
                continue
            target = after.guild.get_channel(int(row["thread_id"])) if row["thread_id"] else None
            if target is None:
                target = after.guild.get_channel(int(row["channel_id"]))
            if target:
                await target.send(f"<@{row['created_by']}> ⚠️ {after.mention} ya no cumple los roles que pide la posición **{cta_number_of_slot_key(snapshot['slots'], row['slot_key'])} ({slot['role_name']}, Party {slot['party_no']})** de la CTA #{row['cta_id']}. Su plaza no se modificó.", allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
        except Exception as exc:
            print(f"Error verificando cambio de roles para CTA #{row['cta_id']}: {exc}")


# ============================================================
# SLASH COMMAND: /verratas
# ============================================================
def parse_albion_member_names(data) -> set:
    """Extrae los nombres (en minúsculas) de la respuesta de la API de miembros de la guild."""
    names = set()
    if isinstance(data, dict):
        data = data.get("members") or data.get("Members") or data.get("data") or []
    for item in data or []:
        name = item.get("Name") or item.get("name") if isinstance(item, dict) else None
        if name and str(name).strip():
            names.add(normalizar_nombre(str(name).strip()))
    return names


def find_ratas(members, guild_names: set) -> list:
    """members: lista de (id, texto_apodo). Devuelve los que NO coinciden con ningún jugador de la guild."""
    return [(uid, label) for uid, label in members if normalizar_nombre(label.strip()) not in guild_names]


def chunk_lines(lines: list, limit: int = 1900) -> list:
    chunks, current, size = [], [], 0
    for line in lines:
        if current and size + len(line) + 1 > limit:
            chunks.append("\n".join(current))
            current, size = [], 0
        current.append(line)
        size += len(line) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks


async def _albion_get_json(url: str, params: Optional[dict] = None):
    """GET con timeout largo (la API de Albion a veces tarda) y reintentos solo ante timeouts o cortes de red."""
    headers = {"User-Agent": "Mozilla/5.0 (compatible; SmoggBot/1.0)", "Accept": "application/json"}
    last_exc: Exception = RuntimeError("sin intentos")
    for attempt in range(1 + ALBION_RETRIES):
        try:
            async with bot.http_session.get(url, params=params, headers=headers,
                                            timeout=aiohttp.ClientTimeout(total=ALBION_TIMEOUT_SECONDS)) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"HTTP {resp.status}")      # error del servidor: no se reintenta
                return await resp.json(content_type=None)
        except (asyncio.TimeoutError, aiohttp.ClientError) as exc:
            last_exc = exc
            if attempt < ALBION_RETRIES:
                await asyncio.sleep(2)
    raise last_exc


def parse_albion_guild_id(data, guild_name: str) -> Optional[str]:
    """Busca el ID de la guild en la respuesta de búsqueda de la API (coincidencia exacta de nombre)."""
    guilds = data.get("guilds", []) if isinstance(data, dict) else []
    for g in guilds:
        if isinstance(g, dict) and str(g.get("Name", "")).casefold() == guild_name.casefold() and g.get("Id"):
            return str(g["Id"])
    return None


async def fetch_albion_guild_member_names() -> set:
    """Lista de jugadores de la guild. Prueba el servidor configurado y luego los demás; si el ID no
    responde, busca la guild por nombre. Guarda el resultado 2 minutos. Si todo falla, el error
    explica qué pasó en cada intento."""
    now = time.time()
    if _albion_roster_cache["names"] and now - _albion_roster_cache["at"] < ALBION_ROSTER_CACHE_SECONDS:
        return _albion_roster_cache["names"]
    bases = [ALBION_GAMEINFO_BASE] + [b for b in ALBION_GAMEINFO_ALL if b != ALBION_GAMEINFO_BASE]
    problems = []
    for base in bases:
        host = base.split("//")[-1]
        guild_ids = [ALBION_GUILD_ID]
        for attempt in ("id", "búsqueda"):
            try:
                if attempt == "búsqueda":
                    found = parse_albion_guild_id(
                        await _albion_get_json(f"{base}/api/gameinfo/search", {"q": ALBION_GUILD_NAME}), ALBION_GUILD_NAME)
                    if not found or found == ALBION_GUILD_ID:
                        problems.append(f"{host}: no encontré la guild por nombre")
                        break
                    guild_ids = [found]
                data = await _albion_get_json(f"{base}/api/gameinfo/guilds/{guild_ids[0]}/members")
                names = parse_albion_member_names(data)
                if names:
                    _albion_roster_cache.update(at=now, names=names)
                    return names
                problems.append(f"{host}: lista vacía ({attempt})")
            except Exception as exc:
                problems.append(f"{host} ({attempt}): {type(exc).__name__} {exc}".strip())
    raise RuntimeError(" | ".join(problems))


@bot.tree.command(name="verratas", description="(Staff) Miembros de Discord que no figuran en la guild de Albion", guild=GUILD_OBJ)
async def verratas_slash(interaction: discord.Interaction):
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        return await respond_ephemeral(interaction, "❌ Este comando solo funciona dentro del servidor.")
    if not is_staff_member(interaction.user):
        return await respond_ephemeral(interaction, "❌ Solo el **Staff** puede usar `/verratas`.")
    role = interaction.guild.get_role(MIEMBRO_ROLE_ID)
    if role is None:
        return await respond_ephemeral(interaction, "❌ No encontré el rol Miembro. Avisale a un administrador.")

    await interaction.response.defer(ephemeral=True)
    try:
        guild_names = await fetch_albion_guild_member_names()
    except Exception as exc:
        print(f"/verratas: no pude consultar la guild: {exc}")
        return await interaction.followup.send(
            "❌ No pude consultar la lista de la guild en Albion. Probá de nuevo en un rato.\n"
            f"Detalle técnico (para el administrador): `{str(exc)[:900]}`", ephemeral=True)
    if not guild_names:
        # Sin lista no se puede comparar: evitamos marcar a todos como "ratas" por un error de la API.
        return await interaction.followup.send(
            f"❌ Albion devolvió una lista vacía para **{ALBION_GUILD_NAME}**. No comparé nada para no dar falsos resultados.",
            ephemeral=True)

    if not interaction.guild.chunked:
        try:
            await interaction.guild.chunk()
        except Exception:
            pass
    members = [(m.id, m.nick or m.display_name) for m in role.members if not m.bot]
    ratas = find_ratas(members, guild_names)
    no_pings = discord.AllowedMentions.none()
    header = (f"🐀 **Sin coincidencia en {ALBION_GUILD_NAME}:** {len(ratas)} de {len(members)} miembros "
              f"(la guild tiene {len(guild_names)} jugadores).\nSe compara el apodo del servidor con el nombre en Albion, sin distinguir mayúsculas.")
    if not ratas:
        return await interaction.followup.send(header + "\n✅ Todos los miembros coinciden.", ephemeral=True, allowed_mentions=no_pings)
    lines = [f"• <@{uid}> · `{label}`" for uid, label in sorted(ratas, key=lambda x: x[1].casefold())]
    chunks = chunk_lines(lines)
    await interaction.followup.send(header, ephemeral=True, allowed_mentions=no_pings)
    for chunk in chunks[:8]:
        await interaction.followup.send(chunk, ephemeral=True, allowed_mentions=no_pings)
    if len(chunks) > 8:
        await interaction.followup.send("…hay más resultados de los que se pueden mostrar.", ephemeral=True)


# ================== /comparar: killboard vs. rol de Discord ==================
COMPARAR_MAX_MESSAGES = 8


def discord_label(member) -> str:
    """Texto que se compara con el nombre de Albion: el apodo del servidor (o el nombre visible si no tiene)."""
    return (getattr(member, "nick", None) or getattr(member, "display_name", None) or getattr(member, "name", "") or "").strip()


def comparar_listas(jugadores_guild: list, role_members: list, all_members: list) -> dict:
    """jugadores_guild: nombres de Albion de la guild en el killboard.
    role_members / all_members: listas de (id, apodo). Comparación exacta, sin distinguir mayúsculas.
    Devuelve:
      coinciden      -> [(nombre_albion, [ids con el rol])]
      sin_rol        -> [(nombre_albion, [ids en el server sin el rol])]  (lista vacía = no está en Discord)
      sin_killboard  -> [(id, apodo)] tienen el rol pero no figuran en el killboard."""
    unique: dict = {}
    for n in jugadores_guild:
        if n and n.strip():
            unique.setdefault(normalizar_nombre(n.strip()), n.strip())
    names = list(unique.values())
    keys = set(unique)
    role_idx: dict = {}
    for uid, label in role_members:
        role_idx.setdefault(normalizar_nombre(label.strip()), []).append(uid)
    all_idx: dict = {}
    for uid, label in all_members:
        all_idx.setdefault(normalizar_nombre(label.strip()), []).append(uid)
    coinciden, sin_rol = [], []
    for name in names:
        k = normalizar_nombre(name)
        if k in role_idx:
            coinciden.append((name, role_idx[k]))
        else:
            sin_rol.append((name, all_idx.get(k, [])))
    sin_killboard = [(uid, label) for uid, label in role_members if normalizar_nombre(label.strip()) not in keys]
    return {"coinciden": coinciden, "sin_rol": sin_rol, "sin_killboard": sin_killboard}


def comparar_allowed(member) -> bool:
    perms = getattr(member, "guild_permissions", None)
    return bool((perms is not None and (perms.manage_roles or perms.manage_guild))
                or any(r.id == STAFF_ROLE_ID for r in getattr(member, "roles", [])))


@bot.tree.command(name="comparar", description="Compara los jugadores de una guild en un killboard con la gente que tiene un rol", guild=GUILD_OBJ)
@app_commands.describe(
    killboard="Link o ID de AlbionBB. Ej: https://albionbb.com/battles/1468514406 (podés poner varios)",
    rol="Rol de Discord a comparar",
    guild="Nombre exacto de la guild de Albion. Ej: Smogg",
)
async def comparar_slash(interaction: discord.Interaction, killboard: str, rol: discord.Role, guild: str):
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        return await respond_ephemeral(interaction, "❌ Solo disponible en el servidor.")
    if not comparar_allowed(interaction.user):
        return await respond_ephemeral(interaction, "❌ Solo el Staff o quien pueda gestionar roles puede usar `/comparar`.")
    battle_ids = extraer_ids(killboard)
    if not battle_ids:
        return await respond_ephemeral(interaction, "❌ No encontré ningún ID de batalla en el killboard. Ejemplo: `https://albionbb.com/battles/1468514406`")
    if len(battle_ids) > 50:
        return await respond_ephemeral(interaction, "❌ Podés consultar como máximo 50 batallas por comando.")
    guild_name = " ".join((guild or "").split())
    if not guild_name:
        return await respond_ephemeral(interaction, "❌ Tenés que indicar el nombre de la guild.")

    await interaction.response.defer(ephemeral=True)
    try:
        jugadores = await obtener_jugadores_albion(battle_ids)
    except Exception as exc:
        print(f"/comparar AlbionBB: {exc!r}")
        return await interaction.followup.send("❌ No pude obtener los participantes de AlbionBB. Revisá el link y probá de nuevo en un rato.", ephemeral=True)
    gkey = normalizar_nombre(guild_name)
    nombres = [d["name"] for d in jugadores.values() if normalizar_nombre(d["guild"]) == gkey]
    if not nombres:
        # Sin jugadores no se puede comparar: evita marcar a todo el rol como ausente por un error o un nombre mal escrito.
        guilds_vistas = sorted({d["guild"] for d in jugadores.values()}, key=str.casefold)
        extra = f"\nGuilds que aparecen en esa batalla: {', '.join(f'`{g}`' for g in guilds_vistas[:15])}" if guilds_vistas else ""
        return await interaction.followup.send(f"❌ No encontré jugadores de **{guild_name}** en el killboard. Revisá cómo está escrito el nombre.{extra}", ephemeral=True)

    if not interaction.guild.chunked:
        try:
            await interaction.guild.chunk()
        except Exception:
            pass
    role_members = [(m.id, discord_label(m)) for m in rol.members if not m.bot]
    all_members = [(m.id, discord_label(m)) for m in interaction.guild.members if not m.bot]
    res = comparar_listas(nombres, role_members, all_members)

    header = (f"## ⚖️ Killboard vs {rol.name}\n"
              f"**Guild:** `{guild_name}` · **Batallas:** {len(battle_ids)}\n"
              f"**Jugadores de la guild en el killboard:** {len(set(normalizar_nombre(n) for n in nombres))}\n"
              f"**Miembros con el rol:** {len(role_members)}\n"
              f"✅ **Están en los dos:** {len(res['coinciden'])}\n"
              f"➖ **Estuvieron pero NO tienen el rol:** {len(res['sin_rol'])}\n"
              f"❓ **Tienen el rol pero NO estuvieron:** {len(res['sin_killboard'])}\n"
              "_Se compara el apodo de Discord con el nombre de Albion, sin distinguir mayúsculas._")
    messages = [header]
    if res["sin_rol"]:
        lines = []
        for name, ids in sorted(res["sin_rol"], key=lambda x: x[0].casefold()):
            if ids:
                lines.append(f"• `{name}` · en el server sin el rol: " + ", ".join(f"<@{u}>" for u in ids[:3]))
            else:
                lines.append(f"• `{name}` · no encontré a nadie con ese apodo en Discord")
        messages += [f"**➖ Estuvieron en el killboard pero no tienen el rol ({len(lines)}):**\n{c}" if i == 0 else c
                     for i, c in enumerate(chunk_lines(lines, 1800))]
    if res["sin_killboard"]:
        lines = [f"• <@{uid}> · `{label}`" for uid, label in sorted(res["sin_killboard"], key=lambda x: x[1].casefold())]
        messages += [f"**❓ Tienen el rol pero no estuvieron en el killboard ({len(lines)}):**\n{c}" if i == 0 else c
                     for i, c in enumerate(chunk_lines(lines, 1800))]
    no_pings = discord.AllowedMentions.none()
    for msg in messages[:COMPARAR_MAX_MESSAGES]:
        await interaction.followup.send(msg, ephemeral=True, allowed_mentions=no_pings)
    if len(messages) > COMPARAR_MAX_MESSAGES:
        await interaction.followup.send("…hay más resultados de los que se pueden mostrar.", ephemeral=True)


# ================== /disarray ==================
# Disarray no usa una fórmula: Albion define una TABLA de umbrales de jugadores por nivel.
# Valores vigentes desde el patch 22.090.1 (todos los umbrales de la temporada 13 bajaron 5 jugadores).
# Fuentes: AlbionZVZ (niveles 1-60, con rangos) y AlbionManager (niveles 1-67), ambas basadas en la wiki de Albion.
# Cada valor es la cantidad MÍNIMA de jugadores para alcanzar ese nivel. Las monturas de batalla suman puntos
# aparte y NO se cuentan acá.
DISARRAY_THRESHOLDS = (
    21, 22, 23, 24, 25, 26, 27, 28, 29, 30,          # 1-10
    31, 32, 33, 34, 36, 37, 39, 41, 44, 46,          # 11-20
    48, 49, 51, 54, 56, 58, 61, 64, 67, 70,          # 21-30
    74, 79, 83, 89, 95, 99, 103, 108, 114, 119,      # 31-40
    126, 133, 141, 148, 154, 160, 167, 175, 183, 192,  # 41-50
    200, 207, 215, 223, 232, 242, 252, 264, 276, 290,  # 51-60
    305, 322, 341, 361, 385, 412, 445,               # 61-67
)
DISARRAY_MAX_LEVEL = len(DISARRAY_THRESHOLDS)
DISARRAY_SINGLE_SOURCE_FROM = 62     # del 62 en adelante solo lo trae una fuente (el 61 coincide con el fin del rango del 60)
DISARRAY_PATCH = "22.090.1"


def disarray_players_for_level(level: int) -> int:
    if not isinstance(level, int) or not 1 <= level <= DISARRAY_MAX_LEVEL:
        raise ValueError(f"El nivel de Disarray tiene que estar entre 1 y {DISARRAY_MAX_LEVEL}.")
    return DISARRAY_THRESHOLDS[level - 1]


def disarray_range(level: int):
    """(mínimo, máximo) de jugadores que dan exactamente ese nivel. El máximo es None en el último nivel."""
    lo = disarray_players_for_level(level)
    hi = DISARRAY_THRESHOLDS[level] - 1 if level < DISARRAY_MAX_LEVEL else None
    return lo, hi


def disarray_message(level: int) -> str:
    lo, hi = disarray_range(level)
    text = f"🌀 El nivel de Disarray **{level}** requiere **{lo} jugadores**, sin contar monturas de batalla."
    if hi is None:
        text += f"\nEs el nivel máximo: {lo} jugadores o más."
    elif hi > lo:
        text += f"\nCon entre {lo} y {hi} jugadores quedás en el nivel {level}; el nivel {level + 1} empieza en {hi + 1}."
    else:
        text += f"\nEl nivel {level + 1} empieza en {hi + 1} jugadores."
    if level >= DISARRAY_SINGLE_SOURCE_FROM:
        text += "\n⚠️ Este valor sale de una sola fuente de la comunidad; no pude confirmarlo con la wiki oficial."
    text += f"\n_Tabla vigente desde el patch {DISARRAY_PATCH}._"
    return text


def disarray_allowed(member) -> bool:
    """Rol Miembro (o Staff)."""
    return any(r.id in {MIEMBRO_ROLE_ID, STAFF_ROLE_ID} for r in getattr(member, "roles", []))


@bot.tree.command(name="disarray", description="Cuántos jugadores hacen falta para un nivel de Disarray (sin contar monturas)", guild=GUILD_OBJ)
@app_commands.describe(numero=f"Nivel de Disarray (1 a {DISARRAY_MAX_LEVEL})")
async def disarray_slash(interaction: discord.Interaction, numero: int):
    if not (isinstance(interaction.user, discord.Member) and disarray_allowed(interaction.user)):
        return await respond_ephemeral(interaction, "❌ Solo los miembros de la guild pueden usar `/disarray`.")
    if not 1 <= numero <= DISARRAY_MAX_LEVEL:
        return await respond_ephemeral(interaction, f"❌ El nivel de Disarray tiene que ser un número entero entre 1 y {DISARRAY_MAX_LEVEL}.")
    await interaction.response.send_message(disarray_message(numero))


# ================== /addrolllist: dar un rol a una lista de apodos ==================
ADDROL_MAX_NAMES = 150
ADDROL_ROLE_MENTION_RE = re.compile(r"<@&(\d+)>")


def addrol_parse_names(text: str) -> list:
    """Apodos separados por coma, punto y coma o salto de línea. Sin repetidos (sin distinguir mayúsculas)."""
    seen, out = set(), []
    for raw in re.split(r"[,;\n]+", text or ""):
        name = " ".join(raw.split())
        if name.startswith("@"):
            name = name[1:].strip()
        key = normalizar_nombre(name)
        if name and key not in seen:
            seen.add(key); out.append(name)
    return out


def addrol_parse_role(text: str):
    """('mention', id) si etiquetaron un rol, ('name', texto) si es una palabra, (None, None) si no hay nada."""
    m = ADDROL_ROLE_MENTION_RE.search(text or "")
    if m:
        return "mention", int(m.group(1))
    name = " ".join((text or "").split())
    return ("name", name) if name else (None, None)


def addrol_resolve(names: list, members: list) -> dict:
    """members: [(id, apodo)]. Coincidencia exacta del apodo, sin distinguir mayúsculas.
    Si dos personas tienen el mismo apodo no se adivina: queda como ambiguo."""
    idx: dict = {}
    for uid, label in members:
        idx.setdefault(normalizar_nombre(label.strip()), []).append(uid)
    found, not_found, ambiguous = [], [], []
    for name in names:
        ids = idx.get(normalizar_nombre(name), [])
        if len(ids) == 1:
            found.append((name, ids[0]))
        elif not ids:
            not_found.append(name)
        else:
            ambiguous.append((name, ids))
    return {"found": found, "not_found": not_found, "ambiguous": ambiguous}


@bot.tree.command(name="addrolllist", description="Da un rol a una lista de apodos de Discord (crea el rol si escribís un nombre)", guild=GUILD_OBJ)
@app_commands.describe(
    apodos="Apodos de Discord separados por coma (también sirve un apodo por línea)",
    rol="Una palabra/nombre para crear el rol, o etiquetá (@) un rol que ya existe",
)
async def addrolllist_slash(interaction: discord.Interaction, apodos: str, rol: str):
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        return await respond_ephemeral(interaction, "❌ Solo disponible en el servidor.")
    if not comparar_allowed(interaction.user):
        return await respond_ephemeral(interaction, "❌ Solo el Staff o quien pueda gestionar roles puede usar este comando.")
    names = addrol_parse_names(apodos)
    if not names:
        return await respond_ephemeral(interaction, "❌ No encontré ningún apodo. Escribilos separados por coma. Ej: `Juan, Pedro, Maria`")
    if len(names) > ADDROL_MAX_NAMES:
        return await respond_ephemeral(interaction, f"❌ Máximo {ADDROL_MAX_NAMES} apodos por comando (escribiste {len(names)}).")
    kind, value = addrol_parse_role(rol)
    if kind is None:
        return await respond_ephemeral(interaction, "❌ Indicá el rol: una palabra para crearlo o etiquetá uno existente.")
    if kind == "name" and len(value) > 100:
        return await respond_ephemeral(interaction, "❌ El nombre del rol puede tener hasta 100 caracteres.")
    guild = interaction.guild
    role = None
    if kind == "mention":
        role = guild.get_role(value)
        if role is None:
            return await respond_ephemeral(interaction, "❌ No encontré ese rol en el servidor.")
    else:
        role = discord.utils.get(guild.roles, name=value)          # si ya existe uno con ese nombre se usa
    if role is not None:
        if role.is_default() or role.managed:
            return await respond_ephemeral(interaction, "❌ Ese rol no se puede asignar manualmente (es @everyone o lo administra una integración).")
        bot_top = guild.me.top_role if guild.me else None
        if bot_top is None or role >= bot_top:
            return await respond_ephemeral(interaction, "❌ Ese rol está por encima (o al mismo nivel) del rol del bot. Mové el rol del bot más arriba.")
        if guild.owner_id != interaction.user.id and role >= interaction.user.top_role:
            return await respond_ephemeral(interaction, "❌ No podés dar un rol que está por encima (o al mismo nivel) de tu rol más alto.")

    await interaction.response.defer(ephemeral=True)
    if not guild.chunked:
        try:
            await guild.chunk()
        except Exception:
            pass
    members = [(m.id, discord_label(m)) for m in guild.members if not m.bot]
    res = addrol_resolve(names, members)
    if not res["found"]:
        hint = "\nSi pegaste los apodos separados por espacios, separalos con comas." if len(names) == 1 and " " in names[0] else ""
        return await interaction.followup.send(
            f"❌ No encontré a nadie con esos apodos, así que no hice cambios{' (no creé el rol)' if role is None else ''}.{hint}",
            ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    created = False
    if role is None:
        try:
            role = await guild.create_role(name=value, reason=f"Creado por {interaction.user} con /addrolllist")
            created = True
        except discord.Forbidden:
            return await interaction.followup.send("❌ No pude crear el rol. Me falta el permiso **Gestionar roles**.", ephemeral=True)
        except discord.HTTPException as exc:
            return await interaction.followup.send(f"❌ Discord rechazó la creación del rol: `{exc}`", ephemeral=True)

    assigned, already, failed = [], [], []
    for name, uid in res["found"]:
        member = guild.get_member(uid)
        if member is None:
            failed.append(name); continue
        if role in member.roles:
            already.append(name); continue
        try:
            await member.add_roles(role, reason=f"Asignado por {interaction.user} con /addrolllist")
            assigned.append(name)
        except discord.HTTPException as exc:
            print(f"/addrolllist: no pude dar {role.name} a {member}: {exc}")
            failed.append(name)

    lines = [f"## ✅ Rol {role.name}",
             f"{'🆕 Rol creado.' if created else 'Usé el rol que ya existía.'}",
             f"**Apodos en la lista:** {len(names)}",
             f"**Asignados ahora:** {len(assigned)}",
             f"**Ya lo tenían:** {len(already)}",
             f"**No encontrados:** {len(res['not_found'])}"]
    if res["ambiguous"]:
        lines.append(f"**Apodo repetido (no asigné a nadie):** {len(res['ambiguous'])}")
    if failed:
        lines.append(f"**No pude asignar:** {len(failed)}")
    messages = ["\n".join(lines)]
    def add_section(title, items):
        if items:
            chunks = chunk_lines([f"• `{i}`" for i in items], 1800)
            messages.extend(f"{title} ({len(items)}):\n{c}" if k == 0 else c for k, c in enumerate(chunks))
    add_section("❓ **No encontré a nadie con estos apodos**", res["not_found"])
    add_section("👥 **Hay más de una persona con estos apodos (no asigné ninguna)**", [n for n, _ in res["ambiguous"]])
    add_section("⚠️ **No pude asignar el rol a**", failed)
    for msg in messages[:COMPARAR_MAX_MESSAGES]:
        await interaction.followup.send(msg, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
    if len(messages) > COMPARAR_MAX_MESSAGES:
        await interaction.followup.send("…hay más resultados de los que se pueden mostrar.", ephemeral=True)


# ================== !topkills / !topattend / !topdeaths ==================
# Datos: página de asistencia de la guild en AlbionBB (la misma que se ve en el navegador; trae kills,
# muertes y asistencia por jugador con el filtro por defecto de la página).
ALBION_TOP_URL = os.getenv("ALBION_TOP_URL", f"https://albionbb.com/guilds/{ALBION_GUILD_ID}/attendance")
ALBION_TOP_SIZE = 10
ALBION_TOP_CACHE_SECONDS = 300          # resultado fresco
ALBION_TOP_STALE_SECONDS = 6 * 3600     # si AlbionBB falla, se usa lo último guardado hasta este límite
ALBION_TOP_USER_COOLDOWN = 15           # segundos entre usos por persona
ALBION_TOP_STATS = {
    "kills": ("topkills", "⚔️", "Top kills", "kills"),
    "attendance": ("topattend", "🛡️", "Top asistencia", "asistencias"),
    "deaths": ("topdeaths", "💀", "Top muertes", "muertes"),
}
_albion_top_cache: dict = {"at": 0.0, "players": []}
_albion_top_last_use: dict = {}
_albion_top_lock = asyncio.Lock()


def _albion_nuxt_decode(raw):
    """Decodifica el formato compacto de Nuxt (__NUXT_DATA__): lista de nodos que se referencian por índice."""
    memo: dict = {}

    def ref(x):
        return get(x) if isinstance(x, int) and not isinstance(x, bool) and x >= 0 else None

    def get(i):
        if i in memo:
            return memo[i]
        v = raw[i]
        if isinstance(v, list):
            if v and isinstance(v[0], str):          # nodo especial, p. ej. ["ShallowReactive", 3]
                out = ref(v[1]) if len(v) > 1 and v[0] in ("ShallowReactive", "Reactive", "ShallowRef", "Ref") else None
                memo[i] = out
                return out
            out = []
            memo[i] = out
            out.extend(ref(x) for x in v)
            return out
        if isinstance(v, dict):
            out = {}
            memo[i] = out
            for k, x in v.items():
                out[k] = ref(x)
            return out
        memo[i] = v
        return v

    return get(0)


def _albion_top_players(html: str) -> list:
    """Saca la lista de jugadores (con kills/deaths/attendance) del HTML de AlbionBB. Lanza ValueError si no la encuentra."""
    m = re.search(r'<script[^>]*id="__NUXT_DATA__"[^>]*>(.*?)</script>', html or "", re.S)
    if not m:
        raise ValueError("la página no trae los datos esperados (¿bloqueo o cambio de formato?)")
    decoded = _albion_nuxt_decode(json.loads(m.group(1)))
    found: list = []

    def walk(o, depth=0):
        if depth > 40 or found:
            return
        if isinstance(o, list):
            dicts = [x for x in o if isinstance(x, dict)]
            if dicts and len(dicts) == len(o) and all(k in dicts[0] for k in ("name", "kills", "deaths", "attendance")):
                found.extend(dicts)
                return
            for x in o:
                walk(x, depth + 1)
        elif isinstance(o, dict):
            for x in o.values():
                walk(x, depth + 1)

    walk(decoded)
    if not found:
        raise ValueError("no encontré la tabla de jugadores en la página")
    return found


def _albion_top_rank(players: list, key: str, size: int = ALBION_TOP_SIZE) -> list:
    """Top por 'key' (mayor primero; empate: orden alfabético). Ignora valores vacíos o 0 y filas inválidas."""
    rows = []
    for p in players:
        try:
            value = int(p.get(key) or 0)
        except (TypeError, ValueError):
            continue
        name = str(p.get("name") or "").strip()
        if name and value > 0:
            rows.append((name, value))
    rows.sort(key=lambda r: (-r[1], r[0].casefold()))
    return rows[:size]


def _albion_top_embed(key: str, rows: list, total_players: int, age_seconds: float, stale: bool) -> discord.Embed:
    _cmd, icon, title, unit = ALBION_TOP_STATS[key]
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    lines = [f"{medals.get(i, f'`{i:>2}.`')} **{discord.utils.escape_markdown(name)}** — {value:,} {unit}".replace(",", ".")
             for i, (name, value) in enumerate(rows, start=1)]
    embed = discord.Embed(title=f"{icon} {title} · {ALBION_GUILD_NAME}",
                          description="\n".join(lines) if lines else "Todavía no hay datos para mostrar.",
                          color=discord.Color.gold())
    mins = int(age_seconds // 60)
    when = "recién actualizado" if mins < 1 else f"actualizado hace {mins} min"
    note = " · ⚠️ AlbionBB no respondió, son datos guardados" if stale else ""
    embed.set_footer(text=f"Fuente: AlbionBB · {total_players} jugadores en la tabla · {when}{note}")
    return embed


async def _albion_top_fetch() -> tuple:
    """Devuelve (jugadores, edad_en_segundos, desactualizado). Usa caché de 5 min; si AlbionBB falla,
    sirve el último resultado guardado (hasta 6 h). Si no hay nada guardado, lanza el error."""
    async with _albion_top_lock:
        now = time.time()
        if _albion_top_cache["players"] and now - _albion_top_cache["at"] < ALBION_TOP_CACHE_SECONDS:
            return _albion_top_cache["players"], now - _albion_top_cache["at"], False
        headers = {"User-Agent": "Mozilla/5.0 (compatible; SmoggBot/1.0)", "Accept": "text/html"}
        try:
            async with bot.http_session.get(ALBION_TOP_URL, headers=headers,
                                            timeout=aiohttp.ClientTimeout(total=ALBION_TIMEOUT_SECONDS)) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"HTTP {resp.status}")
                html = await resp.text()
            players = _albion_top_players(html)
            _albion_top_cache.update(at=time.time(), players=players)
            return players, 0.0, False
        except Exception as exc:
            print(f"!top*: no pude leer AlbionBB: {type(exc).__name__}: {exc}")
            if _albion_top_cache["players"] and now - _albion_top_cache["at"] < ALBION_TOP_STALE_SECONDS:
                return _albion_top_cache["players"], now - _albion_top_cache["at"], True
            raise


async def _albion_top_build(member, guild, key: str):
    """Lógica común de !top* y /top*. Devuelve (texto_de_error, embed, es_privado).
    Solo uno de los dos primeros viene con valor."""
    if guild is None or not isinstance(member, discord.Member):
        return ("Este comando solo funciona dentro del servidor.", None, True)
    member_role = guild.get_role(MIEMBRO_ROLE_ID)
    if not ((member_role is not None and member_role in member.roles) or is_staff_member(member)):
        return ("❌ Este comando es solo para miembros de la guild.", None, True)
    now = time.time()
    if now - _albion_top_last_use.get(member.id, 0.0) < ALBION_TOP_USER_COOLDOWN:
        return ("⏳ Esperá unos segundos antes de volver a usar este comando.", None, True)
    _albion_top_last_use[member.id] = now
    try:
        players, age, stale = await _albion_top_fetch()
    except Exception as exc:
        return ("❌ No pude leer los datos de AlbionBB. Probá de nuevo en un rato.\n"
                f"Detalle técnico (para el administrador): `{type(exc).__name__}: {str(exc)[:300]}`", None, True)
    rows = _albion_top_rank(players, key)
    return (None, _albion_top_embed(key, rows, len(players), age, stale), False)


async def _albion_top_command(ctx: commands.Context, key: str):
    async with ctx.typing():
        error, embed, _private = await _albion_top_build(ctx.author, ctx.guild, key)
    if ctx.guild is None:
        return
    if error:
        return await ctx.reply(error, mention_author=False, delete_after=15 if error.startswith("⏳") else None)
    await ctx.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())


async def _albion_top_slash(interaction: discord.Interaction, key: str):
    """Versión con /. El resultado se publica en el canal (público); los errores los ve solo quien lo usó."""
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        return await respond_ephemeral(interaction, "❌ Este comando solo funciona dentro del servidor.")
    await interaction.response.defer()
    error, embed, _private = await _albion_top_build(interaction.user, interaction.guild, key)
    if error:
        return await interaction.followup.send(error, ephemeral=True)
    await interaction.followup.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())


@bot.command(name="topkills")
async def topkills_cmd(ctx: commands.Context):
    await _albion_top_command(ctx, "kills")


@bot.command(name="topattend")
async def topattend_cmd(ctx: commands.Context):
    await _albion_top_command(ctx, "attendance")


@bot.command(name="topdeaths")
async def topdeaths_cmd(ctx: commands.Context):
    await _albion_top_command(ctx, "deaths")


@bot.tree.command(name="topkills", description="Top 10 de kills de la guild (AlbionBB)", guild=GUILD_OBJ)
async def topkills_slash(interaction: discord.Interaction):
    await _albion_top_slash(interaction, "kills")


@bot.tree.command(name="topattend", description="Top 10 de asistencia de la guild (AlbionBB)", guild=GUILD_OBJ)
async def topattend_slash(interaction: discord.Interaction):
    await _albion_top_slash(interaction, "attendance")


@bot.tree.command(name="topdeaths", description="Top 10 de muertes de la guild (AlbionBB)", guild=GUILD_OBJ)
async def topdeaths_slash(interaction: discord.Interaction):
    await _albion_top_slash(interaction, "deaths")


# ============================================================
# /help: lista de comandos
# ============================================================
# (sección, [(comando, quién puede usarlo, explicación breve)])
HELP_SECTIONS = [
    ("🛡️ CTA y composiciones", [
        ("/cta nombre [tiempo] [tab] [city]", "Staff, Reclutador, Content creator", "Publica una CTA con una composición guardada. Dura 40 min por defecto (mínimo 10). La gente se anota escribiendo su número en el hilo. Si la composición tiene track, tab y city son obligatorios."),
        ("/cta_activas", "Todos", "Muestra las CTA abiertas y cuándo cierran."),
        ("/cta_cerrar cta_id", "Staff, Reclutador", "Cierra una CTA antes de tiempo."),
        ("/cta_asignar · /cta_liberar", "Staff, Reclutador", "Pone o saca a un jugador de una posición."),
        ("/cta_historial", "Todos", "Últimas CTA cerradas."),
        ("/compo crear · listar · ver · editar", "Staff (listar y ver: todos)", "Crear y administrar composiciones: parties, posiciones y roles necesarios para anotarse. En el editor, «Editar party completa» te muestra la lista de la party para reescribirla de una."),
        ("/compo duplicar · renombrar · eliminar", "Staff", "Copiar, renombrar o borrar una composición."),
        ("/compo rol_party nombre party rol", "Staff", "Rol general de una party: sin ese rol nadie se anota en ella (además de los roles propios de cada posición)."),
        ("/compo legacy nombre activar", "Staff", f"true/false: si está activada, en cada /cta con esa composición la Party 1 es solo para el rol Legacy los primeros {CTA_LEGACY_MINUTES} minutos."),
        ("/compo track nombre activar", "Staff", "true/false: con track, cada /cta con esa composición pide Tab y City y, al cerrar la inscripción, genera el split (como /transparencia) con la gente anotada, sin crear rol. Los que se anotan tarde quedan en el hilo del split para que decidas si los sumás."),
        ("/compo builds nombre link", "Staff", "Pone (o quita) el link de builds que aparece como «Builds» en el panel de la CTA."),
    ]),
    ("✍️ Dentro del hilo de una CTA", [
        ("`21`", "Todos", "Te anota en la posición 21 (si escribís otro número libre, te cambia)."),
        ("`-21` · `salir`", "Todos", "Te saca de la posición 21 / de donde estés."),
        ("`@persona 21` · `@persona -21`", "Staff", "Anota o saca a otra persona de una posición."),
    ]),
    ("📊 Estadísticas de la guild", [
        ("/topkills · /topattend · /topdeaths", "Miembros", "Top 10 de kills, asistencia o muertes de la guild (datos de AlbionBB). También con `!topkills`, `!topattend`, `!topdeaths`."),
        ("/comparar killboard rol guild", "Staff, Gestionar roles", "Compara los jugadores de una guild en un killboard de AlbionBB con la gente que tiene un rol (por apodo): quiénes estuvieron sin el rol y quiénes tienen el rol y no estuvieron."),
        ("/addrolllist apodos rol", "Staff, Gestionar roles", "Da un rol a una lista de apodos de Discord (separados por coma). Si escribís una palabra crea el rol; si etiquetás un rol existente lo usa. Te avisa qué apodos no encontró."),
        ("/disarray numero", "Miembros", "Cuántos jugadores hacen falta para un nivel de Disarray (sin contar monturas de batalla). Niveles 1 a 67."),
        ("/verratas", "Staff", "Lista los miembros de Discord cuyo apodo no figura en la guild de Albion."),
        ("/rolalbion ids", "Gestionar roles", "Crea un rol y se lo da a los participantes de una batalla de AlbionBB."),
    ]),
    ("💰 Economía y timers", [
        ("/calcularsplit total reparacion", "Staff", "Calcula el split: descuenta taxes y reparación y suma las bolsitas."),
        ("/tabsell nombre_tab …", "Todos", "Calcula el precio de venta de un tab y arma el panel."),
        ("/deltimer material mapa tiempo [tier]", "Rol Timers, Staff", "Elimina un timer del panel completando los mismos datos con que lo cargaste."),
        ("/apuesta enunciado opciones", "Admin / rol autorizado", "Crea una apuesta con Wuju Coins: panel público + hilo. Opciones separadas por coma."),
        ("/apostar · !apostar opción cantidad", "Todos (dentro del hilo)", "Apuesta Wuju Coins a una opción de una apuesta abierta. Se descuentan al instante."),
        ("/cerrarapuesta · /resolverapuesta opcion · /cancelarapuesta", "Quien creó la apuesta", "Cierra las apuestas, define la opción ganadora (reparte el pozo) o cancela y devuelve todo."),
        ("!dados cantidad", "Habilitado con /perms-canal y /perms-comando", "Partida de dados de 5 minutos. El panel se publica en el canal definido con `/canal dados`; botón Anotarse (con confirmación). Gana el número más alto y se lleva todo el pozo (empates vuelven a tirar). Máx. 5.000.000; no hay baja."),
        ("/canal dados canal", "Council / Admin", "Define el canal donde se publican los paneles de `!dados`."),
        ("/panel apuestas", "Admin / rol autorizado", "Publica los paneles de apuestas: Top apostado, Top ganado (solo positivos), Top perdido (solo negativos) y la lista negra del casino. Solo aparece gente que sigue en el servidor; al salir se reinician sus datos."),
        ("/addhorautc [categoria]", "Council / Admin", "Crea un canal de voz «🕒 HH:MM UTC» (solo para mirar) que se renombra cada 10 minutos, el máximo que permite Discord de forma segura."),
        ("/transparencia titulo [tab] [city] [rol] [total] [reparacion] [bolsitas] [looters]", "Council / Admin", "Crea un split: panel con botón Cerrar split + hilo con la lista de personas y el cálculo (taxes, total por persona). Al cerrar (solo Council) se acredita a cada persona su parte y a cada looter su tax."),
        ("/transparencia-ava titulo [tab] [city] [rol] [total] [reparacion] [bolsitas] [scouts] [pago_scout]", "Council / Admin", "Igual que /transparencia pero sin looters: cada scout cobra un monto fijo (pago_scout) que se descuenta del total a repartir. Las bolsitas se suman al total."),
        ("/editar [id]", "Council / Admin / creador", "Edita un split abierto (usalo dentro de su hilo): datos, montos y taxes, looters (agregar/editar/quitar) y personas o roles. El hilo se actualiza al instante. Si el split ya está cerrado, el Council puede reabrirlo: al «Confirmar edición» se resta a cada persona lo que cobró antes (puede quedar en negativo) y se reparte de nuevo con los datos nuevos."),
        ("/panel cambiomoneda [canal]", "Council / Admin", "Publica dos paneles con tickets: vender Wuju Coins por silver (el Council acepta y retira el balance) y comprar Wuju Coins (depósito + foto; el Council los agrega con doble confirmación)."),
        ("/casino abrir|cerrar", "Council", "Abre o cierra el casino: mientras está cerrado se bloquean /dados, /coinflip y /1v1."),
        ("/casinoban usuario", "Council", "Bloquea a una persona del casino; si lo usás de nuevo con la misma persona, la saca de la lista."),
        ("/1v1 usuario cantidad", "Miembros", "Retá a alguien a un duelo de cara o cruz: la persona retada acepta o rechaza y el que sale se queda con todo. Sin tope (hasta tu balance); la persona retada tiene que tener al menos lo apostado. Se habilita en un canal con /perms-canal (como dados y coinflip). Con el casino cerrado no se pueden crear duelos nuevos, pero sí aceptar los ya creados."),
        ("!coinflip cantidad", "Habilitado con /perms-canal y /perms-comando", "Desafío de cara o cruz de 5 minutos: lo acepta UNA persona y el que sale se queda con todo. Solo uno vivo a la vez (igual que !dados); el panel queda en el canal donde lo creás."),
        ("/apostartodo opción · !apostartodo opción", "Todos (dentro del hilo)", "Apuesta TODO tu saldo a una opción de una apuesta abierta, con confirmación."),
        ("/guild-shop [canal]", "Council", "Publica el Top Vendedores y el panel de compra del shop de la guild (botón Comprar → ticket con Seller)."),
        ("/canal logs guildshop [canal]", "Council", "Define el canal donde se registra cada transacción del Guild Shop."),
        ("/set-balance usuario cantidad", "Admin", "Deja el balance de una persona exactamente en ese número."),
        ("/eco-stats", "Staff", "Muestra el total de Wuju Coins cargado en el servidor."),
        ("/historico-bal reset", "Admin", "Pone en cero el Balance histórico de todos (no toca los balances)."),
        ("/verhistorial [usuario]", "Todos / Staff", "Tus últimos 20 movimientos de Wuju Coins por DM. El staff puede ver los de otra persona (solo lo ves vos)."),
        ("/timeradd material mapa tiempo [tier]", "Rol Timers", "Agrega un timer (hora UTC en que sale) al panel que corresponde: Nodos, Vortex y Cores o Dragones. Los paneles se ordenan solos por hora y etiquetan a quien puso el timer. Cada línea muestra en cuánto sale (timestamp de Discord) y la hora UTC fija."),
    ]),
    ("🎁 Sorteos y roles", [
        ("/sorteo premio tiempo [ganadores]", "Staff", "Crea un sorteo con botón para anotarse; elige ganadores reales y sin repetir."),
        ("/list_role rol", "Staff", "Lista los miembros que tienen un rol."),
        ("/delrole rol", "Staff", "Elimina un rol sin mencionar a nadie."),
        ("!addroll-list NombreRol @u1 @u2 …", "Staff", "Crea un rol y se lo da a las personas mencionadas."),
    ]),
    ("⚙️ Administración", [
        ("!panel", "Administradores", "Publica el panel de reclutamiento."),
        ("!panel_foco", "Administradores", "Publica el panel de tickets de foco."),
        ("/borrar", "Staff, Gestionar mensajes", "Borra los últimos 100 mensajes del canal donde lo uses (no toca fijados ni los paneles del bot)."),
        ("/help", "Todos", "Muestra esta lista."),
    ]),
]


def build_help_embeds() -> list:
    embeds = []
    for title, entries in HELP_SECTIONS:
        lines = [f"**{cmd}** · _{who}_\n{text}" for cmd, who, text in entries]
        embeds.append(discord.Embed(title=title, description="\n\n".join(lines), color=discord.Color.blurple()))
    embeds[0].set_author(name="Comandos del bot de Smogg")
    embeds[-1].set_footer(text="Si un comando no te aparece o te dice que no tenés permiso, es porque necesita un rol que no tenés.")
    return embeds


@bot.tree.command(name="help", description="Lista y explica los comandos del bot", guild=GUILD_OBJ)
async def help_slash(interaction: discord.Interaction):
    embeds = build_help_embeds()
    # Discord permite hasta 10 embeds y 6000 caracteres por mensaje: se manda en grupos que entren.
    groups, current, used = [], [], 0
    for emb in embeds:
        size = len(emb)          # discord.Embed: total de caracteres
        if current and (len(current) >= 10 or used + size > 5500):
            groups.append(current); current, used = [], 0
        current.append(emb); used += size
    if current:
        groups.append(current)
    await interaction.response.send_message(embeds=groups[0], ephemeral=True)
    for group in groups[1:]:
        await interaction.followup.send(embeds=group, ephemeral=True)


# ============================================================
# /borrar: limpia los últimos 100 mensajes del canal
# ============================================================
BORRAR_LIMIT = 100


def borrar_protected_ids(guild_id: int) -> set:
    """Mensajes del bot que no se deben borrar porque son paneles que siguen funcionando."""
    ids = set()
    queries = (
        ("SELECT message_id FROM timer_panels WHERE guild_id=?", (guild_id,)),
        ("SELECT message_id FROM gshop_panels WHERE guild_id=?", (guild_id,)),
        ("SELECT message_id FROM cta_runs WHERE guild_id=? AND status='open' AND message_id IS NOT NULL", (guild_id,)),
        ("SELECT message_id FROM bets WHERE guild_id=? AND status IN ('open','closed') AND message_id IS NOT NULL", (guild_id,)),
    )
    for query, params in queries:
        try:
            ids.update(int(r[0]) for r in db_fetchall(query, params) if r[0])
        except Exception as exc:
            print(f"borrar: no pude leer paneles protegidos ({exc})")
    return ids


def borrar_allowed(member) -> bool:
    perms = getattr(member, "guild_permissions", None)
    return bool((perms is not None and (perms.manage_messages or perms.manage_guild))
                or any(r.id == STAFF_ROLE_ID for r in getattr(member, "roles", [])))


@bot.tree.command(name="borrar", description=f"Borra los últimos {BORRAR_LIMIT} mensajes de este canal (no toca fijados ni paneles del bot)", guild=GUILD_OBJ)
async def borrar_slash(interaction: discord.Interaction):
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        return await respond_ephemeral(interaction, "❌ Solo disponible en el servidor.")
    if not borrar_allowed(interaction.user):
        return await respond_ephemeral(interaction, "❌ Solo el Staff o quien pueda gestionar mensajes puede usar este comando.")
    channel = interaction.channel
    if channel is None or not hasattr(channel, "purge"):
        return await respond_ephemeral(interaction, "❌ No puedo borrar mensajes en este tipo de canal.")
    await interaction.response.defer(ephemeral=True)
    protected = borrar_protected_ids(interaction.guild.id)
    try:
        deleted = await channel.purge(limit=BORRAR_LIMIT, check=lambda m: not m.pinned and m.id not in protected)
    except discord.Forbidden:
        return await interaction.followup.send("❌ No tengo permiso de **Gestionar mensajes** en este canal.", ephemeral=True)
    except discord.HTTPException as exc:
        return await interaction.followup.send(f"❌ Discord no me dejó borrar: {exc}", ephemeral=True)
    await interaction.followup.send(f"🧹 Borré **{len(deleted)}** mensajes.", ephemeral=True)


# ---------- READY (AL FINAL, así PanelView existe) ----------
@bot.event
async def on_ready():
    print(f"✅ Bot conectado como {bot.user}")

    # Views persistentes (1 vez)
    if not hasattr(bot, "_views_registered"):
        bot.add_view(PanelView())
        bot.add_view(FocoPanelView())
        bot.add_view(FocoTicketActionView())
        bot.add_view(RecruitView())
        bot.add_view(GiveawayJoinView())
        bot.add_view(SplitSoldView())
        bot.add_view(TimerDeleteView())
        bot.add_view(CTAPanelView())
        bot._views_registered = True
        print("✅ Views persistentes registradas")

    # Restaurar datos locales una sola vez por proceso. Los sorteos y sus anotados
    # se cargan antes de volver a programar sus finales.
    guild = bot.get_guild(GUILD_ID)
    if guild:
        try:
            cta_ensure_brawl(guild.id, bot.user.id if bot.user else 0)
        except Exception as exc:
            print(f"❌ No se pudo inicializar la composición Brawl: {exc}")
    if guild and not getattr(bot, "_persistent_state_loaded", False):
        try:
            restore_persistent_state(guild)
            bot._persistent_state_loaded = True
            print(f"✅ SQLite restaurado: {len(giveaways)} sorteos, {len(timers)} timers y {len(ticket_images)} imágenes de postulación")
        except Exception as exc:
            print(f"❌ Error restaurando SQLite: {exc}")

    # Reanudar también en reconexiones: si un intento anterior no pudo acceder al canal,
    # el registro sigue en SQLite y se vuelve a programar.
    if guild:
        for g in list(giveaways.values()):
            start_giveaway_task(guild.id, g)

    # CTA: recuperar paneles persistentes y reanudar cierres después de reinicios.
    if not cta_housekeeping.is_running():
        cta_housekeeping.start()
        print("✅ CTA housekeeping iniciado")
    if not getattr(bot, "_cta_views_restored", False):
        for cta_row in db_fetchall("SELECT id, status, message_id FROM cta_runs WHERE message_id IS NOT NULL"):
            try:
                bot.add_view(CTAPanelView(run_id=int(cta_row["id"]), closed=cta_row["status"] != "open"), message_id=int(cta_row["message_id"]))
            except Exception as exc:
                print(f"CTA #{cta_row['id']}: no se pudo restaurar la vista: {exc}")
        bot._cta_views_restored = True
        # Recuperar las CTA abiertas: hilo, tablero y panel (las inscripciones están en SQLite).
        async def _restore_open_ctas():
            # CTA cerradas hace menos de un día cuya asignación de rol quedó pendiente o cortada por un reinicio.
            limit = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
            db_execute("UPDATE cta_runs SET role_id=NULL WHERE status='closed' AND role_id=-1 AND closed_at>?", (limit,))
            for r in db_fetchall("SELECT id FROM cta_runs WHERE status='closed' AND role_id IS NULL AND closed_at>?", (limit,)):
                try:
                    await cta_assign_signup_role(int(r["id"]))
                except Exception as exc:
                    print(f"CTA #{r['id']}: no se pudo asignar el rol pendiente: {exc}")
            # CTA track cerradas hace menos de un día cuyo split quedó pendiente o cortado por un reinicio.
            db_execute("UPDATE cta_runs SET split_state=NULL WHERE status='closed' AND track=1 AND split_state=-1 AND closed_at>?", (limit,))
            for r in db_fetchall("SELECT id FROM cta_runs WHERE status='closed' AND track=1 AND split_state IS NULL AND closed_at>?", (limit,)):
                try:
                    await cta_create_track_split(int(r["id"]))
                except Exception as exc:
                    print(f"CTA #{r['id']}: no se pudo generar el split pendiente: {exc!r}")
            for r in db_fetchall("SELECT id FROM cta_runs WHERE status='open'"):
                try:
                    await cta_ensure_thread_and_board(int(r["id"]))
                except Exception as exc:
                    print(f"CTA #{r['id']}: no se pudo recuperar: {exc}")
        asyncio.create_task(_restore_open_ctas())

    # Tasks
    if not timer_posts_cleanup.is_running():
        timer_posts_cleanup.start()
        print("✅ Timer posts cleanup iniciado")

    if not timers_housekeeping.is_running():
        timers_housekeeping.start()
        print("✅ Timers housekeeping iniciado")

    guild = bot.get_guild(GUILD_ID)
    if guild:
        try:
            await timer_panel_refresh(guild)          # deja los 3 paneles de timers creados y al día
        except Exception as exc:
            print(f"timers: no pude preparar los paneles: {exc}")
        await ensure_timers_board_message(guild)
        await update_timers_board(guild)

# ✅ Ignorar comandos desconocidos (!bal, etc.)
@bot.event
async def on_command_error(ctx: commands.Context, error: Exception):
    if isinstance(error, commands.CommandNotFound):
        return
    raise error

def _is_image_attachment(att: discord.Attachment) -> bool:
    ct = (att.content_type or "").lower()
    if ct.startswith("image/"):
        return True
    # fallback por extensión, por si content_type viene vacío
    name = (att.filename or "").lower()
    return name.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif"))

def _get_recruit_uid_from_channel(ch: discord.abc.GuildChannel) -> Optional[int]:
    if not isinstance(ch, discord.TextChannel):
        return None
    info = parse_recruit_topic(ch.topic)
    uid = info.get("uid")
    if not uid:
        return None
    try:
        return int(uid)
    except ValueError:
        return None

@bot.event
async def on_message(message: discord.Message):
    await bot.process_commands(message)

    if message.guild is None or message.author.bot:
        return

    # ✅ CTA: inscripción escribiendo el número de la posición en el hilo de la CTA
    if isinstance(message.channel, discord.Thread):
        try:
            if await cta_handle_thread_message(message):
                return
        except Exception as exc:
            print(f"Error procesando mensaje en hilo de CTA: {exc}")
            return

    # ✅ RECRUIT: guardar screenshot aunque el bot haya reiniciado (por topic)
    recruit_uid = _get_recruit_uid_from_channel(message.channel)
    if recruit_uid is not None:
        # Solo guardamos si el que manda la imagen es el postulante del ticket
        if message.author.id != recruit_uid:
            return

        if not message.attachments:
            return

        img = None
        for att in message.attachments:
            if _is_image_attachment(att):
                img = att
                break
        if img is None:
            return

        save_ticket_image(message.author.id, img.url, img.filename or "image.png")
        return

    # (Opcional) Si querés conservar tu modo viejo para tickets recién creados:
    opener_channel_id = active_applications.get(message.author.id)
    if opener_channel_id is None or opener_channel_id != message.channel.id:
        return

    if not message.attachments:
        return

    img = None
    for att in message.attachments:
        if _is_image_attachment(att):
            img = att
            break
    if img is None:
        return

    ticket_images[message.author.id] = {
        "url": img.url,
        "filename": img.filename or "image.png",
    }
# ---------- RUN ----------
bot.run(TOKEN)



