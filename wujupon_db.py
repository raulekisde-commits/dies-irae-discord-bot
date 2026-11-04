"""Wujupón (gachapon): catálogo, tiradas, cartas y canjes. Sin dependencias de Discord.

Reglas de integridad:
- Cada cambio de estado es una sola transacción (BEGIN IMMEDIATE) condicionada al estado actual: una carta no se puede
  canjear dos veces y dos clics no cobran dos veces.
- Compras, tiradas y canjes llevan `request_key` única (el id de la interacción): un reintento devuelve el mismo resultado.
- Comprar tiradas descuenta Wuju Coins con `economy_db.tx_move` en la MISMA transacción que suma las tiradas.
- Si el bot se reinicia a mitad de algo, la transacción se revierte completa: no quedan medias operaciones.

Probabilidades: pesos enteros sobre 100.000 (1 = 0,001 %). Son probabilidades individuales sobre el total de tiradas.
"""
import random
import secrets
import sqlite3
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import economy_db as eco
import embo_db
from dbutil import connect, utcnow_iso, write_tx

TOTAL_WEIGHT = 100_000
DEFAULT_PRICE = 500_000
MAX_BUY = 50
EMBO_SILVER_VALUE = 10_000          # 1 embo = 10.000 (Wuju = silver 1:1)

RARITIES: Dict[str, Tuple[str, str, int]] = {      # clave → (nombre, emoji, color)
    "common": ("Común", "⚪", 0xB0B3B8),
    "rare": ("Rara", "🔵", 0x3498DB),
    "epic": ("Épica", "🟣", 0x9B59B6),
    "legendary": ("Legendaria", "🟠", 0xE67E22),
    "mythic": ("Mítica", "🔴", 0xE74C3C),
}
RARITY_ORDER = list(RARITIES)

# Handlers automáticos implementados. Las cartas «Bot» sin handler todavía se canjean por encargo al Council.
AUTO_HANDLERS = ("wuju", "embo", "sell_cooldown", "photo", "shop_ticket")
SHOP_TICKET_PRIZE = "shop_discount"  # se usa desde el Guild Shop, no con «Canjear»
PHOTO_ASSET = "gendarme_photo"       # imagen que se guarda en la base (nunca en el repositorio)

