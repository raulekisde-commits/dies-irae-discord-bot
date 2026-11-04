"""Sistema de invitados: el rol Legacy invita a alguien (/invitar), los miembros ven quién invitó (/quien-invito),
el staff los lista (/invitados), y /info describe sistemas."""
import asyncio
import os
import traceback
from datetime import datetime, timezone
from typing import List, Optional

import discord
from discord import app_commands
from discord.ext import commands

from dbutil import connect, write_tx, utcnow_iso
from guildshop import can_manage

NO_MENTIONS = discord.AllowedMentions.none()
LEGACY_ROLE_ID = int(os.getenv("LEGACY_ROLE_ID", "1465833385804038427"))
GUEST_ROLE_ID = int(os.getenv("GUEST_ROLE_ID", "1565993695461777468"))
PUBLIC_ROLE_ID = int(os.getenv("PUBLIC_ROLE_ID", "1266805315547041902"))
MIEMBRO_ROLE_ID = int(os.getenv("MIEMBRO_ROLE_ID", "1257896455860129822"))
STAFF_ROLE_ID = int(os.getenv("STAFF_ROLE_ID", "1257896709246423083"))
REASON_MAX = 300

SCHEMA = """
CREATE TABLE IF NOT EXISTS guests (
    guild_id   INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    invited_by INTEGER NOT NULL,
    reason     TEXT NOT NULL,
    created_at TEXT NOT NULL,
    active     INTEGER NOT NULL DEFAULT 1,
    ended_at   TEXT,
    PRIMARY KEY (guild_id, user_id)
);
"""

# /info: solo lo que le sirve a un usuario. /info-staff: lo mismo + la sección del equipo (INFO_STAFF).
INFO_TEXTS = {
    "invitados": ("👥 Sistema de invitados",
                  "Permite que alguien de confianza entre a la comunidad sin ser miembro de la guild.\n\n"
                  "• Solo quienes tienen el rol **Legacy** pueden invitar con `/invitar usuario motivo`.\n"
                  "• Al invitar tenés que indicar el nombre exacto de su personaje de Albion: el bot lo busca, te muestra el personaje y vos confirmás que es tu amigo.\n"
                  "• El invitado recibe el rol **Invitado** y tiene acceso a la economía; el casino se le habilita aparte.\n"
                  "• El **Staff** y el **Council** pueden hacer una **invitación premium** con `/invitar-premium`: la persona entra directo con el rol **Miembro**.\n"
                  "• Cualquier miembro puede ver quién invitó a alguien con `/quien-invito usuario`.\n"
                  "• Quien invita responde por su invitado."),
    "warnings": ("⚠️ Sistema de warnings",
                 "Los warnings son sanciones leves que el **Staff** y el **Council** registran cuando alguien no respeta las reglas del servidor.\n\n"
                 "• Los gestiona solo el equipo: no hay comandos para usuarios.\n"
                 "• Si tenés dudas sobre uno, hablá con el Staff o el Council."),
    "casino": ("🎰 Sistema de casino",
               "Juegos para apostar **Wuju Coins**, la moneda del servidor (`/bal` para ver tu saldo, `/top` para el ranking).\n\n"
               "• **Dados** (`/casino dados`): partida abierta de 5 minutos; se anota quien quiere y gana el número más alto, que se lleva el pozo.\n"
               "• **Coinflip** (`/casino coinflip`): desafío de cara o cruz; lo acepta una persona y el ganador se queda con todo.\n"
               "• **1v1** (`/casino 1v1 usuario cantidad`): duelo de cara o cruz contra alguien en particular, que debe aceptar.\n"
               "• **Apuestas** (`/casino apuestas apostar`, `/casino apuestas todo`): se apuesta a una de las opciones de una apuesta abierta; el pozo se reparte entre quienes acertaron.\n"
               "• **Ruleta**: una sola para todo el servidor.\n\n"
               "**Tus estadísticas**\n"
               "• `/casino winrate [usuario]` — winrate en dados y coinflip, total apostado, ganado, perdido y si estás en la lista negra.\n"
               "• `/top winrate` y `/casino total` — rankings. En los paneles de apuestas ves Top apostado, Top ganado y Top perdido.\n\n"
               "**Reglas**\n"
               "• Las apuestas se descuentan al instante y no hay devolución si perdés.\n"
               "• Cada juego funciona solo en ciertos canales y para ciertos roles. Los invitados no tienen acceso al casino salvo que se les habilite.\n"
               "• Si el casino está cerrado o te bloquearon, no podés jugar.\n"
               "• Jugá con responsabilidad: es solo moneda del servidor."),
    "economia": ("💰 Sistema de economía",
                 "La moneda del servidor son los **Wuju Coins**.\n\n"
                 "**Comandos**\n"
                 "• `/bal` — ver tu saldo (o el de otra persona).\n"
                 "• `/give-money usuario cantidad [motivo]` — le pasás monedas a otra persona de tu saldo (el bot te pide confirmar antes de enviar).\n"
                 "• En `/bal` también ves tu **balance de embo** (embotelladas de Albion), que lleva el Staff.\n"
                 "• `/verhistorial [usuario] [completo]` — tus últimos movimientos (te llegan por mensaje privado); con `completo` recibís el historial entero en un archivo CSV.\n\n"
                 "**Splits**\n"
                 "• Los splits de las CTA se hacen y se publican en <#1558630638066602094> (canal de splits): ahí podés ver cada reparto, quiénes participaron y cuánto le tocó a cada uno.\n\n"
                 "**Guild Shop**\n"
                 "• Podés comprar ítems en el **Guild Shop** con un **10% de descuento**: creás un ticket desde el panel del shop y un Seller te atiende.\n\n"
                 "**Cambiar tu balance**\n"
                 "• Si querés cambiar tu balance (por ejemplo canjearlo), hacelo en <#1565241897277067375>."),
    "zvz-vods": ("🎥 ZvZ y VODs (coaching)",
                 "Subís tus VODs de ZvZ y el equipo de coaching te da una devolución para que mejores.\n\n"
                 "**Cómo funciona**\n"
                 "• Buscá el panel de coaching y apretá **Abrir mi post de VODs**: se crea **tu post** en <#1566695879656607924> (uno solo por persona; si ya lo tenés, el botón te lleva a él).\n"
                 "• En tu post pegá el link (YouTube, Twitch, Medal…) o subí el video. Cada link o video cuenta como un VOD.\n"
                 "• Decí con qué rol jugás (por ejemplo «voy de healer»). Si no lo escribís, el bot te lo pregunta con un menú.\n"
                 "• Un coach, Opinólogo, Staff o Council toma tu VOD y te responde en el mismo post: esa es tu devolución.\n\n"
                 "**Rol Observación**\n"
                 "• Si tenés el rol <@&1559680220800360519>, tenés que subir **mínimo 3 VODs por semana** (la semana va de lunes a domingo, hora UTC).\n"
                 "• Los miembros sin ese rol también pueden subir VODs para pedir devolución, pero no tienen mínimo.\n\n"
                 "**Comandos**\n"
                 "• `/coaching mis-vods` — cuántos VODs subiste esta semana y cuántos te faltan.\n"
                 "• `/top coach` — los coaches que más devoluciones dan."),
}

