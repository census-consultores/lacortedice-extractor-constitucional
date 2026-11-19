"""
Observatorio normativo: cifras agregadas del corpus para el piloto con la
Asamblea Nacional. Sin IA y sin consultar SATJE: lee solo lo que ya está en la
base del worker (worker_causas, worker_causa_datos y causa_texto).

Produce un JSON con:
  - cobertura: causas, PDF por nivel y textos indexados;
  - resultados: decisiones según SATJE por nivel y aceptación de la apelación
    por provincia;
  - tiempos: duración de la primera instancia por judicatura y por año;
  - normas: artículos más citados por cuerpo legal (expresiones regulares);
  - señales: sentencias que mencionan vacío, laguna, silencio u oscuridad de la
    ley, analogía, antinomia o normas derogadas, con pasajes literales;
  - citas: sentencias de la Corte Constitucional y resoluciones de la CNJ más
    citadas.

Correrlo desde el shell del worker en Render (tiene DATABASE_URL y STRAPI_URL):

  nohup python -u scripts/observatorio.py --subir > /tmp/observatorio.log 2>&1 &
  python scripts/observatorio.py --limite 2000        # prueba rápida

Con --subir el JSON se publica en la media library del CMS y el log imprime la
URL. Solo lectura sobre la base; el texto se lee por lotes con pausas para no
cargar la CPU de la base.
"""
import argparse
import json
import random
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

LOTE = 200
PAUSA_SEG = 0.05
MAX_PASAJES = 40          # por señal y nivel
TOP_ARTICULOS = 25

# minúsculas sin tildes, carácter por carácter (mantiene las posiciones)
_TRADUCE = str.maketrans("áéíóúüñÁÉÍÓÚÜÑ", "aeiouunaeiouun")


def normalizar(texto: str) -> str:
    return texto.translate(_TRADUCE).lower()


# Cuerpo legal canónico → patrón sobre el texto normalizado.
CUERPOS = {
    "Constitución": r"constitucion(?: de la republica)?(?: del ecuador)?",
    "Código Civil": r"codigo civil",
    "COGEP": r"codigo organico general de procesos|cogep",
    "Código de Procedimiento Civil (derogado)": r"codigo de procedimiento civil",
    "Código de Comercio": r"codigo de comercio",
    "Ley de Compañías": r"ley de companias",
    "Código del Trabajo": r"codigo del? trabajo",
    "Código Tributario": r"codigo tributario",
    "LRTI": r"ley (?:organica )?de regimen tributario interno|lorti|lrti",
    "COA": r"codigo organico administrativo|coa\b",
    "COFJ": r"codigo organico de la funcion judicial|cofj",
    "COOTAD": r"codigo organico de organizacion territorial[a-z ,]*|cootad",
    "COPCI": r"codigo organico de la produccion,? comercio e inversiones|copci",
    "LOSEP": r"ley organica del? servicio publico|losep",
    "LOSNCP": r"ley organica del sistema nacional de contratacion publica|losncp",
    "LOGJCC": r"ley organica de garantias jurisdiccionales y control constitucional|logjcc",
    "Ley de la Jurisdicción Contencioso Administrativa (derogada)":
        r"ley de la jurisdiccion contencioso[- ]administrativa",
    "Ley Orgánica de Defensa del Consumidor": r"ley organica de defensa del consumidor",
    "Ley de Inquilinato": r"ley de inquilinato",
    "Código Orgánico Monetario y Financiero": r"codigo organico monetario y financiero",
    "Ley de Seguridad Social": r"ley de seguridad social",
    "Ley Orgánica de Apoyo Humanitario": r"ley organica de apoyo humanitario",
    "Código de la Niñez y Adolescencia": r"codigo de la ninez y adolescencia",
    "Ley Notarial": r"ley notarial",
}
_RE_CUERPO = re.compile(
    r"\s*,?\s*(?:del|de la|de los|de el)\s+(?:vigente\s+)?(" +
    "|".join(f"(?P<c{i}>{p})" for i, p in enumerate(CUERPOS.values())) + ")")
