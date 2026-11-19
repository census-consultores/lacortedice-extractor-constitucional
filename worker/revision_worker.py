"""
Worker de REVISIÓN de novedades.

Responde una pregunta por número: ¿SATJE tiene algo que nosotros no tengamos?
Lo que falte se encola en worker_lista_items y lo procesa el motor que ya
existe (worker/lista_worker.py). Acá NO se baja ningún PDF ni se llama a la IA.

POR QUÉ EXISTE
  El barrido normal (worker/runner.py) recorre número por número y a cada
  causa que encuentra le vuelve a bajar el PDF, lo resube a Strapi y le corre
  la IA completa, aunque esté idéntica a ayer. Medido sobre los jobs reales:
  50,6 números/hora. Un barrido de 2019–2026 son 17.116 números, o sea ~338
  horas; ni con los 4 slots en paralelo entra en un día.

QUÉ SE ENCOLA — y por qué NO alcanza con comparar contra la revisión anterior
  La primera versión preguntaba "¿cambió la huella desde ayer?". Con la tabla
  vacía no hay ayer, así que las 2.649 causas vivas salían como novedad y se
  encolaban todas: ~141 horas y ~$64 de IA, con $28 de saldo. Habría parecido
  que funcionaba hasta secar la cuenta.

  La pregunta correcta no es "¿cambió desde ayer?" sino "¿me falta algo?".
  Se compara lo que SATJE dice HOY contra lo que hay en NUESTRA base:
    - en trámite, sin sentencia        → registrar, no encolar (no hay qué extraer)
    - con sentencia que ya tenemos     → registrar, no encolar (ya la tenemos)
    - con sentencia que no tenemos     → ENCOLAR tribunal
    - casación resuelta que no tenemos → ENCOLAR corte
  Así la primera corrida levanta el estado sin encolar nada de más, y a partir
  de la segunda la huella sirve para saltarse lo que no se movió.

LA FRONTERA SALE DEL REGISTRO, NO DE LAS SENTENCIAS
  Un número existe en SATJE mucho antes de tener sentencia. `causas` solo
  guarda las que ya la tienen: Guayas 2025 tiene sentencias hasta la 666 pero
  solo 184 causas — las ~480 del medio están en trámite y eran invisibles.
  Si la frontera saliera de ahí, en 2026 (0 causas) arrancaría siempre en el 1
  y nunca avanzaría. Por eso sale de worker_revision_estado, que registra todo
  número confirmado en SATJE tenga sentencia o no.
"""
import hashlib
import os
import time
from datetime import datetime, timezone

import psycopg2.extras

from api import (buscar_causa, id_juicio_de, obtener_incidentes, obtener_actuaciones,
                 normalizar_numero)
from config.config import (TRIBUNALES, MAX_VACIAS_RESOLUCION, ID_CNJ,
                           RESULTADO_EN_TRAMITE, RESULTADO_SIN_ACTUACIONES)
from resolucion.satje.tribunal import buscar_actuacion_resolucion
from resolucion.satje.corte import (encontrar_incidente_cnj,
                                    obtener_actuaciones_cnj,
                                    detectar_sentencia_cnj,
                                    DESCRIPCIONES_SUBSTANTIVAS)
from resolucion.satje import apelacion as satje_apel
from worker import job_manager as jm
from worker import logger as log

# Cortesía con SATJE, que es una API pública del Estado. Con un solo job
# 0,4 s está bien; corriendo 4 en paralelo serían ~10 consultas por segundo
# sostenidas durante horas, así que al particionar se sube a 1 s y el ritmo
# agregado queda en ~4/s — todavía 4 veces más rápido en total.
PAUSA_SEG = float(os.environ.get("REVISION_PAUSA_SEG", "0.4"))
PAUSA_SEG_PARTICION = float(os.environ.get("REVISION_PAUSA_PARTICION", "1.0"))
from config import materia as MATERIA  # filtro de materia (ids SATJE o judicatura)
# Techo de seguridad del tanteo. No es el corte real —ese lo da MAX_VACIAS
# seguidos— sino un límite para que un SATJE que responda cualquier cosa no
# deje el job corriendo indefinidamente. El número más alto visto en el
# histórico es 800.
TANTEO_MAX = int(os.environ.get("REVISION_TANTEO_MAX", "1500"))
# Hasta cuántos años atrás se siguen consultando a diario las causas abiertas.
# En tributario el registro vivo son ~2.600 causas y se miran todas; en civil o
# laboral son decenas de miles, y las de hace más de 3 años casi nunca se
# mueven. Lo más viejo se revisa cuando se lance una corrida sin este límite
# (REVISION_ANIOS_VIGILADOS=0).
ANIOS_VIGILADOS = int(os.environ.get("REVISION_ANIOS_VIGILADOS", "3"))
# Tope de causas ya conocidas que cada job vuelve a mirar por corrida (0 = sin
# tope). En civil/laboral las abiertas son decenas de miles: con tope se
# revisan por turnos, primero las que hace más tiempo no se miran. 2.000 por
# job a ~9 números/min son ~4 h: la corrida cabe en la noche y no se solapa
# con la del día siguiente.
MAX_SEGUIMIENTO = int(os.environ.get("REVISION_MAX_SEGUIMIENTO", "2000"))


def _now():
    return datetime.now(timezone.utc)