INFO_STAFF = {
    "invitados": ("**🎟️ Invitado común** (`/invitar`, rol Legacy)\n"
                  "• `/invitados` — lista todos los invitados activos con quién los invitó, cuándo y el motivo.\n"
                  "• Al invitar, el bot registra al invitado como si hubiera usado `/registrar`, le saca el rol **Public** y le pone **Invitado**.\n"
                  "• Si el invitado pierde el rol Invitado (p. ej. lo hacen miembro), el registro queda como histórico y deja de aparecer en la lista.\n\n"
                  "**⭐ Invitación premium** (**Staff** y **Council**)\n"
                  "• `/invitar-premium usuario quien_es personaje_de_albion` — la persona entra directo con el rol **Miembro**: se la registra (aunque la API de Albion no encuentre el personaje), se le saca **Public** y queda guardado quién la invitó y el motivo.\n"
                  "• Si ya era **invitado común**, pasa a premium: se le saca el rol Invitado y se mantiene el registro que tenía.\n"
                  "• `/invitados-premium` — lista los invitados premium activos con quién los invitó, cuándo y el motivo.\n"
                  "• `/quien-invito` muestra si la invitación fue premium. Si pierde el rol **Miembro**, deja de aparecer en la lista (queda como histórico).\n\n"
                  "• Quien invita queda como responsable: si hay problemas, revisá `/quien-invito` o la lista para saber a quién consultar."),
    "warnings": ("• `/warning usuario razon` — pone un warning. Queda guardado quién lo puso, cuándo y por qué. La respuesta la ve solo quien ejecuta el comando.\n"
                 "• `/warnings usuario` — lista los warnings de esa persona (fecha, quién y motivo). La respuesta es privada, como la del resto de los comandos de warnings.\n"
                 "• `/warning-top` — quién acumula más warnings (privado).\n"
                 "• Los puede usar **Staff** y **Council**. Escribí un motivo claro: es el único registro que queda."),
    "casino": ("• `/casino abrir|cerrar` (Council) — mientras está cerrado se bloquean `/casino dados`, `/casino coinflip` y `/casino 1v1`.\n"
               "• `/casino limite juego cantidad` (Council) — apuesta máxima de dados, coinflip o 1v1; 0 quita el límite. Aplica a partidas nuevas.\n"
               "• `/casino ban usuario` (Council) — bloquea a alguien del casino; repetirlo con la misma persona lo saca de la lista.\n"
               "• `/eco perms-canal` y `/eco perms-comando` — en qué canales y para qué roles funciona cada juego.\n"
               "• `/casino apuestas crear enunciado opciones` — crea una apuesta (panel público + hilo); las opciones van separadas por coma. La resolvés desde el panel.\n"
               "• `/panel apuestas` (Admin / rol autorizado) — publica Top apostado, Top ganado, Top perdido y la lista negra. Los datos son los mismos que ve cualquiera con `/casino winrate`.\n"
               "• `/canal dados canal` — canal donde se publican los paneles de `!dados`."),
    "economia": ("• `/eco addmoney` y `/eco remove-money` — cargan o retiran monedas a una persona o a un rol (con motivo, queda en el historial).\n"
                 "• `/eco embo add|remove|set usuario cantidad [motivo]` — balance de embo (Admin o rol con `/eco perms-comando comando:embo`). No puede quedar negativo.\n"
                 "• `/eco set-balance usuario cantidad` (Admin) — deja el balance exacto. `/eco eco-stats` (Staff) — total de monedas en el servidor. `/eco historico-bal reset` (Admin) — pone en cero el balance histórico.\n"
                 "• **Splits** (Council / Admin): `/split` (con looters) y `/split-ava` (scouts con pago fijo) crean el panel + hilo en el canal de splits. Se editan con el botón **Editar** o `/editar`, y se pagan con **Cerrar split**: el pago es idempotente, no se duplica aunque se repita.\n"
                 "• Listas del split: looters, scouts y la **lista mitad** (`!agregarlistamitad`), que cobra la mitad.\n"
                 "• **Cambio de moneda**: `/panel cambiomoneda` publica los tickets de venta y compra de Wuju Coins; el Council acepta y retira el balance."),
    "zvz-vods": ("• `/panel coaching` (Council / Admin) — publica el panel con el botón que abre el post de cada persona en el foro. Pueden usarlo Observación y Miembros.\n"
                 "• Cada VOD genera una tarea para el rol **Coaching** indicando con qué rol juega la persona. Pueden tomarla Coaching, Staff, Opinólogo y Council. No tiene hilo; el recordatorio es cada 12 h.\n"
                 "• La tarea se completa sola cuando alguien del equipo responde en el post, o con el botón **Terminada**.\n"
                 "• `/coaching reporte [semana]` (Council / Staff / Coaching / Opinólogo) — quién cumplió los 3 VODs semanales (en curso o anterior), quién no y cuántos subió cada uno. Los miembros sin Observación aparecen aparte y no cuentan para el mínimo.\n"
                 "• `/top coach` — quién cierra más devoluciones."),
}