_NOMBRES_CUERPO = list(CUERPOS)
_RE_ART = re.compile(r"\bart(?:iculos?|s?\.)\s*")
# números de artículo con sus conectores; numeral/inciso/literal no son artículos
_RE_TOKEN = re.compile(r"\s*(\d{1,2}\.\d{3}|\d{1,4}(?:\.\d{1,2})?|y|e|al|,|;|"
                       r"numeral(?:es)?|num\.|incisos?|literal(?:es)?|[a-z]\)|"
                       r"primero|segundo|tercero|cuarto|final|innumerado)")


def citas_de_articulos(t: str) -> set:
    """{(cuerpo, artículo)} citados en un texto normalizado. Solo cuenta la cita
    si el cuerpo legal aparece justo después de los números ("art. 1453 del
    Código Civil"); "ibídem" y "del mismo cuerpo legal" se ignoran."""
    out = set()
    for m in _RE_ART.finditer(t):
        pos, nums, saltar = m.end(), [], False
        while True:
            tk = _RE_TOKEN.match(t, pos)
            if not tk:
                break
            v = tk.group(1)
            if v[0].isdigit():
                if not saltar:
                    nums.append(v.replace(".", "") if re.fullmatch(r"\d{1,2}\.\d{3}", v) else v)
                saltar = False
            elif v.startswith(("numeral", "num.", "inciso", "literal")):
                saltar = True
            pos = tk.end()
        if not nums:
            continue
        c = _RE_CUERPO.match(t, pos)
        if not c:
            continue
        i = next(int(k[1:]) for k, v in c.groupdict().items() if v)
        out.update((_NOMBRES_CUERPO[i], n) for n in nums)
    return out


SENALES = {
    "Vacío legal o normativo": r"vacios? (?:legal|legales|normativos?|de la ley)",
    "Laguna de la ley": r"lagunas? (?:legal|legales|normativas?|juridicas?|de la ley)",
    "Silencio, oscuridad o insuficiencia de la ley":
        r"(?:silencio|oscuridad|insuficiencia) de la ley|ley (?:oscura|insuficiente)",
    "La ley no prevé el supuesto":
        r"(?:la|esta) (?:ley|norma) no (?:preve|contempla|regula)",
    "Aplicación por analogía": r"por analogia|aplicacion analogica|analogicamente",
    "Antinomia o contradicción normativa": r"antinomias?|contradiccion (?:normativa|entre normas)",
    "Mención de norma derogada": r"(?:norma|ley|codigo|disposicion) derogad[ao]",
    "Precedente obligatorio o triple reiteración":
        r"triple reiteracion|precedentes? jurisprudencial(?:es)? obligatorios?",
}
_RE_SENALES = {k: re.compile(v) for k, v in SENALES.items()}
_RE_CC = re.compile(r"\b(\d{1,4}-\d{2}-(?:ep|in|jp|cn|is|an|jh|jd|dc|cp|ee|sep|sin|san)"
                    r"(?:/\d{2}|-cc)?)\b")
_RE_RES_CNJ = re.compile(r"resolucion (?:no\.?\s*|n\.?\s*)?(\d{2,3}-\d{4})"
                         r"(?=[^.]{0,120}corte nacional)")
_RE_RESUELVE = re.compile(r"resuelve|administrando justicia")


def fragmento(original: str, ini: int, fin: int, radio: int = 220) -> str:
    a, b = max(0, ini - radio), min(len(original), fin + radio)
    trozo = " ".join(original[a:b].split())
    return ("…" if a else "") + trozo + ("…" if b < len(original) else "")


def decision_por_texto(nivel: str, t: str):
    """Decisión de la parte resolutiva (tras el último 'resuelve'); None si no aparece."""
    from worker import strapi_client as sc
    ms = list(_RE_RESUELVE.finditer(t))
    if not ms:
        return None
    parte = t[ms[-1].end(): ms[-1].end() + 700]
    # tributario no tiene nivel de apelación ni su clasificador
    if nivel == "apelacion" and hasattr(sc, "_clasificar_decision_apelacion"):
        return sc._clasificar_decision_apelacion(parte)
    if nivel == "corte":
        return sc._clasificar_decision_casacion(parte) or None
    return None


