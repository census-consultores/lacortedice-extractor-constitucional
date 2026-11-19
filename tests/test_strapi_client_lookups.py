"""
Tests de parsing/lookups puntuales de worker/strapi_client.py:
  - _split_jurisprudencia: no debe filtrar el título del prompt al blob guardado.
  - _lookup_causales: detecta causales invocadas (Art. 268 COGEP) en la
    sección 8 de la extracción CNJ, en formato numeral u ordinal.
  - _resolver_recurrente: resuelve "la parte actora"/"el demandado" al
    nombre real ya conocido de la causa.
"""
import worker.strapi_client as sc


# ── _split_jurisprudencia ────────────────────────────────────────────────────

def test_split_jurisprudencia_descarta_titulo_del_prompt_cnj():
    blob = (
        "EXTRACCIÓN DE JURISPRUDENCIA Y CRITERIOS DE LOS JUECES DE LA CORTE "
        "NACIONAL DE JUSTICIA\n"
        "1. Jurisprudencia mencionada en el caso:\n\n"
        "Caso / referencia: Resolución No. 02-2020\n"
        "2. Criterio del tribunal para resolver el caso:\n"
        "El criterio real.\n"
    )
    citada, criterio = sc._split_jurisprudencia(blob)
    assert citada.startswith("Caso / referencia:")
    assert "EXTRACCIÓN DE JURISPRUDENCIA" not in citada
    assert criterio == "El criterio real."


def test_split_jurisprudencia_descarta_titulo_del_prompt_tribunal():
    blob = (
        "JURISPRUDENCIA Y CRITERIOS DEL TRIBUNAL\n"
        "1. Jurisprudencia mencionada en el caso\n"
        "No consta en la sentencia.\n"
        "2. Criterio del tribunal para resolver el caso:\n"
        "Este Tribunal considera que...\n"
    )
    citada, criterio = sc._split_jurisprudencia(blob)
    assert citada == "No consta en la sentencia."
    assert "JURISPRUDENCIA Y CRITERIOS DEL TRIBUNAL" not in citada
    assert criterio == "Este Tribunal considera que..."


def test_split_jurisprudencia_sin_titulo_sigue_funcionando():
    blob = (
        "1. Jurisprudencia mencionada en el caso:\n"
        "Cita X.\n"
        "2. Criterio del tribunal para resolver el caso:\n"
        "Criterio Y.\n"
    )
    citada, criterio = sc._split_jurisprudencia(blob)
    assert citada == "Cita X."
    assert criterio == "Criterio Y."


# ── _lookup_causales ─────────────────────────────────────────────────────────

_CATALOGO_CAUSALES = [
    {"documentId": "c1", "nombre": "Causal 1 -  Aplicación indebida..."},
    {"documentId": "c2", "nombre": "Causal  2 - Ausencia de requisitos legales..."},
    {"documentId": "c5", "nombre": "Causal 5 - Aplicación indebida de normas sustantivas..."},
]


def test_lookup_causales_formato_numeral(monkeypatch):
    sc._CACHE_CAUSALES = list(_CATALOGO_CAUSALES)
    client = sc.StrapiClient()
    ids = client._lookup_causales("CAUSAL 5 - Aplicación indebida de normas sustantivas.")
    assert ids == ["c5"]


def test_lookup_causales_formato_ordinal(monkeypatch):
    sc._CACHE_CAUSALES = list(_CATALOGO_CAUSALES)
    client = sc.StrapiClient()
    ids = client._lookup_causales("La parte recurrente invoca la causal quinta del artículo 268 del COGEP.")
    assert ids == ["c5"]


def test_lookup_causales_multiples(monkeypatch):
    sc._CACHE_CAUSALES = list(_CATALOGO_CAUSALES)
    client = sc.StrapiClient()
    ids = client._lookup_causales("CAUSAL 1 - ...\nCAUSAL 5 - Aplicación indebida de normas sustantivas.")
    assert set(ids) == {"c1", "c5"}


def test_lookup_causales_vacio_no_rompe():
    sc._CACHE_CAUSALES = list(_CATALOGO_CAUSALES)
    client = sc.StrapiClient()
    assert client._lookup_causales("") == []
    assert client._lookup_causales("No consta en la sentencia.") == []