def repartir_tribunales(n_particiones: int) -> list[list[str]]:
    """Agrupa los tribunales en n grupos de carga pareja.

    Se parte POR TRIBUNAL y no por rangos de número porque el corte de 30
    vacíos ya se lleva por tribunal: así cada job queda autocontenido, sin
    coordinación ni riesgo de que dos toquen el mismo número.

    El reparto usa la carga real (cuántos hay que vigilar de cada uno), no
    el orden alfabético: Pichincha y Guayas juntos son el 70% del trabajo y
    ponerlos en el mismo grupo dejaría tres slots casi ociosos.
    """
    with jm._conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT tribunal, count(*) FROM worker_revision_estado
                WHERE NOT casacion_cerrada AND NOT cerrada_sin_sentencia
                  AND NOT no_tributaria AND tribunal IS NOT NULL
                GROUP BY tribunal
            """)
            carga = dict(cur.fetchall())
    # Los que nunca se revisaron no están en el registro; igual hay que
    # repartirlos para que no se queden fuera.
    for cod in TRIBUNALES:
        carga.setdefault(cod, 0)

    grupos: list[list[str]] = [[] for _ in range(max(1, n_particiones))]
    pesos = [0] * len(grupos)
    # Greedy de mayor a menor: con pocos elementos da un reparto muy parejo.
    for cod, n in sorted(carga.items(), key=lambda kv: -kv[1]):
        i = pesos.index(min(pesos))
        grupos[i].append(cod)
        pesos[i] += n
    return [g for g in grupos if g]


def asegurar_tabla():
    """worker_revision_estado: una fila por número confirmado en SATJE.

    Es a la vez el registro de lo que existe (incluido lo que está en trámite
    y todavía no es causa), la fuente de la frontera por tribunal/año, y la
    base de los conteos de monitoreo.
    """
    with jm._conn() as conn:
        with conn.cursor() as cur:
            # Los N jobs de la revision particionada arrancan a la vez y los
            # cuatro entran aca. CREATE TABLE IF NOT EXISTS en paralelo no es
            # seguro: comprobado contra Postgres 18, con la tabla ausente
            # 3 de 4 hilos revientan con UniqueViolation sobre
            # pg_type_typname_nsp_index. Y como el hilo muere sin tocar la
            # base, el job queda en 'corriendo' con 0 procesadas y sin log.
            # El lock es de transaccion: se suelta solo al cerrar el bloque.
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (815_247_001,))
            cur.execute("""
                CREATE TABLE IF NOT EXISTS worker_revision_estado (
                    numero          VARCHAR(50) PRIMARY KEY,
                    tribunal        VARCHAR(10),
                    anio            VARCHAR(4),
                    huella          VARCHAR(64),
                    n_actuaciones   INTEGER,
                    ultima_fecha    VARCHAR(40),
                    -- Los tres hechos que se observan en SATJE cada día. Se
                    -- guardan en vez de derivarlos de estado_del_juicio_nombre
                    -- porque ese campo se calculó al extraer y quedó desfasado:
                    -- 470 causas "Archivado" (que por definición no subieron a
                    -- corte) tienen casación vinculada.
                    con_sentencia   BOOLEAN NOT NULL DEFAULT false,
                    en_corte        BOOLEAN NOT NULL DEFAULT false,
                    casacion_resuelta BOOLEAN NOT NULL DEFAULT false,
                    -- Terminó sin pronunciamiento de fondo (abandono,
                    -- desistimiento, caducidad...). No se encola ni se
                    -- vuelve a consultar.
                    cerrada_sin_sentencia BOOLEAN NOT NULL DEFAULT false,
                    resultado_tribunal VARCHAR(200),
                    -- El recorrido en Corte terminó (fallo, inadmisión,
                    -- desistimiento...). La admisión del recurso NO cierra.
                    casacion_cerrada BOOLEAN NOT NULL DEFAULT false,
                    resultado_corte VARCHAR(200),
                    -- Existe en SATJE pero es de otra materia (el 48% de lo
                    -- barrido es contencioso administrativo). Se registra
                    -- porque ocupa un lugar en la secuencia y la frontera
                    -- debe contarlo, pero queda fuera de todos los conteos
                    -- y no se vuelve a consultar.
                    no_tributaria BOOLEAN NOT NULL DEFAULT false,
                    revisado_en     TIMESTAMPTZ DEFAULT NOW(),
                    job_id          INTEGER,
                    cambios         INTEGER NOT NULL DEFAULT 0,
                    encolada_en     TIMESTAMPTZ
                )
            """)
            for col in [
                "ALTER TABLE worker_revision_estado ADD COLUMN IF NOT EXISTS tribunal VARCHAR(10)",
                "ALTER TABLE worker_revision_estado ADD COLUMN IF NOT EXISTS anio VARCHAR(4)",
                "ALTER TABLE worker_revision_estado ADD COLUMN IF NOT EXISTS con_sentencia BOOLEAN NOT NULL DEFAULT false",
                "ALTER TABLE worker_revision_estado ADD COLUMN IF NOT EXISTS en_corte BOOLEAN NOT NULL DEFAULT false",
                "ALTER TABLE worker_revision_estado ADD COLUMN IF NOT EXISTS casacion_resuelta BOOLEAN NOT NULL DEFAULT false",
                "ALTER TABLE worker_revision_estado ADD COLUMN IF NOT EXISTS job_id INTEGER",
                "ALTER TABLE worker_revision_estado ADD COLUMN IF NOT EXISTS encolada_en TIMESTAMPTZ",
                "ALTER TABLE worker_revision_estado ADD COLUMN IF NOT EXISTS cerrada_sin_sentencia BOOLEAN NOT NULL DEFAULT false",
                "ALTER TABLE worker_revision_estado ADD COLUMN IF NOT EXISTS resultado_tribunal VARCHAR(200)",
                "ALTER TABLE worker_revision_estado ADD COLUMN IF NOT EXISTS casacion_cerrada BOOLEAN NOT NULL DEFAULT false",
                "ALTER TABLE worker_revision_estado ADD COLUMN IF NOT EXISTS resultado_corte VARCHAR(200)",
                "ALTER TABLE worker_revision_estado ADD COLUMN IF NOT EXISTS no_tributaria BOOLEAN NOT NULL DEFAULT false",
                "ALTER TABLE worker_revision_estado ADD COLUMN IF NOT EXISTS en_apelacion BOOLEAN NOT NULL DEFAULT false",
                "ALTER TABLE worker_revision_estado ADD COLUMN IF NOT EXISTS apelacion_resuelta BOOLEAN NOT NULL DEFAULT false",
                "CREATE INDEX IF NOT EXISTS idx_rev_trib_anio ON worker_revision_estado (tribunal, anio)",
                "CREATE INDEX IF NOT EXISTS idx_rev_job ON worker_revision_estado (job_id)",
            ]:
                cur.execute(col)
            # Primera revisión de una materia: sembrar el registro con lo que
            # ya barrió la descarga, para no redescubrirlo todo en SATJE.
            # Va bajo el mismo lock: los N jobs particionados entran a la vez.
            cur.execute("SELECT EXISTS (SELECT 1 FROM worker_revision_estado)")
            if not cur.fetchone()[0]:
                _sembrar(cur)
        conn.commit()


def _huella(actuaciones: list, acts_cnj: list) -> tuple:
    """(hash, n_total, fecha_de_la_última). Cubre tribunal y CNJ: una casación
    nueva sobre una causa ya sentenciada también es una novedad."""
    def resumir(acts):
        return [f"{a.get('fecha','')}|{(a.get('tipo') or '').strip()}" for a in acts]
    partes = resumir(actuaciones) + ["CNJ"] + resumir(acts_cnj)
    h = hashlib.sha256("\n".join(partes).encode()).hexdigest()
    total = len(actuaciones) + len(acts_cnj)
    ultima = max([p.split("|")[0] for p in partes if p and p != "CNJ"] or [""])
    return h, total, ultima


def revisar_una(numero: str, tribunal: str) -> dict | None:
    """Consulta SATJE. None si el número no existe; dict con no_tributaria=True
    (léase "de otra materia", el nombre de columna es histórico) si existe pero
    no es de la materia — no es
    un error, es el caso normal al tantear más allá de la frontera."""
    causa = buscar_causa(numero)
    if not causa:
        return None                      # el número no existe en SATJE
    if MATERIA.IDS_MATERIA_SATJE and not MATERIA.es_de_la_materia(causa):
        # EXISTE pero es de otra materia. No es lo mismo que no existir: el
        # 48% de los números barridos son de otra materia, así que contarlos
        # como vacíos haría que una racha de 30 corte el barrido muchísimo
        # antes de la frontera real.
        return {"no_tributaria": True, "idMateria": causa.get("idMateria")}
    id_juicio = id_juicio_de(causa, numero)
    incidentes = obtener_incidentes(id_juicio)
    if incidentes and not MATERIA.IDS_MATERIA_SATJE and not MATERIA.es_de_la_materia(causa, incidentes):
        return {"no_tributaria": True, "idMateria": causa.get("idMateria")}
    if not incidentes:
        return None

    # El incidente de CNJ se pide aparte para no contar sus actuaciones dos
    # veces en la huella.
    actuaciones = []
    for inc in incidentes:
        if inc.get("idJudicatura") == ID_CNJ:
            continue
        actuaciones += obtener_actuaciones(id_juicio, inc) or []

    resultado_trib, _act, _ = buscar_actuacion_resolucion(id_juicio, incidentes, tribunal)
    # El catálogo COGEP tiene ~24 resoluciones terminales y SOLO UNA es
    # "Con sentencia.". Las demás —abandono, desistimiento, caducidad,
    # prescripción, inadmisión, retiro, transacción, mediación— terminan el
    # proceso SIN pronunciamiento de fondo. Tratarlas como sentencia era un
    # error: una causa abandonada en tribunal no tiene sentencia que extraer,
    # y su casación no debe subirse. Mismo criterio que usa runner.py para
    # decidir si guarda la causa ("Con sentencia" in resultado_tribunal).
    en_tramite = (not resultado_trib) or resultado_trib in (
        RESULTADO_EN_TRAMITE, RESULTADO_SIN_ACTUACIONES)
    con_sentencia = (not en_tramite) and "Con sentencia" in resultado_trib
    # Terminó, pero sin sentencia de fondo. La causa está CERRADA: no hay nada
    # que extraer y no hay que seguir vigilándola, ni en tribunal ni en corte.
    cerrada_sin_sentencia = (not en_tramite) and not con_sentencia

    # Apelación (Corte Provincial): solo en materias de tres niveles; en
    # tributario encontrar_incidente_apelacion devuelve siempre None.
    inc_apel = satje_apel.encontrar_incidente_apelacion(incidentes)
    desc_apel = ""
    if inc_apel:
        desc_apel, _ = satje_apel.detectar_sentencia_apelacion(
            satje_apel.obtener_actuaciones_apelacion(id_juicio, inc_apel) or [])

    inc_cnj = encontrar_incidente_cnj(incidentes)
    acts_cnj = obtener_actuaciones_cnj(id_juicio, inc_cnj) if inc_cnj else []
    desc_cnj, _ = detectar_sentencia_cnj(acts_cnj) if acts_cnj else ("", None)

    # detectar_sentencia_cnj devuelve CUALQUIERA de nueve descripciones, y solo
    # tres son un fallo de fondo. Confundirlas infla el trabajo de forma
    # brutal: en una muestra de 8 "casaciones nuevas", 7 eran apenas admisión
    # o inadmisión del recurso. Son dos preguntas distintas:
    #   sustantiva → hay una sentencia de casación que extraer (se encola)
    #   cerrada    → el recorrido en Corte terminó (se deja de consultar)
    # "Art. 270, admisión." NO cierra nada: el recurso recién se aceptó a
    # trámite y el fallo todavía está por venir.
    casacion_sustantiva = desc_cnj in DESCRIPCIONES_SUBSTANTIVAS
    casacion_cerrada = bool(desc_cnj) and desc_cnj != "Art. 270, admisión."

    h, total, ultima = _huella(actuaciones, acts_cnj)
    return {
        "huella": h, "n": total, "ultima": ultima,
        "con_sentencia": con_sentencia,
        "cerrada_sin_sentencia": cerrada_sin_sentencia,
        "resultado_tribunal": resultado_trib or "",
        "en_apelacion": bool(inc_apel),
        "apelacion_resuelta": desc_apel in satje_apel.DESCRIPCIONES_SUBSTANTIVAS,
        "en_corte": bool(inc_cnj),
        "casacion_resuelta": casacion_sustantiva,
        "casacion_cerrada": casacion_cerrada,
        "resultado_corte": desc_cnj or "",
    }


# ── Qué sabe NUESTRA base ────────────────────────────────────────────────────

# Lo que tenemos se lee de worker_causa_datos (las URL de los PDF guardados),
# que existe en todas las materias; la vista mv_causas_listado solo existe en
# tributario.
_SQL_TENEMOS = """
    SELECT numero,
           coalesce(url_pdf_tribunal, '') <> '',
           coalesce(url_pdf_apel, '') <> '',
           coalesce(url_pdf_cnj, '') <> ''
    FROM worker_causa_datos
    WHERE coalesce(url_pdf_tribunal, '') <> ''
