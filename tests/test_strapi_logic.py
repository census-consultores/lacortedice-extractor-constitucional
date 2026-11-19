"""
Test de lógica pura (sin Strapi real): parseo de secciones, jueces y payload.
"""
import sys, os
sys.path.insert(0, "/Users/matildeantepara/Documents/census-dev/lacortedice/tribunal-worker")

from worker.strapi_client import (
    StrapiClient, _parsear_secciones, _split_pruebas, _normalizar, _parsear_cuantia,
    _split_jurisprudencia,
)

VERDE  = "\033[92m"
ROJO   = "\033[91m"
AMARIL = "\033[93m"
RESET  = "\033[0m"

def ok(msg):   print(f"  {VERDE}✓{RESET} {msg}")
def err(msg):  print(f"  {ROJO}✗{RESET} {msg}")
def warn(msg): print(f"  {AMARIL}~{RESET} {msg}")
def chk(label, valor, esperado):
    if valor == esperado:
        ok(f"{label}: {valor!r}")
    else:
        err(f"{label}: esperado {esperado!r}, got {valor!r}")

EXTRACCION_TRIBUNAL = """
1. Instancia:
Primera instancia.

2. Tribunal que emite la sentencia:
Tribunal Distrital de lo Contencioso Tributario del Guayas

3. Jueces que intervinieron:
● Juez Ponente: COHN ZURITA FERNANDO ANTONIO
● Otros Jueces: NÉSTOR PORFIRIO GÓMEZ JARAMILLO, CARLOS RENÉ FERRÍN DE LA TORRE

4. Fecha de Emisión:
15 de junio de 2021

5. Partes Procesales:
Actor: BEBIDAS Y AGUAS GLACIALES S.A. BEISA
Demandado: DIRECTORA GENERAL DEL SERVICIO DE RENTAS INTERNAS

6. Tipo de Acción:
Acción de impugnación

7. Cuantía del Caso:
USD 2'955.797,07.

8. Detalle de la Cuantía:
* Glosa por comisiones locales (Casillero 7322): USD 19.604,37.
* Glosa por depreciación de propiedades no acelerada (Casillero 7512): USD 46.860,14.

9. Acto Impugnado:
Resolución No. 109012019RREC001234 emitida por la Directora General del SRI

10. Otros Actos Administrativos Relacionados:
Liquidación de pago por diferencias en declaración

11. Objeto de la Controversia:
Impugnación de acto administrativo tributario emitido por el SRI

12. Antecedentes:
La empresa BEISA presentó la declaración del impuesto a la renta correspondiente al ejercicio fiscal 2017. El SRI emitió una resolución determinativa por diferencias entre lo declarado y lo establecido en la auditoría.

13. Pruebas Presentadas:
Por el Actor: Declaración de impuesto a la renta, estados financieros, facturas de compra.
Por el Demandado: Resolución administrativa, acta de determinación, informes de auditoría.

14. Argumentos de la Parte Actora:
Los valores objetados por el SRI no consideran los gastos deducibles correctamente registrados en la contabilidad de la empresa.

15. Argumentos de la Parte Demandada:
Los gastos objetados no cumplen los requisitos del artículo 10 de la LRTI al no contar con sustento documental suficiente.

16. Decisión Final:
Con lugar

17. Disposiciones Adicionales:
Se dispone la devolución del impuesto pagado en exceso con intereses legales desde la fecha de pago.

18. Voto Salvado:
No consta en la sentencia.

19. Argumento del Juez sobre el Voto Salvado:
No consta en la sentencia.
"""

