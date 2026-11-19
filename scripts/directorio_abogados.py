"""
Directorio interno de abogados: nombre, correo, matrícula del Foro y teléfono
de los abogados en libre ejercicio que aparecen en las sentencias del corpus.
Sin IA y sin consultar SATJE: lee solo causa_texto y worker_causas.

Uso interno de Census (no campañas): la tabla queda en la base del worker y se
descarga con GET /abogados, que pide la clave del worker. Nunca se sube al CMS
(la media library es pública).

Se excluyen funcionarios públicos: jueces, secretarios, fiscales, defensores
públicos, procuradores del Estado, peritos y correos de dominios .gob.ec,
.mil.ec y de la Función Judicial.

Correrlo desde el shell del worker en Render:

  python scripts/directorio_abogados.py --limite 300 --muestra   # revisar a ojo
  nohup python -u scripts/directorio_abogados.py > /tmp/abogados.log 2>&1 &

Sin --muestra reemplaza la tabla directorio_abogados de la materia en una sola
transacción (si algo falla, queda la versión anterior).
"""
import argparse
import json
import re
import sys
import time
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

LOTE = 200
PAUSA_SEG = 0.05
VENTANA_CORREO = 250      # caracteres entre un nombre y su correo
VENTANA_DATOS = 160       # caracteres tras el nombre para matrícula y teléfono
MAX_CAUSAS = 30           # números de causa guardados por abogado (trazabilidad)

_MAY = "A-ZÁÉÍÓÚÑÜ"
_PAL = rf"[{_MAY}][{_MAY}a-záéíóúñü'´]+"
_CONECTOR = r"(?:de|del|la|las|los|y|DE|DEL|LA|LAS|LOS|Y)"
# Título de abogado + nombre propio de 2 a 7 palabras. "Dr./Ab." es el formato
# de las razones de notificación de SATJE ("del Dr./Ab. PEREZ LOPEZ JUAN").
_RE_ABOGADO = re.compile(
    r"\b(?i:Ab|Abg|Abog|Abogad[oa]|Dr|Dra|Doctor|Doctora)\.?"
    r"(?:\s*/\s*(?i:Ab|Abg|Dra?)\.?)?\s+"
    rf"(?P<nombre>{_PAL}(?:\s+(?:{_CONECTOR}\s+)?{_PAL}){{1,6}})"
)
_RE_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}")
_RE_MATRICULA = re.compile(
    r"(?:matr[ií]cula|mat\.|m\.\s?f\.|foro(?:\s+de\s+abogados)?|reg(?:istro)?\.?\s+profesional)"
    r"[^0-9\n]{0,30}?(?P<mat>\d{1,2}\s?[-.]\s?\d{4}\s?[-.]\s?\d{1,6}(?:\s?[-.]\s?[A-Za-z.]{1,4})?|\d{3,6})",
    re.IGNORECASE,
)
_RE_TELEFONO = re.compile(
    r"(?:tel[eé]fonos?|celular|cel\.|telf\.?|tlf\.?|m[oó]vil|whatsapp)[^0-9\n]{0,15}?"
    r"(?P<tel>(?:\+?593[\s-]?|0)\d(?:[\s-]?\d){7,8})",
    re.IGNORECASE,
)

# Contexto que delata a un funcionario público junto al nombre.
_FUNCIONARIO = re.compile(
    r"\b(?:juez|jueza|jueces|conjuez|conjueza|magistrad[oa]|ponente|secretari[oa]|"
    r"actuari[oa]|fiscal|defensor[ae]?\s+p[uú]blic[oa]|defensor[ií]a\s+p[uú]blica|"
    r"procurador[a]?\s+general|procuradur[ií]a|abogad[oa]\s+del\s+estado|perit[oa]|"
    r"m[eé]dic[oa]|notari[oa]|registrador[a]?|presidente\s+de\s+la\s+sala)\b",
    re.IGNORECASE,
)
_DOMINIOS_PUBLICOS = (".gob.ec", ".mil.ec", "funcionjudicial", "fiscalia", "defensoria",
                      "pge.gob", "cortenacional", "corteconstitucional")