"""


def _lo_que_tenemos() -> dict:
    """{numero: (tiene_causa, tiene_apelacion, tiene_casacion)} desde nuestra base.

    Es el otro lado de la comparación: sin esto no se puede responder "¿me
    falta algo?" y hay que caer en "¿cambió desde ayer?", que en la primera
    corrida da que falta TODO.
    """
    with jm._conn() as conn:
        with conn.cursor() as cur:
            cur.execute(_SQL_TENEMOS)
            return {n: (t, a, c) for n, t, a, c in cur.fetchall()}


def _tenemos_ahora(numero: str) -> tuple:
    """(tiene_causa, tiene_apelacion, tiene_casacion) leído EN ESTE INSTANTE para un número.

    El inventario general se lee una vez al arrancar, pero un job dura horas
    (medido: 302 min el de Pichincha) y en ese rato la base se mueve — una
    extracción manual, o el job de lista de otra partición. Si el inventario
    quedó viejo, una causa guardada mientras tanto parece faltante y se
    re-encola con do_tribunal=True, que sobrescribe la ficha buena.

    Solo se llama antes de encolar, que es raro, así que el costo es mínimo:
    el inventario en memoria sigue filtrando el 99% de los casos.
    """
    with jm._conn() as conn:
        with conn.cursor() as cur:
            cur.execute(_SQL_TENEMOS + " AND numero = %s", (numero,))
            fila = cur.fetchone()
    return (True, bool(fila[2]), bool(fila[3])) if fila else (False, False, False)


def _registro() -> dict:
    with jm._conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT numero, huella FROM worker_revision_estado")
            return dict(cur.fetchall())


def _decidir(numero: str, info: dict, tenemos: dict) -> tuple:
    """(do_tribunal, do_apelacion, do_corte, motivo). Todo False = no encolar.

    Se compara SATJE contra nuestra base, no contra la revisión de ayer.
    """
    tiene_causa, tiene_apel, tiene_casacion = tenemos.get(numero, (False, False, False))

    # REGLA DURA: sin sentencia de tribunal no se sube NADA, ni la casación.
    # Una causa que terminó por abandono, desistimiento, caducidad o similar
    # no tiene pronunciamiento de fondo que extraer; subirle una casación
    # dejaría una casación colgando de una causa que nunca se resolvió.
    if info.get("cerrada_sin_sentencia"):
        return False, False, False, f"cerrada sin sentencia ({info.get('resultado_tribunal','')[:40]})"

    if not info["con_sentencia"]:
        # Todavía en trámite. No hay sentencia que extraer; se registra para
        # seguir vigilándola y para que la frontera avance.
        return False, False, False, "en trámite"

    falta_tribunal = not tiene_causa
    falta_apel     = info.get("apelacion_resuelta", False) and not tiene_apel
    falta_casacion = info["casacion_resuelta"] and not tiene_casacion

    if falta_tribunal or falta_apel or falta_casacion:
        motivo = " + ".join(
            m for m, v in (("sentencia nueva", falta_tribunal),
                           ("apelación nueva", falta_apel),
                           ("casación nueva", falta_casacion)) if v)
        # do_tribunal SOLO si falta la causa. Antes iba siempre en True con la
        # idea de que el lista_worker "no sobrescribe si ya existe" — falso:
        # strapi.guardar_causa() hace upsert y si la causa existe manda un PUT
        # con la ficha entera. O sea que cuando solo faltaba la casación se
        # re-extraía la sentencia con IA y se pisaba la causa buena con una
        # extracción nueva.
        #
        # El lista_worker ya soporta el camino correcto: con do_tribunal en
        # False busca la causa existente por número y le cuelga la casación sin
        # tocar la sentencia (ver su docstring: "solo corte, causa YA existe").
        return falta_tribunal, falta_apel, falta_casacion or info["en_corte"], motivo

    return False, False, False, "ya la tenemos"


# ── De dónde salen los números a revisar ─────────────────────────────────────

def frontera_por_tribunal(anio: str) -> dict:
    """{tribunal: último número CONFIRMADO en SATJE} según el registro.

    Ojo: sale de worker_revision_estado, no de las causas. Un número existe en
    SATJE mucho antes de tener sentencia; si la frontera saliera de `causas`,
    en un año sin sentencias todavía (2026) arrancaría siempre en el 1.
    """
    with jm._conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT tribunal, MAX(split_part(numero,'-',3)::int)
                FROM worker_revision_estado
                WHERE anio = %s AND numero ~ '^[0-9]{5}-[0-9]{4}-[0-9]+$'
                GROUP BY tribunal
            """, (anio,))
            return dict(cur.fetchall())


