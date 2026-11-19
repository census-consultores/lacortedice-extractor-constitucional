"""
Backfill de PDFs de sentencia (tribunal + casación) SIN re-correr la IA.

Contexto: el motor de lista (lista_worker) no llamaba a _subir_archivos_strapi,
así que las causas/casaciones creadas por lista quedaron SIN el PDF adjunto
(file_pdf vacío). Este script baja el PDF de SATJE y lo vincula a la causa/
casación YA existente, sin tocar ningún otro campo (no re-extrae, no gasta IA).

Cómo funciona por cada juicio candidato:
  1. procesar_causa(gpt_ok=False, gpt_cnj_ok=False) → baja los PDFs de SATJE
     (tribunal y CNJ) sin llamar a la IA.
  2. _subir_archivos_strapi → sube a Strapi y setea _file_id_pdf_*.
  3. PUT /api/causas/{doc}      {"data": {"file_pdf": <id>}}   (solo si faltaba)
     PUT /api/casaciones/{doc}  {"data": {"file_pdf": <id>}}   (solo si faltaba)

Es idempotente y resumible: relee de la base qué sigue sin PDF, así que se
puede correr varias veces; lo ya vinculado deja de aparecer. Si SATJE no tiene
el PDF, se salta (esa causa legítimamente no tiene documento).

Uso:
  python scripts/backfill_pdfs.py            # todo
  python scripts/backfill_pdfs.py --limit 2  # prueba
  python scripts/backfill_pdfs.py --solo 09501-2019-00432
"""
import argparse
import os
import re
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# DATABASE_URL desde la línea comentada de config/.env (base "lacortedice")
_env = (ROOT / "config" / ".env").read_text()
os.environ["DATABASE_URL"] = re.search(
    r"#\s*DATABASE_URL=(postgresql://\S+/lacortedice)\b", _env
).group(1)

import psycopg2  # noqa: E402

from resolucion.orquestador import procesar_causa  # noqa: E402
from worker.runner import _subir_archivos_strapi  # noqa: E402
from worker.strapi_client import StrapiClient  # noqa: E402
from config.config import PROJECT_ROOT, TRIBUNALES  # noqa: E402


def _cargar_candidatos(solo: str | None):
    """Devuelve {juicio: {causa_doc, cas_doc, need_causa, need_cas}}."""
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()
    cand: dict = {}

    filtro_solo = "AND ca.juicio = %s" if solo else ""
    args = (solo,) if solo else ()

    # Causas con sentencia real pero sin file_pdf
    cur.execute(f"""
        SELECT ca.juicio, ca.document_id
        FROM causas ca
        WHERE COALESCE(TRIM(ca.resumen_del_caso),'') <> ''
          AND ca.resumen_del_caso NOT ILIKE '%%sin sentencia de fondo%%'
          AND NOT EXISTS (SELECT 1 FROM files_related_mph m
                          WHERE m.related_type='api::causa.causa'
                            AND m.related_id=ca.id AND m.field='file_pdf')
          {filtro_solo}
    """, args)
    for juicio, doc in cur.fetchall():
        cand.setdefault(juicio, {"causa_doc": None, "cas_doc": None,
                                 "need_causa": False, "need_cas": False})
        cand[juicio]["causa_doc"] = doc
        cand[juicio]["need_causa"] = True

    # Casaciones sin file_pdf (vía la causa a la que cuelgan, para saber el juicio)
    filtro_solo2 = "AND cau.juicio = %s" if solo else ""
    cur.execute(f"""
        SELECT cau.juicio, cas.document_id
        FROM casaciones cas
        JOIN casaciones_causa_lnk l ON l.casacion_id = cas.id
        JOIN causas cau ON cau.id = l.causa_id
        WHERE NOT EXISTS (SELECT 1 FROM files_related_mph m
                          WHERE m.related_type='api::casacion.casacion'
                            AND m.related_id=cas.id AND m.field='file_pdf')
          {filtro_solo2}
    """, args)
    for juicio, doc in cur.fetchall():
        cand.setdefault(juicio, {"causa_doc": None, "cas_doc": None,
                                 "need_causa": False, "need_cas": False})
        cand[juicio]["cas_doc"] = doc
        cand[juicio]["need_cas"] = True

    cur.close()
    conn.close()
    return cand


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="máximo de juicios (0 = todos)")
    ap.add_argument("--solo", type=str, default=None, help="un solo juicio")
    args = ap.parse_args()

    cand = _cargar_candidatos(args.solo)
    juicios = sorted(cand)
    if args.limit:
        juicios = juicios[: args.limit]

    total = len(juicios)
    print(f"candidatos: {total} juicios "
          f"({sum(c['need_causa'] for c in cand.values())} causas, "
          f"{sum(c['need_cas'] for c in cand.values())} casaciones)")
    if not total:
        return

    strapi = StrapiClient()
    carpeta = PROJECT_ROOT / "resultados" / "pdfs" / "resolucion" / "backfill-pdfs"
    carpeta.mkdir(parents=True, exist_ok=True)

    fixed_c = fixed_k = sin_pdf = errores = 0
    for i, juicio in enumerate(juicios, 1):
        info = cand[juicio]
        codigo = juicio.split("-")[0] if "-" in juicio else ""
        ctx = {
            "tribunal": codigo, "nombre_tribunal": TRIBUNALES.get(codigo, codigo),
            "ano": "", "corrida_id": "backfill-pdfs",
            "carpeta_local": carpeta, "carpeta_docx": carpeta,
            "gpt_ok": False, "gpt_cnj_ok": False, "skip_pdf": False,
            "modelo": "", "flujo_proveedores": "alibaba", "job_id": None,
        }
        try:
            fila = procesar_causa(juicio, ctx)
            # No subir el PDF que ya existe (evita media huérfana en Strapi):
            # solo dejamos los bytes del que realmente falta vincular.
            if not info["need_causa"]:
                fila.pop("_pdf_bytes_tribunal", None)
            if not info["need_cas"]:
                fila.pop("_pdf_bytes_cnj", None)
            _subir_archivos_strapi(fila, strapi, None)

            hecho = []
            if info["need_causa"] and info["causa_doc"]:
                fid = fila.get("_file_id_pdf_tribunal")
                if fid:
                    strapi._put(f"/api/causas/{info['causa_doc']}",
                                {"data": {"file_pdf": fid}})
                    fixed_c += 1
                    hecho.append("causa PDF ✓")
                else:
                    hecho.append("causa sin PDF en SATJE")
                    sin_pdf += 1
            if info["need_cas"] and info["cas_doc"]:
                fid = fila.get("_file_id_pdf_cnj")
                if fid:
                    strapi._put(f"/api/casaciones/{info['cas_doc']}",
                                {"data": {"file_pdf": fid}})
                    fixed_k += 1
                    hecho.append("casación PDF ✓")
                else:
                    hecho.append("casación sin PDF en SATJE")
                    sin_pdf += 1
            print(f"[{i}/{total}] {juicio} → {' · '.join(hecho) or 'nada'}")
        except Exception as e:
            errores += 1
            print(f"[{i}/{total}] {juicio} → ERROR {type(e).__name__}: {e}")
            traceback.print_exc()
        time.sleep(0.5)

    print(f"\nlisto — causas {fixed_c} · casaciones {fixed_k} · "
          f"sin PDF en SATJE {sin_pdf} · errores {errores}")


if __name__ == "__main__":
    main()
