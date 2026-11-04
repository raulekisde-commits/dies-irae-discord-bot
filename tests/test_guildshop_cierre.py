"""Guild Shop: el canal del ticket se borra solo 2 minutos después de confirmar la venta, y el panel se actualiza solo."""
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import test_guildshop as tg  # noqa: E402
from test_guildshop import run  # noqa: E402
import guildshop as gs  # noqa: E402
import guildshop_db as gdb  # noqa: E402
from dbutil import connect  # noqa: E402


class _Harness(tg.ShopDiscordTests):
    pass


for _name in [n for n in dir(tg.ShopDiscordTests) if n.startswith("test_")]:
    setattr(_Harness, _name, None)


class CierreTests(_Harness):
    def test_por_defecto_son_2_minutos(self):
        self.assertEqual(gs.SALE_CLOSE_DELAY_SECONDS, 120)

    def test_se_borra_solo_despues_de_confirmar_sin_bloquear(self):
        self.cog.sale_close_delay = 0.05
        self.fund(self.buyer, 10_000)
        ch, _ = self.open_ticket()
        self.start_sale(ch, self.seller, "wuju", "1000", "10")

        async def go():
            msg = ch.sent[-1]
            i = self.inter(self.seller2, ch, msg)
            await self.cog.on_confirm_pressed(i)
            self.assertFalse(ch.deleted)                               # la confirmación no espera los 2 minutos
            self.assertIn("se borra solo", ch.sent[-1].embeds[0].footer)
            await asyncio.sleep(0.1)
            self.assertTrue(ch.deleted)
        run(go())

    def test_al_reiniciar_borra_los_canales_vendidos_que_quedaron(self):
        self.fund(self.buyer, 10_000)
        ch, _ = self.open_ticket()
        self.bot.channels[ch.id] = ch
        t = gdb.get_ticket_by_channel(self.db, ch.id)
        conn = connect(self.db)
        conn.execute("UPDATE gshop_tickets SET status = 'sold', closed_at = ? WHERE id = ?",
                     ((datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(), t.id))
        conn.close()
        run(self.cog.sweep_sold_channels())
        self.assertTrue(ch.deleted)

    def test_el_panel_se_actualiza_solo(self):
        self.publish_panels()
        shop_msg = self.shopch.sent[-1]
        shop_msg.embeds[0].description = "texto viejo"
        run(self.cog.refresh_shop_panel())
        self.assertIn("Tickets de descuento", shop_msg.embeds[0].description)
        self.assertIn("30%", shop_msg.embeds[0].description)
