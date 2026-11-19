"""
Selecciona las causas del piloto de revisión (civil v2): sentencias LARGAS y de
tipos de proceso VARIADOS, como pidieron los abogados, para comprobar si la IA
falla con sentencias grandes y no encasillarse en cobros simples.

Solo lectura sobre la base del worker (tabla causa_texto). No llama a la IA ni a
SATJE. Imprime la lista y la línea JSON lista para POST /reextraer.

Correr en el shell del worker en Render (tiene DATABASE_URL):
    python scripts/seleccionar_piloto.py                 # 2 por tipo, ≥ 25.000 caracteres
    python scripts/seleccionar_piloto.py --por-tipo 1 --min-chars 40000
"""
import argparse
import json
import os

# (tipo, consulta websearch en español sobre el texto de primera instancia).
# Cubre los procedimientos y las instituciones del catálogo v2.
TIPOS = [
    ("Ejecutivo: pagaré o letra con excepciones", '"procedimiento ejecutivo" excepciones (pagaré OR "letra de cambio")'),
    ("Ejecutivo: cheque", '"procedimiento ejecutivo" cheque'),
    ("Monitorio", '"procedimiento monitorio"'),
    ("Ordinario: cobro de tarjeta o crédito impugnado", '"procedimiento ordinario" "tarjeta de crédito" impugna'),
    ("Reivindicación con reconvención", 'reivindicación reconvención'),
    ("Prescripción adquisitiva de dominio", '"prescripción adquisitiva" dominio'),
    ("Nulidad de contrato o escritura", '"nulidad absoluta" (escritura OR contrato)'),
    ("Simulación", 'simulación "absoluta" contrato'),
    ("Lesión enorme", '"lesión enorme"'),
    ("Promesa de compraventa: resolución o cumplimiento", '"promesa de compraventa" (resolución OR cumplimiento)'),
    ("Daños y perjuicios / responsabilidad extracontractual", '"daños y perjuicios" (accidente OR culpa OR negligencia) -tarjeta'),
    ("Daño moral", '"daño moral" indemnización'),
    ("Inquilinato: desahucio o terminación", '(inquilinato OR desahucio) arrendamiento'),
    ("Partición o sociedad conyugal", 'partición (bienes OR "sociedad conyugal")'),
    ("Acciones posesorias / servidumbres", '("amparo posesorio" OR servidumbre OR "obra nueva")'),
    ("Sucesiones / testamento", '(testamento OR "petición de herencia" OR "partición hereditaria")'),
    ("Societario: junta general o velo", '("junta general" OR "velo societario" OR "Ley de Compañías") impugnación'),
    ("Seguros", '(siniestro OR póliza) aseguradora'),
    ("Rendición de cuentas / mandato", '"rendición de cuentas" mandatario'),
    ("Tercería o medidas cautelares", '(tercería OR "medida cautelar") embargo'),
    ("Prescripción extintiva alegada", '"prescripción extintiva" excepción'),
    ("Insolvencia / concurso", '(insolvencia OR "concurso de acreedores")'),
    ("Nulidad de laudo arbitral", '"nulidad del laudo" OR "laudo arbitral"'),
]

SQL = """
SELECT t.numero, length(t.texto) AS largo,
       EXISTS (SELECT 1 FROM causa_texto a WHERE a.numero = t.numero AND a.nivel = 'apelacion') AS apel,
       EXISTS (SELECT 1 FROM causa_texto c WHERE c.numero = t.numero AND c.nivel = 'corte')     AS cas
FROM causa_texto t
WHERE t.nivel = 'tribunal'
  AND t.tsv @@ websearch_to_tsquery('spanish', %s)
  AND length(t.texto) >= %s
  AND NOT (t.numero = ANY(%s))
ORDER BY (EXISTS (SELECT 1 FROM causa_texto a WHERE a.numero = t.numero AND a.nivel <> 'tribunal')) DESC,
         length(t.texto) DESC
LIMIT %s
"""


def seleccionar(cur, por_tipo: int, min_chars: int, excluir: list) -> list:
    """Por cada tipo, las causas más largas; primero las que también tienen
    apelación o casación (así el piloto prueba los tres niveles). Una causa no se
    repite entre tipos."""
    elegidas, vistos = [], list(excluir)
    for tipo, consulta in TIPOS:
        cur.execute(SQL, (consulta, min_chars, vistos, por_tipo))
        for numero, largo, apel, cas in cur.fetchall():
            vistos.append(numero)
            elegidas.append({"tipo": tipo, "numero": numero, "caracteres": largo,
                             "apelacion": apel, "casacion": cas})
    return elegidas


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--por-tipo", type=int, default=2)
    ap.add_argument("--min-chars", type=int, default=25000, help="largo mínimo del texto de primera instancia")
    ap.add_argument("--excluir", default="", help="números separados por coma (p. ej., las causas del piloto v1)")
    a = ap.parse_args()
    import psycopg2
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    conn.set_session(readonly=True)
    excluir = [n.strip() for n in a.excluir.split(",") if n.strip()]
    with conn.cursor() as cur:
        filas = seleccionar(cur, a.por_tipo, a.min_chars, excluir)
    for f in filas:
        niveles = "+".join(["1.ª"] + (["apelación"] if f["apelacion"] else []) + (["casación"] if f["casacion"] else []))
        print(f'{f["tipo"][:48]:48}  {f["numero"]:20}  {f["caracteres"]:>8,} car.  {niveles}')
    print(f"\n{len(filas)} causas. Cuerpo para POST /reextraer:")
    print(json.dumps({"numeros": [f["numero"] for f in filas], "niveles": ["tribunal", "apelacion", "corte"],
                      "modelo": "gpt-4o", "iniciado_por": "piloto-civil-v2"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
