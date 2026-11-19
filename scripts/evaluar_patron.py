#!/usr/bin/env python3
"""
Conjunto PATRÓN: mide la extracción de la IA contra fichas corregidas por un
abogado, para afinar prompts y catálogo con un número y no a ojo.

Flujo
  1. Elegir 30–50 causas representativas y extraerlas (POST /jobs/lista o
     POST /reextraer).
  2. `--generar`  → escribe patron/<numero>.json con lo que la IA extrajo.
     El abogado CORRIGE esos JSON a mano (es la "verdad").
  3. Cambiar prompts → POST /reextraer con esos números → `--evaluar`.
  4. Repetir hasta que el puntaje deje de subir. `--etiqueta` guarda cada
     ronda en patron/historial.csv para comparar versiones de prompt.

Uso
  python scripts/evaluar_patron.py --generar 09XXX-2024-00123 09XXX-2024-00456 …
  python scripts/evaluar_patron.py --generar --desde-archivo numeros.txt
  python scripts/evaluar_patron.py --evaluar [--etiqueta "p7-v3"] [--json salida.json]

Lee de la MISMA base que el worker (DATABASE_URL en config/.env o en el
entorno): worker_causa_datos, causa_tipos_defecto, causa_tipos_prueba,
causa_extraccion; y de Strapi (STRAPI_URL/STRAPI_API_TOKEN) el documentId de
la causa y las decisiones de apelación y casación.

Formato de patron/<numero>.json (todo opcional salvo numero; lo que falte no
se evalúa):
{
  "numero": "09XXX-2024-00123",
  "decision_final": "con lugar | sin lugar | parcialmente con lugar",
  "tipo_accion": "procedimiento sumario",
  "categorias": ["Despido intempestivo", "Indemnizaciones"],
  "tipos_defecto": ["DESPIDO_INTEMPESTIVO", "REMUNERACIONES_IMPAGAS"],
  "tipo_prueba_actor": ["ROLES_PAGO", "TESTIMONIAL"],
  "tipo_prueba_demandado": ["CONTRATO_TRABAJO"],
  "eje_decision": "prueba",
  "campos_con_dato": ["cuantia", "tipo_accion", "decision_final", "objeto", "jurisprudencia",
                      "categorizacion", "fecha_emision", "otros_actos", "pruebas", "abogados"],
  "decision_apelacion": "Confirma la sentencia.",
  "decision_casacion": "No casa la sentencia.",
  "notas": "texto libre del abogado"
}

Puntaje por causa (0–100): promedio de los criterios presentes en el patrón —
  decision_final (exacto) · tipo_accion (exacto, sin la pretensión tras el guion)
  · categorias (Jaccard) · tipos_defecto (Jaccard) · tipo_prueba actor/demandado
  (Jaccard) · eje_decision (exacto) · campos_con_dato (proporción que la IA
  llenó) · decision_apelacion / decision_casacion (exacto).
"""
import argparse
import csv
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

PATRON_DIR = PROJECT_ROOT / "patron"
CAMPOS = ["cuantia", "tipo_accion", "decision_final", "objeto", "jurisprudencia",
          "categorizacion", "fecha_emision", "otros_actos", "pruebas", "abogados"]


def _cargar_env():
    env = PROJECT_ROOT / "config" / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip().strip('"').strip("'")
            os.environ.setdefault(k, v)


def _norm(s) -> str:
    s = (s or "").strip().lower()
    for a, b in (("á", "a"), ("é", "e"), ("í", "i"), ("ó", "o"), ("ú", "u"), ("ñ", "n")):
        s = s.replace(a, b)
    return " ".join(s.split())


def _jaccard(a, b) -> float | None:
    A, B = {_norm(x) for x in (a or []) if x}, {_norm(x) for x in (b or []) if x}
    if not A and not B:
        return 1.0
    return len(A & B) / len(A | B)


# ── Lectura del estado actual ────────────────────────────────────────────────