_CORREOS_GENERICOS = ("admin@", "info@", "no-reply@", "noreply@", "soporte@", "notificaciones@")
# En textos en mayúsculas la expresión no distingue el fin del nombre: se corta
# en la primera palabra que no puede ser parte de un nombre propio.
_CORTE_NOMBRE = {"EN", "EL", "CON", "QUIEN", "QUIENES", "POR", "PARA", "COMO", "SU", "SUS",
                 "CALIDAD", "A", "AL", "QUE", "SE", "ES", "SIN", "SOBRE", "ANTE", "MAT",
                 "MATRICULA", "CASILLERO", "CORREO", "FORO", "CI", "CC", "NO", "ABOGADO",
                 "ABOGADA", "PATROCINADOR", "DEFENSOR", "DEFENSA", "OFICIO", "CUYO"}
# Solo se excluye lo público. En penal SATJE notifica casilleros de entidades
# públicas como si fueran abogados ("del Dr./Ab. FISCALIA GENERAL DEL ESTADO",
# "DEFENSORÍA PÚBLICA AZUAY PENAL"). Bancos, cooperativas, estudios jurídicos,
# consultorios y demás casilleros privados sí se conservan (decisión de John, 13-nov).
_ENTIDAD_PUBLICA = {"FISCALIA", "DEFENSORIA", "PROCURADURIA", "CONTRALORIA", "MINISTERIO",
                    "MUNICIPIO", "MUNICIPAL", "GOBIERNO", "ESTADO", "PUBLICA", "PUBLICO", "POLICIA",
                    "IESS", "SEGURIDAD", "GAD", "PREFECTURA", "SUPERINTENDENCIA", "SECRETARIA",
                    "CONSEJO", "JUDICATURA", "TRIBUNAL", "CORTE", "UNIDAD", "COMANDO", "SERVICIO",
                    "AGENCIA", "DIRECCION", "JUNTA", "CNEL", "CNT", "SENAE", "SRI", "ANT", "VICTIMAS"}
# Misma lista para filtrar en SQL sobre la clave (mayúsculas sin tildes).
INSTITUCION_REGEX = "(^| )(" + "|".join(sorted(_ENTIDAD_PUBLICA)) + ")( |$)"

_PALABRAS_NO_NOMBRE = _ENTIDAD_PUBLICA | {"SENTENCIA", "AUTO", "RESOLUCION", "REPUBLICA", "ECUADOR", "CODIGO",
                       "ARTICULO", "CONSTITUCION", "ACTOR", "DEMANDADO", "PARTE", "ACCIONANTE"}


def _sin_tildes(s: str) -> str:
    return unicodedata.normalize("NFD", s).encode("ascii", "ignore").decode("ascii")


def clave_nombre(nombre: str) -> str:
    """Mismo abogado con el orden cambiado ("JUAN PEREZ" / "PEREZ JUAN") = misma clave."""
    toks = [t for t in re.split(r"\s+", _sin_tildes(nombre).upper())
            if t and t not in {"DE", "DEL", "LA", "LAS", "LOS", "Y"}]
    return " ".join(sorted(toks))


def correo_valido(email: str) -> bool:
    e = email.lower()
    return not (any(d in e.split("@", 1)[1] for d in _DOMINIOS_PUBLICOS)
                or e.startswith(_CORREOS_GENERICOS))


