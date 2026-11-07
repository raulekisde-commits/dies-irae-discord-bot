"""Azar verificable ("provably fair") para el casino: coinflip, dados, 1v1 y ruleta.

Es el esquema estándar de los casinos online (mismo algoritmo que Stake, así que sirve cualquier verificador compatible):

1. Al crear la partida (o la ronda de ruleta) el bot genera una semilla secreta `server_seed` (32 bytes al azar, en hex)
   y publica en el panel su huella `SHA-256(server_seed)`. Desde ese momento el resultado ya no se puede cambiar.
2. Al resolver se calcula `HMAC-SHA256(clave=server_seed, mensaje=f"{client_seed}:{nonce}:{ronda}")` (ronda 0, 1, 2…),
   que da 32 bytes por ronda. `client_seed` = semillas de las personas que jugaron, unidas con "-" (cada uno puede
   elegir la suya con /casino semilla); `nonce` = número de la partida.
3. Cada número aleatorio usa los 4 bytes siguientes: f = b0/256 + b1/256² + b2/256³ + b3/256⁴ (0 ≤ f < 1) y
   resultado = floor(f × cantidad de opciones). Moneda: 0 = cara, 1 = cruz. Ruleta: 0-36. Dado: 1 + floor(f × 6).
4. Al terminar se revela `server_seed`: cualquiera comprueba que su SHA-256 coincide con la huella publicada antes y
   recalcula el resultado (con /casino verificar o corriendo este mismo archivo, que solo usa la librería estándar):

       python3 provably_fair.py coinflip <server_seed> <client_seed> <nonce>
       python3 provably_fair.py ruleta   <server_seed> <client_seed> <nonce>
       python3 provably_fair.py dados    <server_seed> <client_seed> <nonce> <id_jugador1,id_jugador2,...>
"""
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import sys
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

GAMES = ("coinflip", "dice", "duel", "roulette")
GAME_ALIASES = {"coinflip": "coinflip", "dados": "dice", "dice": "dice", "1v1": "duel", "duelo": "duel", "duel": "duel",
                "ruleta": "roulette", "roulette": "roulette"}