def en_observacion() -> list:
    """Números que hay que volver a mirar todos los días.

    Son los del registro que todavía pueden moverse: en trámite, o con
    sentencia pero sin casación resuelta. Los cerrados (casación resuelta) se
    dejan de consultar — no les va a pasar nada nuevo.
    """
    with jm._conn() as conn:
        with conn.cursor() as cur:
            # Se deja de consultar lo que ya no puede moverse: el recorrido
            # en Corte terminó (OJO: cerrada, no "resuelta" — un recurso
            # apenas admitido sigue vivo) o cerrada sin sentencia
            # (abandono y similares: no va a haber sentencia ni casación
            # que valga). Cada una que sale de acá es una consulta menos
            # a SATJE todos los días.
            #
            # PERO una causa cerrada en SATJE solo se deja de mirar si ADEMÁS
            # ya la tenemos completa. Sin esa condición, un fallo transitorio
            # se vuelve permanente: la extracción de 01501-2018-00010 falló al
            # bajar el PDF de la CNJ, la causa quedó sin casación, y como
            # SATJE la daba por cerrada nunca más entraba a la revisión.
            # Eran 8 causas en ese estado.
            desde_anio = str(datetime.now().year - ANIOS_VIGILADOS) if ANIOS_VIGILADOS else "0000"
            cur.execute("""
                SELECT r.numero, r.tribunal
                FROM worker_revision_estado r
                LEFT JOIN worker_causa_datos d
                       ON d.numero = r.numero AND coalesce(d.url_pdf_tribunal, '') <> ''
                WHERE NOT r.no_tributaria
                  AND r.anio >= %s
                  AND (
                        (NOT r.casacion_cerrada AND NOT r.cerrada_sin_sentencia)
                     -- Cerrada en SATJE pero incompleta de nuestro lado:
                     -- falta la causa entera, su apelación o su casación.
                     OR (r.con_sentencia AND d.numero IS NULL)
                     OR (r.apelacion_resuelta AND coalesce(d.url_pdf_apel, '') = '')
                     OR (r.casacion_resuelta AND coalesce(d.url_pdf_cnj, '') = '')
                  )
                ORDER BY r.revisado_en NULLS FIRST, r.numero
            """, (desde_anio,))
            return cur.fetchall()