def extraer(texto: str) -> list:
    """Menciones de abogados en libre ejercicio de un texto.
    Devuelve [{nombre, clave, emails, matricula, telefono}] (una por nombre)."""
    if not texto:
        return []
    menciones = []
    for m in _RE_ABOGADO.finditer(texto):
        toks = m.group("nombre").split()
        corte = next((i for i, t in enumerate(toks) if _sin_tildes(t).upper() in _CORTE_NOMBRE),
                     len(toks))
        toks = toks[:corte]
        while toks and _sin_tildes(toks[-1]).upper() in {"DE", "DEL", "LA", "LAS", "LOS", "Y"}:
            toks.pop()
        if len(toks) < 2 or any(_sin_tildes(t).upper() in _PALABRAS_NO_NOMBRE for t in toks):
            continue
        nombre = " ".join(toks)
        fin = m.start("nombre")
        for t in toks:
            fin = texto.index(t, fin) + len(t)
        # Contexto acotado a la misma frase: el cargo de la persona anterior
        # ("…defensor público. La Ab. X") no debe descartar a la siguiente.
        antes = re.split(r"[.;\n]", texto[max(0, m.start() - 60): m.start()])[-1]
        despues = re.split(r"[;\n]", texto[fin: fin + 80])[0]
        if _FUNCIONARIO.search(antes) or _FUNCIONARIO.search(despues):
            continue
        cola = texto[fin: fin + VENTANA_DATOS]
        mat = _RE_MATRICULA.search(cola)
        tel = _RE_TELEFONO.search(cola)
        menciones.append({
            "nombre": nombre, "clave": clave_nombre(nombre), "ini": m.start(), "fin": fin,
            "emails": set(),
            "matricula": re.sub(r"\s", "", mat.group("mat")).upper() if mat else "",
            "telefono": re.sub(r"[\s-]", "", tel.group("tel")) if tel else "",
        })
    if not menciones:
        return []
    # Cada correo va al nombre más cercano dentro de la ventana; un correo sin
    # abogado cerca (de una parte, de una entidad) se descarta.
    for e in _RE_EMAIL.finditer(texto):
        email = e.group(0).strip(".").lower()
        cerca = min(menciones, key=lambda x: min(abs(e.start() - x["fin"]), abs(x["ini"] - e.end())))
        if min(abs(e.start() - cerca["fin"]), abs(cerca["ini"] - e.end())) > VENTANA_CORREO:
            continue
        if correo_valido(email):
            cerca["emails"].add(email)
        elif "@" in email and email.split("@", 1)[1].endswith((".gob.ec", ".mil.ec")):
            cerca["publico"] = True     # su casillero usa un correo del Estado: funcionario
    por_clave = {}
    for x in menciones:
        if x.get("publico"):
            continue
        y = por_clave.setdefault(x["clave"], {"nombre": x["nombre"], "clave": x["clave"],
                                              "emails": set(), "matricula": "", "telefono": ""})
        y["emails"] |= x["emails"]
        y["matricula"] = y["matricula"] or x["matricula"]
        y["telefono"] = y["telefono"] or x["telefono"]
    return list(por_clave.values())


def extraer_notificacion(texto: str) -> list:
    """Razón de notificación de SATJE: cada destinatario va separado por ';'
    ("a: PARTE … correo … del Dr./Ab. NOMBRE;"). Se extrae por destinatario para
    que el correo de una parte sin abogado no se pegue al abogado de al lado."""
    por_clave = {}
    for seg in (texto or "").split(";"):
        for a in extraer(seg):
            y = por_clave.setdefault(a["clave"], a)
            if y is not a:
                y["emails"] |= a["emails"]
                y["matricula"] = y["matricula"] or a["matricula"]
                y["telefono"] = y["telefono"] or a["telefono"]
    return list(por_clave.values())


def ordenar_correos(clave: str, correos: Counter) -> list:
    """Primero el correo que lleva un apellido o nombre del abogado (el suyo
    propio); luego los demás que registró para notificaciones (cliente,
    asistente, institución), por frecuencia."""
    toks = [t.lower() for t in clave.split() if len(t) >= 4]
    return sorted(correos, key=lambda e: (not any(t in e.split("@")[0] for t in toks),
                                          -correos[e], e))