# ── _resolver_recurrente ─────────────────────────────────────────────────────

def test_resolver_recurrente_parte_actora(monkeypatch):
    client = sc.StrapiClient()
    llamados = []
    monkeypatch.setattr(client, "_lookup_or_create_recurrente", lambda n: llamados.append(n) or "doc-1")
    doc = client._resolver_recurrente("La parte actora", "ADISOL CIA.LTDA.", "SRI")
    assert doc == "doc-1"
    assert llamados == ["ADISOL CIA.LTDA."]


def test_resolver_recurrente_parte_demandada(monkeypatch):
    client = sc.StrapiClient()
    llamados = []
    monkeypatch.setattr(client, "_lookup_or_create_recurrente", lambda n: llamados.append(n) or "doc-2")
    doc = client._resolver_recurrente("El SRI, parte demandada, interpone el recurso", "ADISOL CIA.LTDA.", "SRI")
    assert doc == "doc-2"
    assert llamados == ["SRI"]


def test_resolver_recurrente_nombre_propio_directo(monkeypatch):
    client = sc.StrapiClient()
    llamados = []
    monkeypatch.setattr(client, "_lookup_or_create_recurrente", lambda n: llamados.append(n) or "doc-3")
    doc = client._resolver_recurrente("Banco del Austro S.A.", "Banco del Austro S.A.", "SRI")
    assert doc == "doc-3"
    assert llamados == ["Banco del Austro S.A."]


def test_resolver_recurrente_no_consta_no_llama(monkeypatch):
    client = sc.StrapiClient()
    monkeypatch.setattr(client, "_lookup_or_create_recurrente", lambda n: (_ for _ in ()).throw(AssertionError("no debería llamarse")))
    assert client._resolver_recurrente("No consta en la sentencia", "A", "B") is None


# ── _lookup_or_create_entidad ────────────────────────────────────────────────

def test_lookup_entidad_matchea_catalogo_limpio_contra_texto_largo_satje():
    # El "Demandado" de SATJE trae el cargo + representante legal, no el
    # nombre limpio de la entidad — el match debe encontrar la entidad
    # corta ya existente en el catálogo, no crear un duplicado.
    sc._CACHE_ENTIDADES = [
        {"documentId": "e-sri", "nombre": "Servicio de Rentas Internas"},
        {"documentId": "e-old", "nombre": "directora general del servicio de rentas internas- ec. marisol andrade hernandez"},
    ]
    client = sc.StrapiClient()
    doc = client._lookup_or_create_entidad(
        "directora general del servicio de rentas internas- ec. marisol andrade hernandez"
    )
    assert doc == "e-sri"


def test_lookup_entidad_sin_match_crea_una_nueva(monkeypatch):
    sc._CACHE_ENTIDADES = [{"documentId": "e-sri", "nombre": "Servicio de Rentas Internas"}]
    client = sc.StrapiClient()
    monkeypatch.setattr(
        client, "_post",
        lambda path, payload: {"data": {"documentId": "e-new"}},
    )
    doc = client._lookup_or_create_entidad("Compañía Nueva S.A.")
    assert doc == "e-new"


def test_lookup_entidad_no_consta_no_hace_nada():
    client = sc.StrapiClient()
    assert client._lookup_or_create_entidad("No consta en la sentencia") is None
    assert client._lookup_or_create_entidad("") is None


# ── _clasificar_decision_casacion ────────────────────────────────────────────

def test_clasificar_decision_acepta_recurso_y_declara_nulo_no_es_se_caso():
    # Caso real de produccion: la CNJ acepta el recurso pero declara nulo
    # el fallo y remite al tribunal de origen (no resuelve el fondo) - el
    # resultado oficial de SATJE para esta causa decia "No se caso", asi
    # que tratar "acepta el recurso"/"declara nulo" como "Se caso" era un
    # error. Sin evidencia mas fuerte, la clasificacion queda vacia y
    # manda el resultado de SATJE (fuente autorizada) en guardar_casacion.
    texto = (
        "Se acepta el recurso de casación interpuesto por el Servicio de "
        "Rentas Internas, y se declara nulo el fallo recurrido y todo lo "
        "actuado desde la calificación de la demanda, inclusive. Se ordena "
        "remitir el proceso al Tribunal de origen."
    )
    assert sc._clasificar_decision_casacion(texto) == ""


