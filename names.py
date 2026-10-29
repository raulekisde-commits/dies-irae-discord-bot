"""Nombres para mostrar en paneles: los menciones <@id> dentro de un embed se ven como número cuando el cliente no conoce a la persona,
así que en los rankings se muestra el nombre."""
from typing import Dict, Iterable

import discord

MAX_FETCH = 25            # tope de consultas a Discord por panel (lo normal es que todos estén en caché)


def clean(name) -> str:
    return discord.utils.escape_markdown(str(name))[:40] or "?"


def fallback(uid: int) -> str:
    return f"Usuario {uid}"


async def resolve_names(guild, user_ids: Iterable[int], max_fetch: int = MAX_FETCH) -> Dict[int, str]:
    """{id: nombre}. Usa la caché del servidor; si falta alguien intenta pedirlo a Discord (hasta `max_fetch`); si no, 'Usuario <id>'."""
    out: Dict[int, str] = {}
    fetched = 0
    for uid in dict.fromkeys(int(u) for u in user_ids):
        member = guild.get_member(uid) if guild is not None else None
        if member is None and guild is not None and fetched < max_fetch:
            fetched += 1
            try:
                member = await guild.fetch_member(uid)
            except Exception:
                member = None
        out[uid] = clean(getattr(member, "display_name", None) or getattr(member, "name", None)) if member is not None else fallback(uid)
    return out