class Directorio:
    """Acumula menciones por abogado y une claves que comparten matrícula."""

    def __init__(self):
        self.p = defaultdict(lambda: {"nombres": Counter(), "emails": Counter(),
                                      "matriculas": Counter(), "telefonos": Counter(),
                                      "causas": set(), "niveles": Counter(),
                                      "tribunales": Counter(), "anios": set()})
        self.textos = 0

    def agregar(self, numero, nivel, texto, tribunal="", anio="", menciones=None):
        """menciones: ya extraídas (fuente SATJE); si no, se extraen de texto."""
        self.textos += 1
        for a in (extraer(texto) if menciones is None else menciones):
            d = self.p[a["clave"]]
            d["nombres"][a["nombre"]] += 1
            d["emails"].update(a["emails"])
            if a["matricula"]:
                d["matriculas"][a["matricula"]] += 1
            if a["telefono"]:
                d["telefonos"][a["telefono"]] += 1
            if tribunal and numero not in d["causas"]:
                d["tribunales"][tribunal] += 1     # causas por judicatura, no menciones
            d["causas"].add(numero)
            d["niveles"][nivel] += 1
            if anio:
                d["anios"].add(str(anio))

    def _unir_por_matricula(self):
        dueno = {}
        for clave in sorted(self.p, key=lambda k: -len(self.p[k]["causas"])):
            mats = [m for m in self.p[clave]["matriculas"] if "-" in m or "." in m]
            destino = next((dueno[m] for m in mats if m in dueno), None)
            if destino is None:
                for m in mats:
                    dueno[m] = clave
                continue
            o, d = self.p.pop(clave), self.p[destino]
            for k in ("nombres", "emails", "matriculas", "telefonos", "niveles", "tribunales"):
                d[k].update(o[k])
            d["causas"] |= o["causas"]
            d["anios"] |= o["anios"]
            for m in mats:
                dueno.setdefault(m, destino)

    def filas(self, materia: str) -> list:
        self._unir_por_matricula()
        out = []
        for clave, d in self.p.items():
            # Un nombre visto en una sola causa y sin correo ni matrícula es
            # casi siempre ruido de la expresión regular.
            if len(d["causas"]) < 2 and not d["emails"] and not d["matriculas"]:
                continue
            out.append({
                "materia": materia, "clave": clave,
                "nombre": d["nombres"].most_common(1)[0][0],
                "emails": ordenar_correos(clave, d["emails"])[:5],
                "matriculas": [m for m, _ in d["matriculas"].most_common(3)],
                "telefonos": [t for t, _ in d["telefonos"].most_common(3)],
                "n_causas": len(d["causas"]),
                "niveles": dict(d["niveles"]),
                "tribunales": dict(d["tribunales"].most_common(10)),
                "anio_min": min(d["anios"]) if d["anios"] else None,
                "anio_max": max(d["anios"]) if d["anios"] else None,
                "causas": sorted(d["causas"])[-MAX_CAUSAS:],
            })
        out.sort(key=lambda r: -r["n_causas"])
        return out


CREATE_SQL = """
CREATE TABLE IF NOT EXISTS directorio_abogados (
    materia     VARCHAR(30) NOT NULL,
    clave       VARCHAR(300) NOT NULL,
    nombre      VARCHAR(300) NOT NULL,
    emails      TEXT[]  NOT NULL DEFAULT '{}',
    matriculas  TEXT[]  NOT NULL DEFAULT '{}',
    telefonos   TEXT[]  NOT NULL DEFAULT '{}',
    n_causas    INTEGER NOT NULL,
    niveles     JSONB,
    tribunales  JSONB,
    anio_min    VARCHAR(4),
    anio_max    VARCHAR(4),
    causas      TEXT[]  NOT NULL DEFAULT '{}',
    actualizado_en TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (materia, clave)
);
CREATE INDEX IF NOT EXISTS idx_directorio_abogados_clave ON directorio_abogados (clave);
"""


def recorrer(conn, directorio: Directorio, limite=None):
    # Cursor de servidor y sin ORDER BY: ordenar GB de texto saturaría la base.
    with conn.cursor(name="abogados") as cur:
        cur.itersize = LOTE
        cur.execute("""
            SELECT t.numero, t.nivel, t.texto, c.tribunal_codigo, c.año
            FROM causa_texto t LEFT JOIN worker_causas c ON c.numero = t.numero""" + (" LIMIT %s" if limite else ""),
            (limite,) if limite else None)
        for i, (numero, nivel, texto, tribunal, anio) in enumerate(cur, 1):
            directorio.agregar(numero, nivel, texto, tribunal or "", anio or "")
            if i % LOTE == 0:
                time.sleep(PAUSA_SEG)
            if i % 10000 == 0:
                print(f"  {i} textos, {len(directorio.p)} nombres", flush=True)


def _preparar(conn):
    """Crea tabla e índice en su propia transacción. Dentro de la del reemplazo,
    CREATE INDEX deja tomado un bloqueo que choca con el DELETE de otro worker
    que consolida a la vez: deadlock (penal, 13-nov)."""
    with conn, conn.cursor() as cur:
        cur.execute(CREATE_SQL)