def test_clasificar_decision_literal_casa_la_sentencia():
    assert sc._clasificar_decision_casacion("Casa la sentencia recurrida.") == "Se casó"


def test_clasificar_decision_no_casa():
    assert sc._clasificar_decision_casacion("No casa la sentencia recurrida.") == "No se casó"


def test_clasificar_decision_no_acepta_el_recurso():
    assert sc._clasificar_decision_casacion("No se acepta el recurso interpuesto.") == "No se casó"


def test_clasificar_decision_parcial():
    assert sc._clasificar_decision_casacion(
        "Se acepta parcialmente el recurso de casación."
    ) == "Se casó parcialmente"


def test_clasificar_decision_vacio():
    assert sc._clasificar_decision_casacion("") == ""
    assert sc._clasificar_decision_casacion("No consta en la sentencia") == ""


def test_lookup_decision_final_casacion_usa_clasificacion(monkeypatch):
    sc._CACHE_DECISION_CASACION = [
        {"documentId": "d-si", "nombre": "Se casó"},
        {"documentId": "d-no", "nombre": "No se casó"},
        {"documentId": "d-parcial", "nombre": "Se casó parcialmente"},
    ]
    client = sc.StrapiClient()
    texto = "Casa la sentencia recurrida."
    clasif = sc._clasificar_decision_casacion(texto)
    doc = client._lookup_decision_final_casacion(clasif or texto)
    assert doc == "d-si"


def test_lookup_decision_final_casacion_nulidad_no_matchea_nada(monkeypatch):
    # "acepta el recurso... declara nulo" ya no se clasifica como "Se caso"
    # (ver test_clasificar_decision_acepta_recurso_y_declara_nulo_no_es_se_caso)
    # - sin clasificacion, el texto crudo tampoco matchea ningun candidato
    # del catalogo, asi que no se fuerza una decision_final incorrecta.
    sc._CACHE_DECISION_CASACION = [
        {"documentId": "d-si", "nombre": "Se casó"},
        {"documentId": "d-no", "nombre": "No se casó"},
        {"documentId": "d-parcial", "nombre": "Se casó parcialmente"},
    ]
    client = sc.StrapiClient()
    texto = "Se acepta el recurso de casación y se declara nulo el fallo recurrido."
    clasif = sc._clasificar_decision_casacion(texto)
    doc = client._lookup_decision_final_casacion(clasif or texto)
    assert doc is None


def test_clasificar_decision_reconoce_frases_de_satje():
    # detectar_sentencia_cnj (resolucion/satje/corte.py) devuelve estas
    # frases exactas segun el tipo de actuacion del propio SATJE.
    assert sc._clasificar_decision_casacion("Casa la sentencia.") == "Se casó"
    assert sc._clasificar_decision_casacion("No casa la sentencia.") == "No se casó"
    assert sc._clasificar_decision_casacion("Casa parcialmente la sentencia.") == "Se casó parcialmente"


# ── _parsear_secciones_cnj / _recortar_secciones_embebidas ──────────────────
# El modelo a veces omite el número de UNA sección intermedia (ej. escribe
# "Causal o causales invocadas:" en vez de "8. Causal..."). El parser
# numerado no corta ahí, y ese contenido queda atrapado al final de la
# sección anterior en vez de figurar bajo su propio número.

