"""
Regresiones del piloto de contencioso administrativo (18-oct-2026): datos que
la IA extraía bien pero se guardaban mal o vacíos en Strapi.
"""
import worker.strapi_client as sc
import worker.runner as runner
from config import materia as MATERIA


class _Resp:
    def __init__(self, data):
        self._data = data

    def json(self):
        return {"data": self._data}


# ── Tribunal: Quito no debe caer en Guayaquil (mismos 60 primeros caracteres) ──

_TRIBUNALES = list(MATERIA.JUDICATURAS.values())


def _httpx_tribunales(url, params=None, **kw):
    (clave, valor), = params.items()
    v = valor.lower()
    if clave.endswith("[$eqi]"):
        hits = [n for n in _TRIBUNALES if n.lower() == v]
    else:
        hits = [n for n in _TRIBUNALES if v in n.lower()]
    return _Resp([{"documentId": n} for n in hits])


def test_lookup_tribunal_no_confunde_tribunales_con_prefijo_comun(monkeypatch):
    monkeypatch.setattr(sc.httpx, "get", _httpx_tribunales)
    cli = sc.StrapiClient()
    for nombre in _TRIBUNALES:
        assert cli._lookup_tribunal(nombre) == nombre


def test_lookup_tribunal_ambiguo_no_enlaza(monkeypatch):
    monkeypatch.setattr(sc.httpx, "get", _httpx_tribunales)
    assert sc.StrapiClient()._lookup_tribunal("Contencioso Administrativo") is None


# ── Categorías: nombre en la misma línea que la cabecera ─────────────────────

def test_categorias_en_la_misma_linea():
    blob = (
        "Catálogo Aplicable:\nADMINISTRATIVO\n"
        "Categoría 7: Contratación pública\n"
        "• Subcategoría: Reajuste de precios\n"
        "**Categoría 2:** Acto y procedimiento administrativo\n"
        "• Subcategoría: No aplica (no hubo debate)\n"
    )
    assert sc._parsear_nombres_categorias(blob) == [
        "Contratación pública", "Reajuste de precios", "Acto y procedimiento administrativo",
    ]


def test_categorias_en_la_linea_siguiente_sigue_funcionando():
    blob = "Categoría 1:\nPrincipios del derecho administrativo\n• Subcategoría:\nBuena fe\n"
    assert sc._parsear_nombres_categorias(blob) == ["Principios del derecho administrativo", "Buena fe"]


# ── Decisión de casación: etiqueta de tributario → catálogo de la materia ────

def test_decision_casacion_usa_frases_de_la_materia(monkeypatch):
    catalogo = [{"documentId": n, "nombre": n} for n in MATERIA.DECISIONES_CASACION]
    monkeypatch.setattr(sc, "_CACHE_DECISION_CASACION", catalogo)
    cli = sc.StrapiClient()
    assert cli._lookup_decision_final_casacion("Se casó") == MATERIA.DECISIONES_CASACION[0]
    assert cli._lookup_decision_final_casacion("No se casó") == MATERIA.DECISIONES_CASACION[1]
    assert cli._lookup_decision_final_casacion("Se casó parcialmente") == MATERIA.DECISIONES_CASACION[2]


# ── Cuantía ─────────────────────────────────────────────────────────────────

def test_cuantia_formatos_reales():
    casos = {
        "La cuantía se fija en 18 653,62 USD": 18653.62,
        "La cuantía se fija en $1,595.28 USD (MIL QUINIENTOS…)": 1595.28,
        "USD. 620.229,41 más impuestos generales de ley": 620229.41,
        "USD $ 386,00, correspondiente a la multa": 386.0,
        "2'955.797,07": 2955797.07,
        "$1,770 USD": 1770.0,
        "No consta en la sentencia": None,
    }
    for texto, esperado in casos.items():
        assert sc._parsear_cuantia(texto) == esperado, texto


