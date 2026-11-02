"""Filtro de servidores: el bot solo reacciona a lo que pasa en su servidor (y en los extra que se permitan a propósito).

Si el bot está invitado a otro servidor (p. ej. para leer un canal), sus eventos —mensajes, cambios de rol, voz, reacciones— no tienen que
disparar la lógica de este servidor. Se corta en un solo lugar: antes de repartir cada evento a los listeners."""
from typing import Iterable, Optional


def event_guild_id(args) -> Optional[int]:
    """Servidor al que pertenece un evento (mensaje, miembro, payload crudo...), o None si no se puede saber (mensajes directos, eventos del propio bot)."""
    if not args:
        return None
    first = args[0]
    guild = getattr(first, "guild", None)
    gid = getattr(guild, "id", None)
    if gid is None:
        gid = getattr(first, "guild_id", None)
    try:
        return int(gid) if gid is not None else None
    except (TypeError, ValueError):
        return None


def event_allowed(args, allowed: Iterable[int]) -> bool:
    """True si el evento es de un servidor permitido o si no se sabe de cuál (no se descarta lo que no se puede identificar)."""
    gid = event_guild_id(args)
    return gid is None or gid in set(allowed)