def causas_vivas_sin_registro() -> list:
    """Causas de nuestra base que todavía no están en el registro.

    Se mira CUALQUIERA que falte, sin filtrar por estado. Antes se filtraba
    por estado_del_juicio_nombre ('En curso' o 'En Corte sin casación'), que
    es justo el campo que quedó desfasado: tiene 470 causas "Archivado" con
    casación vinculada. Con ese filtro, de las 97 causas fuera del registro
    solo entraban 10 — las 31 archivadas y 56 en Corte quedaban invisibles
    para siempre, porque tampoco las alcanzaba en_observacion().

    Es una sola consulta por causa y después ya queda en el registro, así que
    el costo de no filtrar es despreciable y elimina la dependencia de un
    campo que no es confiable.
    """
    with jm._conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT d.numero, split_part(d.numero,'-',1)
                FROM worker_causa_datos d
                LEFT JOIN worker_revision_estado r ON r.numero = d.numero
                WHERE coalesce(d.url_pdf_tribunal, '') <> '' AND r.numero IS NULL
                ORDER BY d.numero
            """)
            return cur.fetchall()


def _guardar(numero, tribunal, anio, info, job_id, cambio, encolada):
    # El año sale del NÚMERO, no del parámetro del job. El seguimiento no
    # filtra por año —una causa de 2018 puede recibir casación hoy— así que
    # usar el año del job etiquetaba esas causas con el año que se estaba
    # explorando. Además de ensuciar los conteos, corrompe la frontera: si una
    # causa vieja de número alto entra como 2026, el tanteo arrancaría después
    # de ella y se saltaría números reales de ese año.
    partes = numero.split("-")
    anio_real = partes[1] if len(partes) > 2 and len(partes[1]) == 4 else anio
    with jm._conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO worker_revision_estado
                    (numero, tribunal, anio, huella, n_actuaciones, ultima_fecha,
                     con_sentencia, en_corte, casacion_resuelta,
                     cerrada_sin_sentencia, resultado_tribunal,
                     casacion_cerrada, resultado_corte,
                     en_apelacion, apelacion_resuelta,
                     revisado_en, job_id, cambios, encolada_en)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW(),%s,%s,%s)
                ON CONFLICT (numero) DO UPDATE SET
                    huella            = EXCLUDED.huella,
                    n_actuaciones     = EXCLUDED.n_actuaciones,
                    ultima_fecha      = EXCLUDED.ultima_fecha,
                    con_sentencia     = EXCLUDED.con_sentencia,
                    en_corte          = EXCLUDED.en_corte,
                    casacion_resuelta = EXCLUDED.casacion_resuelta,
                    cerrada_sin_sentencia = EXCLUDED.cerrada_sin_sentencia,
                    resultado_tribunal    = EXCLUDED.resultado_tribunal,
                    casacion_cerrada      = EXCLUDED.casacion_cerrada,
                    resultado_corte       = EXCLUDED.resultado_corte,
                    en_apelacion          = EXCLUDED.en_apelacion,
                    apelacion_resuelta    = EXCLUDED.apelacion_resuelta,
                    revisado_en       = NOW(),
                    job_id            = EXCLUDED.job_id,
                    cambios           = worker_revision_estado.cambios + %s,
                    encolada_en       = COALESCE(EXCLUDED.encolada_en,
                                                 worker_revision_estado.encolada_en)
            """, (numero, tribunal, anio_real, info["huella"], info["n"], info["ultima"],
                  info["con_sentencia"], info["en_corte"], info["casacion_resuelta"],
                  info.get("cerrada_sin_sentencia", False),
                  (info.get("resultado_tribunal") or "")[:200],
                  info.get("casacion_cerrada", False),
                  (info.get("resultado_corte") or "")[:200],
                  info.get("en_apelacion", False), info.get("apelacion_resuelta", False),
                  job_id, 1 if cambio else 0, _now() if encolada else None,
                  1 if cambio else 0))
        conn.commit()


