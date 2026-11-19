"""
Indexa en `causa_texto` el texto de los PDF de sentencia YA guardados en Strapi
que todavía no estén indexados (los nuevos se indexan solos al descargarse).
Después, GET /buscar (o la página /buscador) responde en segundos a
"¿qué sentencias hablan de X?" sin volver a abrir ningún PDF.

Correrlo desde el shell del worker en Render (tiene DATABASE_URL y STRAPI_URL):

  nohup python -u scripts/indexar_textos.py > /tmp/indexar.log 2>&1 &
  python scripts/indexar_textos.py --tribunales 09332,17230,17233 --limite 100

Resumible: solo toma lo que falta, así que se puede cortar y relanzar.
"""
import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PAUSA_SEG = 0.2  # el CMS atiende al worker en paralelo; no saturarlo

NIVELES = (("tribunal", "url_pdf_tribunal"), ("apelacion", "url_pdf_apel"), ("corte", "url_pdf_cnj"))


def pendientes(tribunales: list[str], limite: int | None) -> list[tuple[str, str, str]]:
    """(numero, nivel, url) de los PDF guardados sin fila en causa_texto."""
    from worker.job_manager import _conn
    partes = [f"""
        SELECT d.numero, '{nivel}' AS nivel, d.{col} AS url
        FROM worker_causa_datos d
        LEFT JOIN worker_causas c ON c.numero = d.numero
        LEFT JOIN causa_texto t ON t.numero = d.numero AND t.nivel = '{nivel}'
        WHERE COALESCE(d.{col}, '') <> '' AND t.numero IS NULL
        {"AND c.tribunal_codigo = ANY(%s)" if tribunales else ""}
    """ for nivel, col in NIVELES]
    sql = " UNION ALL ".join(partes) + " ORDER BY numero, nivel"
    params = [tribunales] * len(NIVELES) if tribunales else []
    if limite:
        sql += " LIMIT %s"; params.append(limite)
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tribunales", default="", help="códigos separados por coma; vacío = todos")
    ap.add_argument("--limite", type=int, default=None)
    args = ap.parse_args()

    from resolucion.ai._cliente import extraer_texto_pdf
    from worker.job_manager import guardar_texto, init_db
    from worker.reextraccion_worker import descargar_pdf_guardado

    init_db()  # crea causa_texto si el worker desplegado aún no lo hizo
    tribunales = [t.strip() for t in args.tribunales.split(",") if t.strip()]
    tareas = pendientes(tribunales, args.limite)
    print(f"{len(tareas)} PDF por indexar")
    ok = sin_texto = 0
    for i, (numero, nivel, url) in enumerate(tareas, 1):
        pdf = descargar_pdf_guardado(url, causa=numero)
        texto = extraer_texto_pdf(pdf, causa=numero) if pdf else ""
        if texto:
            try:
                guardar_texto(numero, nivel, texto)
                ok += 1
            except Exception as e:  # p. ej. texto que excede el límite del índice; no detiene la corrida
                sin_texto += 1
                print(f"  [error] {numero} {nivel}: {type(e).__name__}: {str(e)[:120]}")
        else:
            sin_texto += 1  # escaneado o no descargable: vuelve a intentarse en la próxima corrida
            print(f"  [sin texto] {numero} {nivel}")
        if i % 200 == 0:
            print(f"{i}/{len(tareas)} — {ok} indexados, {sin_texto} sin texto")
        time.sleep(PAUSA_SEG)
    print(f"Listo: {ok} indexados, {sin_texto} sin texto")


if __name__ == "__main__":
    main()