class Acumulador:
    def __init__(self, semilla: int = 7):
        self.textos = Counter()
        self.articulos = defaultdict(Counter)      # nivel → (cuerpo, art) → causas
        self.cuerpos = defaultdict(Counter)        # nivel → cuerpo → causas
        self.senales = defaultdict(Counter)        # señal → nivel → causas
        self.pasajes = defaultdict(list)           # (señal, nivel) → muestra
        self.vistos = Counter()                    # (señal, nivel) → n, para el muestreo
        self.cc = Counter()
        self.res_cnj = Counter()
        self.decisiones = defaultdict(Counter)     # nivel → decisión por texto
        self._rnd = random.Random(semilla)

    def agregar(self, numero, nivel, texto, tribunal, anio):
        self.textos[nivel] += 1
        t = normalizar(texto)
        mismo_largo = len(t) == len(texto)
        citas = citas_de_articulos(t)
        self.articulos[nivel].update(citas)
        self.cuerpos[nivel].update({c for c, _ in citas})
        for nombre, rx in _RE_SENALES.items():
            m = rx.search(t)
            if not m:
                continue
            self.senales[nombre][nivel] += 1
            clave = (nombre, nivel)
            self.vistos[clave] += 1
            p = {"numero": numero, "nivel": nivel, "judicatura": tribunal, "anio": anio,
                 "pasaje": fragmento(texto if mismo_largo else t, m.start(), m.end())}
            # muestreo de reservorio: pasajes repartidos en todo el corpus
            if len(self.pasajes[clave]) < MAX_PASAJES:
                self.pasajes[clave].append(p)
            else:
                j = self._rnd.randrange(self.vistos[clave])
                if j < MAX_PASAJES:
                    self.pasajes[clave][j] = p
        self.cc.update(set(_RE_CC.findall(t)))
        self.res_cnj.update(set(_RE_RES_CNJ.findall(t)))
        d = decision_por_texto(nivel, t)
        if d:
            self.decisiones[nivel][d] += 1

    def resultado(self) -> dict:
        normas = {}
        for nivel, cnt in self.articulos.items():
            por_cuerpo = defaultdict(list)
            for (cuerpo, art), n in cnt.most_common():
                if len(por_cuerpo[cuerpo]) < TOP_ARTICULOS:
                    por_cuerpo[cuerpo].append({"articulo": art, "causas": n})
            normas[nivel] = {
                "cuerpos": [{"cuerpo": c, "causas": n} for c, n in self.cuerpos[nivel].most_common()],
                "articulos": por_cuerpo,
            }
        return {
            "textos_analizados": dict(self.textos),
            "normas": normas,
            "senales": {k: dict(v) for k, v in self.senales.items()},
            "pasajes": {f"{s}|{n}": v for (s, n), v in self.pasajes.items()},
            "cc_mas_citadas": [{"sentencia": k, "causas": n} for k, n in self.cc.most_common(30)],
            "resoluciones_cnj_mas_citadas": [{"resolucion": k, "causas": n}
                                              for k, n in self.res_cnj.most_common(20)],
            "decision_por_texto": {k: dict(v) for k, v in self.decisiones.items()},
        }


def _filas(cur, sql, params=()):
    cur.execute(sql, params)
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _existe(cur, tabla: str) -> bool:
    cur.execute("SELECT to_regclass(%s)", (tabla,))
    return cur.fetchone()[0] is not None