_EXTRACCION_CNJ_CON_SECCIONES_SIN_NUMERO = """
7. Recurrente
Servicio de Rentas Internas
Causal o causales invocadas:
• CAUSAL 1 - Aplicación indebida, falta de aplicación o errónea interpretación que cause nulidad insubsanable o indefensión grave.



• CAUSAL 5 - Aplicación indebida, falta de aplicación o errónea interpretación de normas sustantivas o precedentes obligatorios que incidan en el fallo.
9. Argumentos del Recurrente
• El tribunal inferior interpretó erróneamente el artículo 10 de la LRTI.

10. Argumentos de la Contraparte
Los gastos sí cumplen los requisitos legales.

11. Decisión Final
Casa la sentencia.

12. Medidas Adicionales
Se ordena remitir dentro del término de treinta días el proceso al Tribunal de origen. No existen costas.

Voto salvado:

14. Argumento del Voto Salvado
No consta en la sentencia.
"""


def test_recorta_causal_embebida_en_recurrente():
    secs = sc._parsear_secciones_cnj(_EXTRACCION_CNJ_CON_SECCIONES_SIN_NUMERO)
    assert secs[7].strip() == "Servicio de Rentas Internas"
    assert "CAUSAL 1" in secs[8]
    assert "CAUSAL 5" in secs[8]
    assert "Argumentos del Recurrente" not in secs[8]


def test_recorta_voto_salvado_embebido_en_medidas_adicionales():
    secs = sc._parsear_secciones_cnj(_EXTRACCION_CNJ_CON_SECCIONES_SIN_NUMERO)
    assert "Voto salvado" not in secs[12]
    assert "remitir dentro del término" in secs[12]


def test_lookup_causales_sobre_seccion_recortada():
    secs = sc._parsear_secciones_cnj(_EXTRACCION_CNJ_CON_SECCIONES_SIN_NUMERO)
    sc._CACHE_CAUSALES = list(_CATALOGO_CAUSALES)
    client = sc.StrapiClient()
    ids = client._lookup_causales(secs.get(8, ""))
    assert set(ids) == {"c1", "c5"}


# ── _TITULOS_TRIBUNAL (prompt 2, 19 secciones) ──────────────────────────────
# Mismo problema que en CNJ, pero en la extracción del TRIBUNAL: el modelo
# escribe "Argumentos de la Contraparte" (terminología de CNJ) en vez de
# "Argumentos de la Parte Demandada" (la que pide el prompt 2), y omite el
# número — el contenido de 3 secciones distintas queda atrapado en la 14.

_EXTRACCION_TRIBUNAL_CON_SECCIONES_SIN_NUMERO = """
14. Argumentos de la Parte Actora
• El actor alega que la circular vulnera sus derechos.

NORMATIVA APLICADA:
Artículo 185.2 del Código Orgánico de la Función Judicial; artículo 320.2 del Código Orgánico General de Procesos.

Argumentos de la Contraparte (ADITIVOS SOLVENTES Y SUSTANCIAS QUÍMICAS ADISOL CIA. LTDA.):
• El demandado sostiene que no hay vulneración.

NORMATIVA APLICADA:
Artículo 273 del COGEP; artículo 101 de la Ley de Régimen Tributario Interno.

Decisión final:
Declara con lugar.

17. Disposiciones Adicionales
No constan disposiciones adicionales.

18. Voto Salvado
No consta en la sentencia.
"""


def test_recorta_secciones_embebidas_en_tribunal():
    secs = sc._recortar_secciones_embebidas(
        sc._parsear_secciones(_EXTRACCION_TRIBUNAL_CON_SECCIONES_SIN_NUMERO),
        sc._TITULOS_TRIBUNAL,
    )
    assert "Contraparte" not in secs[14]
    assert "Decisión final" not in secs[14]
    assert "El demandado sostiene" in secs[15]
    assert "Decisión final" not in secs[15]
    assert secs[16].strip() == "Declara con lugar."


# ── Duración (tribunal + CNJ) llega al payload de Strapi ────────────────────