EXTRACCION_CNJ = """
1. Número de Juicio CNJ:
09501-2019-00010

2. Jueces que intervinieron
• Juez Ponente: MARCO ANTONIO RODRÍGUEZ RUALES
• Otros Jueces: ANA PATRICIA ROBAYO CAMPAÑA, JORGE BLUM CARCELÉN

3. Recurrente:
SERVICIO DE RENTAS INTERNAS

4. Contraparte:
BEBIDAS Y AGUAS GLACIALES S.A. BEISA

5. Antecedentes:
El SRI interpone recurso de casación contra la sentencia del Tribunal Distrital que aceptó la impugnación.

6. Causales Invocadas:
Causal primera del artículo 268 del COGEP

7. Normativa Invocada:
Artículo 10 de la LRTI, artículo 268 del COGEP

8. Objeto del Recurso:
Determinar si los gastos deducibles fueron correctamente valorados por el tribunal inferior.

9. Argumentos del Recurrente:
El tribunal inferior interpretó erróneamente el artículo 10 de la LRTI al aceptar gastos no documentados.

10. Argumentos de la Contraparte:
Los gastos sí cumplen los requisitos legales y fueron debidamente sustentados con documentación.

11. Decisión Final:
No casa la sentencia recurrida.

12. Medidas Adicionales:
Sin costas procesales.

13. Voto Salvado:
BLUM CARCELÉN JORGE

14. Argumento del Voto Salvado:
El suscrito juez considera que la sentencia debió ser casada parcialmente dado que algunos gastos sí carecían de sustento documental adecuado.
"""

# ── 1. Parser de secciones ─────────────────────────────────────────────────
print("\n[1] _parsear_secciones — tribunal (19 secciones)")
secs_t = _parsear_secciones(EXTRACCION_TRIBUNAL)
esperadas_t = [1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19]
presentes_t = sorted(secs_t.keys())
if presentes_t == esperadas_t:
    ok(f"Todas las secciones presentes: {presentes_t}")
else:
    err(f"Secciones: esperadas={esperadas_t}, presentes={presentes_t}")

print("\n[2] _parsear_secciones — CNJ (14 secciones)")
secs_c = _parsear_secciones(EXTRACCION_CNJ)
esperadas_c = [1,2,3,4,5,6,7,8,9,10,11,12,13,14]
presentes_c = sorted(secs_c.keys())
if presentes_c == esperadas_c:
    ok(f"Todas las secciones presentes: {presentes_c}")
else:
    err(f"Secciones: esperadas={esperadas_c}, presentes={presentes_c}")

# ── 2. Parser de jueces ────────────────────────────────────────────────────
print("\n[3] _parsear_jueces_seccion — tribunal (sec 3)")
ponente_t, todos_t = StrapiClient._parsear_jueces_seccion(secs_t.get(3, ""))
chk("ponente tribunal", ponente_t, "COHN ZURITA FERNANDO ANTONIO")
chk("total jueces tribunal", len(todos_t), 3)
print(f"    todos: {todos_t}")

print("\n[4] _parsear_jueces_seccion — CNJ (sec 2)")
ponente_c, todos_c = StrapiClient._parsear_jueces_seccion(secs_c.get(2, ""))
chk("ponente CNJ", ponente_c, "MARCO ANTONIO RODRÍGUEZ RUALES")
chk("total jueces CNJ", len(todos_c), 3)
print(f"    todos: {todos_c}")

# ── 3. Voto salvado ────────────────────────────────────────────────────────
print("\n[5] Voto salvado")
voto_t = secs_t.get(18, "").strip()
voto_c = secs_c.get(13, "").strip()
# Tribunal: "No consta en la sentencia" → no debe crear juez
n_voto_t = _normalizar(voto_t)
if "no consta" in n_voto_t:
    ok(f"Voto tribunal: '{voto_t}' → correctamente ignorado (no consta)")
else:
    err(f"Voto tribunal: '{voto_t}' → debería ser 'No consta'")

# CNJ: "BLUM CARCELÉN JORGE" → debe crear/buscar juez
if voto_c and "no consta" not in _normalizar(voto_c):
    ok(f"Voto CNJ: '{voto_c}' → se enviará a lookup_or_create_juez")
else:
    err(f"Voto CNJ inesperado: '{voto_c}'")

# ── 4. _split_pruebas ──────────────────────────────────────────────────────
print("\n[6] _split_pruebas (sec 13 tribunal)")
actor_p, dem_p = _split_pruebas(secs_t.get(13, ""))
if "Declaración de impuesto" in actor_p:
    ok(f"pruebas_actor: {actor_p[:70]!r}")
else:
    err(f"pruebas_actor vacío: {actor_p!r}")
if "Resolución administrativa" in dem_p:
    ok(f"pruebas_demandado: {dem_p[:70]!r}")