def cifras_sql(conn) -> dict:
    with conn.cursor() as cur:
        tiene_apel = _existe(cur, "worker_causa_datos") and bool(_filas(
            cur, "SELECT 1 FROM information_schema.columns WHERE table_name='worker_causa_datos' "
                 "AND column_name='url_pdf_apel'"))
        col_apel = "d.url_pdf_apel" if tiene_apel else "NULL"
        res_apel = "d.resultado_apel" if tiene_apel else "NULL"
        cob = _filas(cur, f"""
            SELECT COUNT(*) AS causas,
                   COUNT(*) FILTER (WHERE COALESCE(d.url_pdf_tribunal,'') <> '') AS pdf_primera,
                   COUNT(*) FILTER (WHERE COALESCE({col_apel},'') <> '') AS pdf_apelacion,
                   COUNT(*) FILTER (WHERE COALESCE(d.url_pdf_cnj,'') <> '') AS pdf_casacion,
                   COUNT(DISTINCT c.tribunal_codigo) AS judicaturas,
                   MIN(c.año) AS desde, MAX(c.año) AS hasta
            FROM worker_causas c LEFT JOIN worker_causa_datos d ON d.numero = c.numero""")[0]
        if _existe(cur, "causa_texto"):
            cob["textos_indexados"] = {r["nivel"]: r["n"] for r in _filas(
                cur, "SELECT nivel, COUNT(*) AS n FROM causa_texto GROUP BY nivel")}
        por_anio = _filas(cur, "SELECT año AS anio, COUNT(*) AS causas FROM worker_causas "
                               "GROUP BY año ORDER BY año")
        resultados = {nivel: _filas(cur, f"""
            SELECT {col} AS resultado, COUNT(*) AS causas FROM worker_causa_datos
            WHERE COALESCE({col},'') <> '' GROUP BY 1 ORDER BY 2 DESC LIMIT 15""")
            for nivel, col in (("primera", "resultado"), ("casacion", "resultado_cnj"))
            + ((("apelacion", "resultado_apel"),) if tiene_apel else ())}
        apel_prov = _filas(cur, f"""
            SELECT LEFT(c.tribunal_codigo, 2) AS provincia, {res_apel} AS resultado, COUNT(*) AS causas
            FROM worker_causas c JOIN worker_causa_datos d ON d.numero = c.numero
            WHERE COALESCE({res_apel},'') <> '' GROUP BY 1, 2 ORDER BY 1, 3 DESC""") if tiene_apel else []
        tiempos_jud = _filas(cur, """
            SELECT tribunal_codigo AS codigo, MAX(tribunal_nombre) AS judicatura, COUNT(*) AS causas,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY duracion_dias) AS mediana_dias,
                   percentile_cont(0.75) WITHIN GROUP (ORDER BY duracion_dias) AS p75_dias
            FROM worker_causas WHERE duracion_dias > 0
            GROUP BY tribunal_codigo HAVING COUNT(*) >= 30 ORDER BY 3 DESC""")
        tiempos_anio = _filas(cur, """
            SELECT año AS anio, COUNT(*) AS causas,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY duracion_dias) AS mediana_dias
            FROM worker_causas WHERE duracion_dias > 0 GROUP BY año ORDER BY año""")
        tiempos_pais = _filas(cur, """
            SELECT COUNT(*) AS causas,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY duracion_dias) AS mediana_dias,
                   percentile_cont(0.75) WITHIN GROUP (ORDER BY duracion_dias) AS p75_dias
            FROM worker_causas WHERE duracion_dias > 0""")[0]
    return {"cobertura": cob, "causas_por_anio": por_anio, "resultados_satje": resultados,
            "apelacion_por_provincia": apel_prov,
            "tiempos": {"pais": tiempos_pais, "por_judicatura": tiempos_jud, "por_anio": tiempos_anio}}


def recorrer_textos(conn, acc: Acumulador, limite=None) -> None:
    with conn.cursor() as cur:
        indexado = _existe(cur, "causa_texto")
    filas = _textos_indexados(conn, limite) if indexado else _textos_de_pdf(conn, limite)
    n, t0 = 0, time.time()
    for numero, nivel, texto, tribunal, anio in filas:
        acc.agregar(numero, nivel, texto or "", tribunal, anio)
        n += 1
        if n % LOTE == 0:
            time.sleep(PAUSA_SEG)
        if n % 2000 == 0:
            print(f"  {n} textos · {n / (time.time() - t0):.1f}/s", flush=True)
    print(f"Textos analizados: {n}", flush=True)


