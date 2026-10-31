"""Seguimiento del canal de voz de las CTA: desde que cierra la inscripción hasta que Council/Caller cierran la CTA.
Se registra cada entrada/salida; presente = al menos 2 minutos en total. No se excluye a nadie."""
import asyncio
import traceback
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Set, Tuple

import discord
from discord.ext import commands

import ctautil as cu
from dbutil import connect, write_tx

MIN_PRESENT_SECONDS = 120

SCHEMA = """
CREATE TABLE IF NOT EXISTS cta_voice_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cta_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    joined_at TEXT NOT NULL,
    left_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_voice_cta ON cta_voice_sessions(cta_id, user_id);
"""


def _iso(dt: Optional[datetime] = None) -> str:
    return (dt or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()


def _dt(text: str) -> datetime:
    d = datetime.fromisoformat(text)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def init_schema(db_path: str) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


def tracking_runs(db_path: str) -> List[int]:
    """CTA con seguimiento activo: la inscripción ya terminó y todavía no se cerró ni canceló."""
    conn = connect(db_path)
    try:
        return [int(r[0]) for r in conn.execute("SELECT id FROM cta_runs WHERE status='closed' AND final_state IS NULL")]
    except Exception:
        return []
    finally:
        conn.close()


def sync(db_path: str, cta_id: int, present_ids: Iterable[int], now: Optional[datetime] = None) -> None:
    """Deja las sesiones abiertas igual a quienes están ahora en el canal (abre las que faltan, cierra las que sobran)."""
    present: Set[int] = set(int(x) for x in present_ids)
    conn = connect(db_path)
    try:
        with write_tx(conn):
            open_rows = conn.execute("SELECT id, user_id FROM cta_voice_sessions WHERE cta_id=? AND left_at IS NULL", (cta_id,)).fetchall()
            open_users = {int(r["user_id"]) for r in open_rows}
            for r in open_rows:
                if int(r["user_id"]) not in present:
                    conn.execute("UPDATE cta_voice_sessions SET left_at=? WHERE id=?", (_iso(now), r["id"]))
            for uid in present - open_users:
                conn.execute("INSERT INTO cta_voice_sessions(cta_id, user_id, joined_at) VALUES (?,?,?)", (cta_id, uid, _iso(now)))
    finally:
        conn.close()


def user_joined(db_path: str, cta_id: int, user_id: int, now: Optional[datetime] = None) -> None:
    conn = connect(db_path)
    try:
        with write_tx(conn):
            if not conn.execute("SELECT 1 FROM cta_voice_sessions WHERE cta_id=? AND user_id=? AND left_at IS NULL", (cta_id, user_id)).fetchone():
                conn.execute("INSERT INTO cta_voice_sessions(cta_id, user_id, joined_at) VALUES (?,?,?)", (cta_id, user_id, _iso(now)))
    finally:
        conn.close()


def user_left(db_path: str, cta_id: int, user_id: int, now: Optional[datetime] = None) -> None:
    conn = connect(db_path)
    try:
        with write_tx(conn):
            conn.execute("UPDATE cta_voice_sessions SET left_at=? WHERE cta_id=? AND user_id=? AND left_at IS NULL", (_iso(now), cta_id, user_id))
    finally:
        conn.close()


def stop(db_path: str, cta_id: int, now: Optional[datetime] = None) -> None:
    """Fin del seguimiento: cierra las sesiones que sigan abiertas."""
    conn = connect(db_path)
    try:
        with write_tx(conn):
            conn.execute("UPDATE cta_voice_sessions SET left_at=? WHERE cta_id=? AND left_at IS NULL", (_iso(now), cta_id))
    finally:
        conn.close()


def seconds_by_user(db_path: str, cta_id: int, now: Optional[datetime] = None) -> Dict[int, float]:
    now = now or datetime.now(timezone.utc)
    conn = connect(db_path)
    try:
        out: Dict[int, float] = {}
        for r in conn.execute("SELECT user_id, joined_at, left_at FROM cta_voice_sessions WHERE cta_id=?", (cta_id,)):
            end = _dt(r["left_at"]) if r["left_at"] else now
            out[int(r["user_id"])] = out.get(int(r["user_id"]), 0.0) + max(0.0, (end - _dt(r["joined_at"])).total_seconds())
        return out
    finally:
        conn.close()


def present_users(db_path: str, cta_id: int, now: Optional[datetime] = None, minimum: int = MIN_PRESENT_SECONDS) -> Set[int]:
    return {u for u, secs in seconds_by_user(db_path, cta_id, now).items() if secs >= minimum}


def compare_with_signups(db_path: str, cta_id: int, now: Optional[datetime] = None) -> Tuple[List[int], List[int]]:
    """(anotados que NO estuvieron en voz, gente en voz que NO estaba anotada). Anotados = historial de posiciones."""
    signed = {int(p["user_id"]) for p in cu.positions(db_path, cta_id)}
    voice = present_users(db_path, cta_id, now)
    return sorted(signed - voice), sorted(voice - signed)


class CTAVoiceCog(commands.Cog):
    def __init__(self, bot, db_path: str, guild_id: int, channel_id: int = cu.CTA_VOICE_ID):
        self.bot, self.db_path, self.guild_id, self.channel_id = bot, db_path, guild_id, channel_id

    async def cog_load(self):
        await asyncio.to_thread(init_schema, self.db_path)

    async def run(self, fn, *a):
        return await asyncio.to_thread(fn, self.db_path, *a)

    def _members_in_channel(self) -> List[int]:
        guild = self.bot.get_guild(self.guild_id)
        ch = guild.get_channel(self.channel_id) if guild else None
        return [m.id for m in getattr(ch, "members", []) if not getattr(m, "bot", False)]

    async def start(self, cta_id: int):
        """Empieza (o retoma) el seguimiento de una CTA con quienes estén en el canal ahora."""
        await self.run(sync, cta_id, self._members_in_channel())

    async def stop_run(self, cta_id: int):
        await self.run(stop, cta_id)

    @commands.Cog.listener()
    async def on_ready(self):
        """Tras un reinicio: se vuelve a escanear el canal para no perder a quienes siguen conectados."""
        try:
            for cid in await self.run(tracking_runs):
                await self.start(cid)
        except Exception:
            traceback.print_exc()

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if getattr(member, "bot", False):
            return
        was = getattr(before.channel, "id", None) == self.channel_id
        now_in = getattr(after.channel, "id", None) == self.channel_id
        if was == now_in:
            return
        try:
            for cid in await self.run(tracking_runs):
                await self.run(user_joined if now_in else user_left, cid, member.id)
        except Exception:
            traceback.print_exc()


async def setup_ctavoice(bot, db_path: str, guild_id: int, channel_id: int = cu.CTA_VOICE_ID) -> CTAVoiceCog:
    cog = CTAVoiceCog(bot, db_path, guild_id, channel_id)
    await bot.add_cog(cog, guilds=[discord.Object(id=guild_id)])
    return cog