else:
    err(f"pruebas_demandado vacío: {dem_p!r}")

# ── 5. _parsear_cuantia ────────────────────────────────────────────────────
print("\n[7] _parsear_cuantia")
chk("cuantia '$ 1.245.678,00'", _parsear_cuantia("$ 1.245.678,00"), 1245678.0)
chk("cuantia None",             _parsear_cuantia(""),              None)
# Formato ecuatoriano con apóstrofe como separador de millones — antes se
# perdía (el apóstrofe cortaba el regex de dígitos y solo quedaba el "2").
chk("cuantia \"USD 2'955.797,07.\"", _parsear_cuantia("USD 2'955.797,07."), 2955797.07)

# ── 5b. _split_jurisprudencia ───────────────────────────────────────────────
print("\n[7b] _split_jurisprudencia")
# "1.**Jurisprudencia" sin espacio entre el punto y el markdown — antes el
# regex exigía "\.\s+" justo después del número y dejaba título + "1." pegados
# al contenido en vez de recortarlos.
blob_pegado = (
    "JURISPRUDENCIA Y CRITERIOS DEL TRIBUNAL\n\n"
    "1.**Jurisprudencia mencionada en el caso**\n\n"
    "Resolución No. 73-2018 — Sala Especializada...\n\n"
    "2.**Criterio del tribunal para resolver el caso**\n\n"
    "El Tribunal considera que..."
)
citada, criterio = _split_jurisprudencia(blob_pegado)
if citada.startswith("Resolución No. 73-2018") and "JURISPRUDENCIA Y CRITERIOS" not in citada:
    ok(f"jurisprudencia_citada (sin título ni '1.'): {citada[:50]!r}")
else:
    err(f"jurisprudencia_citada mal recortada: {citada[:80]!r}")
if criterio.startswith("El Tribunal considera"):
    ok(f"criterio_sala: {criterio[:50]!r}")
else:
    err(f"criterio_sala mal recortado: {criterio[:80]!r}")

# "No consta..." + explicación no pedida por el prompt — debe cortarse ahí.
blob_no_consta = (
    "1. Jurisprudencia mencionada en el caso\n\n"
    "No consta en la sentencia.\n\n"
    "(A pesar de que se analizan normas, evolución reglamentaria y se citan "
    "artículos legales, no se hace referencia expresa a ningún fallo, "
    "resolución o expediente judicial previo como precedente vinculante o "
    "persuasivo. No se menciona jurisprudencia específica emitida por "
    "tribunales anteriores.)\n\n"
    "2. Criterio del tribunal para resolver el caso\n\n"
    "No consta en la sentencia.\n(Explicación no solicitada que también debe cortarse.)"
)
citada_nc, criterio_nc = _split_jurisprudencia(blob_no_consta)
chk("jurisprudencia_citada 'No consta' sin explicación colgada", citada_nc, "No consta en la sentencia.")
chk("criterio_sala 'No consta' sin explicación colgada", criterio_nc, "No consta en la sentencia.")

# ── 6. _mapear_causa con Strapi mockeado ───────────────────────────────────
print("\n[8] _mapear_causa con lookups mockeados")

fila_mock = {
    "Número de juicio":   "09501-2019-00010",
    "Tribunal":           "Tribunal Distrital de lo Contencioso Tributario del Guayas",
    "Actor":              "BEBIDAS Y AGUAS GLACIALES S.A. BEISA",
    "Demandado":          "DIRECTORA GENERAL DEL SERVICIO DE RENTAS INTERNAS",
    "Abogado del Actor":     "Ab. Juan Carlos Pérez Solórzano",
    "Abogado del Demandado": "Dra. María Fernanda López",
    "Estado de Juicio":   "Con sentencia",
    "Tipo de Acción":     "Acción de impugnación",
    "Decisión Final":     "Con lugar",
    # "No consta" a propósito en estos dos: simulan que la llamada JSON aparte
    # (_extraer_campos_qwen) falló, para probar que _mapear_causa rescata el
    # dato real desde las secciones 7/8/11 del blob de extracción en vez de
    # quedarse con este valor vacío.
    "Objeto Controversia":"No consta",
    "Resumen Ejecutivo":  "La empresa impugnó una resolución del SRI sobre IR 2017.",
    "Cuantía":            "No consta",
    "Observaciones":      "",
    "_extraccion":        EXTRACCION_TRIBUNAL,
    "_jurisprudencia":    "Criterio jurisprudencial sobre deducibilidad de gastos.",
    "_resumen_cnj":       "El SRI recurre en casación contra la sentencia del tribunal.",
    "_extraccion_cnj":    EXTRACCION_CNJ,
    "_jurisprudencia_cnj":"Jurisprudencia CNJ sobre casación tributaria.",
    "_tribunal_codigo":   "09501",
}