INFO_TEXTS.update({
    "splits": ("💸 Splits (reparto de las CTA)",
               "El split es el reparto de lo que se gana en una CTA. Se publican en <#1558630638066602094> (canal de splits): ahí podés ver **todos los detalles** de cada reparto, quiénes participaron y cuánto cobró cada uno.\n\n"
               "**Quién cobra**\n"
               "• Solo recibe split la gente que se **anotó en la CTA** (`/info zvz`).\n"
               "• Si llegaste tarde, igual podés anotarte: el **Staff decide** si entrás al split o no.\n\n"
               "**Cómo se calcula**\n"
               "• La guild se queda el **5%** de las tabs.\n"
               "• Al vender las tabs, el precio baja un **20% en HO** y un **15% en city**.\n"
               "• El **looter** se lleva el **10%** de lo que looteó, menos la reparación, y además cuenta como uno más en el split general.\n"
               "• Lo que queda se divide en partes iguales entre todos los anotados y se te acredita directo en tu balance (`/bal`).\n\n"
               "Cada split queda registrado en el canal, así que siempre podés revisar cómo se hizo el cálculo."),
    "zvz": ("⚔️ ZvZ: cómo anotarte a una CTA",
            "**Anotarte**\n"
            "• Cuando sale una CTA se publica un panel con la composición y un hilo. Las inscripciones tienen un **tiempo seteado por el caller** (el panel muestra cuándo cierran).\n"
            "• Para anotarte, escribí en el hilo el **número de la posición** que querés ocupar. Ej: `5`.\n"
            "• A veces la Party 1 es solo para Legacy durante los primeros minutos; el panel lo avisa.\n"
            "• Si tenés el rol **Bomb**, te anotás con `+` en el hilo de Bomb o con su botón.\n\n"
            "**Tu build**\n"
            "• En el panel de la CTA tocá **🧩 Ver mi build**: te muestra la build del slot en el que te anotaste.\n\n"
            "**Loot**\n"
            "• Tenés que **depositar todo lo looteado** de la CTA en la **tab que elija el caller**. Después se vende y se reparte: mirá `/info splits`.\n\n"
            "**Después de la CTA**\n"
            "• El Staff carga los killboards y te cuenta para `/stats` (`/info stats`).\n"
            "• Para pedir feedback de tus VODs: `/info zvz-vods`."),
    "reclutamiento": ("🧾 Cómo unirte a la guild",
                      "• Buscá el panel de reclutamiento y tocá el botón para postularte.\n"
                      "• Completás un formulario: tu nick exacto en Albion, rol main, otros roles que sabés jugar, horarios UTC, gremio anterior, si podés grabar tus CTAs, idioma y si tenés amigos en la guild que te puedan vouchear.\n"
                      "• Se abre un canal privado donde solo vos y los reclutadores ven tu postulación. Pueden hacerte preguntas ahí.\n"
                      "• Si te aceptan, tu cuenta queda **registrada** con ese personaje y recibís los roles apenas aparezcas dentro de la guild en el juego, así que acordate de pedir la invitación en Albion.\n"
                      "• Si te rechazan, te llega un mensaje privado y podés volver a aplicar más adelante."),
    "timers": ("⏱️ Timers de recursos",
               "Los timers avisan cuándo sale un recurso en un mapa.\n\n"
               "**Cargar un timer**\n"
               "• `/timeradd material mapa tiempo [tier]` — lo pueden usar los **miembros registrados**.\n"
               "• `tiempo` es el horario UTC en que sale, formato `HH:MM` (ej: `16:10`).\n"
               "• Tier: 4.4 a 8.4 para fibra, cuero, mineral y madera; Common a Legendary para vortex y core. **Dragon** no lleva tier.\n"
               "• El bot no deja subir **duplicados**: si ya existe el mismo material, mapa, tier y hora, te avisa quién lo cargó.\n\n"
               "**Dónde ver los timers**\n"
               "• Los timers aparecen en los paneles de <#1462516135361777875>, ordenados por categoría, con el tiempo que falta.\n"
               "• **60 minutos antes** de que salga, el bot avisa en <#1462184630835740732>.\n\n"
               "Eliminar un timer lo hace el Staff."),
    "stats": ("📊 Estadísticas",
              "**`/stats [usuario]`** (sin nombre, ves las tuyas). Necesitás tener tu personaje de Albion registrado. Muestra:\n"
              "• ⚔️ Kills, 💀 muertes y 🛡️ asistencia (peleas en las que apareciste).\n"
              "• 💥 Muertes en el primer engage (porcentaje y cantidad).\n"
              "• 🚫 CTAs skipeadas: estabas conectado en la CTA y no apareciste en el killboard ni anotado.\n"
              "• 🎭 Tus roles más jugados.\n"
              "• 📈 IP promedio, máxima y mínima.\n"
              "Solo cuentan los killboards que cargó el staff.\n\n"
              "**`/casino winrate [usuario]`** — estadísticas del casino: winrate en dados y coinflip, total apostado, ganado, perdido, resultado neto y si estás en la lista negra.\n\n"
              "**Rankings**\n"
              "• `/top` — Wuju Coins, kills, muertes, asistencia, skippers, IP, winrate, tareas del staff y coaches.\n"
              "• `/coaching mis-vods` — tus VODs de la semana (`/info zvz-vods`)."),
    "registro": ("📝 Registro de tu personaje",
                 "El registro vincula tu cuenta de Discord con tu personaje de Albion. Sin registro no podés usar varios comandos de miembros (si el registro es obligatorio).\n\n"
                 "**Comandos**\n"
                 "• `/registrar usuario_de_albion` — escribí tu nick y el bot busca el personaje. Tiene que estar en la guild.\n"
                 "• `/mi_registro` — ver qué personaje tenés vinculado.\n\n"
                 "**Reglas**\n"
                 "• Un personaje solo puede estar vinculado a **una** cuenta de Discord. Una cuenta de Discord puede tener varios personajes.\n"
                 "• El nombre se toma exacto desde Albion, así que escribilo tal cual está en el juego.\n"
                 "• Si alguien más te registra, tenés que **aceptar vos** desde tu cuenta: nadie se registra por otro sin permiso.\n"
                 "• Si cambiaste de personaje o hay un error, pedile ayuda al Staff.\n"
                 "• Tu apodo en el servidor se ajusta al nombre de tu personaje."),
})

INFO_TEXTS["alters"] = ("🔗 Alters",
                        "Un **alter** es una cuenta o personaje extra que pertenece a alguien del Discord.\n\n"
                        "• Solo el **Staff, el Council y los administradores** pueden enlazar un alter a una persona.\n"
                        "• Cada alter guarda **quién lo autorizó** (quien ejecutó el comando) y la fecha.\n"
                        "• Un alter no puede estar enlazado a dos personas, ni ser el personaje principal de otra.\n"
                        "• Si tenés un alter y no está enlazado, pedíselo al Staff.")
INFO_TEXTS["roles"] = ("🎭 Roles de Observación y CTA",
                       "El equipo puede darte o quitarte el rol **Observación** y los roles de **CTA** (Tank, Healer, Supp, DPS, Battle Mount, Flex, Bomb).\n\n"
                       "• Cada cambio **queda registrado**: a quién, qué rol, quién lo hizo y cuándo.\n"
                       "• Si tenés Observación, tenés que subir tus VODs semanales (`/info zvz-vods`).\n"
                       "• Si creés que te falta un rol o te lo sacaron por error, hablá con el Staff o con Coaching.")