def _sin_lookups_red(client, monkeypatch):
    """Evita llamadas HTTP reales en _mapear_causa/guardar_casacion."""
    monkeypatch.setattr(client, "_lookup_decision_final", lambda t: None)
    monkeypatch.setattr(client, "_lookup_decision_final_casacion", lambda t: None)
    monkeypatch.setattr(client, "_lookup_tipo_accion", lambda t: None)
    monkeypatch.setattr(client, "_lookup_tribunal", lambda t: None)
    monkeypatch.setattr(client, "_lookup_or_create_entidad", lambda t, adm=None: None)
    monkeypatch.setattr(client, "_lookup_estado_juicio", lambda t: None)
    monkeypatch.setattr(client, "_lookup_or_create_juez", lambda n: None)
    monkeypatch.setattr(client, "_lookup_or_create_abogado", lambda n: None)
    monkeypatch.setattr(client, "_lookup_categorias", lambda t: [])
    monkeypatch.setattr(client, "_resolver_recurrente", lambda *a, **kw: None)
    monkeypatch.setattr(client, "_lookup_causales", lambda t: [])


def test_mapear_causa_incluye_duracion_tribunal(monkeypatch):
    client = sc.StrapiClient()
    _sin_lookups_red(client, monkeypatch)
    fila = {
        "Número de juicio": "09501-2019-00010",
        "_fecha_inicio_caso": "2019-01-10",
        "_fecha_sentencia_caso": "2019-04-22",
        "_duracion_dias": 102,
    }
    payload = client._mapear_causa(fila)["data"]
    assert payload["fecha_inicio_caso"] == "2019-01-10"
    assert payload["fecha_sentencia_caso"] == "2019-04-22"
    assert payload["duracion_dias"] == 102


def test_mapear_causa_sin_duracion_no_manda_los_campos():
    client = sc.StrapiClient()
    payload = client._mapear_causa({"Número de juicio": "09501-2019-00010"})["data"]
    assert "fecha_inicio_caso" not in payload
    assert "fecha_sentencia_caso" not in payload
    assert "duracion_dias" not in payload


def test_guardar_casacion_incluye_duracion_cnj(monkeypatch):
    client = sc.StrapiClient()
    _sin_lookups_red(client, monkeypatch)
    monkeypatch.setattr(client, "_buscar_casacion", lambda x: None)
    guardado = {}
    monkeypatch.setattr(
        client, "_post",
        lambda path, payload: guardado.update(payload.get("data", {})) or {"data": {"documentId": "c-1"}},
    )
    fila = {
        "Actor": "A", "Demandado": "B",
        "_fecha_inicio_cnj": "2023-06-09",
        "_fecha_sentencia_cnj": "2023-10-10",
        "_duracion_dias_cnj": 123,
    }
    client.guardar_casacion(fila, "causa-1")
    assert guardado["fecha_inicio_cnj"] == "2023-06-09"
    assert guardado["fecha_sentencia_cnj"] == "2023-10-10"
    assert guardado["duracion_dias_cnj"] == 123


def test_guardar_casacion_prioriza_resultado_satje_sobre_extraccion_ia(monkeypatch):
    """
    fila["Resultado en Corte"] (detectado por SATJE, confiable) debe
    ganarle a la seccion 11 de la extraccion IA cuando ambos existen —
    y debe funcionar aunque la extraccion IA este vacia o sea ambigua.
    """
    sc._CACHE_DECISION_CASACION = [
        {"documentId": "d-si", "nombre": "Se casó"},
        {"documentId": "d-no", "nombre": "No se casó"},
        {"documentId": "d-parcial", "nombre": "Se casó parcialmente"},
    ]
    client = sc.StrapiClient()
    monkeypatch.setattr(client, "_buscar_casacion", lambda x: None)
    guardado = {}
    monkeypatch.setattr(client, "_post", lambda path, payload: guardado.update(payload.get("data", {})) or {"data": {"documentId": "c-1"}})
    monkeypatch.setattr(client, "_parsear_jueces_seccion", lambda s: ("", []))
    monkeypatch.setattr(client, "_lookup_or_create_juez", lambda n: None)
    monkeypatch.setattr(client, "_resolver_recurrente", lambda *a, **kw: None)
    monkeypatch.setattr(client, "_lookup_causales", lambda t: [])

    fila = {
        "Resultado en Corte": "Casa la sentencia.",
        "_extraccion_cnj": "11. Decisión Final\nTexto ambiguo sin verbo casar ni acepta.\n",
        "Actor": "A", "Demandado": "B",
    }
    client.guardar_casacion(fila, "causa-1")
    assert guardado.get("decision_final") == "d-si"