import httpx as _httpx_orig

# Mock de todos los lookups HTTP para que retornen IDs ficticios
import unittest.mock as mock

MOCK_JUECES   = [{"documentId": "juez-001", "nombre": "X"}]
MOCK_ABOGADOS = [{"documentId": "abogado-001", "nombre": "X"}]
MOCK_ENTIDAD  = [{"documentId": "ent-001", "nombre": "X"}]
MOCK_DECISION = [{"documentId": "dec-001", "attributes": {"nombre": "Con lugar"}}]
MOCK_TIPO     = [{"documentId": "tipo-001", "attributes": {"nombre": "Acción de impugnación"}}]
MOCK_TRIBUNAL = [{"documentId": "trib-001", "nombre": "Tribunal Distrital de lo Contencioso Tributario del Guayas"}]
MOCK_ESTADO   = [{"documentId": "est-001", "attributes": {"nombre": "Con sentencia"}}]

def mock_get(url, params=None, headers=None, timeout=None):
    resp = mock.MagicMock()
    resp.is_success = True
    if "/jueces" in url:
        resp.json.return_value = {"data": MOCK_JUECES}
    elif "/abogados" in url:
        resp.json.return_value = {"data": MOCK_ABOGADOS}
    elif "/entidades" in url:
        resp.json.return_value = {"data": MOCK_ENTIDAD}
    elif "/decision-finals" in url and "casacion" not in url:
        resp.json.return_value = {"data": MOCK_DECISION}
    elif "decisiones-finales-casacion" in url:
        resp.json.return_value = {"data": [{"documentId": "dec-cnj-001", "attributes": {"nombre": "No casa"}}]}
    elif "/tipo-de-accions" in url:
        resp.json.return_value = {"data": MOCK_TIPO}
    elif "/tribunales" in url:
        resp.json.return_value = {"data": MOCK_TRIBUNAL}
    elif "/estado-del-juicios" in url:
        resp.json.return_value = {"data": MOCK_ESTADO}
    else:
        resp.json.return_value = {"data": []}
    return resp

strapi = StrapiClient()
strapi._headers = {}  # sin token real

import worker.strapi_client as sc_mod
with mock.patch.object(_httpx_orig, "get", side_effect=mock_get):
    # Resetear caches globales para que haga el fetch
    sc_mod._CACHE_DECISION_FINAL = []
    sc_mod._CACHE_DECISION_CASACION = []
    sc_mod._CACHE_TIPO_ACCION = []
    sc_mod._CACHE_ESTADO_JUICIO = []

    try:
        payload = strapi._mapear_causa(fila_mock)["data"]
    except Exception as e:
        err(f"_mapear_causa lanzó excepción: {e}")
        import traceback; traceback.print_exc()
        payload = {}

campos_rel = {
    "decision_final":    "dec-001",
    "tipo_de_accion":    "tipo-001",
    "causa_tribunal":    "trib-001",
    "entidad_demandada": "ent-001",
    "estado_del_juicio": "est-001",
    "juez":              "juez-001",
    "abogado_actor":     "abogado-001",
    "abogado_demandado": "abogado-001",
}
for campo, id_esperado in campos_rel.items():
    v = payload.get(campo)
    if v == id_esperado:
        ok(f"{campo}: {v}")
    else:
        err(f"{campo}: esperado {id_esperado!r}, got {v!r}")

# causa_jueces → {"connect": [...]}
cj = payload.get("causa_jueces", {})
if isinstance(cj, dict) and "connect" in cj and cj["connect"]:
    ok(f"causa_jueces connect: {cj['connect']}")