# ── Entidad demandada: no la Procuraduría ────────────────────────────────────

def test_entidad_demandada_salta_la_procuraduria():
    partes = (
        "• Actor (Demandante): Consorcio H&T\n"
        "• Demandado (Entidad Pública): Servicio de Contratación de Obras, SECOB\n"
        "• Procuraduría General del Estado (interviene en defensa de los intereses del Estado)\n"
        "• Abogado del Demandado: Jacinto Bohórquez\n"
    )
    assert sc._entidad_demandada(partes, "Procuraduria General del Estado") == \
        "Servicio de Contratación de Obras, SECOB"


def test_entidad_demandada_respaldos():
    assert sc._entidad_demandada("", "Director del IESS") == "Director del IESS"
    assert sc._entidad_demandada("• Demandado: Procuraduría General del Estado",
                                 "Procuraduria General del Estado") == "Procuraduría General del Estado"


# ── Jueces con la acotación del prompt copiada ───────────────────────────────

def test_otros_jueces_con_acotacion():
    ponente, todos = sc.StrapiClient._parsear_jueces_seccion(
        "• Juez Ponente: Diana Vintimilla Zea\n• Otros Jueces (máximo dos): Gonzalo Urgilés León; Paúl Jiménez"
    )
    assert ponente == "Diana Vintimilla Zea"
    assert todos == ["Diana Vintimilla Zea", "Gonzalo Urgilés León", "Paúl Jiménez"]


# ── Word: una sola subida por ficha ──────────────────────────────────────────

class _StrapiFalso:
    def __init__(self):
        self.subidos = []

    def subir_docx(self, data, nombre):
        self.subidos.append(nombre)
        return f"/uploads/{nombre}"


def test_ficha_no_se_sube_dos_veces(monkeypatch):
    monkeypatch.setattr(runner.log, "info", lambda *a, **k: None)
    strapi = _StrapiFalso()
    fila = {"Número de juicio": "X", "_docx_bytes_tribunal": b"d", "_docx_nombre_tribunal": "X_ficha.docx"}
    runner._subir_archivos_strapi(fila, strapi, 1)
    fila["_docx_bytes_cnj"], fila["_docx_nombre_cnj"] = b"c", "X_ficha_cnj.docx"
    runner._subir_archivos_strapi(fila, strapi, 1)
    assert strapi.subidos == ["X_ficha.docx", "X_ficha_cnj.docx"]


# ── Re-extracción: conserva tipo de actor y actor normalizado ────────────────

def test_fila_base_clasifica_actor():
    from worker.reextraccion_worker import _fila_base
    fila = _fila_base({"numero": "17811-2017-00027"}, {"actor": "Diaz Cedeño Monica Tatiana"})
    assert fila["Tipo de Actor"] == "persona_natural"
    assert fila["Actor Normalizado"]


# ── Validación: tipo de acción con tildes ────────────────────────────────────

def test_validacion_reconoce_tipos_de_accion_con_tilde():
    from resolucion.ai.tribunal import _validar
    for tipo in MATERIA.TIPOS_ACCION_ENUM_IA[:-1]:
        _, problemas = _validar({"tipo_accion": tipo, "resumen": "x" * 2000,
                                 "objeto_controversia": "y"}, "")
        assert not any("Tipo de acción" in p for p in problemas), tipo


# ── Abogados: un solo nombre, sin acotación y no el de la Procuraduría ───────

def test_primer_abogado():
    assert sc._primer_abogado(
        "Jacinto Armando Bohórquez (representante legal del SECOB); "
        "Marcos Arteaga Valenzuela (Director Nacional de Patrocinio y delegado del Procurador General del Estado)"
    ) == "Jacinto Armando Bohórquez"
    assert sc._primer_abogado(
        "Juan Pérez (delegado de la Procuraduría General del Estado); Ana Ruiz (abogada del IESS)"
    ) == "Ana Ruiz"
    assert sc._primer_abogado("Ana Carmen Hidalgo Martinod") == "Ana Carmen Hidalgo Martinod"


