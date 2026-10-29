# Wuju Coins — guía rápida

## Archivos nuevos (subir a GitHub junto a bot_persistente.py)
dbutil.py · economy_db.py · economy.py · .gitignore  (tests/ es opcional)

## Cambios en bot_persistente.py
- `from economy import setup_economy`
- En `setup_hook`, antes del `tree.sync`: `await setup_economy(self, DB_PATH, GUILD_ID, MIEMBRO_ROLE_ID)`

## Railway (IMPORTANTE)
El disco de Railway se borra en cada deploy. Creá un **Volume** (ej. montado en `/data`) y agregá la variable
`BOT_DB_PATH=/data/bot_data.sqlite3`. Sin eso se pierden saldos, CTA, sorteos, etc.
No cambia ninguna variable existente (DISCORD_TOKEN sigue igual).

## Puesta en marcha en Discord (admin / dueño)
1. `/config-moneda icono:` emoji personalizado del server (ej. :wuju:)
2. `/bal-registro #canal` canal de auditoría
3. En cada canal donde se use: `/perms-canal comando:bal` (y addmoney, addmoney-role, remove-money, top)
4. `/perms-comando @Rol comando:addmoney` para dar permiso a un rol (bal y top: por defecto rol Miembro)
5. `/perms-ver` para revisar. `/bal-reintentar-registro` reenvía logs pendientes.

## Comandos
`!bal [@user]` · `!top` · `!addmoney @user 1.500.000` · `!addmoney-role @Rol 50.000` (por miembro) · `!remove-money @user|@Rol monto`
Números con `.` o `,` como separador de miles; siempre se muestran con puntos. Se permite saldo negativo.

## Probado / no probado
Probado con un reemplazo de discord.py (57 pruebas: atomicidad, idempotencia, concurrencia, permisos, paginado, reintento de logs).
NO probado contra Discord real: probalo en el server antes de usarlo. Requiere discord.py >= 2.1 y el intent Server Members activo.

## Novedades

- `/give-money usuario cantidad [motivo]`: pasa Wuju Coins de TU balance a otra persona (atómico, queda en el historial de ambos, no suma al Balance histórico).
- `/dados` y `/coinflip` con `/` además de `!`. Anotarse/aceptar es un solo toque. `!winrate @persona` (y `/winrate`) muestra el winrate en dados y coinflip.
- `/casino abrir|cerrar` (Council): mientras está cerrado se bloquean dados, coinflip y 1v1 (crear y anotarse). `/casinoban usuario` (Council) bloquea a una persona; usarlo otra vez la saca de la lista.
- `/1v1 usuario cantidad`: duelo de cara o cruz. La persona retada acepta o rechaza; sin tope (el límite es el balance de quien reta) y la persona retada tiene que tener al menos lo apostado. Si no responde en 5 minutos se devuelve la apuesta.
- `/panel apuestas` (antes `/apuestas panel`): seis paneles — Top apostado, Top ganado (ganado − perdido, con negativos), Total perdido (número exacto), Top winrate de dados, Top winrate de coinflip y lista negra del casino.
- `/panel cambiomoneda` (antes `/agregar wuju-coins`): publica dos paneles con tickets.
  - **Vender Wuju Coins por silver:** el ticket muestra el `!bal` de la persona y avisa al Council, que tiene **Aceptar** (pide el monto, por defecto todo el balance, y lo retira dejando registro en el log) y **Cerrar**.
  - **Comprar Wuju Coins:** explica que hay que depositar y mandar la foto; el ticket pide la cantidad y el Council tiene **Agregar Wuju Coins** (con doble confirmación; agrega los Wuju Coins con motivo y registro en el log) y **Cerrar**.
- El slash de reclutamiento `/panel` pasó a llamarse `/panel-reclutamiento` (el `!panel` no cambia), porque `/panel` ahora es un grupo.