def _leer_actual(numero: str) -> dict:
    from worker import job_manager as jm
    from worker.strapi_client import (
        StrapiClient, _parsear_nombres_categorias, _clasificar_decision_tribunal,
    )
    import httpx
    from worker.strapi_client import STRAPI_URL

    datos = jm.obtener_causa_datos(numero) or {}
    out = {
        "numero": numero,
        "decision_final": _clasificar_decision_tribunal(datos.get("decision_final") or "") or "",
        "tipo_accion": _norm((datos.get("tipo_accion") or "").split("—")[0]),
        "categorias": _parsear_nombres_categorias(datos.get("categorizacion") or ""),
        "campos_con_dato": [c for c in CAMPOS if datos.get(f"faltante_{c}") is False],
        "completitud": datos.get("completitud") or "",
    }
    strapi = StrapiClient()
    causa = strapi._buscar_causa(numero)
    doc_id = causa.get("documentId") if causa else None
    out["document_id"] = doc_id
    if doc_id:
        with jm._conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT codigo FROM causa_tipos_defecto WHERE document_id=%s", (doc_id,))
                out["tipos_defecto"] = sorted(r[0] for r in cur.fetchall())
                cur.execute("SELECT lado, codigo FROM causa_tipos_prueba WHERE document_id=%s", (doc_id,))
                filas = cur.fetchall()
                out["tipo_prueba_actor"] = sorted(c for l, c in filas if l == "actor")
                out["tipo_prueba_demandado"] = sorted(c for l, c in filas if l == "demandado")
                cur.execute("SELECT eje_decision FROM causa_extraccion WHERE document_id=%s", (doc_id,))
                r = cur.fetchone()
                out["eje_decision"] = (r[0] if r else "") or ""
        for coleccion, clave in (("apelaciones", "decision_apelacion"), ("casaciones", "decision_casacion")):
            try:
                resp = httpx.get(f"{STRAPI_URL}/api/{coleccion}",
                                 params={"filters[causa][documentId][$eq]": doc_id,
                                         "populate[decision_final][fields][0]": "nombre"},
                                 headers=strapi._headers, timeout=30)
                items = resp.json().get("data", [])
                dec = ((items[0].get("attributes", items[0]) if items else {}).get("decision_final") or {})
                dec = dec.get("data", dec)
                dec = (dec or {}).get("attributes", dec) or {}
                out[clave] = dec.get("nombre", "") if isinstance(dec, dict) else ""
            except Exception:
                out[clave] = ""
    return out


# ── Comparación ─────────────────────────────────────────────────────────────

