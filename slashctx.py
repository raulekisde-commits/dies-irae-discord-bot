"""Adaptador para reutilizar un comando de prefijo (`!comando`) como comando slash (`/comando`).

La lógica del comando se escribe una sola vez: el slash arma un "contexto" con lo mínimo que usan los comandos
(author, guild, channel, message.id, reply, send) y lo ejecuta. Los mensajes de error (que empiezan con ❌) se
responden en privado; el resultado de un comando exitoso es público, igual que con `!`.
"""
from types import SimpleNamespace
from typing import Any

PRIVATE_PREFIXES = ("❌", "Uso:", "ℹ️")


class SlashContext:
    def __init__(self, interaction):
        self.interaction = interaction
        self.author = interaction.user
        self.guild = interaction.guild
        self.channel = interaction.channel
        self.bot = getattr(interaction, "client", None)
        self.message = SimpleNamespace(id=interaction.id, content="", mentions=[], role_mentions=[], author=interaction.user)

    async def _send(self, content: Any = None, **kw):
        kw.pop("mention_author", None)
        kw.pop("reference", None)
        ephemeral = kw.pop("ephemeral", None)
        if ephemeral is None:
            ephemeral = isinstance(content, str) and content.startswith(PRIVATE_PREFIXES)
        payload = {k: v for k, v in kw.items() if v is not None}
        if content is not None:
            payload["content"] = content
        payload["ephemeral"] = bool(ephemeral)
        if self.interaction.response.is_done():
            return await self.interaction.followup.send(**payload)
        return await self.interaction.response.send_message(**payload)

    reply = _send
    send = _send