# ── Casación: la frase canónica de cierre manda sobre el párrafo resolutivo ──

def test_casacion_usa_frase_canonica_de_cierre(monkeypatch):
    monkeypatch.setattr(sc, "_CACHE_DECISION_CASACION",
                        [{"documentId": n, "nombre": n} for n in MATERIA.DECISIONES_CASACION])
    cli = sc.StrapiClient()
    guardado = {}
    monkeypatch.setattr(cli, "_buscar_casacion", lambda x: None)
    monkeypatch.setattr(cli, "_post", lambda path, p: guardado.update(p["data"]) or {"data": {"documentId": "c"}})
    monkeypatch.setattr(cli, "_lookup_or_create_juez", lambda n: None)
    monkeypatch.setattr(cli, "_resolver_recurrente", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_lookup_causales", lambda t: [])
    fila = {"Resultado en Corte": "", "Actor": "A", "Demandado": "B", "_extraccion_cnj": (
        "\n11. Decisión Final\n"
        "La Sala acepta el recurso, casa la sentencia y acepta parcialmente la demanda.\n"
        "Casa la sentencia.\n")}
    cli.guardar_casacion(fila, "causa-1")
    assert guardado["decision_final"] == MATERIA.DECISIONES_CASACION[0]


# ── Piloto v3: entidad, abogado y actor ──────────────────────────────────────

def test_entidad_no_cae_en_un_cargo_mas_largo(monkeypatch):
    monkeypatch.setattr(sc, "_CACHE_ENTIDADES", [
        {"documentId": "cargo", "nombre": "Directora Nacional de Recursos de Revision de la Contraloria General del Estado"},
        {"documentId": "iess", "nombre": "Instituto Ecuatoriano de Seguridad Social"},
    ])
    creadas = []
    cli = sc.StrapiClient()
    monkeypatch.setattr(cli, "_post", lambda path, p: creadas.append(p["data"]["nombre"]) or {"data": {"documentId": "nueva"}})
    assert cli._lookup_or_create_entidad("Contraloría General del Estado") == "nueva"
    assert cli._lookup_or_create_entidad("Director Provincial del Instituto Ecuatoriano de Seguridad Social") == "iess"
    assert creadas == ["Contraloría General del Estado"]


def test_abogado_prefiere_registro_exacto(monkeypatch):
    sucio = {"documentId": "sucio", "nombre": "Jacinto Armando Bohórquez (Representante Legal del Secob); Marcos Arteaga"}
    limpio = {"documentId": "limpio", "nombre": "Jacinto Armando Bohórquez"}
    monkeypatch.setattr(sc.httpx, "get", lambda *a, **k: _Resp([sucio, limpio]))
    assert sc.StrapiClient()._lookup_or_create_abogado("Jacinto Armando Bohórquez (representante legal del SECOB)") == "limpio"


def test_abogado_registro_sucio_se_corrige(monkeypatch):
    sucio = {"documentId": "sucio", "nombre": "Jacinto Armando Bohórquez (Representante Legal del Secob); Marcos Arteaga"}
    monkeypatch.setattr(sc.httpx, "get", lambda *a, **k: _Resp([sucio]))
    cli = sc.StrapiClient()
    puestos = []
    monkeypatch.setattr(cli, "_put", lambda path, p: puestos.append(p["data"]["nombre"]))
    assert cli._lookup_or_create_abogado("Jacinto Armando Bohórquez") == "sucio"
    assert puestos == ["Jacinto Armando Bohórquez"]


def test_actor_de_partes_y_representante():
    partes = "• Actor (Demandante): Gobierno Autónomo Descentralizado Provincial de Loja\n• Abogado del Actor: No consta"
    assert sc._actor_de_partes(partes) == "Gobierno Autónomo Descentralizado Provincial de Loja"
    assert sc._RE_REPRESENTANTE.search("Paulo Carrión, Procuradores Judiciales del Señor Prefecto")
    assert not sc._RE_REPRESENTANTE.search("Diaz Cedeño Monica Tatiana")


# ── Apelación: la frase canónica de cierre manda sobre el párrafo resolutivo ─

def test_apelacion_usa_frase_canonica_de_cierre(monkeypatch):
    monkeypatch.setattr(sc, "_CACHE_DECISION_APELACION",
                        [{"documentId": n, "nombre": n} for n in MATERIA.DECISIONES_APELACION])
    cli = sc.StrapiClient()
    guardado = {}
    monkeypatch.setattr(cli, "_buscar_apelacion", lambda x: None, raising=False)
    monkeypatch.setattr(cli, "_post", lambda path, p: guardado.update(p["data"]) or {"data": {"documentId": "a"}})
    monkeypatch.setattr(cli, "_lookup_or_create_juez", lambda n: None)
    monkeypatch.setattr(cli, "_resolver_recurrente", lambda *a, **k: None)
    fila = {"Resultado en Apelación": "", "Actor": "A", "Demandado": "B", "_extraccion_apel": (
        "\n11. Decisión Final\n"
        "La Sala, al no advertir nulidad alguna, rechaza el recurso de apelación.\n"
        "Confirma la sentencia.\n")}
    cli.guardar_apelacion(fila, "causa-1")
    assert guardado["decision_final"] == "Confirma la sentencia."
# ── Casación/apelación: un bucle de repetición no deja la ficha truncada ─────

def test_corte_reintenta_extraccion_truncada(monkeypatch):
    import resolucion.ai.corte as corte
    monkeypatch.setattr(corte, "cargar_api_key_openrouter", lambda: "k")
    monkeypatch.setattr(corte, "extraer_texto_pdf", lambda *a, **k: "texto de la sentencia")
    monkeypatch.setattr(corte, "crear_docx_corte", lambda *a, **k: b"docx")
    monkeypatch.setattr(corte, "GPT_PAUSA_SEG", 0)
    monkeypatch.setattr(corte.log, "info", lambda *a, **k: None)
    monkeypatch.setattr(corte.log, "warn", lambda *a, **k: None)
    respuestas = iter([("resumen", "stop"), ("bucle bucle", "length"),
                       ("1. ...\n11. Decisión Final\nNo casa la sentencia.", "stop"), ("juris", "stop"),
                       ("Categoría 1: Principios y reglas generales del derecho civil\n• Subcategoría: Buena fe", "stop"), ("SIN_ESPECIFICAR", "stop")])
    temperaturas = []

    def _qwen(*a, temperatura=None, **k):
        temperaturas.append(temperatura)
        return next(respuestas)

    monkeypatch.setattr(corte, "llamar_qwen", _qwen)
    r = corte.analizar(b"%PDF", "17741-2020-00001", modelo="qwen/qwen3")
    assert "Decisión Final" in r["extraccion"]
    # Las dos últimas llamadas son la clasificación (prompt 4) y los tipos (prompt 7).
    assert temperaturas == [None, None, 0.35, None, None, None]
    assert not any("truncada" in p for p in r["validacion_problemas"])


# ── Categorías: la subcategoría va bajo la categoría a la que pertenece ──────

def test_subcategoria_mal_ubicada_se_reubica():
    from resolucion.ai._cliente import _reubicar_subcategorias
    cats = {16: ("Títulos valores", ["Pagaré a la orden"]), 24: ("Procedimiento civil", ["Procedimiento ejecutivo"])}
    perfil = [[16, "Títulos valores", ["Pagaré a la orden", "Procedimiento ejecutivo"]]]
    assert _reubicar_subcategorias(perfil, cats) == [
        [16, "Títulos valores", ["Pagaré a la orden"]],
        [24, "Procedimiento civil", ["Procedimiento ejecutivo"]],
    ]
    # Una categoría que solo tenía la subcategoría mal ubicada desaparece.
    assert _reubicar_subcategorias([[16, "Títulos valores", ["Procedimiento ejecutivo"]]], cats) == [
        [24, "Procedimiento civil", ["Procedimiento ejecutivo"]],
    ]


# ── Piloto v2: reintentos, validación y auto de inadmisión ───────────

def _corte_simulado(monkeypatch, respuestas):
    import resolucion.ai.corte as corte
    monkeypatch.setattr(corte, "cargar_api_key_openrouter", lambda: "k")
    monkeypatch.setattr(corte, "extraer_texto_pdf", lambda *a, **k: "texto de la sentencia")
    monkeypatch.setattr(corte, "crear_docx_corte", lambda *a, **k: b"docx")
    monkeypatch.setattr(corte, "GPT_PAUSA_SEG", 0)
    monkeypatch.setattr(corte.log, "info", lambda *a, **k: None)
    monkeypatch.setattr(corte.log, "warn", lambda *a, **k: None)
    # + clasificación (prompt 4) y tipos de defecto (prompt 7) al final.
    it = iter(list(respuestas) + [("Categoría 1: Principios y reglas generales del derecho civil\n• Subcategoría: Buena fe", "stop"), ("SIN_ESPECIFICAR", "stop")])
    # La verificación de argumentos (segunda llamada corta) responde "[]": no toca nada.
    monkeypatch.setattr(corte, "llamar_qwen",
                        lambda p, *a, **k: ("[]", "stop") if p.startswith("Eres un verificador") else next(it))
    return corte


def test_reintento_truncado_prefiere_menos_repeticion():
    from resolucion.ai._cliente import elegir_salida_reintento, proporcion_lineas_repetidas
    bucle = "1. A\n9. Args\n" + "• misma frase\n" * 50          # larga y repetida
    limpia = "1. A\n9. Args\n• uno\n• dos\n10. Contraparte"     # corta y sin bucle
    assert proporcion_lineas_repetidas(bucle) > proporcion_lineas_repetidas(limpia)
    # Ambas truncadas: gana la de menos repetición aunque sea más corta…
    assert elegir_salida_reintento(bucle, limpia, "length") is True
    assert elegir_salida_reintento(limpia, bucle, "length") is False
    # …y una salida completa siempre reemplaza a la truncada.
    assert elegir_salida_reintento(limpia, bucle, "stop") is True


def test_corte_todos_truncados_conserva_el_de_menos_repeticion(monkeypatch):
    limpia = "1. Instancia\n2. Jueces\n9. Argumentos\n• uno\n• dos"
    bucle = "1. Instancia\n9. Argumentos\n" + "• frase repetida\n" * 80
    corte = _corte_simulado(monkeypatch, [("resumen", "stop"), (limpia, "length"),
                                          (bucle, "length"), (bucle, "length"), ("juris", "stop")])
    r = corte.analizar(b"%PDF", "09359-2022-00070", modelo="qwen/qwen3", nivel="apelacion")
    assert r["extraccion"] == limpia


def test_validar_corte_exige_seccion_decision_final():
    from resolucion.ai.corte import _validar
    base = "x" * 600 + "\n7. Recurrente\n8. Causal\n"
    sin_11 = {"resumen": "r" * 400, "jurisprudencia": "j" * 200,
              "extraccion": base + "6. Decisión de primera instancia\nAceptó la demanda.\n"}
    ok, problemas = _validar(sin_11)
    assert not ok and any("DECISION FINAL" in p for p in problemas)
    con_11 = dict(sin_11, extraccion=sin_11["extraccion"] + "**11. Decisión Final**\nReforma la sentencia.\n")
    ok, problemas = _validar(con_11)
    assert not any("DECISION FINAL" in p for p in problemas)


def test_corte_reintenta_jurisprudencia_truncada(monkeypatch):
    corte = _corte_simulado(monkeypatch, [
        ("resumen", "stop"), ("1. ...\n11. Decisión Final\nNo casa la sentencia.", "stop"),
        ("• JUICIO No. 14-2007 / Corte Nacional de Justicia –", "length"),
        ("• JUICIO No. 14-2007\nCorte Nacional de Justicia\nFecha: No consta en la sentencia", "stop")])
    r = corte.analizar(b"%PDF", "17741-2020-00001", modelo="qwen/qwen3")
    assert r["jurisprudencia"].endswith("No consta en la sentencia")
    assert not any("Jurisprudencia" in p and "truncada" in p for p in r["validacion_problemas"])


def test_casacion_inadmision_se_guarda_con_su_frase(monkeypatch):
    assert "Inadmite el recurso de casación." in MATERIA.DECISIONES_CASACION
    monkeypatch.setattr(sc, "_CACHE_DECISION_CASACION",
                        [{"documentId": n, "nombre": n} for n in MATERIA.DECISIONES_CASACION])
    cli = sc.StrapiClient()
    guardado = {}
    monkeypatch.setattr(cli, "_buscar_casacion", lambda x: None)
    monkeypatch.setattr(cli, "_post", lambda path, p: guardado.update(p["data"]) or {"data": {"documentId": "c"}})
    monkeypatch.setattr(cli, "_lookup_or_create_juez", lambda n: None)
    monkeypatch.setattr(cli, "_resolver_recurrente", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_lookup_causales", lambda t: [])
    fila = {"Resultado en Corte": "", "Actor": "A", "Demandado": "B", "_extraccion_cnj": (
        "\n11. Decisión Final\n"
        "El conjuez inadmite el recurso por el caso 1.\n"
        "Inadmite el recurso de casación.\n")}
    cli.guardar_casacion(fila, "causa-1")
    assert guardado["decision_final"] == "Inadmite el recurso de casación."
    # El mapeo de las tres frases de fondo no cambia.
    assert cli._lookup_decision_final_casacion("No se casó") == "No casa la sentencia."


# ── Apelación y casación: categorías y puntos controvertidos del recurso ─────

def _cliente_corte(monkeypatch, guardado):
    monkeypatch.setattr(sc, "_CACHE_DECISION_CASACION",
                        [{"documentId": n, "nombre": n} for n in MATERIA.DECISIONES_CASACION])
    monkeypatch.setattr(sc, "_CACHE_DECISION_APELACION",
                        [{"documentId": n, "nombre": n} for n in MATERIA.DECISIONES_APELACION])
    cli = sc.StrapiClient()
    monkeypatch.setattr(cli, "_buscar_casacion", lambda x: None)
    monkeypatch.setattr(cli, "_buscar_apelacion", lambda x: None, raising=False)
    monkeypatch.setattr(cli, "_post", lambda path, p: guardado.update(p["data"]) or {"data": {"documentId": "r"}})
    monkeypatch.setattr(cli, "_lookup_or_create_juez", lambda n: None)
    monkeypatch.setattr(cli, "_resolver_recurrente", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_lookup_causales", lambda t: [])
    monkeypatch.setattr(cli, "_lookup_categorias", lambda t: ["cat-9", "cat-9a"] if t else [])
    return cli


def test_casacion_y_apelacion_guardan_categorias(monkeypatch):
    clas = "Categoría 9: Responsabilidad civil extracontractual\n• Subcategoría: Nexo causal"
    for metodo, suf in (("guardar_casacion", "cnj"), ("guardar_apelacion", "apel")):
        guardado = {}
        cli = _cliente_corte(monkeypatch, guardado)
        getattr(cli, metodo)({"Actor": "A", "Demandado": "B", f"_extraccion_{suf}": "x",
                              f"_categorizacion_{suf}": clas}, "causa-1")
        assert guardado["categorias"] == {"set": ["cat-9", "cat-9a"]}, metodo
        # Sin clasificación en la fila (solo descarga) no se tocan las categorías.
        guardado.clear()
        getattr(cli, metodo)({"Actor": "A", "Demandado": "B", f"_extraccion_{suf}": "x"}, "causa-1")
        assert "categorias" not in guardado, metodo


def test_tipos_defecto_del_recurso_con_documentid_de_la_instancia(monkeypatch):
    from resolucion.cms import corte as cms_corte, apelacion as cms_apelacion
    import worker.job_manager as jm
    guardados = []
    monkeypatch.setattr(jm, "guardar_tipos_defecto", lambda d, t: guardados.append((d, t)) or 1)
    monkeypatch.setattr(cms_corte.log, "info", lambda *a, **k: None)

    class _S:
        def guardar_casacion(self, f, c): return {"data": {"documentId": "cas-1"}}
        def guardar_apelacion(self, f, c): return {"data": {"documentId": "apel-1"}}

    cms_corte.guardar({"_tipos_defecto_cnj": "DESPIDO_INTEMPESTIVO"}, "causa-1", _S())
    cms_apelacion.guardar({"_tipos_defecto_apel": "HORAS_EXTRA"}, "causa-1", _S())
    cms_corte.guardar({}, "causa-1", _S())                     # sin prompt 7: nada
    assert guardados == [("cas-1", "DESPIDO_INTEMPESTIVO"), ("apel-1", "HORAS_EXTRA")]


def test_ficha_de_corte_trae_perfil_del_caso():
    from resolucion.ai._cliente import _perfil_caso_texto, _SECCIONES_PERFIL_CORTE, crear_docx_corte
    extraccion = ("6. Decisión de las instancias anteriores\nAceptó la demanda.\n"
                  "**11. Decisión Final**\nLa Sala no encuentra el vicio alegado.\nNo casa la sentencia.\n"
                  "12. Medidas Adicionales\nNinguna.\n")
    clas = "Categoría 9: Responsabilidad civil extracontractual\n• Subcategoría: Nexo causal"
    txt = _perfil_caso_texto(extraccion, clas, {"controversias": ["DESPIDO_INTEMPESTIVO"]},
                             secciones=_SECCIONES_PERFIL_CORTE)
    assert "• Decisión: No casa la sentencia." in txt
    assert "Responsabilidad civil extracontractual" in txt and "Nexo causal" in txt
    assert "• Puntos controvertidos: Despido intempestivo" in txt
    assert "Tipo de acción" not in txt and "Cuantía" not in txt

    import io
    from docx import Document
    data = crear_docx_corte("17741-2020-00001", "Resumen.", extraccion, "Jur.",
                            clasificacion=clas, perfil={"controversias": ["DESPIDO_INTEMPESTIVO"]})
    textos = [p.text for p in Document(io.BytesIO(data)).paragraphs]
    assert "PERFIL DEL CASO" in textos
    assert textos.index("PERFIL DEL CASO") < textos.index("I. RESUMEN EJECUTIVO")


def test_corte_clasifica_el_objeto_del_recurso(monkeypatch):
    prompts = []
    corte = _corte_simulado(monkeypatch, [
        ("resumen", "stop"), ("1. ...\n11. Decisión Final\nNo casa la sentencia.", "stop"), ("juris", "stop")])
    llamar = corte.llamar_qwen
    monkeypatch.setattr(corte, "llamar_qwen", lambda p, *a, **k: prompts.append(p) or llamar(p, *a, **k))
    r = corte.analizar(b"%PDF", "17741-2020-00001", modelo="qwen/qwen3", nivel="apelacion")
    assert prompts[-2].startswith("ESTA ES UNA SENTENCIA DE APELACIÓN")
    assert r["categorias"].startswith("Categoría 1") and r["tipos_defecto"] == "SIN_ESPECIFICAR"