INFO_STAFF.update({
    "splits": ("**Crear**\n"
               "• `/split titulo [tab] [city] [rol] [total] [reparacion] [bolsitas] [looters]` (Council / Admin) — crea el panel con botones **Editar**, **Cerrar split** y **Cancelar split**, más el hilo con la lista y el cálculo.\n"
               "• Si la composición de la CTA es «track», el split se crea solo al cerrar la CTA con la gente anotada. Si falla, se arma a mano con `/split`.\n"
               "• Anotados tarde: aparecen con botones **Agregar al split** / **Descartar**. Deciden Council, Staff, Caller o quien creó el split.\n\n"
               "**Editar** (botón **Editar** o `/editar [id]`; el panel es solo tuyo y vence a los 15 min)\n"
               "• 📝 Datos (título, tab, city) · 💰 Montos y taxes (total, reparación, tax de venta y de guild) · 👜 Bolsitas.\n"
               "• 🪙 Agregar / ✏️ editar / ➖ quitar looters (máx. 25; cada looter tiene su looteado, reparación y tax propio).\n"
               "• 👥 Agregar / 🧹 quitar personas · 🎭 Agregar rol (suma a todos los del rol) · ⚔️ Agregar killboard (links de AlbionBB y guild: suma a quienes pelearon).\n"
               "• En el hilo: `/agregarlista` o `!agregarlista @a @b`.\n\n"
               "**Cálculo**\n"
               "• Por defecto: tax de venta **20%** (HO; en city poné **15%** en Montos y taxes), tax de guild **5%**, tax looter **10%**.\n"
               "• Neto = total + looteado − reparación. A eso se le descuentan tax de venta, de guild y la parte de los looters.\n"
               "• Las **bolsitas** se suman al final y no pagan taxes. El resto se divide en partes iguales; lo que sobra por redondeo no se reparte.\n\n"
               "**Cerrar y pagar**\n"
               "• **Cerrar split** pide confirmación y acredita el balance a cada persona. El pago es **idempotente**: no se duplica aunque se repita.\n"
               "• Si te equivocaste, **Reabrir para editar** y luego **Confirmar edición**: el bot resta lo ya pagado y paga de nuevo con los datos corregidos.\n\n"
               "**`/split-ava`** (Council / Admin) — para AVA: sin looters. Los **scouts** cobran un monto fijo (`pago_scout`) que se descuenta antes de repartir. Se cargan con `scouts` o desde Editar.\n"
               "• **Lista mitad**: gente que cobra la mitad de una parte. Se agrega con `!agregarlistamitad @persona` o con el botón **Agregar lista mitad** (quitar: **Quitar lista mitad**). Solo existe en los split AVA."),
    "zvz": ("**Quién puede qué**\n"
            "• Iniciar y administrar CTA: **Staff, Caller y Council / Admin**.\n"
            "• Botones **Cerrar CTA**, **Cancelar CTA** y **Agregar Bomb**: solo **Council y Caller**.\n"
            "• **Ver mi build**: cualquier miembro.\n\n"
            "**Publicar**\n"
            "• `/cta nombre [tiempo] [titulo] [programar]` — `nombre` es una composición guardada. `tiempo` es la duración de las inscripciones (`HH:MM` o minutos; mínimo 10, por defecto 40). `programar` es una hora UTC `HH:MM` para publicarla más tarde con @everyone y @here.\n"
            "• Crea el panel con las parties y el hilo de inscripción. La gente se anota escribiendo el número de posición.\n"
            "• Las composiciones se administran con `/compo` (Council / Admin). `/compo builds nombre link` pone el link de «Builds».\n\n"
            "**Gestionar una CTA**\n"
            "• `/cta-admin activas` y `/cta-admin historial` — ver CTAs activas y las últimas cerradas.\n"
            "• `/cta-admin programadas` y `/cta-admin cancelar-programada` — las que esperan su hora.\n"
            "• `/cta-admin cerrar` — cierra la inscripción antes de tiempo.\n"
            "• `/cta-admin asignar` y `/cta-admin liberar` — poner o sacar a un jugador de una posición a mano.\n\n"
            "**Bomb**\n"
            "• **Agregar Bomb** crea un panel e hilo aparte en el canal de Bomb. Puede haber varios por CTA. Solo el rol Bomb se anota (`+` o botón) y cuentan en el split.\n\n"
            "**Cerrar**\n"
            "• **Cerrar CTA** crea el panel y el hilo de stats (`/info-staff stats`) y deja de aceptar anotados. Si la composición es «track», genera el split (`/info-staff splits`).\n"
            "• Durante los primeros 5 min la Party 1 es solo Legacy si la composición lo tiene activado."),
    "reclutamiento": ("**Montaje**\n"
                      "• `/panel reclutamiento` (Admin) publica el panel con el botón de postulación en el canal donde lo uses.\n"
                      "• Cada postulación abre un canal privado visible para el postulante y el rol **Reclutador**. Arriba queda el resumen con todos los datos y el nick de Albion.\n\n"
                      "**Cómo reclutar paso a paso**\n"
                      "1. Leé el resumen: nick, rol main, otros roles, horarios, gremio anterior, si graba CTAs, idioma y **amigos en la guild** (para vouch). Hablá con la persona en el canal.\n"
                      "2. Tocá **✔ Aceptar**. El bot te pregunta qué **roles de CTA** darle (el main viene preseleccionado) y si le das el rol **Observación**. Confirmá.\n"
                      "3. El bot **registra** al postulante con su nick de Albion (`/info-staff registro`).\n"
                      "4. Si ya está en la guild, recibe al instante **Miembro** + los roles elegidos y se le saca **Public**. Si todavía no, los recibe **automáticamente** cuando aparezca dentro de la guild. Si pasan 14 días sin entrar, se cancela la espera.\n"
                      "5. Revisá los pendientes con `/reclutas-pendientes`.\n\n"
                      "**Rechazar / Cerrar**\n"
                      "• **❌ Rechazar** manda un mensaje privado al postulante, deja registro en el canal de logs y borra el canal.\n"
                      "• **🔒 Cerrar** cierra el ticket sin aceptar ni rechazar.\n"
                      "• Cada aceptación y rechazo queda en el canal de logs con quién lo hizo.\n\n"
                      "Solo los reclutadores pueden usar Aceptar, Rechazar y Cerrar. Quien apretó Aceptar es quien confirma los roles."),
    "timers": ("• **Cargar**: lo hacen los miembros registrados con `/timeradd`.\n"
               "• **Eliminar**: solo **Staff**, con `/deltimer` (completando los mismos datos con que se cargó) o con el botón **Eliminar** del panel. Los miembros no pueden borrar.\n"
               "• Antes de guardar, el bot revisa duplicados (mismo material, mapa, tier y hora UTC) y no sube el repetido.\n"
               "• Al agregar o borrar, el panel de esa categoría se actualiza solo; si falta, se vuelve a crear.\n"
               "• Si el bot no puede escribir en el canal de timers, avisa que guardó el timer pero no pudo actualizar los paneles: revisá sus permisos.\n"
               "• Si hay un timer mal cargado, borralo y pedí que lo vuelvan a subir con el dato correcto."),
    "stats": ("**Cargar killboards (estadísticas de la CTA)**\n"
              "• Al cerrar la CTA el bot crea el **hilo de stats** en <#1565980248133345300>. Ahí hay dos botones: **🗡️ Cargar killboard** y **📋 Cargar conectados**.\n"
              "• Los pueden usar **Staff, Council, Caller y Opinólogo**.\n"
              "• **Killboard**: pegá links o IDs de AlbionBB (varios separados por coma o espacio; hay un máximo por carga). También con `/killboard cargar enlaces` dentro del hilo. Fuera de un hilo de CTA solo Staff y Council.\n"
              "• **Conectados**: subí el `.txt` del bloc de notas con *Character Name / Last Seen / Roles* (botón o `/killboard conectados archivo`).\n\n"
              "**Qué hace con eso**\n"
              "• Suma kills, muertes, asistencia, primer engage, roles jugados e IP a cada jugador registrado.\n"
              "• Con el killboard **y** la lista de conectados calcula los **skippers**: gente conectada que no aparece en el killboard ni anotada. Si falta uno de los dos, el bot avisa.\n"
              "• Sin los dos datos cargados, las stats y el `/top` no cuentan esa CTA.\n\n"
              "**Administrar**\n"
              "• `/killboard borrar batalla` (Staff / Council) — saca un killboard para que deje de contar.\n"
              "• `/killboard probar batalla` (Staff / Council) — muestra qué datos trae AlbionBB sin guardar nada.\n"
              "• Los jugadores sin personaje registrado no suman: usá `/sin-albion`.\n"
              "• Las stats del casino (`/casino winrate`) salen del historial de apuestas y no dependen de killboards."),
    "registro": ("• `/admin_registro usuario` — ver los personajes vinculados a un miembro.\n"
                 "• `/admin_desvincular usuario` — quitar un personaje vinculado. También sirve si dos personas reclaman el mismo.\n"
                 "• `/admin_vincular` (excepcional) — vincula a mano sin que la persona acepte.\n"
                 "• `/sin-albion` — miembros de Discord sin personaje (con `mencionar:true` los etiqueta). `/sin-discord` — jugadores de la guild sin Discord vinculado.\n"
                 "• `/guild-sin-discord archivo` (Council / Admin) — subís el export de la guild de Albion (Character Name / Last Seen / Roles) y publica quiénes están en la guild sin Discord registrado, y aparte los registrados sin el rol Miembro. Los alters enlazados no se listan.\n"
                 "• `/limpiar-miembros [dar_public]` (Admin) — muestra quiénes tienen **Miembro** sin cuenta enlazada y, si confirmás, les saca el rol (y les da Public, salvo `dar_public:false`). No toca bots, admins, Staff, Council, invitados premium ni aceptados en espera. No borra balance ni estadísticas.\n"
                 "• `/sincronizar-registros` — vuelve a buscar en Albion los registros que quedaron sin ID (vínculos con `/admin_vincular` hechos con la API caída o con el nombre sin verificar), los vincula bien y corrige el apodo; lista los que no pudo verificar. `/sincronizar-apodos` solo revisa apodos.\n"
                 "• `/sincronizar-apodos` — revisa que cada apodo sea el personaje y corrige los que no.\n"
                 "• `/reclutas-pendientes` — aceptados que todavía no entraron a la guild.\n"
                 "• `/recuperar-lista` y `/recuperar @persona` — los últimos 5 registrados que se fueron del servidor; si vuelve, devuelve registro, roles y apodo.\n"
                 "• `/estado-api` (Admin) — revisa si la API de Albion responde. Si no registra a nadie, empezá por acá.\n"
                 "• `/canal registro` y `/registro_obligatorio` (Admin) — canal de avisos y exigir (o no) estar registrado.\n"
                 "• `/desvincular` (Council) — quita el personaje de tu propia cuenta.\n\n"
                 "**Qué garantiza y qué no**\n"
                 "• La API de Albion no tiene login: prueba que el personaje existe y su guild, pero **no quién lo controla**.\n"
                 "• El nombre sale exacto de la API, un personaje solo puede ir con una cuenta y nadie registra a otro sin que acepte."),
})
INFO_STAFF["alters"] = ("• `/alter agregar usuario personaje [descripcion]` — enlaza un alter a la persona (p. ej. «alter de crafteo»). Queda guardado quién lo agregó (vos) y cuándo.\n"
                        "• `/alter eliminar personaje` — desenlaza un alter.\n"
                        "• `/alter listar` — todos los alters con su dueño, quién los agregó y la descripción.\n"
                        "• `/alter ver [usuario] [alter]` — alters de una persona, o de quién es un alter (con quién lo autorizó). Máximo 10 por persona.\n"
                        "• Solo Staff, Council y administradores; se valida en el bot, no solo por permisos de Discord.")
