#!/usr/bin/env python3
"""
Descubre, contra SATJE, qué judicaturas (prefijos de 5 dígitos) y qué
idMateria corresponden a la materia de este extractor — el dato que hay que
pegar en config/materia.py (JUDICATURAS, JUDICATURAS_SLUG, IDS_MATERIA_SATJE)
antes de la primera corrida real.

Cómo funciona: el número de causa en SATJE es <judicatura>-<año>-<secuencial>.
Para cada prefijo candidato se consulta el secuencial 00001 de un año (y
unos pocos más por si el primero fue anulado) y se imprime la judicatura y
la materia que SATJE devuelve. No descarga PDFs ni llama a la IA.

Uso:
  python scripts/descubrir_judicaturas.py --provincia 09 --ano 2024
  python scripts/descubrir_judicaturas.py --prefijos 09359 09334 17230 --ano 2023
  python scripts/descubrir_judicaturas.py --provincia 09 --ano 2024 --filtro LABORAL,TRABAJO

  --provincia  : dos dígitos de la provincia (09 Guayas, 17 Pichincha, 01 Azuay…);
                 barre los 1000 prefijos 09000..09999 — lento (pausa 1.5 s), usar
                 --desde/--hasta para acotar.
  --filtro     : palabras (coma) que deben aparecer en el nombre de la judicatura
                 para listarla; por defecto se usan PRIMERA_INSTANCIA_CLAVES y
                 APELACION_CLAVES de config/materia.py.
  --todas      : imprime todas las judicaturas encontradas, sin filtrar.
  --csv ruta   : además escribe un CSV (prefijo, judicatura, idMateria, materia, muestra).

Salida (ejemplo):
  09359  UNIDAD JUDICIAL DE TRABAJO CON SEDE EN EL CANTÓN GUAYAQUIL   idMateria=?? (LABORAL)   muestra: 09359-2024-00001

Al terminar imprime un bloque listo para pegar en config/materia.py.
"""
import argparse
import csv
import os
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import api  # noqa: E402
from api import obtener_incidentes, normalizar_numero  # noqa: E402
from config import materia as MATERIA  # noqa: E402
from config.config import PAUSA_SEG  # noqa: E402


def _norm(s: str) -> str:
    return (s or "").upper().replace("Á", "A").replace("É", "E").replace("Í", "I")\
        .replace("Ó", "O").replace("Ú", "U").replace("Ñ", "N")


def _slug(nombre: str) -> str:
    """Slug corto a partir del cantón/provincia que aparece en el nombre."""
    n = _norm(nombre)
    for marca in ("CANTON ", "SEDE EN ", "DE "):
        if marca in n:
            cola = n.split(marca)[-1].strip()
            if cola:
                return cola.split()[0].lower()
    return n.split()[-1].lower() if n else "judicatura"


def buscar_una_vez(numero: str) -> dict | None:
    """Una sola consulta a SATJE, SIN los reintentos de api.buscar_causa: al
    sondear miles de números inexistentes, cada reintento multiplica el
    tiempo por tres. Para el descubrimiento basta con un intento."""
    body = {
        "numeroCausa": normalizar_numero(numero),
        "actor": {"cedulaActor": "", "nombreActor": ""},
        "demandado": {"cedulaDemandado": "", "nombreDemandado": ""},
        "provincia": "", "canton": None, "materia": None, "dependencia": None,
        "numeroFiscalia": "", "first": 1, "pageSize": 10, "recaptcha": "verdad",
    }
    try:
        r = api._post(f"{api.BASE_CAUSAS}/informacion/buscarCausas?page=1&size=10",
                      headers=api.HEADERS, json=body, timeout=(8, 20))
        r.raise_for_status()
        data = r.json()
        return data[0] if data else None
    except Exception as e:
        print(f"  [aviso] {numero}: {type(e).__name__}: {e}")
        return None