def _textos_indexados(conn, limite):
    with conn.cursor(name="obs_textos") as cur:   # cursor de servidor: no carga todo en memoria
        cur.itersize = LOTE
        cur.execute(f"""
            SELECT t.numero, t.nivel, t.texto, c.tribunal_nombre, c.año
            FROM causa_texto t LEFT JOIN worker_causas c ON c.numero = t.numero
            {"LIMIT %s" % int(limite) if limite else ""}""")
        yield from cur


def _textos_de_pdf(conn, limite):
    """Sin causa_texto (tributario): baja cada PDF guardado en el CMS y extrae su
    texto con pypdf. Más lento (~1 PDF por segundo) y sin IA."""
    import httpx
    from resolucion.ai._cliente import extraer_texto_pdf
    from worker.strapi_client import STRAPI_URL
    print("Sin tabla causa_texto: se leen los PDF del CMS.", flush=True)
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT d.numero, x.nivel, x.url, c.tribunal_nombre, c.año
            FROM worker_causa_datos d LEFT JOIN worker_causas c ON c.numero = d.numero
            CROSS JOIN LATERAL (VALUES ('tribunal', d.url_pdf_tribunal), ('corte', d.url_pdf_cnj)) x(nivel, url)
            WHERE COALESCE(x.url, '') <> '' ORDER BY d.numero
            {"LIMIT %s" % int(limite) if limite else ""}""")
        pendientes = cur.fetchall()
    with httpx.Client(timeout=60, follow_redirects=True) as http:
        for numero, nivel, url, tribunal, anio in pendientes:
            if url.startswith("/"):
                url = STRAPI_URL.rstrip("/") + url
            try:
                r = http.get(url)
                r.raise_for_status()
                texto = extraer_texto_pdf(r.content)
            except Exception as e:
                print(f"  [omitido {numero} {nivel}] {type(e).__name__}: {e}", flush=True)
                continue
            time.sleep(0.2)   # el CMS sigue atendiendo a la web
            yield numero, nivel, texto, tribunal, anio


def _a_json(v):
    return float(v) if hasattr(v, "as_integer_ratio") and not isinstance(v, (int, float)) else str(v)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--limite", type=int, help="máximo de textos (prueba)")
    ap.add_argument("--salida", default="/tmp/observatorio.json")
    ap.add_argument("--subir", action="store_true", help="publicar el JSON en el CMS")
    args = ap.parse_args()

    import psycopg2
    from worker.job_manager import DATABASE_URL
    try:
        from config import materia as MATERIA
        nombre, slug = MATERIA.NOMBRE, MATERIA.SLUG
    except ImportError:   # el extractor tributario no tiene config/materia.py
        nombre, slug = "Tributario", "tributario"

    conn = psycopg2.connect(DATABASE_URL)
    conn.set_session(readonly=True)
    try:
        datos = {"materia": nombre, "slug": slug,
                 "generado_en": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 "limite_prueba": args.limite}
        print("Cifras por SQL…", flush=True)
        datos.update(cifras_sql(conn))
        print("Recorriendo textos…", flush=True)
        acc = Acumulador()
        recorrer_textos(conn, acc, args.limite)
        datos.update(acc.resultado())
    finally:
        conn.close()

    contenido = json.dumps(datos, ensure_ascii=False, default=_a_json, indent=1).encode()
    Path(args.salida).write_bytes(contenido)
    print(f"Guardado en {args.salida} ({len(contenido) // 1024} KB)", flush=True)
    if args.subir:
        from worker.strapi_client import StrapiClient
        archivo = f"observatorio_{slug}_{datetime.now():%Y%m%d_%H%M}.json"
        url = StrapiClient().subir_archivo(contenido, archivo, "application/json")
        print(f"URL: {url}" if url else "No se pudo subir al CMS; el JSON quedó en disco.", flush=True)


if __name__ == "__main__":
    main()