def _bloquear_materia(cur, materia: str):
    """Dos consolidaciones de la misma materia (la automática y una manual) van en fila."""
    cur.execute("SELECT pg_advisory_xact_lock(hashtext('directorio_abogados:' || %s))", (materia,))


def guardar(conn, filas: list, materia: str):
    import psycopg2.extras
    _preparar(conn)
    with conn, conn.cursor() as cur:
        _bloquear_materia(cur, materia)
        cur.execute("DELETE FROM directorio_abogados WHERE materia = %s", (materia,))
        psycopg2.extras.execute_values(cur, """
            INSERT INTO directorio_abogados (materia, clave, nombre, emails, matriculas,
                telefonos, n_causas, niveles, tribunales, anio_min, anio_max, causas)
            VALUES %s""", [(
                f["materia"], f["clave"][:300], f["nombre"][:300], f["emails"], f["matriculas"],
                f["telefonos"], f["n_causas"], json.dumps(f["niveles"]),
                json.dumps(f["tribunales"]), f["anio_min"], f["anio_max"], f["causas"],
            ) for f in filas], page_size=1000)


# ── Consolidado (fuente SATJE) ──────────────────────────────────────────────
# La agregación la hace Postgres: en Python, civil (1,1 millones de causas)
# no cabe en los 512 MB del worker. Se lee en la base de la materia y se escribe
# en la base central (DIRECTORIO_DATABASE_URL; si falta, la misma base: civil).
CONSOLIDAR_SQL = """
WITH m AS (
    SELECT a.numero, x->>'clave' AS clave, x->>'nombre' AS nombre, x->'emails' AS emails,
           NULLIF(x->>'matricula', '') AS mat, NULLIF(x->>'telefono', '') AS tel,
           c.tribunal_codigo AS t, c.año AS anio
    FROM abogado_satje a
    CROSS JOIN LATERAL jsonb_array_elements(a.menciones) x
    LEFT JOIN worker_causas c ON c.numero = a.numero
    WHERE a.error IS NULL AND x->>'clave' <> '' AND NOT (x->>'clave') ~ %(inst)s
),
e AS (SELECT clave, array_agg(em ORDER BY n DESC, em) AS emails
      FROM (SELECT clave, em, count(*) AS n
            FROM m CROSS JOIN LATERAL jsonb_array_elements_text(m.emails) em
            GROUP BY 1, 2) s GROUP BY clave),
t AS (SELECT clave, jsonb_object_agg(t, n) AS trib
      FROM (SELECT clave, t, n, row_number() OVER (PARTITION BY clave ORDER BY n DESC, t) AS r
            FROM (SELECT clave, t, count(DISTINCT numero) AS n FROM m
                  WHERE t IS NOT NULL GROUP BY 1, 2) s0) s WHERE r <= 10 GROUP BY clave),
nm AS (SELECT DISTINCT ON (clave) clave, nombre
       FROM (SELECT clave, nombre, count(*) AS n FROM m GROUP BY 1, 2) s
       ORDER BY clave, n DESC, nombre),
g AS (SELECT clave, count(DISTINCT numero) AS n_causas,
             array_remove(array_agg(DISTINCT mat), NULL) AS mats,
             array_remove(array_agg(DISTINCT tel), NULL) AS tels,
             min(anio) AS amin, max(anio) AS amax,
             (array_agg(DISTINCT numero ORDER BY numero DESC))[1:10] AS causas
      FROM m GROUP BY clave)
SELECT g.clave, nm.nombre, coalesce((e.emails)[1:8], '{}'), (g.mats)[1:3], (g.tels)[1:3],
       g.n_causas, coalesce(t.trib, '{}'::jsonb), g.amin, g.amax, g.causas
FROM g JOIN nm USING (clave) LEFT JOIN e USING (clave) LEFT JOIN t USING (clave)
WHERE e.emails IS NOT NULL OR g.n_causas >= 2
"""