def _encolar(encolar: list, job_id: int, descarga: bool = False):
    """Encola lo encontrado: en modo descarga, jobs solo_descarga por
    (tribunal, año) que bajan PDF y metadatos de los tres niveles sin IA
    (fase "corpus primero"); si no, el job de lista que extrae con IA.
    Devuelve un dict con los ids, o None si no había nada."""
    if not encolar:
        return None
    if descarga:
        ids = jm.crear_jobs_descarga_numeros([n for n, *_ in encolar], f"revision:job-{job_id}")
        log.info(f"Encoladas {len(encolar)} causa(s) → {len(ids)} job(s) de descarga {ids}",
                 job_id=job_id)
        return {"id": ids}
    # shortcut: el motor de lista no procesa la apelación por separado; si falta
    # solo la apelación se re-extrae la causa entera (que incluye el nivel).
    # Mejorar cuando el lista_worker tenga flag de apelación.
    items = [(n, t or a, c) for n, t, a, c in encolar]
    job = jm.crear_job_lista(items, modelo="qwen",
                             iniciado_por=f"revision:job-{job_id}")
    log.info(f"Encoladas {len(encolar)} causa(s) → job de lista #{job['id']}",
             job_id=job_id)
    return job


def ejecutar_job_revision(job_id: int, pause_ev, kill_ev,
                          anio: str = "", modo: str = "diaria",
                          tribunales: list | None = None, descarga: bool = False):
    """Revisa SATJE y encola solo lo que nos falta.

    modo="diaria"   — vigila lo que puede moverse y avanza la frontera 30 más.
    modo="completa" — además barre desde el 1 hasta MAX_VACIAS seguidos sin
                      encontrar nada. Es el barrido de línea base de un año
                      que nunca se revisó (2026 hoy: ~500 por tribunal).

    descarga   — encola jobs solo_descarga (PDF + metadatos, sin IA) en vez
                 del job de lista con IA.
    tribunales — subconjunto a revisar. Sirve para partir el trabajo en
                 varios jobs que corran en paralelo en los 4 slots: medido en
                 Render son ~9 números/minuto, así que los 5.535 en un solo
                 job son ~10 h. Repartidos bajan a ~4 h, que es lo que tarda
                 el grupo más cargado.
    """
    asegurar_tabla()
    inicio = _now()
    anio = anio or str(datetime.now().year)
    codigos = [c for c in (tribunales or TRIBUNALES) if c in TRIBUNALES]
    if not codigos:
        codigos = list(TRIBUNALES)
    # Si el trabajo va repartido, bajar el ritmo de cada job para no
    # multiplicar por 4 la carga sobre SATJE.
    pausa = PAUSA_SEG_PARTICION if len(codigos) < len(TRIBUNALES) else PAUSA_SEG

    tenemos  = _lo_que_tenemos()
    conocido = _registro()

    # ── Armado de la lista de números ───────────────────────────────────────
    # (numero, tribunal, fase). La fase importa: el corte por vacíos solo
    # aplica a la exploración de frontera. Si se aplicara al seguimiento, un
    # tribunal que alcanza su frontera dejaría de revisar TODAS sus causas
    # vivas en esa corrida.
    candidatos: list[tuple[str, str, str]] = []
    vistos = set()

    def agregar(num, trib, fase="seguimiento"):
        if num not in vistos:
            vistos.add(num)
            candidatos.append((num, trib, fase))

    if modo == "completa":
        # Barrido de línea base: desde el 1. El corte por MAX_VACIAS se evalúa
        # durante el recorrido, no acá, porque depende de lo que responda SATJE.
        for cod in codigos:
            for n in range(1, TANTEO_MAX):
                agregar(f"{cod}-{anio}-{n:05d}", cod, "frontera")
    else:
        # PRIMERO la frontera, no al final. Descubrir causas nuevas es lo más
        # valioso de la corrida y lo único que no se puede posponer: el
        # seguimiento de las viejas aguanta un día más, una demanda recién
        # presentada no aparece hasta que alguien la busca. Además, si el job
        # se cancela o falla a mitad, lo que se pierde es seguimiento y no
        # descubrimiento. (Iba al final y por eso una corrida de 10 h no
        # llegaba a 2026 hasta la última media hora.)
        # Tanteo ABIERTO desde la frontera: sigue mientras SATJE devuelva
        # registros y corta recién con MAX_VACIAS seguidos, igual que el
        # extractor. Un tope fijo de 30 por noche no sirve para ponerse al
        # día: 2026 tiene ~500 por tribunal y tardaría ~17 noches.
        frontera = frontera_por_tribunal(anio)
        for cod in codigos:
            desde = frontera.get(cod, 0) + 1
            for n in range(desde, desde + TANTEO_MAX):
                agregar(f"{cod}-{anio}-{n:05d}", cod, "frontera")
        seguimiento = [(n, t) for n, t in causas_vivas_sin_registro() + en_observacion()
                       if t in codigos]
        if MAX_SEGUIMIENTO:
            seguimiento = seguimiento[:MAX_SEGUIMIENTO]
        for num, trib in seguimiento:
            agregar(num, trib)

    nombres = ", ".join(TRIBUNALES[c].split()[-1] for c in codigos)
    log.info(f"Revisión {modo}{' (solo descarga)' if descarga else ''} año {anio} — {nombres} — "
             f"{len(candidatos)} número(s) a consultar · pausa {pausa}s · registro previo: {len(conocido)}",
             job_id=job_id)

    encolar, revisadas = [], 0
    sin_cambio = inexistentes = errores = no_tributarias = 0
    vacias_seguidas = {cod: 0 for cod in codigos}
    cortados = set()

    for numero, tribunal, fase in candidatos:
        # Al cortar, encolar lo que ya se encontró. Antes la lista se creaba
        # SOLO al terminar el bucle, así que un job interrumpido dejaba sus
        # novedades marcadas como encoladas sin haberlas encolado nunca —
        # pasó en la corrida #114: 11 casaciones detectadas, cero procesadas.
        if kill_ev.is_set():
            _encolar(encolar, job_id, descarga)
            jm._actualizar(job_id, estado="cancelado", completado_en=_now())
            log.info(f"Cancelado — {revisadas} revisadas, {len(encolar)} encoladas",
                     job_id=job_id)
            return
        if pause_ev.is_set():
            _encolar(encolar, job_id, descarga)
            jm._actualizar(job_id, estado="pausado")
            log.info(f"Pausado — {revisadas} revisadas, {len(encolar)} encoladas",
                     job_id=job_id)
            return
        # El corte solo frena la exploración. El seguimiento de causas ya
        # conocidas se revisa igual: no tiene nada que ver con la frontera.
        if fase == "frontera" and tribunal in cortados:
            continue

        try:
            info = revisar_una(numero, tribunal)
        except Exception as e:
            errores += 1
            log.error(f"{type(e).__name__}: {e}", job_id=job_id, causa=numero)
            time.sleep(pausa)
            continue

        revisadas += 1
        if info is not None and info.get("no_tributaria"):
            # Existe, no nos sirve, pero NO es un vacío: resetea el contador.
            no_tributarias += 1
            if fase == "frontera":
                vacias_seguidas[tribunal] = 0
            time.sleep(pausa)
            continue
        if info is None:
            inexistentes += 1
            if fase == "frontera":
                vacias_seguidas[tribunal] = vacias_seguidas.get(tribunal, 0) + 1
                if vacias_seguidas[tribunal] >= MAX_VACIAS_RESOLUCION:
                    cortados.add(tribunal)
                    log.info(f"{tribunal}: {MAX_VACIAS_RESOLUCION} vacíos seguidos, "
                             f"frontera alcanzada", job_id=job_id)
            time.sleep(pausa)
            continue

        if fase == "frontera":
            vacias_seguidas[tribunal] = 0
        # Distinguir "nunca la habíamos mirado" de "cambió desde ayer". Las
        # filas sembradas desde worker_caso_log vienen SIN huella (ese log no
        # guarda el detalle de actuaciones), así que en el primer contacto la
        # comparación da distinto para las 20.627 y el log decía "se movió"
        # sobre todas — inútil y alarmante. No afecta qué se encola: esa
        # decisión compara contra la base, no contra la huella.
        primera_vez = conocido.get(numero) is None
        cambio = (not primera_vez) and conocido.get(numero) != info["huella"]
        do_trib, do_apel, do_corte, motivo = _decidir(numero, info, tenemos)
        if do_trib or do_apel or do_corte:
            # Releer de la base antes de encolar: el inventario puede haber
            # quedado viejo durante las horas que dura el job.
            fresco = {numero: _tenemos_ahora(numero)}
            do_trib, do_apel, do_corte, motivo = _decidir(numero, info, fresco)
        hay_que_encolar = do_trib or do_apel or do_corte

        if hay_que_encolar:
            encolar.append((numero, do_trib, do_apel, do_corte))
            log.info(f"{motivo} → se encola", job_id=job_id, causa=numero)
        elif primera_vez:
            sin_cambio += 1
            log.info(f"primera revisión — {motivo}", job_id=job_id, causa=numero)
        elif cambio:
            log.info(f"se movió pero {motivo}", job_id=job_id, causa=numero)
        else:
            sin_cambio += 1

        _guardar(numero, tribunal, anio, info, job_id, cambio, hay_que_encolar)
        jm._actualizar(job_id, total_procesadas=revisadas, exitosas=len(encolar),
                       errores=errores, omitidas=inexistentes)
        if revisadas % 25 == 0:
            jm._emitir_sse(job_id, {"tipo": "progreso", "revisadas": revisadas,
                                    "total": len(candidatos),
                                    "por_encolar": len(encolar)})
        time.sleep(pausa)

    job_lista = _encolar(encolar, job_id, descarga)
    if not job_lista:
        log.info("Nada que encolar: SATJE no tiene nada que no tengamos", job_id=job_id)

    jm._actualizar(job_id, estado="completado", completado_en=_now(),
                   total_procesadas=revisadas, exitosas=len(encolar),
                   errores=errores, omitidas=inexistentes)
    seg = (_now() - inicio).total_seconds()
    log.info(f"Revisión lista en {seg/60:.1f} min: {revisadas} consultadas · "
             f"{len(encolar)} encoladas · {sin_cambio} sin novedad · "
             f"{inexistentes} inexistentes · {no_tributarias} de otra materia · "
             f"{errores} con error"
             + (f" · lista #{job_lista['id']}" if job_lista else ""),
             job_id=job_id)


