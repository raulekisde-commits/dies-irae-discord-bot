"""Filtro para los BOTONES de los juegos (anotarse a dados, aceptar coinflip o duelo).

Los comandos ya pasan por el filtro de registro y por los permisos de la economía, pero un botón es otra puerta: acá se exige,
igual que en los comandos, tener el rol Miembro Y estar registrado (los administradores quedan exentos)."""
from typing import Optional


async def button_block_reason(bot, user) -> Optional[str]:
    """None si puede seguir; si no, el texto (privado) que se le muestra."""
    if getattr(user, "bot", False):
        return None
    perms = getattr(user, "guild_permissions", None)
    if perms is not None and perms.administrator:
        return None
    eco_cog = bot.get_cog("EconomyCog")
    member_role_id = getattr(eco_cog, "member_role_id", None)
    if member_role_id and member_role_id not in {r.id for r in getattr(user, "roles", [])}:
        return "🔒 Solo los miembros de la guild pueden jugar."
    gate = getattr(bot, "registration_gate", None)
    if gate is not None and not await gate.allowed(user, "coinflip"):
        return gate.MESSAGE
    return None
