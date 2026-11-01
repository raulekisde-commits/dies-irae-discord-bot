"""/compo reglas: qué rol de combate corresponde a cada arma (tank, healer, dps, supp...). Servirá para asignar roles al importar compos."""
import re
from typing import List, Optional

import discord
from discord import app_commands

from armas_roles_default import DEFAULT_WEAPONS
from dbutil import connect, write_tx, utcnow_iso

ROLE_MAX = 30
SCHEMA = """
CREATE TABLE IF NOT EXISTS weapon_roles (
    guild_id  INTEGER NOT NULL,
    weapon_id TEXT NOT NULL,
    name_en   TEXT NOT NULL,
    name_es   TEXT NOT NULL,
    role      TEXT NOT NULL DEFAULT '',
    is_custom INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (guild_id, weapon_id)
);
CREATE TABLE IF NOT EXISTS weapon_roles_seeded (guild_id INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS combat_role_discord (
    guild_id INTEGER NOT NULL,
    label    TEXT NOT NULL,
    role_id  INTEGER NOT NULL,
    PRIMARY KEY (guild_id, label)
);
"""
_TIER_RE = re.compile(r"^T\d_")


def init_schema(db_path: str) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


def normalize_id(weapon_id: str) -> str:
    """T8_MAIN_MACE@2 -> MAIN_MACE"""
    return _TIER_RE.sub("", (weapon_id or "").strip().upper().split("@")[0])


def clean_role(role: str) -> str:
    return re.sub(r"\s+", " ", (role or "").strip().lower())[:ROLE_MAX]


_refreshed: set = set()


def _refresh_defaults(conn, guild_id: int) -> None:
    """Si cambió el rol por defecto de un arma (y nadie la editó a mano), se actualiza; además se agregan las armas nuevas. Una vez por arranque."""
    key = (conn.execute("PRAGMA database_list").fetchone()[2], guild_id)         # (archivo de la base, servidor)
    if key in _refreshed:
        return
    _refreshed.add(key)
    now = utcnow_iso()
    with write_tx(conn):
        for wid, en, es, role in DEFAULT_WEAPONS:
            conn.execute("INSERT OR IGNORE INTO weapon_roles(guild_id,weapon_id,name_en,name_es,role,is_custom,updated_at) VALUES (?,?,?,?,?,0,?)", (guild_id, wid, en, es, role, now))
            conn.execute("UPDATE weapon_roles SET role=?, updated_at=? WHERE guild_id=? AND weapon_id=? AND is_custom=0 AND role<>?", (role, now, guild_id, wid, role))


def _seed(conn, guild_id: int) -> None:
    """Carga la lista por defecto la primera vez que el servidor la usa; después solo actualiza los roles por defecto que nadie editó."""
    if conn.execute("SELECT 1 FROM weapon_roles_seeded WHERE guild_id=?", (guild_id,)).fetchone():
        return _refresh_defaults(conn, guild_id)
    now = utcnow_iso()
    with write_tx(conn):
        conn.execute("INSERT OR IGNORE INTO weapon_roles_seeded(guild_id) VALUES (?)", (guild_id,))
        conn.executemany("INSERT OR IGNORE INTO weapon_roles(guild_id,weapon_id,name_en,name_es,role,is_custom,updated_at) VALUES (?,?,?,?,?,0,?)",
                         [(guild_id, wid, en, es, role, now) for wid, en, es, role in DEFAULT_WEAPONS])


def seed_discord_roles(db_path: str, guild_id: int, defaults: dict) -> None:
    """Carga {rol de combate: id de rol de Discord} por defecto solo para los que el servidor todavía no configuró."""
    conn = connect(db_path)
    try:
        with write_tx(conn):
            conn.executemany("INSERT OR IGNORE INTO combat_role_discord(guild_id,label,role_id) VALUES (?,?,?)",
                             [(guild_id, clean_role(k), int(v)) for k, v in defaults.items() if v])
    finally:
        conn.close()


def discord_role_for(db_path: str, guild_id: int, label: str) -> Optional[int]:
    """Id del rol de Discord que corresponde a un rol de combate (None si no hay)."""
    conn = connect(db_path)
    try:
        row = conn.execute("SELECT role_id FROM combat_role_discord WHERE guild_id=? AND label=?", (guild_id, clean_role(label))).fetchone()
        return int(row["role_id"]) if row else None
    finally:
        conn.close()


def discord_roles_map(db_path: str, guild_id: int) -> dict:
    conn = connect(db_path)
    try:
        return {r["label"]: int(r["role_id"]) for r in conn.execute("SELECT label, role_id FROM combat_role_discord WHERE guild_id=?", (guild_id,))}
    finally:
        conn.close()


def set_discord_role(db_path: str, guild_id: int, label: str, role_id: Optional[int]) -> None:
    conn = connect(db_path)
    try:
        with write_tx(conn):
            if role_id:
                conn.execute("INSERT INTO combat_role_discord(guild_id,label,role_id) VALUES (?,?,?) ON CONFLICT(guild_id,label) DO UPDATE SET role_id=excluded.role_id",
                             (guild_id, clean_role(label), int(role_id)))
            else:
                conn.execute("DELETE FROM combat_role_discord WHERE guild_id=? AND label=?", (guild_id, clean_role(label)))
    finally:
        conn.close()