# (clave, nombre, rareza, peso/100.000, origen Bot|Staff, handler, cantidad, costo, moneda del costo, afecta a terceros,
#  descripción, forzada)
# - origen «Bot»: lo va a hacer el bot; mientras no tenga handler se entrega por encargo al Council.
# - forzada (☠️ cartas malas): se aplica sola al salir; no se guarda en el inventario (va directo al Council).
# Los pesos suman 100.000 y respetan 45 / 35 / 15,2 / 4,6 / 0,2 por rareza.
SHIELD_NOTE = " El escudo y los inmunes la bloquean."
CATALOG = [
    # ⚪ Comunes — 45 %
    ("casino_block", "Bloqueo de casino", "common", 5200, "Bot", None, 0, 0, "", 1,
     "Bloqueás a un jugador del casino hasta que el casino se cierre y se vuelva a abrir." + SHIELD_NOTE, 0),
    ("flex_cta", "Anotación libre en CTA", "common", 6200, "Bot", None, 0, 0, "", 0,
     "En el próximo /cta te podés anotar en el rol que quieras.", 0),
    ("sell_cooldown", "Cancelar cooldown de cobro", "common", 6200, "Bot", "sell_cooldown", 0, 0, "", 0,
     "Te saca el cooldown de «Cobrar balance». Si no tenés cooldown, la carta no se gasta.", 0),
    ("gendarme_photo", "Foto de la cola de Gendarme", "common", 6200, "Bot", "photo", 0, 0, "", 0,
     "Te llega la foto en el canal +18, como spoiler, y se borra a los 60 s.", 0),
    ("wuju_100k", "100.000 Wuju Coins", "common", 3500, "Bot", "wuju", 100_000, 100_000, "wuju", 0,
     "Se acreditan al instante en tu balance.", 0),
    ("embo_50", "50 de embo", "common", 1500, "Bot", "embo", 50, 50, "embo", 0,
     "Se acreditan al instante en tu balance de embo.", 0),
    ("party_switch", "Staff a tu elección en la CTA", "common", 3000, "Bot", None, 0, 0, "", 1,
     "En una CTA activa, elegís a alguien del Staff y el slot libre donde tiene que jugar." + SHIELD_NOTE, 0),
    ("mute_chat_5", "Mute de chat 5 min", "common", 2000, "Bot", None, 0, 0, "", 1,
     "Le muteás el chat a un jugador por 5 minutos." + SHIELD_NOTE, 0),
    ("mute_voice_5", "Mute de voice 5 min", "common", 2000, "Bot", None, 0, 0, "", 1,
     "Muteás a un jugador en el voice por 5 minutos." + SHIELD_NOTE, 0),
    ("shield_1h", "🛡️ Escudo 1 h", "common", 2000, "Bot", None, 0, 0, "", 0,
     "Durante 1 h nadie te puede usar cartas y no te afectan las cartas malas. No se acumula.", 0),
    ("clean_chat", "🧹 Limpiar mute de chat", "common", 1500, "Bot", None, 0, 0, "", 0,
     "Te saca un mute de chat (también el autosilencio). Si no tenés, no se gasta.", 0),
    ("clean_voice", "🧹 Limpiar mute de voice", "common", 1500, "Bot", None, 0, 0, "", 0,
     "Te saca un mute de voice (también el autosilencio). Si no tenés, no se gasta.", 0),
    ("bad_wuju_100k", "☠️ −100.000 Wuju Coins", "common", 1200, "Bot", None, 100_000, -100_000, "wuju", 0,
     "Carta mala: se te descuentan 100.000 (sin bajar de 0). Se aplica sola; el escudo la bloquea.", 1),
    ("bad_embo_15", "☠️ −15 de embo", "common", 800, "Bot", None, 15, -15, "embo", 0,
     "Carta mala: se te descuentan 15 de embo (sin bajar de 0). Se aplica sola; el escudo la bloquea.", 1),
    ("bad_selfmute_chat_5", "☠️ Te silenciás el chat 5 min", "common", 600, "Bot", None, 0, 0, "", 0,
     "Carta mala: quedás muteado del chat 5 minutos. Se aplica sola; el escudo la bloquea.", 1),
    ("bad_selfmute_voice_5", "☠️ Te silenciás el voice 5 min", "common", 600, "Bot", None, 0, 0, "", 0,
     "Carta mala: quedás muteado del voice 5 minutos. Se aplica sola; el escudo la bloquea.", 1),
    ("bad_cd_4h", "☠️ Cooldown de cobro +4 h", "common", 1000, "Bot", None, 0, 0, "", 0,
     "Carta mala: suma 4 h de cooldown a «Cobrar balance». Se aplica sola; el escudo la bloquea.", 1),
    # 🔵 Raras — 35 %
    ("mute_15", "Mute de chat 15 min", "rare", 3100, "Bot", None, 0, 0, "", 1,
     "Le muteás el chat a un jugador por 15 minutos." + SHIELD_NOTE, 0),
    ("voice_mute_15", "Mute de voice 15 min", "rare", 2100, "Bot", None, 0, 0, "", 1,
     "Muteás a un jugador en el voice por 15 minutos." + SHIELD_NOTE, 0),
    ("undo_coinflip", "Coinflip hasta 10M", "rare", 5600, "Bot", None, 0, 0, "", 0,
     "Un coinflip del casino con apuesta máxima de 10M. Rigen todas las reglas del coinflip.", 0),
    ("casino_open", "Abrir el casino", "rare", 2500, "Bot", None, 0, 0, "", 0,
     "Abrís el casino si está cerrado.", 0),
    ("casino_close", "Cerrar el casino", "rare", 2500, "Bot", None, 0, 0, "", 0,
     "Cerrás el casino si está abierto.", 0),
    ("free_build_p1", "Build libre en Party PRIO 1", "rare", 2500, "Staff", None, 0, 0, "", 0,
     "En Party PRIO 1 jugás con la build que quieras.", 0),
    ("party1_slot", "Slot libre en Party 1", "rare", 3300, "Bot", None, 0, 0, "", 0,
     "Te anotás en Party 1 en cualquier rol libre, sin la espera de 5 min de Legacy.", 0),
    ("wuju_500k", "500.000 Wuju Coins", "rare", 4000, "Bot", "wuju", 500_000, 500_000, "wuju", 0,
     "Se acreditan al instante en tu balance.", 0),
    ("embo_100", "100 de embo", "rare", 2000, "Bot", "embo", 100, 100, "embo", 0,
     "Se acreditan al instante en tu balance de embo.", 0),
    ("freeze_30m", "Congelar balance 30 min", "rare", 1000, "Bot", None, 0, 0, "", 1,
     "Durante 30 min el jugador no puede cobrar, jugar al casino, usar give-money ni el Guild Shop. Los splits le llegan igual."
     + SHIELD_NOTE, 0),
    ("shield_3h", "🛡️ Escudo 3 h", "rare", 2100, "Bot", None, 0, 0, "", 0,
     "Durante 3 h nadie te puede usar cartas y no te afectan las cartas malas. No se acumula.", 0),
    ("clean_freeze", "🧹 Limpiar congelamiento", "rare", 1000, "Bot", None, 0, 0, "", 0,
     "Te descongela el balance (si te lo congeló una carta). Si no estás congelado, no se gasta.", 0),
    ("wjp_freeze_1h", "❄️ Congelar Wujupón 1 h", "rare", 1000, "Bot", None, 0, 0, "", 1,
     "Durante 1 h el jugador no puede tirar, comprar ni usar cartas. No pierde nada." + SHIELD_NOTE, 0),
    ("bad_wuju_300k", "☠️ −300.000 Wuju Coins", "rare", 600, "Bot", None, 300_000, -300_000, "wuju", 0,
     "Carta mala: se te descuentan 300.000 (sin bajar de 0). Se aplica sola; el escudo la bloquea.", 1),
    ("bad_embo_30", "☠️ −30 de embo", "rare", 400, "Bot", None, 30, -30, "embo", 0,
     "Carta mala: se te descuentan 30 de embo (sin bajar de 0). Se aplica sola; el escudo la bloquea.", 1),
    ("bad_selfmute_chat_15", "☠️ Te silenciás el chat 15 min", "rare", 400, "Bot", None, 0, 0, "", 0,
     "Carta mala: quedás muteado del chat 15 minutos. Se aplica sola; el escudo la bloquea.", 1),
    ("bad_selfmute_voice_15", "☠️ Te silenciás el voice 15 min", "rare", 400, "Bot", None, 0, 0, "", 0,
     "Carta mala: quedás muteado del voice 15 minutos. Se aplica sola; el escudo la bloquea.", 1),
    ("bad_cd_8h", "☠️ Cooldown de cobro +8 h", "rare", 500, "Bot", None, 0, 0, "", 0,
     "Carta mala: suma 8 h de cooldown a «Cobrar balance». Se aplica sola; el escudo la bloquea.", 1),
    # 🟣 Épicas — 15,2 %
    ("regear_4m", "Reequipo próxima CTA (4M)", "epic", 600, "Staff", None, 0, 4_000_000, "silver", 0,
     "Te reequipan para la próxima CTA con un presupuesto de hasta 4M.", 0),
    ("mount_escort", "Montura libre en un porte de la guild", "epic", 850, "Staff", None, 0, 0, "", 0,
     "En un porte de la guild vas con la montura que quieras.", 0),
    ("loot_3_items", "Tres ítems de loot (7M)", "epic", 500, "Staff", None, 0, 7_000_000, "silver", 0,
     "Tres ítems del loot por un valor estimado de 7M.", 0),
    ("repair_4m", "Ticket de reparación (4M)", "epic", 700, "Staff", None, 0, 4_000_000, "silver", 0,
     "Te cubren la reparación hasta 4M.", 0),
    ("ava_replace", "Reemplazo en Avalonian", "epic", 800, "Staff", None, 0, 0, "", 1,
     "Reemplazás a alguien en una Avalonian antes de que salga.", 0),
    ("caerleon_port", "Porte a Caerleon", "epic", 600, "Staff", None, 0, 0, "", 0,
     "Te portean a Caerleon.", 0),
    ("plot_tax0", "Tax 0 en plots 48 h", "epic", 800, "Staff", None, 0, 0, "", 0,
     "Tus plots sin tax durante 48 horas.", 0),
    ("alter_ho_24h", "Permisos de alter en el HO 24 h", "epic", 700, "Staff", None, 0, 0, "", 0,
     "Un alter tuyo tiene permisos en el HO durante 24 horas.", 0),
    ("shop_discount", "🎟️ Ticket de descuento Guild Shop", "epic", 300, "Bot", "shop_ticket", 0, 5_000_000, "silver", 0,
     "En una compra del Guild Shop el descuento pasa de 10 % a 20 % (hasta 50M). Al abrir el ticket de compra, el bot te pregunta si querés usarlo.", 0),
    ("duel_1v1", "Duelo 1v1 especial", "epic", 1300, "Bot", None, 0, 0, "", 1,
     "Retás a alguien del Top 10 de apostado; se anuncia con @here. Tiene 24 h para aceptar o pierde el 5 % de su balance. "
     "Tope 200M." + SHIELD_NOTE, 0),
    ("ho_house", "Casa del HO", "epic", 400, "Staff", None, 0, 0, "", 0,
     "Reclamás una casa del HO.", 0),
    ("lootsplit_remove", "Sacar del lootsplit", "epic", 700, "Staff", None, 0, 0, "", 1,
     "Sacás a un jugador del lootsplit de la última CTA." + SHIELD_NOTE, 0),
    ("weapon_73", "Arma 7.3 del Guild Shop (8M)", "epic", 600, "Staff", None, 0, 8_000_000, "silver", 0,
     "Un arma 7.3 del Guild Shop de hasta 8M.", 0),
    ("wuju_2m", "2.000.000 Wuju Coins", "epic", 750, "Bot", "wuju", 2_000_000, 2_000_000, "wuju", 0,
     "Se acreditan al instante en tu balance.", 0),
    ("embo_500", "500 de embo", "epic", 650, "Bot", "embo", 500, 500, "embo", 0,
     "Se acreditan al instante en tu balance de embo.", 0),
    ("freeze_2h", "Congelar balance 2 h", "epic", 600, "Bot", None, 0, 0, "", 1,
     "Durante 2 h el jugador no puede cobrar, jugar al casino, usar give-money ni el Guild Shop. Los splits le llegan igual."
     + SHIELD_NOTE, 0),
    ("split_no_join", "Split sin participar", "epic", 600, "Staff", None, 0, 0, "", 0,
     "Cobrás el split de una CTA del día sin haber ido.", 0),
    ("coinflip_15m", "Coinflip hasta 15M", "epic", 850, "Bot", None, 0, 0, "", 0,
     "Un coinflip del casino con apuesta máxima de 15M. Rigen todas las reglas del coinflip.", 0),
    ("morgana_chest", "Cofre de Morgana en el HO", "epic", 600, "Staff", None, 0, 0, "", 0,
     "Ponés un cofre de Morgana en el HO, sobre un spot que no sea de la guild.", 0),
    ("mute_chat_30", "Mute de chat 30 min", "epic", 300, "Bot", None, 0, 0, "", 1,
     "Le muteás el chat a un jugador por 30 minutos." + SHIELD_NOTE, 0),
    ("mute_voice_30", "Mute de voice 30 min", "epic", 300, "Bot", None, 0, 0, "", 1,
     "Muteás a un jugador en el voice por 30 minutos." + SHIELD_NOTE, 0),
    ("shield_12h", "🛡️ Escudo 12 h", "epic", 600, "Bot", None, 0, 0, "", 0,
     "Durante 12 h nadie te puede usar cartas y no te afectan las cartas malas. No se acumula.", 0),
    ("clean_all", "🧹 Limpieza total", "epic", 400, "Bot", None, 0, 0, "", 0,
     "Te saca mutes de chat y voice, el congelamiento de balance y el de Wujupón. Si no tenés nada, no se gasta.", 0),
    ("wjp_freeze_6h", "❄️ Congelar Wujupón 6 h", "epic", 400, "Bot", None, 0, 0, "", 1,
     "Durante 6 h el jugador no puede tirar, comprar ni usar cartas. No pierde nada." + SHIELD_NOTE, 0),
    ("bad_cd_12h", "☠️ Cooldown de cobro +12 h", "epic", 300, "Bot", None, 0, 0, "", 0,
     "Carta mala: suma 12 h de cooldown a «Cobrar balance». Se aplica sola; el escudo la bloquea.", 1),
    # 🟠 Legendarias — 4,6 %
    ("weapon_83", "Arma 8.3 (hasta 12M)", "legendary", 250, "Staff", None, 0, 12_000_000, "silver", 0,
     "Un arma 8.3 de hasta 12M.", 0),
    ("wuju_15m", "Jackpot 15M Wuju Coins", "legendary", 450, "Bot", "wuju", 15_000_000, 15_000_000, "wuju", 0,
     "Se acreditan al instante en tu balance.", 0),
    ("regear_full_5m", "Reequipo completo (5M)", "legendary", 400, "Staff", None, 0, 5_000_000, "silver", 0,
     "Reequipo completo para una CTA, con presupuesto de hasta 5M.", 0),
    ("custom_bomb", "Bomb personalizada", "legendary", 400, "Staff", None, 0, 0, "", 0,
     "Armás una bomb con 5 personas a elección.", 0),
    ("cta_free_mount", "CTA con montura de combate libre", "legendary", 200, "Staff", None, 0, 0, "", 0,
     "Vas a una CTA con la montura de combate que quieras. La montura la ponés vos, no el staff.", 0),
    ("embo_1000", "1.000 de embo", "legendary", 300, "Bot", "embo", 1000, 1000, "embo", 0,
     "Se acreditan al instante en tu balance de embo.", 0),
    ("freeze_12h", "Congelar balance 12 h", "legendary", 300, "Bot", None, 0, 0, "", 1,
     "Durante 12 h el jugador no puede cobrar, jugar al casino, usar give-money ni el Guild Shop. Los splits le llegan igual."
     + SHIELD_NOTE, 0),
    ("deny_cta", "Denegar una CTA", "legendary", 300, "Staff", None, 0, 0, "", 1,
     "Un jugador no puede ir a una CTA." + SHIELD_NOTE, 0),
    ("guild_desc_1d", "Descripción de la guild 1 día", "legendary", 200, "Staff", None, 0, 0, "", 0,
     "Elegís la descripción de la guild durante 1 día.", 0),
    ("staff_build", "Elegirle la build a un staff", "legendary", 250, "Staff", None, 0, 0, "", 1,
     "Elegís la build que va a usar alguien del staff." + SHIELD_NOTE, 0),
    ("craft_alter_7d", "Alter de crafteo 7 días", "legendary", 400, "Staff", None, 0, 0, "", 0,
     "Un alter de crafteo durante 7 días.", 0),
    ("coinflip_20m", "Coinflip hasta 20M", "legendary", 350, "Bot", None, 0, 0, "", 0,
     "Un coinflip del casino con apuesta máxima de 20M. Rigen todas las reglas del coinflip.", 0),
    ("mute_chat_60", "Mute de chat 1 h", "legendary", 100, "Bot", None, 0, 0, "", 1,
     "Le muteás el chat a un jugador por 1 hora." + SHIELD_NOTE, 0),
    ("mute_voice_60", "Mute de voice 1 h", "legendary", 100, "Bot", None, 0, 0, "", 1,
     "Muteás a un jugador en el voice por 1 hora." + SHIELD_NOTE, 0),
    ("shield_24h", "🛡️ Escudo 24 h", "legendary", 200, "Bot", None, 0, 0, "", 0,
     "Durante 24 h nadie te puede usar cartas y no te afectan las cartas malas. No se acumula.", 0),
    ("purge_shield", "💥 Purgar escudo", "legendary", 150, "Bot", None, 0, 0, "", 1,
     "Le rompés el escudo a otro jugador. No funciona contra los inmunes. Si no tiene escudo, no se gasta.", 0),
    ("wjp_freeze_24h", "❄️ Congelar Wujupón 24 h", "legendary", 150, "Bot", None, 0, 0, "", 1,
     "Durante 24 h el jugador no puede tirar, comprar ni usar cartas. No pierde nada." + SHIELD_NOTE, 0),
    ("bad_cd_24h", "☠️ Cooldown de cobro +24 h", "legendary", 100, "Bot", None, 0, 0, "", 0,
     "Carta mala: suma 24 h de cooldown a «Cobrar balance». Se aplica sola; el escudo la bloquea.", 1),
    # 🔴 Míticas — 0,2 %
    ("premium_30m", "Premium de Albion (30M VAL)", "mythic", 20, "Staff", None, 0, 30_000_000, "val", 0,
     "Un premio equivalente a Premium de Albion (30M VAL).", 0),
    ("freeze_24h", "Congelar balance 72 h", "mythic", 50, "Bot", None, 0, 0, "", 1,
     "Durante 72 h el jugador no puede cobrar, jugar al casino, usar give-money ni el Guild Shop. Los splits le llegan igual."
     + SHIELD_NOTE, 0),
    ("guild_kick", "Kick de la guild", "mythic", 30, "Staff", None, 0, 0, "", 1,
     "Sacás a un miembro de la guild. Requiere aprobación del Council." + SHIELD_NOTE, 0),
    ("coinflip_nolimit", "Coinflip sin límite", "mythic", 30, "Bot", None, 0, 0, "", 0,
     "Un coinflip del casino sin tope de apuesta. Rigen todas las reglas del coinflip.", 0),
    ("mute_chat_180", "Mute de chat 24 h", "mythic", 10, "Bot", None, 0, 0, "", 1,
     "Le muteás el chat a un jugador por 24 horas." + SHIELD_NOTE, 0),
    ("mute_voice_180", "Mute de voice 24 h", "mythic", 10, "Bot", None, 0, 0, "", 1,
     "Muteás a un jugador en el voice por 24 horas." + SHIELD_NOTE, 0),
    ("shield_4d", "🛡️ Escudo 4 días", "mythic", 40, "Bot", None, 0, 0, "", 0,
     "Durante 4 días nadie te puede usar cartas y no te afectan las cartas malas. No se acumula.", 0),
    ("bad_cd_48h", "☠️ Cooldown de cobro +48 h", "mythic", 10, "Bot", None, 0, 0, "", 0,
     "Carta mala: suma 48 h de cooldown a «Cobrar balance». Se aplica sola; el escudo la bloquea.", 1),
]
CATALOG_VERSION = 5
# Cartas que cambió cada versión: al actualizar, solo esas vuelven a tomar lo del código (aunque el Council las haya editado).
# Versión 2 = catálogo completo nuevo (se resetean todas).
CATALOG_CHANGES = {
    3: ("guild_kick", "cta_free_mount", "mount_escort", "plot_tax0", "alter_ho_24h", "undo_coinflip", "casino_open",
        "casino_close", "free_build_p1", "ava_replace", "duel_1v1", "lootsplit_remove", "split_no_join", "caerleon_port",
        "custom_bomb", "freeze_24h", "coinflip_nolimit"),
    4: ("bad_wuju_100k", "bad_embo_15", "bad_selfmute_chat_5", "bad_selfmute_voice_5", "casino_block", "flex_cta",
        "sell_cooldown", "gendarme_photo", "mute_15", "voice_mute_15", "undo_coinflip", "shield_3h", "party1_slot"),
    5: ("premium_30m", "freeze_24h", "mute_chat_180", "mute_voice_180", "plot_tax0", "shield_4d", "coinflip_nolimit",
        "duel_1v1", "lootsplit_remove", "ava_replace", "shop_discount"),
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS wjp_prizes (
    key           TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    rarity        TEXT NOT NULL,
    weight        INTEGER NOT NULL,
    origin        TEXT NOT NULL,              -- Bot | Staff (como figura en el catálogo)
    handler       TEXT,                       -- canje automático (NULL = encargo al Council)
    amount        INTEGER NOT NULL DEFAULT 0,
    cost          INTEGER NOT NULL DEFAULT 0,
    cost_currency TEXT NOT NULL DEFAULT '',   -- wuju | embo | silver | val | ''
    third_party   INTEGER NOT NULL DEFAULT 0,
    enabled       INTEGER NOT NULL DEFAULT 1,
    sort          INTEGER NOT NULL DEFAULT 0,
    description   TEXT NOT NULL DEFAULT '',
    custom        INTEGER NOT NULL DEFAULT 0,   -- 1 = carta creada desde el bot (/wujupon carta-agregar)
    name_edited   INTEGER NOT NULL DEFAULT 0,   -- 1 = el Council cambió el nombre: el catálogo del código ya no lo pisa
    odds_edited   INTEGER NOT NULL DEFAULT 0,   -- 1 = el Council cambió la probabilidad o la rareza: tampoco se pisan
    desc_edited   INTEGER NOT NULL DEFAULT 0,   -- 1 = el Council cambió la descripción
    forced        INTEGER NOT NULL DEFAULT 0    -- 1 = carta mala: se aplica sola al salir (va directo al Council)
);
CREATE TABLE IF NOT EXISTS wjp_settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS wjp_spins (
    user_id    INTEGER PRIMARY KEY,
    spins      INTEGER NOT NULL DEFAULT 0 CHECK (spins >= 0),
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS wjp_ops (              -- compras y regalos de tiradas (idempotencia)
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    request_key TEXT NOT NULL UNIQUE,
    kind        TEXT NOT NULL,                     -- buy | gift
    user_id     INTEGER NOT NULL,
    actor_id    INTEGER NOT NULL,
    spins       INTEGER NOT NULL,
    cost        INTEGER NOT NULL DEFAULT 0,
    reason      TEXT,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS wjp_pulls (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    request_key  TEXT NOT NULL UNIQUE,
    user_id      INTEGER NOT NULL,
    prize_key    TEXT NOT NULL,
    rarity       TEXT NOT NULL,
    weight       INTEGER NOT NULL,
    total_weight INTEGER NOT NULL,
    card_id      INTEGER NOT NULL,
    announced    INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS wjp_cards (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    prize_key  TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'available',  -- available | reserved | consumed
    source     TEXT NOT NULL,                       -- pull | gift
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_wjp_cards_user ON wjp_cards(user_id, status);
CREATE TABLE IF NOT EXISTS wjp_requests (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    request_key  TEXT NOT NULL UNIQUE,
    card_id      INTEGER NOT NULL,
    user_id      INTEGER NOT NULL,
    prize_key    TEXT NOT NULL,
    details      TEXT,
    status       TEXT NOT NULL,       -- auto | pending | taken | approved | delivered | rejected | returned
    taker_id     INTEGER,
    approved_by  INTEGER,
    rejected_by  INTEGER,
    delivered_by INTEGER,
    refunded     INTEGER NOT NULL DEFAULT 0,
    note         TEXT,
    channel_id   INTEGER,
    message_id   INTEGER,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_wjp_requests_user ON wjp_requests(user_id);
CREATE TABLE IF NOT EXISTS wjp_assets (           -- imágenes de premios, subidas por un admin desde Discord
    key         TEXT PRIMARY KEY,
    filename    TEXT NOT NULL,
    content     BLOB NOT NULL,
    uploaded_by INTEGER NOT NULL,
    uploaded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS wjp_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_id   INTEGER NOT NULL,
    action     TEXT NOT NULL,
    user_id    INTEGER,
    ref        TEXT,
    detail     TEXT,
    created_at TEXT NOT NULL
);
"""

OPEN_STATUSES = ("pending", "taken", "approved")
STATUS_LABELS = {
    "auto": "✅ Entregado automáticamente", "pending": "🕓 Pendiente", "taken": "✋ En proceso",
    "approved": "👍 Aprobado", "delivered": "✅ Entregado", "rejected": "❌ Rechazado", "returned": "↩️ Carta devuelta",
    "wiped": "🧹 Anulado (limpieza de cartas)",
}


class WujuponError(ValueError):
    """Error apto para mostrar al usuario."""


@dataclass
class Prize:
    key: str
    name: str
    rarity: str
    weight: int
    origin: str
    handler: Optional[str]
    amount: int
    cost: int
    cost_currency: str
    third_party: bool
    enabled: bool
    description: str = ""
    custom: bool = False
    forced: bool = False

    @property
    def automatic(self) -> bool:
        return self.handler in AUTO_HANDLERS

    @property
    def rarity_label(self) -> str:
        name, emoji, _ = RARITIES[self.rarity]
        return f"{emoji} {name}"

    @property
    def color(self) -> int:
        return RARITIES[self.rarity][2]


@dataclass
class Card:
    id: int
    user_id: int
    prize_key: str
    status: str
    source: str


@dataclass
class Request:
    id: int
    card_id: int
    user_id: int
    prize_key: str
    details: str
    status: str
    taker_id: Optional[int]
    approved_by: Optional[int]
    rejected_by: Optional[int]
    delivered_by: Optional[int]
    refunded: bool
    note: str
    channel_id: Optional[int]
    message_id: Optional[int]
    created_at: str
    updated_at: str


def chance_text(weight: int, total: int = TOTAL_WEIGHT) -> str:
    """Porcentaje con coma decimal, sin ceros de más (0,075 %, 11 %, 15,2 %)."""
    if total <= 0:
        return "0 %"
    pct = weight * 100 / total
    text = f"{pct:.3f}".rstrip("0").rstrip(".")
    return text.replace(".", ",") + " %"


# ============================================================
# Esquema y catálogo
# ============================================================
_ready: set = set()


def validate_catalog(catalog=CATALOG) -> None:
    keys = [c[0] for c in catalog]
    if len(keys) != len(set(keys)):
        raise ValueError("Claves de premio duplicadas")
    total = sum(c[3] for c in catalog)
    if total != TOTAL_WEIGHT:
        raise ValueError(f"Las probabilidades suman {total}/{TOTAL_WEIGHT}")


def init_schema(db_path: str) -> None:
    if db_path in _ready:
        return
    validate_catalog()
    eco.init_schema(db_path)
    embo_db.init_schema(db_path)
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        cols = {c["name"] for c in conn.execute("PRAGMA table_info(wjp_prizes)").fetchall()}
        for col, ddl in (("description", "TEXT NOT NULL DEFAULT ''"), ("custom", "INTEGER NOT NULL DEFAULT 0"),
                         ("name_edited", "INTEGER NOT NULL DEFAULT 0"), ("odds_edited", "INTEGER NOT NULL DEFAULT 0"),
                         ("desc_edited", "INTEGER NOT NULL DEFAULT 0"), ("forced", "INTEGER NOT NULL DEFAULT 0")):
            if col not in cols:
                conn.execute(f"ALTER TABLE wjp_prizes ADD COLUMN {col} {ddl}")
        with write_tx(conn):
            conn.execute("CREATE TABLE IF NOT EXISTS wjp_settings (key TEXT PRIMARY KEY, value TEXT)")
            v = conn.execute("SELECT value FROM wjp_settings WHERE key = 'catalog_version'").fetchone()
            current = int(v["value"] or 0) if v is not None else 0
            if current < CATALOG_VERSION:
                # Catálogo nuevo: las cartas que cambiaron toman lo del código (nombre, descripción, probabilidad y rareza)
                # aunque el Council las haya editado. Las cartas creadas desde el bot no se tocan.
                if current < 2:
                    conn.execute("UPDATE wjp_prizes SET name_edited = 0, odds_edited = 0, desc_edited = 0 WHERE custom = 0")
                else:
                    keys_changed = sorted({k for ver, ks in CATALOG_CHANGES.items() if ver > current for k in ks})
                    if keys_changed:
                        conn.execute(f"UPDATE wjp_prizes SET name_edited = 0, odds_edited = 0, desc_edited = 0 "
                                     f"WHERE custom = 0 AND key IN ({','.join('?' * len(keys_changed))})", keys_changed)
                conn.execute("INSERT OR REPLACE INTO wjp_settings(key, value) VALUES ('catalog_version', ?)", (str(CATALOG_VERSION),))
            for i, (key, name, rarity, weight, origin, handler, amount, cost, cur, third, desc, forced) in enumerate(CATALOG):
                # INSERT OR IGNORE: lo que el Council habilitó/deshabilitó se conserva; lo editado desde el bot no se pisa.
                conn.execute("INSERT OR IGNORE INTO wjp_prizes(key, name, rarity, weight, origin, handler, amount, cost, cost_currency, "
                             "third_party, enabled, sort, description, forced) VALUES (?,?,?,?,?,?,?,?,?,?,1,?,?,?)",
                             (key, name, rarity, weight, origin, handler, amount, cost, cur, third, i, desc, forced))
                conn.execute("UPDATE wjp_prizes SET name = CASE WHEN name_edited = 1 THEN name ELSE ? END, "
                             "rarity = CASE WHEN odds_edited = 1 THEN rarity ELSE ? END, "
                             "weight = CASE WHEN odds_edited = 1 THEN weight ELSE ? END, "
                             "description = CASE WHEN desc_edited = 1 THEN description ELSE ? END, origin=?, "
                             "handler=?, amount=?, cost=?, cost_currency=?, third_party=?, forced=?, sort=? WHERE key=? AND custom = 0",
                             (name, rarity, weight, desc, origin, handler, amount, cost, cur, third, forced, i, key))
            keys = [c[0] for c in CATALOG]
            # premios del código que ya no existen: quedan deshabilitados (sus cartas siguen visibles en el historial)
            conn.execute(f"UPDATE wjp_prizes SET enabled = 0 WHERE custom = 0 AND key NOT IN ({','.join('?' * len(keys))})", keys)
    finally:
        conn.close()
    _ready.add(db_path)


def _prize(r) -> Prize:
    keys = r.keys()
    return Prize(r["key"], r["name"], r["rarity"], int(r["weight"]), r["origin"], r["handler"], int(r["amount"]), int(r["cost"]),
                 r["cost_currency"], bool(r["third_party"]), bool(r["enabled"]),
                 (r["description"] or "") if "description" in keys else "", bool(r["custom"]) if "custom" in keys else False,
                 bool(r["forced"]) if "forced" in keys else False)


def _open(db_path: str) -> sqlite3.Connection:
    init_schema(db_path)
    return connect(db_path)


def list_prizes(db_path: str, enabled_only: bool = False) -> List[Prize]:
    conn = _open(db_path)
    try:
        sql = "SELECT * FROM wjp_prizes" + (" WHERE enabled = 1" if enabled_only else "") + " ORDER BY sort"
        return [_prize(r) for r in conn.execute(sql).fetchall()]
    finally:
        conn.close()


def get_prize(db_path: str, key: str) -> Optional[Prize]:
    conn = _open(db_path)
    try:
        r = conn.execute("SELECT * FROM wjp_prizes WHERE key = ?", (key,)).fetchone()
        return _prize(r) if r else None
    finally:
        conn.close()


def _log(conn, actor_id: int, action: str, user_id: Optional[int] = None, ref: str = "", detail: str = "") -> None:
    conn.execute("INSERT INTO wjp_log(actor_id, action, user_id, ref, detail, created_at) VALUES (?,?,?,?,?,?)",
                 (actor_id, action, user_id, ref, detail[:1000], utcnow_iso()))


def log_action(db_path: str, actor_id: int, action: str, user_id: Optional[int] = None, ref: str = "", detail: str = "") -> None:
    conn = _open(db_path)
    try:
        with write_tx(conn):
            _log(conn, actor_id, action, user_id, ref, detail)
    finally:
        conn.close()


def set_prize_enabled(db_path: str, key: str, enabled: bool, actor_id: int) -> bool:
    conn = _open(db_path)
    try:
        with write_tx(conn):
            cur = conn.execute("UPDATE wjp_prizes SET enabled = ? WHERE key = ? AND enabled != ?", (int(enabled), key, int(enabled)))
            if cur.rowcount:
                _log(conn, actor_id, "prize_enabled" if enabled else "prize_disabled", ref=key)
            return cur.rowcount > 0
    finally:
        conn.close()


MAX_NAME = 90
MAX_DESC = 500
CUSTOM_SORT_BASE = 1000


def _clean_text(text: Optional[str], limit: int, what: str) -> str:
    text = " ".join((text or "").split())
    if len(text) > limit:
        raise WujuponError(f"{what} puede tener hasta {limit} caracteres.")
    return text


def parse_chance(text: str) -> int:
    """'0,5' / '0.5%' / '11' → peso sobre 100.000 (mínimo 0,001 %)."""
    raw = (text or "").strip().replace("%", "").replace(" ", "").replace(",", ".")
    try:
        value = float(raw)
    except ValueError:
        raise WujuponError("Escribí la probabilidad como porcentaje (ej: 0,5 o 2).")
    weight = round(value * TOTAL_WEIGHT / 100)
    if abs(weight * 100 / TOTAL_WEIGHT - value) > 1e-9 or not 1 <= weight <= TOTAL_WEIGHT:
        raise WujuponError("La probabilidad tiene que estar entre 0,001 % y 100 %, con hasta 3 decimales.")
    return weight


def enabled_total(db_path: str) -> int:
    conn = _open(db_path)
    try:
        return int(conn.execute("SELECT COALESCE(SUM(weight), 0) FROM wjp_prizes WHERE enabled = 1").fetchone()[0])
    finally:
        conn.close()


def add_prize(db_path: str, *, actor_id: int, name: str, description: str, rarity: str, weight: int,
              third_party: bool = False) -> Prize:
    """Carta nueva creada desde el bot. Siempre se canjea por encargo al Council (no tiene canje automático)."""
    if rarity not in RARITIES:
        raise WujuponError("Rareza inválida.")
    name = _clean_text(name, MAX_NAME, "El nombre")
    if not name:
        raise WujuponError("El nombre no puede estar vacío.")
    description = _clean_text(description, MAX_DESC, "La descripción")
    if not 1 <= int(weight) <= TOTAL_WEIGHT:
        raise WujuponError("Probabilidad inválida.")
    conn = _open(db_path)
    try:
        with write_tx(conn):
            if conn.execute("SELECT 1 FROM wjp_prizes WHERE lower(name) = lower(?)", (name,)).fetchone():
                raise WujuponError("Ya hay una carta con ese nombre.")
            n = int(conn.execute("SELECT COUNT(*) FROM wjp_prizes WHERE custom = 1").fetchone()[0]) + 1
            key = f"custom_{n}"
            while conn.execute("SELECT 1 FROM wjp_prizes WHERE key = ?", (key,)).fetchone():
                n += 1
                key = f"custom_{n}"
            conn.execute("INSERT INTO wjp_prizes(key, name, rarity, weight, origin, handler, amount, cost, cost_currency, third_party, "
                         "enabled, sort, description, custom, name_edited) VALUES (?,?,?,?,'Staff',NULL,0,0,'',?,1,?,?,1,1)",
                         (key, name, rarity, int(weight), int(bool(third_party)), CUSTOM_SORT_BASE + n, description))
            _log(conn, actor_id, "prize_added", ref=key, detail=f"{name} · {rarity} · {chance_text(int(weight))}")
            return _prize(conn.execute("SELECT * FROM wjp_prizes WHERE key = ?", (key,)).fetchone())
    finally:
        conn.close()


def edit_prize(db_path: str, key: str, *, actor_id: int, name: Optional[str] = None,
               description: Optional[str] = None) -> Tuple[Prize, Prize]:
    """Cambia nombre y/o descripción. Las cartas ya obtenidas muestran el texto nuevo. Devuelve (antes, después)."""
    if name is None and description is None:
        raise WujuponError("Indicá un nombre nuevo, una descripción nueva o ambos.")
    conn = _open(db_path)
    try:
        with write_tx(conn):
            row = conn.execute("SELECT * FROM wjp_prizes WHERE key = ?", (key,)).fetchone()
            if row is None:
                raise WujuponError("Esa carta no existe.")
            before = _prize(row)
            changes = []
            if name is not None:
                new_name = _clean_text(name, MAX_NAME, "El nombre")
                if not new_name:
                    raise WujuponError("El nombre no puede estar vacío.")
                if conn.execute("SELECT 1 FROM wjp_prizes WHERE lower(name) = lower(?) AND key != ?", (new_name, key)).fetchone():
                    raise WujuponError("Ya hay otra carta con ese nombre.")
                conn.execute("UPDATE wjp_prizes SET name = ?, name_edited = 1 WHERE key = ?", (new_name, key))
                changes.append(f"nombre: {before.name} → {new_name}")
            if description is not None:
                new_desc = "" if description.strip().lower() in ("-", "quitar", "borrar") else _clean_text(description, MAX_DESC, "La descripción")
                conn.execute("UPDATE wjp_prizes SET description = ?, desc_edited = 1 WHERE key = ?", (new_desc, key))
                changes.append("descripción actualizada" if new_desc else "descripción quitada")
            _log(conn, actor_id, "prize_edited", ref=key, detail=" · ".join(changes))
            return before, _prize(conn.execute("SELECT * FROM wjp_prizes WHERE key = ?", (key,)).fetchone())
    finally:
        conn.close()


def set_prize_odds(db_path: str, key: str, *, actor_id: int, weight: Optional[int] = None,
                   rarity: Optional[str] = None) -> Tuple[Prize, Prize]:
    """Cambia la probabilidad y/o la rareza (tier) de una carta. Devuelve (antes, después)."""
    if weight is None and rarity is None:
        raise WujuponError("Indicá una probabilidad nueva, una rareza nueva o ambas.")
    if rarity is not None and rarity not in RARITIES:
        raise WujuponError("Rareza inválida.")
    if weight is not None and not 1 <= int(weight) <= TOTAL_WEIGHT:
        raise WujuponError("Probabilidad inválida.")
    conn = _open(db_path)
    try:
        with write_tx(conn):
            row = conn.execute("SELECT * FROM wjp_prizes WHERE key = ?", (key,)).fetchone()
            if row is None:
                raise WujuponError("Esa carta no existe.")
            before = _prize(row)
            changes = []
            if weight is not None:
                conn.execute("UPDATE wjp_prizes SET weight = ?, odds_edited = 1 WHERE key = ?", (int(weight), key))
                changes.append(f"probabilidad: {chance_text(before.weight)} → {chance_text(int(weight))}")
            if rarity is not None:
                conn.execute("UPDATE wjp_prizes SET rarity = ?, odds_edited = 1 WHERE key = ?", (rarity, key))
                changes.append(f"rareza: {RARITIES[before.rarity][0]} → {RARITIES[rarity][0]}")
            _log(conn, actor_id, "prize_odds", ref=key, detail=" · ".join(changes))
            return before, _prize(conn.execute("SELECT * FROM wjp_prizes WHERE key = ?", (key,)).fetchone())
    finally:
        conn.close()


def rarity_totals(db_path: str) -> Dict[str, int]:
    """Peso habilitado por rareza."""
    conn = _open(db_path)
    try:
        return {r["rarity"]: int(r["w"]) for r in
                conn.execute("SELECT rarity, SUM(weight) AS w FROM wjp_prizes WHERE enabled = 1 GROUP BY rarity").fetchall()}
    finally:
        conn.close()


@dataclass
class WipeResult:
    cards: int
    users: int
    requests: List[Request]
    spins: int


def wipe_all_cards(db_path: str, *, actor_id: int, include_spins: bool = False) -> WipeResult:
    """(Admin) Anula TODAS las cartas disponibles o reservadas de todos los jugadores y los encargos abiertos.
    El historial de tiradas y canjes se conserva (las cartas quedan con estado 'wiped')."""
    conn = _open(db_path)
    try:
        with write_tx(conn):
            now = utcnow_iso()
            users = int(conn.execute("SELECT COUNT(DISTINCT user_id) FROM wjp_cards WHERE status IN ('available', 'reserved')")
                        .fetchone()[0])
            cards = conn.execute("UPDATE wjp_cards SET status = 'wiped', updated_at = ? WHERE status IN ('available', 'reserved')",
                                 (now,)).rowcount
            marks = ",".join("?" * len(OPEN_STATUSES))
            reqs = [_request(r) for r in conn.execute(f"SELECT * FROM wjp_requests WHERE status IN ({marks})", OPEN_STATUSES).fetchall()]
            conn.execute(f"UPDATE wjp_requests SET status = 'wiped', note = 'Limpieza general de cartas', updated_at = ? "
                         f"WHERE status IN ({marks})", (now, *OPEN_STATUSES))
            spins = 0
            if include_spins:
                spins = int(conn.execute("SELECT COALESCE(SUM(spins), 0) FROM wjp_spins").fetchone()[0])
                conn.execute("UPDATE wjp_spins SET spins = 0, updated_at = ? WHERE spins != 0", (now,))
            _log(conn, actor_id, "wipe_all_cards", detail=f"{cards} cartas de {users} personas · {len(reqs)} encargos"
                                                          + (f" · {spins} tiradas" if include_spins else ""))
            for r in reqs:
                r.status = "wiped"
            return WipeResult(int(cards), users, reqs, spins)
    finally:
        conn.close()


def get_daily_limit(db_path: str) -> int:
    """Máximo de tiradas que una persona puede COMPRAR por día UTC (0 = sin límite)."""
    v = get_setting(db_path, "daily_limit")
    try:
        return max(0, int(v)) if v else 0
    except ValueError:
        return 0


def _day_start_iso(now=None) -> str:
    from datetime import datetime, timezone
    now = now or datetime.now(timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()


def _bought_today(conn, user_id: int) -> int:
    return int(conn.execute("SELECT COALESCE(SUM(spins), 0) FROM wjp_ops WHERE kind = 'buy' AND user_id = ? AND created_at >= ?",
                            (user_id, _day_start_iso())).fetchone()[0])


def bought_today(db_path: str, user_id: int) -> int:
    conn = _open(db_path)
    try:
        return _bought_today(conn, user_id)
    finally:
        conn.close()


MAX_ASSET_BYTES = 8 * 1024 * 1024


def set_asset(db_path: str, key: str, *, filename: str, content: bytes, actor_id: int) -> None:
    if not content or len(content) > MAX_ASSET_BYTES:
        raise WujuponError("La imagen tiene que pesar menos de 8 MB.")
    conn = _open(db_path)
    try:
        with write_tx(conn):
            conn.execute("INSERT OR REPLACE INTO wjp_assets(key, filename, content, uploaded_by, uploaded_at) VALUES (?,?,?,?,?)",
                         (key, filename, sqlite3.Binary(content), actor_id, utcnow_iso()))
            _log(conn, actor_id, "asset_set", ref=key, detail=f"{filename} · {len(content)} bytes")
    finally:
        conn.close()


def get_asset(db_path: str, key: str) -> Optional[Tuple[str, bytes]]:
    conn = _open(db_path)
    try:
        r = conn.execute("SELECT filename, content FROM wjp_assets WHERE key = ?", (key,)).fetchone()
        return (r["filename"], bytes(r["content"])) if r else None
    finally:
        conn.close()


def delete_asset(db_path: str, key: str, actor_id: int) -> bool:
    conn = _open(db_path)
    try:
        with write_tx(conn):
            n = conn.execute("DELETE FROM wjp_assets WHERE key = ?", (key,)).rowcount
            if n:
                _log(conn, actor_id, "asset_deleted", ref=key)
            return n > 0
    finally:
        conn.close()


def get_setting(db_path: str, key: str) -> Optional[str]:
    conn = _open(db_path)
    try:
        r = conn.execute("SELECT value FROM wjp_settings WHERE key = ?", (key,)).fetchone()
        return r["value"] if r else None
    finally:
        conn.close()


def set_setting(db_path: str, key: str, value: Optional[str]) -> None:
    conn = _open(db_path)
    try:
        with write_tx(conn):
            if value is None:
                conn.execute("DELETE FROM wjp_settings WHERE key = ?", (key,))
            else:
                conn.execute("INSERT OR REPLACE INTO wjp_settings(key, value) VALUES (?,?)", (key, str(value)))
    finally:
        conn.close()


def get_price(db_path: str) -> int:
    v = get_setting(db_path, "price")
    try:
        return int(v) if v else DEFAULT_PRICE
    except ValueError:
        return DEFAULT_PRICE


# ============================================================
# Tiradas
# ============================================================
def _spins(conn, user_id: int) -> int:
    r = conn.execute("SELECT spins FROM wjp_spins WHERE user_id = ?", (user_id,)).fetchone()
    return int(r["spins"]) if r else 0


def _add_spins(conn, user_id: int, delta: int) -> int:
    now = utcnow_iso()
    conn.execute("INSERT OR IGNORE INTO wjp_spins(user_id, spins, updated_at) VALUES (?,0,?)", (user_id, now))
    after = _spins(conn, user_id) + delta
    if after < 0:
        raise WujuponError("No tiene tantas tiradas.")
    conn.execute("UPDATE wjp_spins SET spins = ?, updated_at = ? WHERE user_id = ?", (after, now, user_id))
    return after


def get_spins(db_path: str, user_id: int) -> int:
    conn = _open(db_path)
    try:
        return _spins(conn, user_id)
    finally:
        conn.close()


@dataclass
class BuyResult:
    status: str               # ok | duplicate | funds | frozen | invalid | limit
    spins: int = 0
    cost: int = 0
    balance: int = 0
    remaining_today: int = 0  # status 'limit': cuántas puede comprar todavía hoy


def buy_spins(db_path: str, user_id: int, count: int, request_key: str) -> BuyResult:
    if not 1 <= count <= MAX_BUY:
        return BuyResult("invalid")
    price = get_price(db_path)
    limit = get_daily_limit(db_path)
    cost = price * count
    conn = _open(db_path)
    try:
        with write_tx(conn):
            if conn.execute("SELECT 1 FROM wjp_ops WHERE request_key = ?", (request_key,)).fetchone():
                return BuyResult("duplicate", _spins(conn, user_id), cost, eco_balance(conn, user_id))
            if limit:
                left = max(0, limit - _bought_today(conn, user_id))
                if count > left:
                    return BuyResult("limit", cost=cost, remaining_today=left)
            op_id = eco.tx_new_operation(conn, kind="remove", actor_id=user_id, request_key=f"wjp:buy:{request_key}",
                                         total_amount=cost, target_count=1, reason=f"Wujupón: {count} tirada(s)",
                                         related_id=f"wjp:buy:{request_key}")
            try:
                _, after = eco.tx_move(conn, op_id=op_id, user_id=user_id, delta=-cost, mov_type="wujupon_purchase",
                                       reason=f"Wujupón: {count} tirada(s)", actor_id=user_id,
                                       related_id=f"wjp:buy:{request_key}", require_funds=True)
            except eco.InsufficientFunds as exc:
                raise _Abort(BuyResult("funds", cost=cost, balance=int(exc.args[0]) if exc.args else 0))
            except eco.BalanceFrozen:
                raise _Abort(BuyResult("frozen", cost=cost))
            spins = _add_spins(conn, user_id, count)
            conn.execute("INSERT INTO wjp_ops(request_key, kind, user_id, actor_id, spins, cost, created_at) VALUES (?,?,?,?,?,?,?)",
                         (request_key, "buy", user_id, user_id, count, cost, utcnow_iso()))
            return BuyResult("ok", spins, cost, after)
    except _Abort as a:
        return a.result
    finally:
        conn.close()


class _Abort(Exception):
    """Revierte la transacción y devuelve un resultado (sin propagar error)."""
    def __init__(self, result):
        super().__init__()
        self.result = result


def eco_balance(conn, user_id: int) -> int:
    r = conn.execute("SELECT balance FROM eco_accounts WHERE user_id = ?", (user_id,)).fetchone()
    return int(r["balance"]) if r else 0


def gift_spins(db_path: str, *, actor_id: int, user_id: int, count: int, request_key: str, reason: str) -> int:
    """Regala (count > 0) o quita (count < 0) tiradas. Devuelve las tiradas resultantes."""
    if count == 0 or abs(count) > 1000:
        raise WujuponError("La cantidad de tiradas tiene que estar entre 1 y 1000.")
    conn = _open(db_path)
    try:
        with write_tx(conn):
            if conn.execute("SELECT 1 FROM wjp_ops WHERE request_key = ?", (request_key,)).fetchone():
                return _spins(conn, user_id)
            after = _add_spins(conn, user_id, count)
            conn.execute("INSERT INTO wjp_ops(request_key, kind, user_id, actor_id, spins, cost, reason, created_at) "
                         "VALUES (?,?,?,?,?,0,?,?)", (request_key, "gift", user_id, actor_id, count, reason, utcnow_iso()))
            _log(conn, actor_id, "gift_spins" if count > 0 else "remove_spins", user_id, request_key, f"{count} · {reason}")
            return after
    finally:
        conn.close()


@dataclass
class PullResult:
    status: str                       # ok | duplicate | no_spins | empty
    prize: Optional[Prize] = None
    card_id: int = 0
    pull_id: int = 0
    weight: int = 0
    total_weight: int = 0
    spins_left: int = 0
    request_id: Optional[int] = None  # carta mala: encargo creado solo para el Council


def draw(prizes: List[Prize], rng=None) -> Tuple[Prize, int]:
    """Elige un premio según su peso. Devuelve (premio, peso total)."""
    total = sum(p.weight for p in prizes)
    if total <= 0:
        raise WujuponError("No hay premios habilitados.")
    roll = rng.randrange(total) if rng is not None else secrets.randbelow(total)
    acc = 0
    for p in prizes:
        acc += p.weight
        if roll < acc:
            return p, total
    return prizes[-1], total


def pull(db_path: str, user_id: int, request_key: str, rng: Optional[random.Random] = None) -> PullResult:
    conn = _open(db_path)
    try:
        with write_tx(conn):
            prev = conn.execute("SELECT * FROM wjp_pulls WHERE request_key = ?", (request_key,)).fetchone()
            if prev is not None:
                pr = conn.execute("SELECT * FROM wjp_prizes WHERE key = ?", (prev["prize_key"],)).fetchone()
                return PullResult("duplicate", _prize(pr), int(prev["card_id"]), int(prev["id"]), int(prev["weight"]),
                                  int(prev["total_weight"]), _spins(conn, user_id))
            if _spins(conn, user_id) <= 0:
                return PullResult("no_spins")
            prizes = [_prize(r) for r in conn.execute("SELECT * FROM wjp_prizes WHERE enabled = 1 ORDER BY sort").fetchall()]
            if not prizes:
                return PullResult("empty")
            prize, total = draw(prizes, rng)
            left = _add_spins(conn, user_id, -1)
            now = utcnow_iso()
            card_id = conn.execute("INSERT INTO wjp_cards(user_id, prize_key, status, source, created_at, updated_at) "
                                   "VALUES (?,?,'available','pull',?,?)", (user_id, prize.key, now, now)).lastrowid
            pull_id = conn.execute("INSERT INTO wjp_pulls(request_key, user_id, prize_key, rarity, weight, total_weight, card_id, created_at) "
                                   "VALUES (?,?,?,?,?,?,?,?)",
                                   (request_key, user_id, prize.key, prize.rarity, prize.weight, total, card_id, now)).lastrowid
            req_id = None
            if prize.forced:
                # Carta mala: no se guarda para «usar»; queda reservada y va directo al Council para que la aplique
                # (hasta que el bot lo haga solo). Si el jugador tenía escudo, el Council la rechaza y se consume.
                conn.execute("UPDATE wjp_cards SET status = 'reserved' WHERE id = ?", (card_id,))
                req_id = conn.execute("INSERT INTO wjp_requests(request_key, card_id, user_id, prize_key, details, status, note, "
                                      "created_at, updated_at) VALUES (?,?,?,?,?,'pending','',?,?)",
                                      (f"forced:{request_key}", card_id, user_id, prize.key,
                                       "☠️ Carta mala: aplicarla al jugador que la sacó (salvo que tenga escudo o sea inmune).",
                                       now, now)).lastrowid
            return PullResult("ok", prize, int(card_id), int(pull_id), prize.weight, total, left,
                              int(req_id) if req_id else None)
    finally:
        conn.close()


def unannounced_pulls(db_path: str, limit: int = 20) -> List[Tuple[int, int, str, int, int]]:
    """(pull_id, user_id, prize_key, weight, total_weight) sin anunciar, más viejas primero."""
    conn = _open(db_path)
    try:
        rows = conn.execute("SELECT id, user_id, prize_key, weight, total_weight FROM wjp_pulls WHERE announced = 0 "
                            "ORDER BY id LIMIT ?", (limit,)).fetchall()
        return [(int(r["id"]), int(r["user_id"]), r["prize_key"], int(r["weight"]), int(r["total_weight"])) for r in rows]
    finally:
        conn.close()


def mark_all_announced(db_path: str) -> None:
    conn = _open(db_path)
    try:
        with write_tx(conn):
            conn.execute("UPDATE wjp_pulls SET announced = 1 WHERE announced = 0")
    finally:
        conn.close()


def mark_announced(db_path: str, pull_ids: List[int]) -> None:
    if not pull_ids:
        return
    conn = _open(db_path)
    try:
        with write_tx(conn):
            conn.executemany("UPDATE wjp_pulls SET announced = 1 WHERE id = ?", [(i,) for i in pull_ids])
    finally:
        conn.close()


# ============================================================
# Cartas e inventario
# ============================================================
def _card(r) -> Optional[Card]:
    return Card(int(r["id"]), int(r["user_id"]), r["prize_key"], r["status"], r["source"]) if r else None


def get_card(db_path: str, card_id: int) -> Optional[Card]:
    conn = _open(db_path)
    try:
        return _card(conn.execute("SELECT * FROM wjp_cards WHERE id = ?", (card_id,)).fetchone())
    finally:
        conn.close()


@dataclass
class InventoryRow:
    prize: Prize
    available: int
    reserved: int
    first_card_id: Optional[int]      # carta disponible más vieja (la que se canjea)


def inventory(db_path: str, user_id: int) -> List[InventoryRow]:
    conn = _open(db_path)
    try:
        rows = conn.execute(
            """SELECT p.*, SUM(c.status = 'available') AS av, SUM(c.status = 'reserved') AS rs,
                      MIN(CASE WHEN c.status = 'available' THEN c.id END) AS first_id
                 FROM wjp_cards c JOIN wjp_prizes p ON p.key = c.prize_key
                WHERE c.user_id = ? AND c.status IN ('available', 'reserved')
                GROUP BY p.key ORDER BY p.sort""", (user_id,)).fetchall()
        return [InventoryRow(_prize(r), int(r["av"] or 0), int(r["rs"] or 0), int(r["first_id"]) if r["first_id"] else None)
                for r in rows]
    finally:
        conn.close()


def give_card(db_path: str, *, actor_id: int, user_id: int, prize_key: str, reason: str) -> int:
    conn = _open(db_path)
    try:
        with write_tx(conn):
            if conn.execute("SELECT 1 FROM wjp_prizes WHERE key = ?", (prize_key,)).fetchone() is None:
                raise WujuponError("Ese premio no existe.")
            now = utcnow_iso()
            card_id = conn.execute("INSERT INTO wjp_cards(user_id, prize_key, status, source, created_at, updated_at) "
                                   "VALUES (?,?,'available','gift',?,?)", (user_id, prize_key, now, now)).lastrowid
            _log(conn, actor_id, "give_card", user_id, f"card:{card_id}", f"{prize_key} · {reason}")
            return int(card_id)
    finally:
        conn.close()


def return_card(db_path: str, *, card_id: int, actor_id: int, reason: str) -> Tuple[Card, Optional[int]]:
    """Devuelve una carta usada (o reservada) al inventario de su dueño. Si tenía un encargo abierto, lo cierra como
    «carta devuelta». Devuelve (carta, id del encargo cerrado o None)."""
    conn = _open(db_path)
    try:
        with write_tx(conn):
            card = _card(conn.execute("SELECT * FROM wjp_cards WHERE id = ?", (card_id,)).fetchone())
            if card is None:
                raise WujuponError("No existe esa carta.")
            if card.status == "available":
                raise WujuponError("Esa carta ya está disponible en el inventario: no hay nada que devolver.")
            now = utcnow_iso()
            conn.execute("UPDATE wjp_cards SET status = 'available', updated_at = ? WHERE id = ?", (now, card_id))
            req = conn.execute(f"SELECT id FROM wjp_requests WHERE card_id = ? AND status IN ({','.join('?' * len(OPEN_STATUSES))}) "
                               "ORDER BY id DESC LIMIT 1", (card_id, *OPEN_STATUSES)).fetchone()
            req_id = int(req["id"]) if req else None
            if req_id:
                conn.execute("UPDATE wjp_requests SET status = 'returned', refunded = 1, note = ?, updated_at = ? WHERE id = ?",
                             (reason[:500], now, req_id))
            _log(conn, actor_id, "return_card", card.user_id, f"card:{card_id}", reason)
            card.status = "available"
            return card, req_id
    finally:
        conn.close()


# ============================================================
# Canjes
# ============================================================
def _request(r) -> Optional[Request]:
    if r is None:
        return None
    return Request(int(r["id"]), int(r["card_id"]), int(r["user_id"]), r["prize_key"], r["details"] or "", r["status"],
                   r["taker_id"], r["approved_by"], r["rejected_by"], r["delivered_by"], bool(r["refunded"]), r["note"] or "",
                   r["channel_id"], r["message_id"], r["created_at"], r["updated_at"])


def get_request(db_path: str, req_id: int) -> Optional[Request]:
    conn = _open(db_path)
    try:
        return _request(conn.execute("SELECT * FROM wjp_requests WHERE id = ?", (req_id,)).fetchone())
    finally:
        conn.close()


def find_request_by_message(db_path: str, message_id: int) -> Optional[Request]:
    conn = _open(db_path)
    try:
        return _request(conn.execute("SELECT * FROM wjp_requests WHERE message_id = ?", (message_id,)).fetchone())
    finally:
        conn.close()


def user_requests(db_path: str, user_id: int, limit: int = 15) -> List[Request]:
    conn = _open(db_path)
    try:
        return [_request(r) for r in conn.execute("SELECT * FROM wjp_requests WHERE user_id = ? AND status != 'auto' "
                                                  "ORDER BY id DESC LIMIT ?", (user_id, limit)).fetchall()]
    finally:
        conn.close()


def open_requests(db_path: str, limit: int = 25) -> List[Request]:
    conn = _open(db_path)
    try:
        q = f"SELECT * FROM wjp_requests WHERE status IN ({','.join('?' * len(OPEN_STATUSES))}) ORDER BY id LIMIT ?"
        return [_request(r) for r in conn.execute(q, (*OPEN_STATUSES, limit)).fetchall()]
    finally:
        conn.close()


def unpublished_requests(db_path: str) -> List[int]:
    conn = _open(db_path)
    try:
        q = f"SELECT id FROM wjp_requests WHERE message_id IS NULL AND status IN ({','.join('?' * len(OPEN_STATUSES))}) ORDER BY id"
        return [int(r["id"]) for r in conn.execute(q, OPEN_STATUSES).fetchall()]
    finally:
        conn.close()


def set_request_message(db_path: str, req_id: int, channel_id: int, message_id: int) -> None:
    conn = _open(db_path)
    try:
        with write_tx(conn):
            conn.execute("UPDATE wjp_requests SET channel_id = ?, message_id = ? WHERE id = ?", (channel_id, message_id, req_id))
    finally:
        conn.close()


@dataclass
class RedeemResult:
    status: str               # auto | pending | duplicate | not_found | not_yours | not_available | disabled | no_effect
    message: str = ""
    request: Optional[Request] = None
    prize: Optional[Prize] = None


def _run_handler(conn, prize: Prize, card: Card, *, guild_id: int, req_key: str) -> str:
    """Ejecuta un canje automático dentro de la transacción. Devuelve el texto del resultado o lanza _Abort."""
    if prize.handler == "wuju":
        op_id = eco.tx_new_operation(conn, kind="add", actor_id=card.user_id, request_key=f"wjp:card:{card.id}",
                                     total_amount=prize.amount, target_count=1, reason=f"Wujupón: {prize.name}",
                                     related_id=f"wjp:card:{card.id}")
        _, after = eco.tx_move(conn, op_id=op_id, user_id=card.user_id, delta=prize.amount, mov_type="wujupon_prize",
                               reason=f"Wujupón: {prize.name}", actor_id=card.user_id, related_id=f"wjp:card:{card.id}")
        return f"Se acreditaron **{eco.format_amount(prize.amount)} {eco.CURRENCY_NAME}**. Balance: {eco.format_amount(after)}."
    if prize.handler == "embo":
        ch = embo_db.tx_apply(conn, user_id=card.user_id, mode="add", amount=prize.amount, actor_id=card.user_id,
                              request_key=f"wjp:card:{card.id}", reason=f"Wujupón: {prize.name}", kind="wujupon_prize")
        return f"Se acreditaron **{eco.format_amount(prize.amount)} de embo**. Balance de embo: {eco.format_amount(ch.after)}."
    if prize.handler == "photo":
        # la carta se consume acá; el envío lo hace la capa de Discord y, si falla, devuelve la carta
        if conn.execute("SELECT 1 FROM wjp_assets WHERE key = ?", (PHOTO_ASSET,)).fetchone() is None:
            raise _Abort(RedeemResult("no_effect", "Todavía no se cargó la foto de este premio. Tu carta sigue en el inventario.",
                                      prize=prize))
        return "photo"
    if prize.handler == "shop_ticket":
        raise _Abort(RedeemResult("no_effect", "🎟️ Este ticket se usa al comprar en el **Guild Shop**: abrí un ticket de compra y el bot "
                                               "te pregunta si querés usarlo. La carta sigue en tu inventario.", prize=prize))
    if prize.handler == "sell_cooldown":
        import deposits_db
        if not deposits_db.tx_reset_sell_cooldown(conn, guild_id, card.user_id):
            raise _Abort(RedeemResult("no_effect", "No tenés el cooldown de «Cobrar balance» activo: guardá la carta para cuando lo tengas.",
                                      prize=prize))
        return "Listo: ya podés volver a abrir un ticket de **Cobrar balance**."
    raise ValueError(f"handler desconocido: {prize.handler}")


def redeem(db_path: str, *, card_id: int, user_id: int, details: str, request_key: str, guild_id: int) -> RedeemResult:
    conn = _open(db_path)
    try:
        with write_tx(conn):
            prev = _request(conn.execute("SELECT * FROM wjp_requests WHERE request_key = ?", (request_key,)).fetchone())
            if prev is not None:
                return RedeemResult("duplicate", request=prev)
            card = _card(conn.execute("SELECT * FROM wjp_cards WHERE id = ?", (card_id,)).fetchone())
            if card is None:
                return RedeemResult("not_found", "No existe esa carta.")
            if card.user_id != user_id:
                return RedeemResult("not_yours", "Esa carta no es tuya.")
            prize = _prize(conn.execute("SELECT * FROM wjp_prizes WHERE key = ?", (card.prize_key,)).fetchone())
            if not prize.enabled:
                return RedeemResult("disabled", "Ese premio está deshabilitado por el Council por ahora. La carta sigue en tu inventario.",
                                    prize=prize)
            now = utcnow_iso()
            moved = conn.execute("UPDATE wjp_cards SET status = ?, updated_at = ? WHERE id = ? AND status = 'available'",
                                 ("consumed" if prize.automatic else "reserved", now, card_id)).rowcount
            if not moved:
                return RedeemResult("not_available", "Esa carta ya fue usada o tiene un canje en curso.", prize=prize)
            message = _run_handler(conn, prize, card, guild_id=guild_id, req_key=request_key) if prize.automatic else ""
            status = "auto" if prize.automatic else "pending"
            req_id = conn.execute("INSERT INTO wjp_requests(request_key, card_id, user_id, prize_key, details, status, note, "
                                  "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                                  (request_key, card_id, user_id, prize.key, (details or "")[:1000], status, message, now, now)).lastrowid
            req = _request(conn.execute("SELECT * FROM wjp_requests WHERE id = ?", (req_id,)).fetchone())
            return RedeemResult(status, message, req, prize)
    except _Abort as a:
        return a.result
    finally:
        conn.close()


@dataclass
class Change:
    ok: bool
    message: str = ""
    request: Optional[Request] = None


def _transition(db_path: str, req_id: int, actor_id: int, action: str, *, refund: bool = False, note: str = "") -> Change:
    conn = _open(db_path)
    try:
        with write_tx(conn):
            req = _request(conn.execute("SELECT * FROM wjp_requests WHERE id = ?", (req_id,)).fetchone())
            if req is None:
                return Change(False, "No existe ese encargo.")
            if req.status not in OPEN_STATUSES:
                return Change(False, f"El encargo ya está cerrado ({STATUS_LABELS.get(req.status, req.status)}).", req)
            now = utcnow_iso()
            if action == "take":
                if req.taker_id == actor_id:
                    return Change(False, "Ya lo tenés tomado vos.", req)
                conn.execute("UPDATE wjp_requests SET taker_id = ?, status = CASE WHEN status = 'pending' THEN 'taken' ELSE status END, "
                             "updated_at = ? WHERE id = ?", (actor_id, now, req_id))
            elif action == "approve":
                if req.status == "approved":
                    return Change(False, "Ya estaba aprobado.", req)
                conn.execute("UPDATE wjp_requests SET status = 'approved', approved_by = ?, taker_id = COALESCE(taker_id, ?), "
                             "updated_at = ? WHERE id = ?", (actor_id, actor_id, now, req_id))
            elif action == "deliver":
                conn.execute("UPDATE wjp_requests SET status = 'delivered', delivered_by = ?, taker_id = COALESCE(taker_id, ?), "
                             "updated_at = ? WHERE id = ?", (actor_id, actor_id, now, req_id))
                conn.execute("UPDATE wjp_cards SET status = 'consumed', updated_at = ? WHERE id = ?", (now, req.card_id))
            elif action == "reject":
                forced = conn.execute("SELECT forced FROM wjp_prizes WHERE key = ?", (req.prize_key,)).fetchone()
                if forced is not None and forced["forced"]:
                    refund = False                     # una carta mala nunca vuelve al inventario
                conn.execute("UPDATE wjp_requests SET status = 'rejected', rejected_by = ?, refunded = ?, note = ?, updated_at = ? "
                             "WHERE id = ?", (actor_id, int(refund), note[:500], now, req_id))
                conn.execute("UPDATE wjp_cards SET status = ?, updated_at = ? WHERE id = ?",
                             ("available" if refund else "consumed", now, req.card_id))
            else:
                raise ValueError(action)
            _log(conn, actor_id, f"request_{action}", req.user_id, f"req:{req_id}", note if action == "reject" else "")
            return Change(True, "", _request(conn.execute("SELECT * FROM wjp_requests WHERE id = ?", (req_id,)).fetchone()))
    finally:
        conn.close()


def take_request(db_path: str, req_id: int, actor_id: int) -> Change:
    return _transition(db_path, req_id, actor_id, "take")


def approve_request(db_path: str, req_id: int, actor_id: int) -> Change:
    return _transition(db_path, req_id, actor_id, "approve")


def deliver_request(db_path: str, req_id: int, actor_id: int) -> Change:
    return _transition(db_path, req_id, actor_id, "deliver")


def reject_request(db_path: str, req_id: int, actor_id: int, *, refund: bool, note: str = "") -> Change:
    return _transition(db_path, req_id, actor_id, "reject", refund=refund, note=note)


# ============================================================
# Estadísticas y registro
# ============================================================
@dataclass
class Stats:
    pulls: int
    by_rarity: Dict[str, int]
    spins_bought: int
    wuju_spent: int
    spins_gifted: int
    spins_unused: int
    paid: Dict[str, int]          # moneda → entregado (canjes automáticos + encargos entregados)
    pending: Dict[str, int]       # moneda → pasivo de cartas sin canjear o con encargo abierto
    open_requests: int


def _cost_by_currency(rows) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for r in rows:
        if r["cost_currency"]:
            out[r["cost_currency"]] = out.get(r["cost_currency"], 0) + int(r["total"] or 0)
    return out


def get_stats(db_path: str) -> Stats:
    conn = _open(db_path)
    try:
        pulls = int(conn.execute("SELECT COUNT(*) FROM wjp_pulls").fetchone()[0])
        by_rarity = {r["rarity"]: int(r["n"]) for r in conn.execute("SELECT rarity, COUNT(*) AS n FROM wjp_pulls GROUP BY rarity")}
        b = conn.execute("SELECT COALESCE(SUM(spins),0) AS s, COALESCE(SUM(cost),0) AS c FROM wjp_ops WHERE kind = 'buy'").fetchone()
        gifted = int(conn.execute("SELECT COALESCE(SUM(spins),0) FROM wjp_ops WHERE kind = 'gift'").fetchone()[0])
        unused = int(conn.execute("SELECT COALESCE(SUM(spins),0) FROM wjp_spins").fetchone()[0])
        paid = _cost_by_currency(conn.execute(
            "SELECT p.cost_currency, SUM(p.cost) AS total FROM wjp_requests r JOIN wjp_prizes p ON p.key = r.prize_key "
            "WHERE r.status IN ('auto', 'delivered') GROUP BY p.cost_currency").fetchall())
        pending = _cost_by_currency(conn.execute(
            "SELECT p.cost_currency, SUM(p.cost) AS total FROM wjp_cards c JOIN wjp_prizes p ON p.key = c.prize_key "
            "WHERE c.status IN ('available', 'reserved') GROUP BY p.cost_currency").fetchall())
        q = f"SELECT COUNT(*) FROM wjp_requests WHERE status IN ({','.join('?' * len(OPEN_STATUSES))})"
        open_n = int(conn.execute(q, OPEN_STATUSES).fetchone()[0])
        return Stats(pulls, by_rarity, int(b["s"]), int(b["c"]), gifted, unused, paid, pending, open_n)
    finally:
        conn.close()


def silver_equivalent(by_currency: Dict[str, int]) -> int:
    """Wuju = silver 1:1, embo = 10.000. VAL no se convierte (se informa aparte)."""
    return (by_currency.get("wuju", 0) + by_currency.get("silver", 0) + by_currency.get("embo", 0) * EMBO_SILVER_VALUE)


def expected_cost(prizes: List[Prize]) -> Dict[str, float]:
    """Costo esperado por tirada, por moneda, según los pesos habilitados."""
    total = sum(p.weight for p in prizes) or 1
    out: Dict[str, float] = {}
    for p in prizes:
        if p.cost_currency:
            out[p.cost_currency] = out.get(p.cost_currency, 0.0) + p.cost * p.weight / total
    return out


def recent_log(db_path: str, limit: int = 20) -> List[sqlite3.Row]:
    conn = _open(db_path)
    try:
        return conn.execute("SELECT * FROM wjp_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    finally:
        conn.close()


def recent_pulls(db_path: str, user_id: Optional[int] = None, limit: int = 20) -> List[sqlite3.Row]:
    conn = _open(db_path)
    try:
        if user_id is None:
            return conn.execute("SELECT * FROM wjp_pulls ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return conn.execute("SELECT * FROM wjp_pulls WHERE user_id = ? ORDER BY id DESC LIMIT ?", (user_id, limit)).fetchall()
    finally:
        conn.close()