def _primero_el_propio(clave: str, emails: list) -> list:
    """Mismo criterio que ordenar_correos, conservando el orden por frecuencia."""
    toks = [t.lower() for t in clave.split() if len(t) >= 4]
    return sorted(emails, key=lambda e: not any(t in e.split("@")[0] for t in toks))


def consolidar(slug: str, muestra: bool = False) -> int:
    """Recalcula el directorio de la materia desde abogado_satje y lo reemplaza en
    la base central en una sola transacción. Devuelve cuántos abogados escribió."""
    import os
    import psycopg2
    import psycopg2.extras
    from worker.job_manager import DATABASE_URL
    central_url = os.environ.get("DIRECTORIO_DATABASE_URL") or DATABASE_URL
    lectura = psycopg2.connect(DATABASE_URL)
    lectura.set_session(readonly=True)
    escritura = None if muestra else psycopg2.connect(central_url)
    n = 0
    try:
        with lectura.cursor(name="consolidar") as cur:
            cur.itersize = 2000
            cur.execute(CONSOLIDAR_SQL, {"inst": INSTITUCION_REGEX})
            if muestra:
                for i, r in enumerate(cur):
                    if i < 50:
                        print(f"{r[5]:>5}  {r[1]:<45} {', '.join(_primero_el_propio(r[0], r[2])[:3])}")
                    n += 1
                return n
            _preparar(escritura)
            with escritura, escritura.cursor() as w:
                _bloquear_materia(w, slug)
                w.execute("DELETE FROM directorio_abogados WHERE materia = %s", (slug,))
                while True:
                    lote = cur.fetchmany(2000)
                    if not lote:
                        break
                    psycopg2.extras.execute_values(w, """
                        INSERT INTO directorio_abogados (materia, clave, nombre, emails, matriculas,
                            telefonos, n_causas, niveles, tribunales, anio_min, anio_max, causas)
                        VALUES %s""", [(
                            slug, clave[:300], (nombre or "")[:300],
                            _primero_el_propio(clave, emails)[:5], mats or [], tels or [], nc,
                            json.dumps({"satje": nc}), json.dumps(trib), amin, amax, causas or [],
                        ) for clave, nombre, emails, mats, tels, nc, trib, amin, amax, causas in lote])
                    n += len(lote)
        return n
    finally:
        lectura.close()
        if escritura is not None:
            escritura.close()


# ── Consulta consolidada (página /abogados/ver) ──────────────────────────────
def _filtros(q="", materia="", provincia="", con_correo=False):
    # Filtra también aquí: lo ya consolidado antes del arreglo trae instituciones.
    where, params = ["NOT clave ~ %s"], [INSTITUCION_REGEX]
    if materia:
        where.append("materia = %s")
        params.append(materia)
    if provincia:
        where.append("EXISTS (SELECT 1 FROM jsonb_object_keys(tribunales) k WHERE left(k, 2) = %s)")
        params.append(provincia)
    if con_correo:
        where.append("cardinality(emails) > 0")
    q = (q or "").strip()
    if q:
        toks = [t for t in re.split(r"\s+", _sin_tildes(q).upper()) if t]
        por_nombre = " AND ".join(["clave LIKE %s"] * len(toks))
        where.append(f"(({por_nombre}) OR array_to_string(emails, ' ') ILIKE %s)")
        params += [f"%{t}%" for t in toks] + [f"%{q}%"]
    return (" WHERE " + " AND ".join(where)) if where else "", params


CONSULTA_SQL = """
SELECT clave, (array_agg(nombre ORDER BY n_causas DESC))[1] AS nombre, sum(n_causas) AS n_causas,
       min(anio_min) AS anio_min, max(anio_max) AS anio_max,
       jsonb_agg(jsonb_build_object('materia', materia, 'n', n_causas, 'emails', emails,
           'matriculas', matriculas, 'telefonos', telefonos, 'tribunales', tribunales,
           'causas', causas) ORDER BY n_causas DESC) AS det
FROM directorio_abogados{where}
GROUP BY clave ORDER BY sum(n_causas) DESC, clave
"""