def list_rules(db_path: str, guild_id: int) -> List[dict]:
    conn = connect(db_path)
    try:
        _seed(conn, guild_id)
        return [dict(r) for r in conn.execute("SELECT * FROM weapon_roles WHERE guild_id=? ORDER BY name_en COLLATE NOCASE", (guild_id,))]
    finally:
        conn.close()


def role_for(db_path: str, guild_id: int, weapon_id: str) -> Optional[str]:
    """Rol de combate configurado para un arma (acepta ids con tier/encantamiento). None si no tiene regla."""
    conn = connect(db_path)
    try:
        _seed(conn, guild_id)
        row = conn.execute("SELECT role FROM weapon_roles WHERE guild_id=? AND weapon_id=?", (guild_id, normalize_id(weapon_id))).fetchone()
        return (row["role"] or None) if row else None
    finally:
        conn.close()


def weapon_names(db_path: str, guild_id: int) -> dict:
    """{id de arma: nombre en inglés} para poner el nombre real del arma en las posiciones."""
    return {r["weapon_id"]: r["name_en"] for r in list_rules(db_path, guild_id)}


def find_weapon(rules: List[dict], text: str) -> Optional[dict]:
    t = (text or "").strip().lower()
    if not t:
        return None
    for r in rules:
        if t in (r["weapon_id"].lower(), r["name_en"].lower(), r["name_es"].lower()):
            return r
    return None


def set_role(db_path: str, guild_id: int, weapon_id: str, role: str) -> Optional[dict]:
    """Cambia el rol de un arma (role vacío = sin regla). None si el arma no existe."""
    conn = connect(db_path)
    try:
        _seed(conn, guild_id)
        wid = normalize_id(weapon_id)
        with write_tx(conn):
            cur = conn.execute("UPDATE weapon_roles SET role=?, is_custom=1, updated_at=? WHERE guild_id=? AND weapon_id=?",
                               (clean_role(role), utcnow_iso(), guild_id, wid))
            if cur.rowcount == 0:
                return None
        return dict(conn.execute("SELECT * FROM weapon_roles WHERE guild_id=? AND weapon_id=?", (guild_id, wid)).fetchone())
    finally:
        conn.close()


def reset_defaults(db_path: str, guild_id: int) -> int:
    conn = connect(db_path)
    try:
        with write_tx(conn):
            conn.execute("DELETE FROM weapon_roles WHERE guild_id=?", (guild_id,))
            conn.execute("DELETE FROM weapon_roles_seeded WHERE guild_id=?", (guild_id,))
        _seed(conn, guild_id)
        return len(DEFAULT_WEAPONS)
    finally:
        conn.close()


def summary_lines(rules: List[dict], only_role: str = "", discord_map: Optional[dict] = None) -> List[str]:
    groups = {}
    for r in rules:
        groups.setdefault(r["role"] or "", []).append(r["name_es"] if r["name_es"] else r["name_en"])
    only = clean_role(only_role)
    lines = []
    for role in sorted(groups, key=lambda x: (x == "", x)):
        if only and role != only:
            continue
        names = ", ".join(sorted(groups[role], key=str.lower))
        dr = (discord_map or {}).get(role)
        tag = f" → <@&{dr}>" if dr else ""
        lines.append(f"**{role or 'sin regla'}**{tag} ({len(groups[role])}): {names}")
    return lines


def _chunks(lines: List[str], limit: int = 1900) -> List[str]:
    out, cur = [], ""
    for ln in lines:
        while len(ln) > limit:                              # una línea muy larga se corta en el último separador
            cut = ln.rfind(", ", 0, limit)
            cut = cut if cut > 0 else limit
            if cur:
                out.append(cur); cur = ""
            out.append(ln[:cut]); ln = ln[cut:].lstrip(", ")
        if len(cur) + len(ln) + 1 > limit and cur:
            out.append(cur); cur = ""
        cur += ln + "\n"
    if cur.strip():
        out.append(cur)
    return out