INFO_STAFF["roles"] = ("• `/rol dar usuario rol [motivo]` y `/rol quitar usuario rol [motivo]` — el bot cambia el rol y lo registra con quién lo hizo.\n"
                       "• **Observación:** dar = Staff, Council, Admin, Coaching y Opinólogo · quitar = Staff, Council, Admin y Coaching.\n"
                       "• **Roles de CTA** (Tank, Healer, Supp, DPS, Battle Mount, Flex, Bomb): dar = Staff, Council, Admin y Coaching · quitar = Staff, Council y Admin.\n"
                       "• `/rol historial [usuario]` — últimos movimientos (quién dio o quitó cada rol).\n"
                       "• No te podés cambiar roles a vos mismo, y el rol del bot tiene que estar por encima del rol que se cambia.")
INFO_TEXTS["wujupon"] = ("🎴 Wujupón (gachapon)",
    "Comprás tiradas con **Wuju Coins** y cada tirada te da una **carta** al azar con un premio. Todo se usa desde el **panel de Wujupón**, "
    "y las respuestas son privadas.\n\n"
    "**Botones del panel**\n"
    "• 🛒 **Comprar tiradas** — se descuentan de tu balance. Puede haber un límite de compras por día (se renueva a las 00:00 UTC).\n"
    "• 🎰 **Tirar Wujupón** — usa una tirada y te muestra la carta. El mensaje se borra solo a los 60 s, pero la carta ya queda guardada.\n"
    "• 🎒 **Mi inventario** — tus cartas: elegí una para ver qué hace y tocá **Canjear**.\n"
    "• 📜 **Ver premios** — todas las cartas con su rareza y probabilidad.\n"
    "• 📨 **Mis solicitudes** — el estado de tus canjes con el Council.\n\n"
    "**Rarezas**: ⚪ Común · 🔵 Rara · 🟣 Épica · 🟠 Legendaria · 🔴 Mítica.\n\n"
    "**Canjes**\n"
    "• 🤖 **Automáticos** (Wuju Coins, embo, cancelar el cooldown de *Cobrar balance*): se aplican al instante.\n"
    "• 🛠️ **Los entrega el Council**: se abre un encargo y la carta queda **reservada** hasta que lo resuelvan. Si lo rechazan, la carta "
    "puede volver a tu inventario.\n"
    "• ⚠️ Las cartas que afectan a otra persona necesitan aprobación del Council.\n"
    "• ☠️ **Cartas malas** (descuentos, autosilencio, más cooldown de cobro): no van a tu inventario; se aplican solas "
    "(por ahora las aplica el Council).\n\n"
    "**Tipos de carta**: 🛡️ escudos (nadie te puede usar cartas y te protegen de las malas) · 💥 purgar escudo · "
    "🧹 limpiezas (te sacan mutes o congelamientos) · ❄️ congelar Wujupón (el otro no puede tirar ni usar cartas). "
    "Qué hace cada carta lo ves al abrirla en 🎒 **Mi inventario**.\n\n"
    "**Reglas**: cada carta se usa una sola vez. Si tenés el balance congelado no podés comprar tiradas. Cada tirada se anuncia en el canal "
    "de Wujupón con la carta, la rareza y la probabilidad.")