# ── Siembra desde el histórico ───────────────────────────────────────────────

def sembrar_desde_caso_log() -> dict:
    """Precarga worker_revision_estado con lo que ya barrió la extracción masiva.

    worker_caso_log guarda UNA FILA POR NÚMERO TOCADO, no solo por causa
    guardada — incluidos los que quedaron en trámite y nunca llegaron a ser
    causa. Son 22.237 números de 2019–2025 con su resultado COGEP y su
    resultado de Corte, que es exactamente lo que necesita el registro.

    Sin esto la primera revisión tendría que redescubrir todo desde SATJE: ~12
    horas de consultas para averiguar algo que ya está en la base.

    Es idempotente y NO pisa lo que haya escrito una revisión real: esas filas
    tienen datos frescos de SATJE y estas vienen de un barrido de septiembre.
    """
    with jm._conn() as conn:
        with conn.cursor() as cur:
            insertados = _sembrar(cur)
        conn.commit()

    with jm._conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM worker_revision_estado")
            total = cur.fetchone()[0]
    return {"insertados": insertados, "total": total}


def _sembrar(cur) -> int:
    """El INSERT de sembrar_desde_caso_log, sobre un cursor ya abierto."""
    SUSTANTIVAS = ("Casa la sentencia.", "No casa la sentencia.",
                   "Casa parcialmente la sentencia.")
    # Estados de Corte que NO cierran: el recurso sigue su curso.
    ABIERTAS_CNJ = ("Art. 270, admisión.", "Casación presentada.")
    cur.execute(("""
                INSERT INTO worker_revision_estado
                    (numero, tribunal, anio, con_sentencia, cerrada_sin_sentencia,
                     resultado_tribunal, en_corte, casacion_resuelta,
                     casacion_cerrada, resultado_corte, no_tributaria, revisado_en)
                SELECT DISTINCT ON (l.numero)
                    l.numero,
                    split_part(l.numero,'-',1),
                    split_part(l.numero,'-',2),
                    l.resultado LIKE 'Con sentencia%%',
                    (l.resultado IS NOT NULL
                     AND l.resultado <> ''
                     AND l.resultado NOT LIKE 'Con sentencia%%'
                     AND l.resultado NOT LIKE 'En tramite%%'
                     AND l.estado_juicio <> '__OTRA_MATERIA__'),
                    left(coalesce(l.resultado,''), 200),
                    l.estado_juicio = '__EN_CNJ__',
                    coalesce(l.resultado_cnj,'') = ANY(%s),
                    (coalesce(l.resultado_cnj,'') <> ''
                     AND NOT (coalesce(l.resultado_cnj,'') = ANY(%s))),
                    left(coalesce(l.resultado_cnj,''), 200),
                    l.estado_juicio = '__OTRA_MATERIA__',
                    l.procesado_en
                FROM worker_caso_log l
                WHERE l.numero ~ '^[0-9]{5}-[0-9]{4}-[0-9]+$'
                  -- 'Sin resultados' = el número no existe en SATJE. No va al
                  -- registro: registrar un hueco movería la frontera a un
                  -- número que no existe.
                  AND l.estado_juicio NOT IN ('Sin resultados', 'Error de procesamiento')
                ORDER BY l.numero, l.procesado_en DESC
                ON CONFLICT (numero) DO NOTHING
            """).replace("__OTRA_MATERIA__", MATERIA.ETIQUETA_OTRA_MATERIA).replace("__EN_CNJ__", MATERIA.ESTADO_EN_CNJ), (list(SUSTANTIVAS), list(ABIERTAS_CNJ)))
    return cur.rowcount