def make_reglas_group(db_path: str, is_allowed, default_discord_roles: Optional[dict] = None, guild_id: Optional[int] = None) -> app_commands.Group:
    """Subgrupo /compo reglas. is_allowed(member) -> bool decide quién puede usarlo (Council / administradores)."""
    init_schema(db_path)
    if default_discord_roles and guild_id:
        seed_discord_roles(db_path, guild_id, default_discord_roles)
    group = app_commands.Group(name="reglas", description="Qué rol de combate es cada arma (tank, healer, dps...)")

    async def deny(interaction):
        await interaction.response.send_message("Solo el Council o los administradores pueden ver y editar las reglas de armas.", ephemeral=True)

    async def weapon_ac(interaction: discord.Interaction, current: str):
        rules = list_rules(db_path, interaction.guild_id)
        c = (current or "").strip().lower()
        out = []
        for r in rules:
            if not c or c in r["name_en"].lower() or c in r["name_es"].lower() or c in r["weapon_id"].lower():
                label = f"{r['name_es']} / {r['name_en']} → {r['role'] or 'sin regla'}"[:100]
                out.append(app_commands.Choice(name=label, value=r["weapon_id"]))
            if len(out) >= 25:
                break
        return out

    async def role_ac(interaction: discord.Interaction, current: str):
        roles = sorted({r["role"] for r in list_rules(db_path, interaction.guild_id) if r["role"]})
        c = (current or "").strip().lower()
        return [app_commands.Choice(name=x, value=x) for x in roles if c in x][:25]

    @group.command(name="listar", description="Ver qué rol tiene cada arma")
    @app_commands.describe(rol="Mostrar solo las armas de este rol (opcional)")
    @app_commands.autocomplete(rol=role_ac)
    async def listar(interaction: discord.Interaction, rol: Optional[str] = None):
        if not is_allowed(interaction.user):
            return await deny(interaction)
        lines = summary_lines(list_rules(db_path, interaction.guild_id), rol or "", discord_roles_map(db_path, interaction.guild_id))
        if not lines:
            return await interaction.response.send_message("No hay armas con ese rol.", ephemeral=True)
        parts = _chunks(lines)
        await interaction.response.send_message(parts[0], ephemeral=True)
        for p in parts[1:]:
            await interaction.followup.send(p, ephemeral=True)

    @group.command(name="agregar", description="Asignar (o cambiar) el rol de un arma")
    @app_commands.describe(arma="Arma (nombre en inglés/español o ID)", rol="Rol de combate: tank, healer, dps, supp...")
    @app_commands.autocomplete(arma=weapon_ac, rol=role_ac)
    async def agregar(interaction: discord.Interaction, arma: str, rol: str):
        if not is_allowed(interaction.user):
            return await deny(interaction)
        role = clean_role(rol)
        if not role:
            return await interaction.response.send_message("Escribí el rol (por ejemplo `tank`, `healer`, `dps`).", ephemeral=True)
        rules = list_rules(db_path, interaction.guild_id)
        w = find_weapon(rules, arma)
        if not w:
            return await interaction.response.send_message("No encontré esa arma. Usá el autocompletado para elegirla.", ephemeral=True)
        before = w["role"] or "sin regla"
        set_role(db_path, interaction.guild_id, w["weapon_id"], role)
        await interaction.response.send_message(f"✅ **{w['name_es']}** ({w['name_en']}): {before} → **{role}**", ephemeral=True)

    @group.command(name="quitar", description="Dejar un arma sin regla de rol")
    @app_commands.describe(arma="Arma (nombre en inglés/español o ID)")
    @app_commands.autocomplete(arma=weapon_ac)
    async def quitar(interaction: discord.Interaction, arma: str):
        if not is_allowed(interaction.user):
            return await deny(interaction)
        w = find_weapon(list_rules(db_path, interaction.guild_id), arma)
        if not w:
            return await interaction.response.send_message("No encontré esa arma. Usá el autocompletado para elegirla.", ephemeral=True)
        set_role(db_path, interaction.guild_id, w["weapon_id"], "")
        await interaction.response.send_message(f"✅ **{w['name_es']}** quedó sin regla.", ephemeral=True)

    @group.command(name="discord", description="Elegir qué rol de Discord corresponde a un rol de combate (tank, healer...)")
    @app_commands.describe(rol_combate="Rol de combate (tank, healer, dps, supp...)", rol_discord="Rol de Discord; si lo dejás vacío se quita el vínculo")
    @app_commands.autocomplete(rol_combate=role_ac)
    async def discord_cmd(interaction: discord.Interaction, rol_combate: str, rol_discord: Optional[discord.Role] = None):
        if not is_allowed(interaction.user):
            return await deny(interaction)
        label = clean_role(rol_combate)
        if not label:
            return await interaction.response.send_message("Escribí el rol de combate (por ejemplo `tank`).", ephemeral=True)
        set_discord_role(db_path, interaction.guild_id, label, rol_discord.id if rol_discord else None)
        msg = f"✅ **{label}** → {rol_discord.mention}" if rol_discord else f"✅ **{label}** quedó sin rol de Discord."
        await interaction.response.send_message(msg, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @group.command(name="restablecer", description="Volver a la lista de armas y roles por defecto (borra tus cambios)")
    @app_commands.describe(confirmar="Poné true para confirmar: se pierden todos los cambios que hiciste")
    async def restablecer(interaction: discord.Interaction, confirmar: bool = False):
        if not is_allowed(interaction.user):
            return await deny(interaction)
        if not confirmar:
            return await interaction.response.send_message("Esto borra todos tus cambios y vuelve a la lista por defecto. Ejecutalo de nuevo con `confirmar: true`.", ephemeral=True)
        n = reset_defaults(db_path, interaction.guild_id)
        await interaction.response.send_message(f"✅ Lista restablecida ({n} armas).", ephemeral=True)

    return group
