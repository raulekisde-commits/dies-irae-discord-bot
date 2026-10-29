"""Paginación con botones Anterior / Siguiente para listas largas (Top, rankings)."""
from typing import List, Optional

import discord


def chunk(items: list, size: int = 10) -> list:
    return [items[i:i + size] for i in range(0, len(items), size)] or [[]]


class PagedView(discord.ui.View):
    """Muestra una lista de embeds de a uno. Si `owner_id` está, solo esa persona navega (la lista es suya)."""

    def __init__(self, pages: List[discord.Embed], owner_id: Optional[int] = None, timeout: float = 300.0):
        super().__init__(timeout=timeout)
        self.pages, self.owner_id, self.index = pages, owner_id, 0
        self.message = None
        self._sync()

    def _sync(self):
        self.prev.disabled = self.index <= 0
        self.next.disabled = self.index >= len(self.pages) - 1

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.owner_id is not None and interaction.user.id != self.owner_id:
            await interaction.response.send_message("Esta lista es de otra persona. Usá el comando para ver la tuya.", ephemeral=True)
            return False
        return True

    async def _go(self, interaction: discord.Interaction, step: int):
        self.index = max(0, min(len(self.pages) - 1, self.index + step))
        self._sync()
        await interaction.response.edit_message(embed=self.pages[self.index], view=self)

    @discord.ui.button(label="Anterior", emoji="◀️", style=discord.ButtonStyle.secondary)
    async def prev(self, interaction: discord.Interaction, button):
        await self._go(interaction, -1)

    @discord.ui.button(label="Siguiente", emoji="▶️", style=discord.ButtonStyle.primary)
    async def next(self, interaction: discord.Interaction, button):
        await self._go(interaction, +1)

    async def on_timeout(self):
        if self.message is not None:
            try:
                await self.message.edit(view=None)
            except Exception:
                pass