def _evaluar_una(patron: dict, actual: dict) -> dict:
    criterios = {}
    if patron.get("decision_final"):
        criterios["decision_final"] = float(_norm(patron["decision_final"]) == _norm(actual.get("decision_final")))
    if patron.get("tipo_accion"):
        criterios["tipo_accion"] = float(_norm(patron["tipo_accion"].split("—")[0])
                                         == _norm((actual.get("tipo_accion") or "").split("—")[0]))
    for k in ("categorias", "tipos_defecto", "tipo_prueba_actor", "tipo_prueba_demandado"):
        if k in patron:
            criterios[k] = _jaccard(patron[k], actual.get(k))
    if patron.get("eje_decision"):
        criterios["eje_decision"] = float(_norm(patron["eje_decision"]) == _norm(actual.get("eje_decision")))
    if patron.get("campos_con_dato"):
        esperados = [c for c in patron["campos_con_dato"] if c in CAMPOS]
        if esperados:
            llenos = set(actual.get("campos_con_dato") or [])
            criterios["campos_con_dato"] = sum(1 for c in esperados if c in llenos) / len(esperados)
    for k in ("decision_apelacion", "decision_casacion"):
        if patron.get(k):
            criterios[k] = float(_norm(patron[k]) == _norm(actual.get(k)))
    puntaje = round(100 * sum(criterios.values()) / len(criterios), 1) if criterios else None
    return {"numero": patron["numero"], "puntaje": puntaje, "criterios": criterios,
            "completitud": actual.get("completitud", "")}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--generar", nargs="*", metavar="NUMERO", help="escribe patron/<numero>.json con la extracción actual")
    g.add_argument("--evaluar", action="store_true", help="compara patron/*.json contra la extracción actual")
    ap.add_argument("--desde-archivo", help="archivo con un número de causa por línea (para --generar)")
    ap.add_argument("--sobrescribir", action="store_true", help="--generar pisa un patrón ya existente")
    ap.add_argument("--etiqueta", help="nombre de la ronda (versión de prompt) para patron/historial.csv")
    ap.add_argument("--json", help="ruta para volcar el detalle de la evaluación")
    args = ap.parse_args()
    _cargar_env()
    PATRON_DIR.mkdir(exist_ok=True)

    if args.generar is not None:
        numeros = list(args.generar)
        if args.desde_archivo:
            numeros += [l.strip() for l in Path(args.desde_archivo).read_text().splitlines() if l.strip()]
        if not numeros:
            ap.error("indicá números de causa o --desde-archivo")
        for n in numeros:
            destino = PATRON_DIR / f"{n}.json"
            if destino.exists() and not args.sobrescribir:
                print(f"  {n}: ya existe (usar --sobrescribir para pisarlo)"); continue
            actual = _leer_actual(n)
            patron = {k: actual.get(k) for k in (
                "numero", "decision_final", "tipo_accion", "categorias", "tipos_defecto",
                "tipo_prueba_actor", "tipo_prueba_demandado", "eje_decision", "campos_con_dato",
                "decision_apelacion", "decision_casacion")}
            patron["notas"] = "REVISAR: generado automáticamente desde la extracción de la IA; corregir a mano."
            destino.write_text(json.dumps(patron, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"  {n}: patrón generado ({'con' if actual.get('document_id') else 'SIN'} causa en Strapi)")
        print(f"\n{len(numeros)} patrón(es) en {PATRON_DIR}. Ahora: corregirlos a mano y correr --evaluar.")
        return 0

    patrones = sorted(PATRON_DIR.glob("*.json"))
    if not patrones:
        print(f"No hay patrones en {PATRON_DIR}. Primero --generar."); return 1
    resultados = []
    for pf in patrones:
        patron = json.loads(pf.read_text(encoding="utf-8"))
        if not patron.get("numero"):
            continue
        actual = _leer_actual(patron["numero"])
        resultados.append(_evaluar_una(patron, actual))

    con_puntaje = [r for r in resultados if r["puntaje"] is not None]
    promedio = round(sum(r["puntaje"] for r in con_puntaje) / len(con_puntaje), 1) if con_puntaje else 0.0
    por_criterio: dict = {}
    for r in resultados:
        for k, v in r["criterios"].items():
            por_criterio.setdefault(k, []).append(v)

    print(f"\nCONJUNTO PATRÓN — {len(resultados)} causas · puntaje promedio {promedio}/100\n")
    print(f"{'criterio':24} {'prom':>6}  n")
    for k, vals in sorted(por_criterio.items(), key=lambda kv: sum(kv[1]) / len(kv[1])):
        print(f"{k:24} {100 * sum(vals) / len(vals):6.1f}  {len(vals)}")
    print("\nPeores causas:")
    for r in sorted(con_puntaje, key=lambda r: r["puntaje"])[:10]:
        flojos = ", ".join(k for k, v in r["criterios"].items() if v < 0.5)
        print(f"  {r['numero']}  {r['puntaje']:5.1f}  {r['completitud'][:22]:22}  {flojos}")

    if args.etiqueta:
        hist = PATRON_DIR / "historial.csv"
        nuevo = not hist.exists()
        with open(hist, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if nuevo:
                w.writerow(["fecha", "etiqueta", "n", "promedio"] + sorted(por_criterio))
            w.writerow([datetime.now(timezone.utc).isoformat(timespec="minutes"), args.etiqueta,
                        len(resultados), promedio]
                       + [round(100 * sum(por_criterio[k]) / len(por_criterio[k]), 1) for k in sorted(por_criterio)])
        print(f"\nRonda '{args.etiqueta}' registrada en {hist}")
    if args.json:
        Path(args.json).write_text(json.dumps({"promedio": promedio, "causas": resultados},
                                              ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