INFO_STAFF["wujupon"] = (
    "**Montaje** (Council / Admin)\n"
    "• `/panel wujupon` — publica el panel en el canal.\n"
    "• `/wujupon canal-encargos [canal]` — canal privado donde llegan los canjes.\n"
    "• `/wujupon canal-anuncios [canal] [quitar]` — canal público de tiradas; `quitar: Sí` deja de anunciarlas.\n\n"
    "**Economía**\n"
    "• `/wujupon precio cantidad` — precio de cada tirada (actualiza el panel).\n"
    "• `/wujupon limite-diario cantidad` — compras por persona por día UTC (`0` = sin límite).\n"
    "• `/wujupon stats` — ingresos, entregado y pendiente por moneda.\n\n"
    "**Cartas**\n"
    "• `/wujupon carta-agregar` — carta nueva (la entrega el Council).\n"
    "• `/wujupon carta-editar carta [nombre] [descripcion]` — `quitar` borra la descripción.\n"
    "• `/wujupon carta-probabilidad carta [probabilidad] [rareza]` — cambia la chance o la mueve de tier.\n"
    "• `/wujupon cooldown-cobro` — suma, saca o muestra el cooldown de «Cobrar balance».\n"
    "• `/wujupon premio carta activo` — habilita o deshabilita una carta.\n"
    "• Si las habilitadas no suman 100 %, salen en proporción a su peso (el bot avisa).\n\n"
    "**Jugadores**\n"
    "• `/wujupon regalar-tiradas usuario cantidad` (negativo = quitar) · `/wujupon dar-carta usuario premio`.\n"
    "• `/wujupon devolver-carta numero` — vuelve una carta usada al inventario. Si era automática, lo entregado no se descuenta.\n"
    "• `/wujupon inventario usuario` · `/wujupon encargos` · `/wujupon historial [usuario]` · `/wujupon registro`.\n"
    "• **Encargos**: **Tomar** → **Aprobar** → **Completado**. **Rechazar** pregunta si se devuelve la carta.\n"
    "• ☠️ **Cartas malas**: llegan solas como encargo. Aplicalas a mano (`/eco remove-money`, `/eco embo remove`, timeout, `/wujupon cooldown-cobro`) y "
    "marcá **Completado**; si no corresponde, **Rechazar** (nunca vuelven al inventario).\n\n"
    "**Solo Admin**\n"
    "• `/wujupon publicar-premios [canal]` — lista pública de premios (no se actualiza sola).\n"
    "• `/wujupon pausa pausado [motivo]` — pausa o reanuda comprar, tirar y canjear (el inventario se sigue viendo).\n"
    "• `/wujupon foto-premio` y `/wujupon canal-nsfw` — imagen del premio con foto (spoiler en el canal +18, se borra a los 60 s).\n"
    "• `/wujupon limpiar-cartas [tambien_tiradas]` — borra las cartas sin usar de todos y anula los encargos abiertos.")

INFO_STAFF["economia"] = INFO_STAFF["economia"] + ("\n• **Guild Shop** (`/panel shop [canal]`, Council): publica el Top Vendedores y el panel de compra. Quien compra abre un ticket con el rol Seller; la venta se confirma ahí. Los logs van al canal de `/canal logs guildshop`.")


def _roles(m) -> set:
    return {r.id for r in getattr(m, "roles", [])}


GUEST_KINDS = ("guest", "premium")


def ensure_schema(db_path: str) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(guests)").fetchall()}
        if "kind" not in cols:                                    # migración aditiva: los invitados viejos quedan como 'guest'
            conn.execute("ALTER TABLE guests ADD COLUMN kind TEXT NOT NULL DEFAULT 'guest'")
            conn.commit()
    finally:
        conn.close()


def add_guest(db_path: str, guild_id: int, user_id: int, invited_by: int, reason: str, kind: str = "guest") -> bool:
    """False si ya es invitado activo de ese mismo tipo (un invitado común sí puede pasar a premium)."""
    conn = connect(db_path)
    try:
        with write_tx(conn):
            r = conn.execute("SELECT active, kind FROM guests WHERE guild_id=? AND user_id=?", (guild_id, user_id)).fetchone()
            if r is not None and r["active"] and (r["kind"] or "guest") == kind:
                return False
            conn.execute("INSERT OR REPLACE INTO guests(guild_id,user_id,invited_by,reason,created_at,active,ended_at,kind) VALUES (?,?,?,?,?,1,NULL,?)",
                         (guild_id, user_id, invited_by, reason, utcnow_iso(), kind))
            return True
    finally:
        conn.close()


def get_guest(db_path: str, guild_id: int, user_id: int) -> Optional[dict]:
    conn = connect(db_path)
    try:
        r = conn.execute("SELECT * FROM guests WHERE guild_id=? AND user_id=?", (guild_id, user_id)).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def list_guests(db_path: str, guild_id: int, kind: str = "guest") -> List[dict]:
    conn = connect(db_path)
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM guests WHERE guild_id=? AND active=1 AND kind=? ORDER BY created_at", (guild_id, kind))]
    finally:
        conn.close()


def end_guest(db_path: str, guild_id: int, user_id: int, kind: str = "guest") -> None:
    conn = connect(db_path)
    try:
        with write_tx(conn):
            conn.execute("UPDATE guests SET active=0, ended_at=? WHERE guild_id=? AND user_id=? AND active=1 AND kind=?",
                         (utcnow_iso(), guild_id, user_id, kind))
    finally:
        conn.close()


def remove_guest_row(db_path: str, guild_id: int, user_id: int) -> None:
    conn = connect(db_path)
    try:
        with write_tx(conn):
            conn.execute("DELETE FROM guests WHERE guild_id=? AND user_id=?", (guild_id, user_id))
    finally:
        conn.close()


async def _say(interaction, text, **kw):
    try:
        if interaction.response.is_done():
            return await interaction.followup.send(text, ephemeral=True, allowed_mentions=NO_MENTIONS, **kw)
        return await interaction.response.send_message(text, ephemeral=True, allowed_mentions=NO_MENTIONS, **kw)
    except Exception:
        traceback.print_exc()