MAX_TIE_ROUNDS = 50                         # dados: tope de desempates (igual que games_db)
CLIENT_SEED_RE = re.compile(r"^[A-Za-z0-9_]{1,32}$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS pf_seeds (
    game        TEXT NOT NULL,              -- coinflip | dice | duel | roulette
    ref_id      INTEGER NOT NULL,           -- id de la partida / duelo / ronda
    server_seed TEXT NOT NULL,              -- secreta hasta que se resuelve
    server_hash TEXT NOT NULL,              -- SHA-256 de server_seed: se publica al crear
    client_seed TEXT,                       -- se fija al resolver
    nonce       INTEGER,
    result      TEXT,                       -- JSON con el resultado calculado
    created_at  TEXT NOT NULL,
    revealed_at TEXT,                       -- NULL = todavía secreta
    PRIMARY KEY (game, ref_id)
);
CREATE TABLE IF NOT EXISTS pf_client_seeds (
    user_id     INTEGER PRIMARY KEY,
    client_seed TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
"""


# ============================================================
# Algoritmo (puro, sin base de datos)
# ============================================================
def new_server_seed() -> str:
    return secrets.token_hex(32)


def seed_hash(server_seed: str) -> str:
    return hashlib.sha256(server_seed.encode()).hexdigest()


def hmac_round(server_seed: str, client_seed: str, nonce: int, rnd: int) -> bytes:
    return hmac.new(server_seed.encode(), f"{client_seed}:{nonce}:{rnd}".encode(), hashlib.sha256).digest()


class FairRng:
    """Generador determinista: mismas semillas → mismos números. Expone lo que usa el código de los juegos
    (randint, randrange, choice, sample) para reemplazar a random.SystemRandom sin tocar la lógica de cada juego."""

    def __init__(self, server_seed: str, client_seed: str, nonce: int):
        self.server_seed, self.client_seed, self.nonce = server_seed, client_seed, int(nonce)
        self._round, self._buf, self._pos = 0, b"", 32
        self.used = 0                                          # cuántos números se sacaron (cursor / 4 bytes)

    def _byte(self) -> int:
        if self._pos >= 32:
            self._buf, self._pos = hmac_round(self.server_seed, self.client_seed, self.nonce, self._round), 0
            self._round += 1
        b = self._buf[self._pos]
        self._pos += 1
        return b

    def random(self) -> float:
        self.used += 1
        return sum(self._byte() / 256 ** (i + 1) for i in range(4))

    def randrange(self, n: int) -> int:
        if n <= 0:
            raise ValueError("rango vacío")
        return int(self.random() * n)

    def randint(self, a: int, b: int) -> int:
        return a + self.randrange(b - a + 1)

    def choice(self, seq: Sequence):
        return seq[self.randrange(len(seq))]

    def sample(self, population: Iterable, k: int) -> list:
        pool = list(population)
        if not 0 <= k <= len(pool):
            raise ValueError("muestra más grande que la población")
        for i in range(k):                                     # Fisher-Yates parcial
            j = i + self.randrange(len(pool) - i)
            pool[i], pool[j] = pool[j], pool[i]
        return pool[:k]


def coin(rng) -> str:
    return "cara" if rng.randint(0, 1) == 0 else "cruz"


def roulette_number(rng) -> int:
    return rng.randrange(37)


def roll_dice(players: List[int], rng) -> Tuple[int, List[List[Tuple[int, int]]], str]:
    """Cada participante (en orden de inscripción) tira un dado; si hay empate arriba, solo los empatados vuelven a tirar."""
    contenders = list(players)
    rounds: List[List[Tuple[int, int]]] = []
    for _ in range(MAX_TIE_ROUNDS):
        rolls = [(uid, rng.randint(1, 6)) for uid in contenders]
        rounds.append(rolls)
        top = max(r for _, r in rolls)
        tied = [uid for uid, r in rolls if r == top]
        if len(tied) == 1:
            return tied[0], rounds, ""
        contenders = tied
    return rng.choice(contenders), rounds, "Se llegó al máximo de desempates: se sorteó entre los empatados."


def compute(game: str, server_seed: str, client_seed: str, nonce: int, players: Optional[List[int]] = None) -> dict:
    """Recalcula el resultado de una partida a partir de las semillas (lo que hace cualquier verificador)."""
    rng = FairRng(server_seed, client_seed, nonce)
    if game in ("coinflip", "duel"):
        return {"side": coin(rng)}
    if game == "roulette":
        return {"number": roulette_number(rng)}
    if game == "dice":
        winner, rounds, note = roll_dice(players or [], rng)
        return {"winner": winner, "rounds": [[[u, r] for u, r in rnd] for rnd in rounds]}
    raise ValueError("juego inválido")


# ============================================================
# Persistencia (dentro de la transacción del juego)
# ============================================================
def _connect(db_path: str):
    from dbutil import connect
    return connect(db_path)


def init_schema(db_path: str) -> None:
    conn = _connect(db_path)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


def _now() -> str:
    from dbutil import utcnow_iso
    return utcnow_iso()


def tx_commit(conn, game: str, ref_id: int) -> str:
    """Genera y guarda la semilla secreta de una partida nueva. Devuelve el hash que se publica."""
    seed = new_server_seed()
    conn.execute("INSERT OR IGNORE INTO pf_seeds(game, ref_id, server_seed, server_hash, created_at) VALUES (?,?,?,?,?)",
                 (game, int(ref_id), seed, seed_hash(seed), _now()))
    return conn.execute("SELECT server_hash FROM pf_seeds WHERE game = ? AND ref_id = ?", (game, int(ref_id))).fetchone()[0]


def tx_client_seed(conn, user_id: int) -> str:
    r = conn.execute("SELECT client_seed FROM pf_client_seeds WHERE user_id = ?", (int(user_id),)).fetchone()
    if r is not None:
        return r[0]
    seed = secrets.token_hex(8)
    conn.execute("INSERT INTO pf_client_seeds(user_id, client_seed, updated_at) VALUES (?,?,?)", (int(user_id), seed, _now()))
    return seed


def tx_rng(conn, game: str, ref_id: int, user_ids: List[int]) -> FairRng:
    """Arma el generador de una partida que se está resolviendo y deja registradas client seed y nonce."""
    tx_commit(conn, game, ref_id)                              # partidas creadas antes de este sistema: se genera ahora
    server_seed = conn.execute("SELECT server_seed FROM pf_seeds WHERE game = ? AND ref_id = ?", (game, int(ref_id))).fetchone()[0]
    seen, ordered = set(), []
    for u in user_ids:
        if u not in seen:
            seen.add(u)
            ordered.append(int(u))
    client_seed = "-".join(tx_client_seed(conn, u) for u in ordered) or "casino"
    conn.execute("UPDATE pf_seeds SET client_seed = ?, nonce = ? WHERE game = ? AND ref_id = ?", (client_seed, int(ref_id), game, int(ref_id)))
    return FairRng(server_seed, client_seed, int(ref_id))


def tx_finish(conn, game: str, ref_id: int, result: dict) -> Optional[Dict[str, object]]:
    """Revela la semilla (la partida ya está resuelta) y devuelve los datos para mostrar y verificar."""
    conn.execute("UPDATE pf_seeds SET result = ?, revealed_at = ? WHERE game = ? AND ref_id = ?",
                 (json.dumps(result), _now(), game, int(ref_id)))
    return _record(conn, game, ref_id)


def _record(conn, game: str, ref_id: int) -> Optional[Dict[str, object]]:
    r = conn.execute("SELECT * FROM pf_seeds WHERE game = ? AND ref_id = ?", (game, int(ref_id))).fetchone()
    if r is None:
        return None
    keys = [d[0] for d in conn.execute("SELECT * FROM pf_seeds LIMIT 0").description]
    row = dict(zip(keys, tuple(r)))
    revealed = row["revealed_at"] is not None
    return {"game": game, "ref_id": int(ref_id), "server_hash": row["server_hash"], "revealed": revealed,
            "server_seed": row["server_seed"] if revealed else None,       # nunca se muestra antes de resolver
            "client_seed": row["client_seed"], "nonce": row["nonce"],
            "result": json.loads(row["result"]) if row["result"] else None}


def get_record(db_path: str, game: str, ref_id: int) -> Optional[Dict[str, object]]:
    conn = _connect(db_path)
    try:
        return _record(conn, game, ref_id)
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()


def get_hash(db_path: str, game: str, ref_id: int) -> Optional[str]:
    rec = get_record(db_path, game, ref_id)
    return rec["server_hash"] if rec else None


def get_client_seed(db_path: str, user_id: int) -> str:
    from dbutil import connect, write_tx
    conn = connect(db_path)
    try:
        with write_tx(conn):
            return tx_client_seed(conn, user_id)
    finally:
        conn.close()


def set_client_seed(db_path: str, user_id: int, seed: str) -> str:
    seed = (seed or "").strip()
    if not CLIENT_SEED_RE.match(seed):
        raise ValueError("La semilla tiene que tener entre 1 y 32 caracteres: letras, números o `_`.")
    from dbutil import connect, write_tx
    conn = connect(db_path)
    try:
        with write_tx(conn):
            conn.execute("INSERT INTO pf_client_seeds(user_id, client_seed, updated_at) VALUES (?,?,?) "
                         "ON CONFLICT(user_id) DO UPDATE SET client_seed = excluded.client_seed, updated_at = excluded.updated_at",
                         (int(user_id), seed, _now()))
        return seed
    finally:
        conn.close()


# ============================================================
# Textos para los paneles (sin dependencia de discord)
# ============================================================
COMMAND_NAME = {"coinflip": "coinflip", "dice": "dados", "duel": "1v1", "roulette": "ruleta"}
SEALED_TITLE = "🔒 Resultado sellado"


def sealed_text(server_hash: Optional[str]) -> Optional[str]:
    if not server_hash:
        return None
    return (f"`{server_hash}`\nEs el SHA-256 de la semilla secreta del bot: el resultado ya quedó fijado y "
            "la semilla se revela al terminar para que cualquiera lo verifique.")


def proof_text(proof: Optional[dict]) -> Optional[str]:
    """Bloque que acompaña al resultado: todo lo necesario para recalcularlo de forma independiente."""
    if not proof or not proof.get("revealed"):
        return None
    client = str(proof.get("client_seed") or "")
    if len(client) > 120:
        client = client[:120] + "… (completa en /casino verificar)"
    return (f"Hash publicado: `{proof['server_hash']}`\n"
            f"Semilla del bot: `{proof['server_seed']}`\n"
            f"Semilla de jugadores: `{client}` · Nonce: `{proof.get('nonce')}`\n"
            f"Para comprobarlo: `/casino verificar juego:{COMMAND_NAME[proof['game']]} numero:{proof['ref_id']}`")[:1024]


# ============================================================
# Verificación contra lo que quedó registrado en el juego
# ============================================================
@dataclass
class Verification:
    status: str                         # ok | mismatch | pending | notfound
    record: Optional[dict] = None
    hash_ok: bool = False
    computed: Optional[dict] = None
    stored: Optional[dict] = None


def _stored_result(conn, game: str, ref_id: int) -> Tuple[Optional[dict], Optional[List[int]]]:
    """Lo que el juego realmente pagó (fuente de verdad del dinero) y, en dados, el orden de inscripción."""
    if game in ("coinflip", "dice"):
        r = conn.execute("SELECT kind, status, winner_id, outcome FROM games WHERE id = ?", (ref_id,)).fetchone()
        if r is None or r[0] != game or r[1] != "done":
            return None, None
        out = json.loads(r[3] or "{}")
        if game == "coinflip":
            return {"side": out.get("side")}, None
        players = [int(x[0]) for x in conn.execute("SELECT user_id FROM game_players WHERE game_id = ? ORDER BY rowid", (ref_id,))]
        return {"winner": int(r[2]), "rounds": out.get("rounds")}, players
    if game == "duel":
        r = conn.execute("SELECT status, side FROM duels WHERE id = ?", (ref_id,)).fetchone()
        return ({"side": r[1]} if r is not None and r[0] == "done" else None), None
    r = conn.execute("SELECT status, number FROM roulette_rounds WHERE id = ?", (ref_id,)).fetchone()
    return ({"number": int(r[1])} if r is not None and r[1] is not None else None), None


def verify(db_path: str, game: str, ref_id: int) -> Verification:
    conn = _connect(db_path)
    try:
        try:
            rec = _record(conn, game, ref_id)
        except sqlite3.OperationalError:
            rec = None
        if rec is None:
            return Verification("notfound")
        if not rec["revealed"]:
            return Verification("pending", rec)
        stored, players = _stored_result(conn, game, ref_id)
    finally:
        conn.close()
    hash_ok = hmac.compare_digest(seed_hash(rec["server_seed"]), rec["server_hash"])
    if stored is None:                                         # devuelta o cancelada: no hubo resultado que pagar
        return Verification("ok" if hash_ok else "mismatch", rec, hash_ok, None, None)
    computed = compute(game, rec["server_seed"], rec["client_seed"], rec["nonce"], players)
    same = computed == stored
    return Verification("ok" if hash_ok and same else "mismatch", rec, hash_ok, computed, stored)


# ============================================================
# Verificador independiente (línea de comandos)
# ============================================================
def _main(argv: List[str]) -> int:
    if len(argv) < 4 or argv[0].lower() not in GAME_ALIASES:
        print(__doc__)
        return 2
    game = GAME_ALIASES[argv[0].lower()]
    server_seed, client_seed, nonce = argv[1], argv[2], int(argv[3])
    players = [int(x) for x in argv[4].split(",")] if game == "dice" and len(argv) > 4 else None
    print(f"SHA-256 de la server seed: {seed_hash(server_seed)}  (tiene que ser igual al hash publicado en el panel)")
    print("Resultado:", json.dumps(compute(game, server_seed, client_seed, nonce, players), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