else:
    err(f"causa_jueces mal formado: {cj}")

# voto_salvado debería ser None (no consta en tribunal)
vs = payload.get("voto_salvado")
if vs is None:
    ok("voto_salvado: None (correcto, no consta)")
else:
    warn(f"voto_salvado: {vs} (se seteó cuando no debería)")

# Campos textuales clave
textuales_chk = [
    ("juicio",               "09501-2019-00010"),
    ("acto_impugnado",       "Resolución No."),
    ("antecedentes",         "La empresa BEISA"),
    ("pruebas_del_actor",    "Declaración de impuesto"),
    ("pruebas_del_demandado","Resolución administrativa"),
    ("argumentos_del_actor", "Los valores objetados"),
    ("argumentos_del_demandado", "Los gastos objetados"),
    ("medidas_adicionales",  "Se dispone la devolución"),
    ("detalle_de_cuantia",   "Glosa por comisiones locales"),
    ("objeto_de_controversia", "Impugnación de acto administrativo tributario"),
]

# cuantia numérica: debe venir de la sección 7, no del "No consta" de fila_mock
chk("cuantia (payload, desde sección 7)", payload.get("cuantia"), 2955797.07)
print("\n  Campos textuales:")
for campo, fragmento in textuales_chk:
    v = payload.get(campo, "")
    if fragmento in str(v):
        ok(f"  {campo}: OK")
    elif v:
        warn(f"  {campo}: tiene contenido pero sin '{fragmento}': {str(v)[:60]!r}")
    else:
        err(f"  {campo}: VACÍO")

# ── 7. guardar_casacion mockeado ────────────────────────────────────────────
print("\n[9] guardar_casacion con lookups mockeados")

with mock.patch.object(_httpx_orig, "get", side_effect=mock_get):
    sc_mod._CACHE_DECISION_CASACION = []

    campos_guardados = {}

    def mock_post_casacion(path, payload_d):
        campos_guardados.update(payload_d.get("data", {}))
        return {"data": {"documentId": "casacion-fake-001"}}

    strapi._post = mock_post_casacion
    strapi._buscar_casacion = lambda x: None

    try:
        strapi.guardar_casacion(fila_mock, "causa-fake-001")
    except Exception as e:
        err(f"guardar_casacion lanzó excepción: {e}")
        import traceback; traceback.print_exc()

# Verificar campos de casación
casacion_rel = {
    "causa":    "causa-fake-001",
    "juez":     "juez-001",
    "decision_final": "dec-cnj-001",
}
for campo, esperado in casacion_rel.items():
    v = campos_guardados.get(campo)
    if v == esperado:
        ok(f"casacion.{campo}: {v}")
    else:
        err(f"casacion.{campo}: esperado {esperado!r}, got {v!r}")

# jueces manyToMany
jueces_cnj = campos_guardados.get("jueces", {})
if isinstance(jueces_cnj, dict) and "connect" in jueces_cnj and jueces_cnj["connect"]:
    ok(f"casacion.jueces connect: {jueces_cnj['connect']}")
else:
    err(f"casacion.jueces mal formado: {jueces_cnj}")

# voto_salvado CNJ: BLUM CARCELÉN JORGE → debería setearse
vs_cnj = campos_guardados.get("voto_salvado")
if vs_cnj:
    ok(f"casacion.voto_salvado: {vs_cnj} (juez con voto salvado encontrado)")
else:
    warn("casacion.voto_salvado: None — sección 13 no parseó nombre o lookup falló")

# Campos textuales casación
casacion_text = [
    ("antecedentes",            "El SRI interpone"),
    ("argumentos_del_recurrente","tribunal inferior interpretó"),
    ("argumentos_contraparte",  "Los gastos sí cumplen"),
    ("argumento_juez_salvado",  "suscrito juez"),
]
print("\n  Campos textuales casación:")
for campo, fragmento in casacion_text:
    v = campos_guardados.get(campo, "")
    if fragmento in str(v):
        ok(f"  {campo}: OK")
    elif v:
        warn(f"  {campo}: tiene contenido pero sin '{fragmento}': {str(v)[:60]!r}")
    else:
        err(f"  {campo}: VACÍO")

print("\n─── Fin del test ───\n")