def _unix(iso: str) -> int:
    d = datetime.fromisoformat(iso)
    return int((d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp())


class ConfirmInviteView(discord.ui.View):
    """Solo quien invita puede confirmar que el personaje es su amigo."""
    def __init__(self, cog, inviter_id: int, target, reason: str, character, premium: bool = False):
        super().__init__(timeout=300)
        self.cog, self.inviter_id, self.target, self.reason, self.character, self.done = cog, inviter_id, target, reason, character, False
        self.premium = premium
        ok = discord.ui.Button(label="Sí, es mi amigo", style=discord.ButtonStyle.success, emoji="✅")
        no = discord.ui.Button(label="No, cancelar", style=discord.ButtonStyle.secondary, emoji="✖️")
        ok.callback, no.callback = self._ok, self._no
        self.add_item(ok)
        self.add_item(no)

    async def interaction_check(self, interaction) -> bool:
        if interaction.user.id != self.inviter_id:
            await interaction.response.send_message("🔒 Esta confirmación es de quien invita.", ephemeral=True)
            return False
        return True

    async def _no(self, interaction):
        self.done = True
        await interaction.response.edit_message(content="✖️ Invitación cancelada. No se cambió nada.", embeds=[], view=None)

    async def _ok(self, interaction):
        if self.done:
            return await interaction.response.send_message("Ya fue procesada.", ephemeral=True)
        self.done = True
        await interaction.response.edit_message(content="⏳ Procesando…", embeds=[], view=None)
        text = await self.cog.finish_invite(interaction, interaction.user, self.target, self.reason, self.character, premium=self.premium)
        await interaction.edit_original_response(content=text)


class InvitadosCog(commands.Cog):
    def __init__(self, bot, db_path: str, guild_id: int):
        self.bot, self.db_path, self.guild_id = bot, db_path, guild_id

    async def cog_load(self):
        ensure_schema(self.db_path)

    async def run(self, fn, *a, **kw):
        return await asyncio.to_thread(lambda: fn(self.db_path, *a, **kw))

    # ---------- /invitar ----------
    @app_commands.command(name="invitar", description="(Legacy) Invitar a alguien: pasa de Public a Invitado y se registra con su personaje")
    @app_commands.describe(usuario="La persona de Discord que invitás", quien_es="Quién es / por qué lo invitás",
                           personaje_de_albion="Nombre exacto de su personaje en Albion (se registra igual que con /registrar)")
    @app_commands.guild_only()
    async def invitar(self, interaction: discord.Interaction, usuario: discord.Member, quien_es: str, personaje_de_albion: str):
        me = interaction.user
        perms = getattr(me, "guild_permissions", None)
        if LEGACY_ROLE_ID not in _roles(me) and not (perms is not None and perms.administrator):
            return await _say(interaction, "🔒 Solo quienes tienen el rol **Legacy** pueden invitar.")
        await self._start_invite(interaction, usuario, quien_es, personaje_de_albion, premium=False)

    # ---------- /invitar-premium ----------
    @app_commands.command(name="invitar-premium", description="(Staff/Council) Invitación premium: entra directo con el rol Miembro y queda quién lo invitó")
    @app_commands.describe(usuario="La persona de Discord que invitás", quien_es="Quién es / por qué lo invitás",
                           personaje_de_albion="Nombre exacto de su personaje en Albion (se registra igual que con /registrar)")
    @app_commands.guild_only()
    async def invitar_premium(self, interaction: discord.Interaction, usuario: discord.Member, quien_es: str, personaje_de_albion: str):
        if not (can_manage(interaction.user) or STAFF_ROLE_ID in _roles(interaction.user)):
            return await _say(interaction, "🔒 Solo el **Staff** y el **Council** pueden hacer invitaciones premium.")
        await self._start_invite(interaction, usuario, quien_es, personaje_de_albion, premium=True)

    async def _start_invite(self, interaction: discord.Interaction, usuario: discord.Member, quien_es: str, personaje_de_albion: str, premium: bool):
        import registration_db as rdb
        me = interaction.user
        reason = " ".join((quien_es or "").split())
        if not reason:
            return await _say(interaction, "❌ Tenés que indicar quién es el invitado.")
        if len(reason) > REASON_MAX:
            return await _say(interaction, f"❌ Máximo {REASON_MAX} caracteres.")
        err = self._check_target(interaction.guild, me, usuario, premium)
        if err:
            return await _say(interaction, err)
        reg_cog = self.bot.get_cog("RegistrationCog")
        if reg_cog is None:
            return await _say(interaction, "❌ El sistema de registro no está disponible ahora. Probá más tarde.")
        await interaction.response.defer(ephemeral=True)
        if premium:                                               # si ya tiene registro (p. ej. era invitado común), se usa ese
            existing = await self.run(rdb.get_registration, usuario.id)
            if existing is not None:
                import registration as _reg
                c = _reg.Character(char_id=existing.char_id, name=existing.char_name, region=None, guild="", alliance="", kill_fame=0, death_fame=0)
                view = ConfirmInviteView(self, me.id, usuario, reason, c, premium=True)
                return await interaction.followup.send(
                    f"⭐ {usuario.mention} ya está registrado como **{discord.utils.escape_markdown(existing.char_name)}**: se mantiene ese registro. "
                    "¿Confirmás la invitación premium (rol **Miembro**)?", view=view, ephemeral=True, allowed_mentions=NO_MENTIONS)
        look = await reg_cog.lookup_character(personaje_de_albion, any_guild=True)
        # Registro forzado: si la API no responde, no lo encuentra o hay homónimos, se registra igual con el nombre escrito (sin ID, como /admin_vincular).
        manual = {"error": "la API de Albion no respondió", "none": "no encontré ese personaje en la API de Albion",
                  "ambiguous": "hay varios personajes con ese nombre en distintos servidores de Albion"}.get(look.kind)
        if manual:
            import registration as _reg
            name = _reg._clean_name(personaje_de_albion)
            if not name:
                return await _say(interaction, "❌ Nombre de personaje inválido.")
            c = _reg.Character(char_id=None, name=name, region=None, guild="", alliance="", kill_fame=0, death_fame=0)
            view = ConfirmInviteView(self, me.id, usuario, reason, c, premium=premium)
            return await interaction.followup.send(
                f"⚠️ {manual[0].upper() + manual[1:]}. Si confirmás, {usuario.mention} queda registrado **a mano** como "
                f"**{discord.utils.escape_markdown(name)}** (revisá que esté bien escrito; `/sincronizar-registros` completa el ID después).",
                view=view, ephemeral=True, allowed_mentions=NO_MENTIONS)
        if look.kind != "ok":
            return await _say(interaction, "❌ No pude validar ese personaje.")
        c = look.character
        embed = reg_cog.character_embed(c, title="🎟️ ¿Es este tu amigo?")
        embed.set_footer(text=f"Vas a invitar a {usuario.display_name} con este personaje. Confirmá solo si es la persona correcta.")
        view = ConfirmInviteView(self, me.id, usuario, reason, c, premium=premium)
        await interaction.followup.send(f"Confirmá que **{discord.utils.escape_markdown(c.name)}** es el personaje de {usuario.mention}:", embed=embed, view=view,
                                        ephemeral=True, allowed_mentions=NO_MENTIONS)

    def _check_target(self, guild, me, usuario, premium: bool = False) -> Optional[str]:
        if usuario.bot or usuario.id == me.id:
            return "❌ No podés invitar a un bot ni a vos mismo."
        have = _roles(usuario)
        if MIEMBRO_ROLE_ID in have:
            return "❌ Esa persona ya es miembro de la guild."
        if premium:                                               # un invitado común sí puede pasar a premium
            if guild.get_role(MIEMBRO_ROLE_ID) is None:
                return "❌ No encuentro el rol Miembro en el servidor."
            return None
        if GUEST_ROLE_ID in have:
            return f"❌ {usuario.mention} ya es invitado."
        if guild.get_role(GUEST_ROLE_ID) is None:
            return "❌ No encuentro el rol Invitado en el servidor."
        return None

    async def finish_invite(self, interaction: discord.Interaction, inviter, usuario: discord.Member, reason: str, c, premium: bool = False) -> str:
        """Tras confirmar: registra al invitado con ese personaje, le da el rol Invitado (o Miembro si es premium), le saca Public y guarda quién lo invitó."""
        import registration_db as rdb
        guild = interaction.guild
        err = self._check_target(guild, inviter, usuario, premium)
        if err:
            return err
        reg_cog = self.bot.get_cog("RegistrationCog")
        existing = await self.run(rdb.get_registration, usuario.id) if premium else None
        linked_now = existing is None
        if existing is not None and existing.char_name.casefold() != c.name.casefold():
            return f"❌ {usuario.mention} ya está registrado como **{discord.utils.escape_markdown(existing.char_name)}**."
        if linked_now:
            try:
                await self.run(rdb.admin_link, usuario.id, c.name, inviter.id, char_id=c.char_id or None, region=c.region or None, guild_name=c.guild or None,
                               method="guest_invite" if c.char_id else "admin_override")
            except rdb.RegError as exc:
                return f"❌ {exc}"
        role_id, role_name = (MIEMBRO_ROLE_ID, "Miembro") if premium else (GUEST_ROLE_ID, "Invitado")
        kind_txt = "Invitación premium" if premium else "Invitado"
        try:
            await usuario.add_roles(guild.get_role(role_id), reason=f"{kind_txt} por {inviter} ({inviter.id}): {reason}")
        except discord.HTTPException as exc:
            if linked_now:
                await self.run(rdb.unlink, usuario.id, inviter.id, c.name, f"no se pudo dar el rol {role_name}")
            return f"❌ No pude darle el rol {role_name} (revisá que mi rol esté por encima y tenga Gestionar roles): {exc}"
        warn = ""
        guest = guild.get_role(GUEST_ROLE_ID)
        if premium and guest is not None and GUEST_ROLE_ID in _roles(usuario):
            try:
                await usuario.remove_roles(guest, reason=f"Pasa a invitación premium ({inviter})")
            except discord.HTTPException:
                warn += "\n⚠️ No pude sacarle el rol Invitado; avisale al staff."
        public = guild.get_role(PUBLIC_ROLE_ID)
        if public is not None and PUBLIC_ROLE_ID in _roles(usuario):
            try:
                await usuario.remove_roles(public, reason=f"Invitado por {inviter} ({inviter.id})")
            except discord.HTTPException:
                warn += "\n⚠️ Quedó con el rol Invitado pero no pude sacarle Public; avisale al staff."
        try:
            await self.run(add_guest, guild.id, usuario.id, inviter.id, reason, "premium" if premium else "guest")
        except Exception:
            traceback.print_exc()
            warn += "\n⚠️ No pude guardar el registro de la invitación; avisale al staff."
        try:
            nick = await reg_cog.apply_nickname(usuario, c.name)
        except Exception:
            nick = ""
        if nick:
            warn += f"\n{nick}"
        try:
            icon, extra = ("⭐", " con **invitación premium**") if premium else ("🎟️", "")
            await interaction.channel.send(f"{icon} {inviter.mention} invitó a {usuario.mention}{extra} (**{discord.utils.escape_markdown(c.name)}**): *{reason}*", allowed_mentions=NO_MENTIONS)
        except Exception:
            pass
        if premium:
            return f"✅ {usuario.mention} entró con **invitación premium** (rol Miembro) y quedó registrado como **{discord.utils.escape_markdown(c.name)}**.{warn}"
        return f"✅ {usuario.mention} ahora es invitado y quedó registrado como **{discord.utils.escape_markdown(c.name)}**.{warn}"

    # ---------- /quien-invito ----------
    @app_commands.command(name="quien-invito", description="Ver quién invitó a alguien y por qué")
    @app_commands.describe(usuario="El invitado")
    @app_commands.guild_only()
    async def quien_invito(self, interaction: discord.Interaction, usuario: discord.Member):
        me = interaction.user
        if MIEMBRO_ROLE_ID not in _roles(me) and not can_manage(me) and STAFF_ROLE_ID not in _roles(me):
            return await _say(interaction, "🔒 Solo los miembros de la guild pueden usar este comando.")
        g = await self.run(get_guest, interaction.guild.id, usuario.id)
        if g is None:
            return await _say(interaction, f"{usuario.mention} no figura como invitado.")
        premium = (g.get("kind") or "guest") == "premium"
        estado = ("invitado premium" if premium else "invitado actual") if g["active"] else ("ya no es invitado premium" if premium else "ya no es invitado")
        await _say(interaction, f"🎟️ {usuario.mention} ({estado})\nInvitado por: <@{g['invited_by']}>\nMotivo: {g['reason']}\nFecha: <t:{_unix(g['created_at'])}:f>")

    # ---------- /invitados y /invitados-premium ----------
    @app_commands.command(name="invitados", description="(Staff) Listar los invitados actuales")
    @app_commands.guild_only()
    async def invitados(self, interaction: discord.Interaction):
        await self._list(interaction, "guest")

    @app_commands.command(name="invitados-premium", description="(Staff) Listar los invitados premium actuales")
    @app_commands.guild_only()
    async def invitados_premium(self, interaction: discord.Interaction):
        await self._list(interaction, "premium")

    async def _list(self, interaction: discord.Interaction, kind: str):
        me = interaction.user
        if not (can_manage(me) or STAFF_ROLE_ID in _roles(me)):
            return await _say(interaction, "🔒 Solo el Staff puede ver la lista de invitados.")
        rows = await self.run(list_guests, interaction.guild.id, kind)
        title = "Invitados premium" if kind == "premium" else "Invitados"
        if not rows:
            return await _say(interaction, f"No hay {title.lower()} actualmente.")
        lines = [f"• <@{r['user_id']}> · invitado por <@{r['invited_by']}> el <t:{_unix(r['created_at'])}:d> · {r['reason']}" for r in rows]
        chunks, cur = [], ""
        for ln in lines:
            if len(cur) + len(ln) + 1 > 1800:
                chunks.append(cur)
                cur = ""
            cur += ln + "\n"
        chunks.append(cur)
        await _say(interaction, f"**{title} ({len(rows)})**\n" + chunks[0])
        for c in chunks[1:]:
            await _say(interaction, c)

    # ---------- /info (usuarios) y /info-staff (usuarios + equipo) ----------
    INFO_CHOICES = [app_commands.Choice(name=n, value=n) for n in ("splits", "zvz", "reclutamiento", "registro", "timers", "stats", "economia", "casino", "zvz-vods", "invitados", "warnings", "alters", "roles", "wujupon")]

    @staticmethod
    def info_embed(value: str, staff: bool) -> discord.Embed:
        title, text = INFO_TEXTS[value]
        if staff:
            title = f"{title} · Staff"
            text = f"{text}\n\n**🛡️ Para el equipo (Staff / Council)**\n{INFO_STAFF[value]}"
        return discord.Embed(title=title, description=text[:4000], color=discord.Color.gold() if staff else discord.Color.blurple())

    @app_commands.command(name="info", description="Explica cómo funciona un sistema del servidor (para usuarios)")
    @app_commands.describe(tema="Qué sistema querés conocer")
    @app_commands.choices(tema=INFO_CHOICES)
    async def info(self, interaction: discord.Interaction, tema: app_commands.Choice[str]):
        value = getattr(tema, "value", tema)
        await interaction.response.send_message(embed=self.info_embed(value, False), ephemeral=True)

    @app_commands.command(name="info-staff", description="(Staff) Lo mismo que /info más la parte del equipo, con más detalle")
    @app_commands.describe(tema="Qué sistema querés conocer")
    @app_commands.choices(tema=INFO_CHOICES)
    @app_commands.guild_only()
    async def info_staff(self, interaction: discord.Interaction, tema: app_commands.Choice[str]):
        me = interaction.user
        if not (can_manage(me) or STAFF_ROLE_ID in _roles(me)):
            return await _say(interaction, "🔒 Solo el Staff y el Council pueden usar `/info-staff`.")
        value = getattr(tema, "value", tema)
        await interaction.response.send_message(embed=self.info_embed(value, True), ephemeral=True)

    # ---------- si pierde el rol Invitado (p. ej. lo hacen miembro) el registro queda como histórico ----------
    @commands.Cog.listener()
    async def on_member_update(self, before, after):
        for role_id, kind in ((GUEST_ROLE_ID, "guest"), (MIEMBRO_ROLE_ID, "premium")):
            if role_id in _roles(before) and role_id not in _roles(after):
                try:
                    await self.run(end_guest, after.guild.id, after.id, kind)
                except Exception:
                    traceback.print_exc()


async def setup_invitados(bot, db_path: str, guild_id: int) -> InvitadosCog:
    cog = InvitadosCog(bot, db_path, guild_id)
    await bot.add_cog(cog, guilds=[discord.Object(id=guild_id)])
    return cog