def fusionar(clave, nombre, n_causas, anio_min, anio_max, det) -> dict:
    """Une las filas de un mismo abogado en varias materias."""
    emails, mats, tels, trib, causas = Counter(), [], [], Counter(), []
    for d in det:
        for i, e in enumerate(d["emails"]):
            emails[e] += d["n"] - i * 0.001     # frecuencia de la materia, luego su orden
        mats += [m for m in d["matriculas"] if m not in mats]
        tels += [t for t in d["telefonos"] if t not in tels]
        trib.update(d["tribunales"] or {})
        causas += d["causas"][:5]
    correos = _primero_el_propio(clave, [e for e, _ in emails.most_common()])
    return {
        "nombre": nombre, "correo": correos[0] if correos else "", "otros_correos": correos[1:6],
        "materias": {d["materia"]: d["n"] for d in det}, "n_causas": int(n_causas),
        "provincias": sorted({k[:2] for k in trib}), "tribunales": dict(trib.most_common(5)),
        "anio_min": anio_min, "anio_max": anio_max, "matriculas": mats[:3], "telefonos": tels[:3],
        "causas": causas[:10],
    }


def resumen(cur) -> dict:
    cur.execute("""SELECT materia, count(*), count(*) FILTER (WHERE cardinality(emails) > 0),
                          max(actualizado_en) FROM directorio_abogados WHERE NOT clave ~ %s
                   GROUP BY materia ORDER BY materia""", [INSTITUCION_REGEX])
    por = {m: {"abogados": a, "con_correo": c, "actualizado": str(t)[:16]} for m, a, c, t in cur.fetchall()}
    cur.execute("""SELECT count(DISTINCT clave), count(DISTINCT clave) FILTER (WHERE cardinality(emails) > 0)
                   FROM directorio_abogados WHERE NOT clave ~ %s""", [INSTITUCION_REGEX])
    total, con = cur.fetchone()
    return {"abogados": total, "con_correo": con, "materias": por}


def consultar(cur, q="", materia="", provincia="", con_correo=False, limite=50, offset=0) -> dict:
    where, params = _filtros(q, materia, provincia, con_correo)
    cur.execute(f"SELECT count(DISTINCT clave) FROM directorio_abogados{where}", params)
    total = cur.fetchone()[0]
    cur.execute(CONSULTA_SQL.format(where=where) + " LIMIT %s OFFSET %s", params + [limite, offset])
    return {"total": total, "filas": [fusionar(*r) for r in cur.fetchall()]}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--limite", type=int, help="máximo de textos (prueba)")
    ap.add_argument("--fuente", choices=("texto", "satje"), default="texto",
                    help="texto: sentencias (causa_texto); satje: notificaciones (abogado_satje)")
    ap.add_argument("--muestra", action="store_true",
                    help="no escribe en la base: imprime los 50 primeros abogados")
    args = ap.parse_args()

    import psycopg2
    from worker.job_manager import DATABASE_URL
    try:
        from config import materia as MATERIA
        slug = MATERIA.SLUG
    except ImportError:   # el extractor tributario no tiene config/materia.py
        slug = "tributario"

    if args.fuente == "satje":
        n = consolidar(slug, muestra=args.muestra)
        print(f"{n} abogados de {slug}" + ("" if args.muestra else " consolidados en directorio_abogados"),
              flush=True)
        return

    directorio = Directorio()
    lectura = psycopg2.connect(DATABASE_URL)
    lectura.set_session(readonly=True)
    try:
        recorrer(lectura, directorio, args.limite)
    finally:
        lectura.close()
    filas = directorio.filas(slug)
    con_correo = sum(1 for f in filas if f["emails"])
    print(f"{directorio.textos} textos → {len(filas)} abogados ({con_correo} con correo, "
          f"{sum(1 for f in filas if f['matriculas'])} con matrícula)", flush=True)
    if args.muestra:
        for f in filas[:50]:
            print(f"{f['n_causas']:>5}  {f['nombre']:<45} {', '.join(f['emails'])}  "
                  f"{', '.join(f['matriculas'])}  {', '.join(f['telefonos'])}  {f['causas'][-1]}")
        return
    escritura = psycopg2.connect(DATABASE_URL)
    try:
        guardar(escritura, filas, slug)
    finally:
        escritura.close()
    print(f"Tabla directorio_abogados actualizada ({slug}).", flush=True)


if __name__ == "__main__":
    main()