def sondear_prefijo(prefijo: str, ano: str, intentos: int = 2) -> dict | None:
    """Devuelve {judicatura, idMateria, materia, muestra} o None si no hay causas."""
    for sec in range(1, intentos + 1):
        numero = f"{prefijo}-{ano}-{sec:05d}"
        causa = buscar_una_vez(numero)
        time.sleep(0.6)
        if not causa:
            continue
        incidentes = obtener_incidentes(normalizar_numero(numero)) or []
        time.sleep(0.6)
        nombre = ""
        for inc in incidentes:
            if str(inc.get("idJudicatura", "")) == prefijo:
                nombre = inc.get("nombreJudicatura", "")
                break
        if not nombre and incidentes:
            nombre = incidentes[0].get("nombreJudicatura", "")
        if not nombre:
            nombre = causa.get("nombreJudicatura", "") or "(sin nombre de judicatura)"
        return {
            "prefijo":   prefijo,
            "judicatura": nombre,
            "idMateria": causa.get("idMateria"),
            "materia":   causa.get("nombreMateria") or causa.get("materia") or "",
            "delito":    causa.get("nombreDelito", ""),
            "muestra":   numero,
            "incidentes": [i.get("nombreJudicatura", "") for i in incidentes],
        }
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--provincia", help="dos dígitos de provincia; barre NN000..NN999")
    ap.add_argument("--prefijos", nargs="*", help="prefijos concretos de 5 dígitos")
    ap.add_argument("--ano", default="2024")
    ap.add_argument("--desde", type=int, default=0)
    ap.add_argument("--hasta", type=int, default=999)
    ap.add_argument("--filtro", help="palabras separadas por coma que debe contener la judicatura")
    ap.add_argument("--todas", action="store_true")
    ap.add_argument("--csv")
    args = ap.parse_args()

    if args.prefijos:
        prefijos = [p.zfill(5) for p in args.prefijos]
    elif args.provincia:
        pv = args.provincia.zfill(2)
        prefijos = [f"{pv}{n:03d}" for n in range(args.desde, args.hasta + 1)]
    else:
        ap.error("indicá --provincia NN o --prefijos ...")

    if args.filtro:
        claves = [tuple(k.strip().upper() for k in args.filtro.split(","))]
        claves = [(k,) for k in claves[0]]          # cualquiera de las palabras
        def acepta(nombre): return any(all(x in _norm(nombre) for x in g) for g in claves)
    else:
        def acepta(nombre): return MATERIA.es_primera_instancia(nombre) or MATERIA.es_apelacion(nombre)

    encontradas = []
    print(f"Sondeando {len(prefijos)} prefijos, año {args.ano} … (Ctrl-C para cortar; lo hallado se imprime igual)\n", flush=True)
    csv_f = csv_w = None
    if args.csv:
        nuevo = not os.path.exists(args.csv) or os.path.getsize(args.csv) == 0
        csv_f = open(args.csv, "a", newline="", encoding="utf-8")
        csv_w = csv.writer(csv_f)
        if nuevo:
            csv_w.writerow(["prefijo", "judicatura", "idMateria", "materia", "muestra", "incidentes"])
            csv_f.flush()
    try:
        for i, pf in enumerate(prefijos, 1):
            if i % 50 == 0:
                print(f"  … {i}/{len(prefijos)} prefijos sondeados ({pf})", flush=True)
            r = sondear_prefijo(pf, args.ano)
            if not r:
                continue
            if args.todas or acepta(r["judicatura"]):
                encontradas.append(r)
                print(f"{r['prefijo']}  {r['judicatura'][:70]:70}  idMateria={r['idMateria']} ({r['materia'] or r['delito']})  muestra: {r['muestra']}", flush=True)
                if csv_w:
                    csv_w.writerow([r["prefijo"], r["judicatura"], r["idMateria"], r["materia"], r["muestra"], " | ".join(r["incidentes"])])
                    csv_f.flush()
    except KeyboardInterrupt:
        print("\n[interrumpido]")
    if csv_f:
        csv_f.close()
        print(f"\nCSV: {args.csv}")

    if not encontradas:
        print("\nNo se encontró ninguna judicatura que cumpla el filtro.")
        return 1

    primera = [r for r in encontradas if MATERIA.es_primera_instancia(r["judicatura"])] or encontradas
    ids = sorted({r["idMateria"] for r in primera if r["idMateria"] is not None})
    print("\n# ── Pegar en config/materia.py ───────────────────────────────────")
    print(f"IDS_MATERIA_SATJE = {{{', '.join(map(str, ids))}}}   # verificado {args.ano}")
    print("JUDICATURAS = {")
    for r in primera:
        print(f'    "{r["prefijo"]}": "{r["judicatura"].title()}",')
    print("}")
    print("JUDICATURAS_SLUG = {")
    usados = set()
    for r in primera:
        s = _slug(r["judicatura"])
        base, n = s, 2
        while s in usados:
            s = f"{base}{n}"; n += 1
        usados.add(s)
        print(f'    "{r["prefijo"]}": "{s}",')
    print("}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
