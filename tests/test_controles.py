"""
Controles deterministas de la ficha (resolucion/ai/controles.py), auditoría v3.
Textos sintéticos: cada test fija una regla de reglas_civil.md (piloto v3).
"""
from resolucion.ai import controles as C
import worker.strapi_client as sc

_RELLENO = "Antecedentes del proceso civil. " * 30   # la mayoría siempre es larga
# Los montos de los casos de prueba constan en la sentencia (regla cero de montos).
_RELLENO += " Valores del expediente: 96246,00; 620229,41; 1865362; 1595,28; 400206,03; 39672,37; 37034,43. "

SENTENCIA_CON_VOTO = (
    _RELLENO
    + "Por lo expuesto, ADMINISTRANDO JUSTICIA, se confirma por unanimidad la sentencia.\n"
    + "VOTO SALVADO DE: DR. JUAN CARLOS PÉREZ DE LA TORRE\n"
    + "VISTOS: Respetados colegas, " + "repito los antecedentes. " * 600
    + "Por tanto, discrepo: el rancho integra la remuneración. Revoco la sentencia.\n"
    + "Firmado por JUAN CARLOS PEREZ C=EC"
)

EXTRACCION_TRIBUNAL = """1. Instancia:
Primera instancia – Unidad Judicial de Trabajo
3. Jueces que intervinieron:
• Juez Ponente: Lenny Solís
• Otros Jueces (máximo dos): No consta en la sentencia
7. Cuantía del Caso:
La cuantía se fija en USD 96.246,00
10. Excepciones de la Parte Demandada:
• Negativa pura y simple; asegura que todo fue pagado.
15. Argumentos de la Parte Demandada:
• El rancho es un beneficio social.
• El actor no tiene derecho al rancho.
16. Decisión Final:
Se declara con lugar la demanda.
Acepta la demanda.
17. Disposiciones Adicionales:
Pague la demandada.
18. Voto Salvado:
No consta en la sentencia
19. Argumento del Juez sobre el Voto Salvado:
No consta en la sentencia
"""


def _secciones(texto):
    return sc._parsear_secciones(texto)


# ── 1. Voto salvado ──────────────────────────────────────────────────────────

def test_separa_voto_salvado_y_extrae_nombre():
    mayoria, voto = C.separar_voto(SENTENCIA_CON_VOTO)
    assert "VOTO SALVADO" not in mayoria and "discrepo" not in mayoria
    assert mayoria.endswith("se confirma por unanimidad la sentencia.")
    assert voto["votos"] == [("Juan Carlos Pérez de la Torre", "salvado")]


def test_sin_encabezado_no_cambia_nada():
    texto = _RELLENO + "La jueza no consigna voto salvado alguno. Por voto salvado del juez..."
    assert C.separar_voto(texto) == (texto, None)


def test_voto_concurrente_con_nombre_en_linea_siguiente():
    texto = _RELLENO + "Se resuelve.\nVOTO CONCURRENTE\nJUEZ PONENTE: MARÍA LÓPEZ\nComparto la decisión."
    _, voto = C.separar_voto(texto)
    assert voto["votos"] == [("María López", "concurrente")]


def test_extracto_voto_toma_el_final_sin_firmas_en_un_parrafo():
    _, voto = C.separar_voto(SENTENCIA_CON_VOTO)
    ext = C.extracto_voto(voto["texto"], 50)
    assert len(ext.split()) <= 51
    assert ext.endswith("Revoco la sentencia.") and "Firmado" not in ext and "\n" not in ext
    assert not ext.startswith("VISTOS")


def test_secciones_de_voto_se_llenan_con_el_voto_separado():
    mayoria, voto = C.separar_voto(SENTENCIA_CON_VOTO)
    ext = EXTRACCION_TRIBUNAL.replace("Primera instancia", "Segunda instancia")
    r = {"resumen": "La Sala confirmó por unanimidad la sentencia.", "extraccion": ext}
    C.aplicar(r, mayoria, voto, nivel="tribunal")
    secs = _secciones(r["extraccion"])
    assert secs[18] == "Juan Carlos Pérez de la Torre"
    assert secs[19].endswith("Revoco la sentencia.") and len(secs[19].split()) <= 1201
    assert "por mayoría" in r["resumen"] and "unanimidad" not in r["resumen"]
    # El extracto no rompe el parser: la sección 17 sigue intacta.
    assert secs[17] == "Pague la demandada."


# ── 2. Normalizaciones de forma ──────────────────────────────────────────────

def test_normalizaciones_de_forma_primera_instancia():
    r = {"resumen": "La Unidad aceptó la demanda. No consta voto salvado en la sentencia.",
         "extraccion": EXTRACCION_TRIBUNAL}
    C.aplicar(r, _RELLENO, None, nivel="tribunal")
    secs = _secciones(r["extraccion"])
    assert "máximo" not in r["extraccion"] and "Otros Jueces" not in r["extraccion"]
    assert secs[3] == "• Juez Ponente: Lenny Solís"
    assert secs[18] == secs[19] == "No aplica (juez unipersonal)."
    assert r["resumen"] == "La Unidad aceptó la demanda."
    assert sc.StrapiClient._lookup_or_create_juez(None, secs[18]) is None


def test_voto_vacio_en_corte_y_auto_unipersonal():
    ext = ("2. Jueces que intervinieron:\n• Juez Ponente: Ana Ruiz\n• Otros Jueces: Pedro Mora; Luis Paz\n"
           "11. Decisión Final\nNo casa la sentencia.\n13. Voto Salvado\nNo consta en la sentencia.\n"
           "14. Argumento del Voto Salvado\nNo consta en la sentencia.\n")
    r = {"extraccion": ext}
    C.aplicar(r, _RELLENO, None, nivel="casacion")
    assert _secciones(r["extraccion"])[13] == "No hubo voto salvado."
    assert "Otros Jueces: Pedro Mora" in r["extraccion"]

    auto = ext.replace("Pedro Mora; Luis Paz", "No aplica").replace("No casa la sentencia.", "Inadmite el recurso de casación.")
    r = {"extraccion": auto}
    C.aplicar(r, _RELLENO, None, nivel="casacion")
    assert _secciones(r["extraccion"])[14] == "No aplica (auto unipersonal)."
    assert "Otros Jueces" not in r["extraccion"]


def test_resumen_largo_sin_oraciones_intermedias_no_se_corta():
    # Sin oraciones intermedias que quitar, el resumen queda igual (nunca se
    # corta el final, donde va la decisión) y aplicar() deja una señal.
    resumen = "Primera oración corta. " + "palabra " * 400 + "final."
    assert C.recortar_palabras(resumen, 350) == resumen
    assert C.recortar_palabras("Corto.", 350) == "Corto."
    r = {"resumen": resumen}
    info = C.aplicar(r, _RELLENO, None, nivel="tribunal")
    assert r["resumen"] == resumen and any("palabras (máximo 350)" in s for s in info["senales"])


# ── 3. Compuertas por palabra clave ──────────────────────────────────────────

def test_compuertas_quitan_prescripcion_sin_respaldo():
    # Fichas B, D y E: prescripción inventada (categoría 22 y subcategoría 16.10).
    r = {"categorias": ("Problemática Principal:\nCobro de un pagaré.\nCatálogo Aplicable:\nCIVIL\n"
                        "Categoría 16: Títulos valores y procedimiento ejecutivo\n"
                        "• Subcategoría: Pagaré a la orden\n"
                        "• Subcategoría: Prescripción de la acción cambiaria y ejecutiva\n"
                        "Categoría 22: Prescripción extintiva y caducidad\n"
                        "• Subcategoría: Prescripción extintiva de acciones ejecutivas\n"
                        "Categoría 15: Sucesiones\n• Subcategoría: Petición de herencia"),
         "tipos_defecto": "COBRO_OBLIGACION_DINERARIA\nPRESCRIPCION_EXTINTIVA_CADUCIDAD\nSUCESIONES"}
    sentencia = _RELLENO + "Se reclama el pago del pagaré suscrito por el causante."
    info = C.aplicar(r, sentencia, None, nivel="tribunal")
    assert "Categoría 22" not in r["categorias"] and "acción cambiaria" not in r["categorias"]
    assert r["tipos_defecto"].split("\n") == ["COBRO_OBLIGACION_DINERARIA", "SUCESIONES"]
    assert sc._parsear_nombres_categorias(r["categorias"]) == [
        "Títulos valores y procedimiento ejecutivo", "Pagaré a la orden", "Sucesiones", "Petición de herencia"]
    assert any("Prescripción" in a for a in info["ajustes"])

    r = {"categorias": "Categoría 22: Prescripción extintiva y caducidad\n• Subcategoría: Caducidad de la acción",
         "tipos_defecto": "PRESCRIPCION_EXTINTIVA_CADUCIDAD"}
    C.aplicar(r, _RELLENO + "La demandada alegó la caducidad de la acción.", None, nivel="tribunal")
    assert "Categoría 22" in r["categorias"] and r["tipos_defecto"] == "PRESCRIPCION_EXTINTIVA_CADUCIDAD"


def test_sin_oposicion_se_quita_con_transaccion_o_contestacion():
    # Ficha C: transacción aprobada; no cabe SENTENCIA_SIN_OPOSICION.
    r = {"tipos_defecto": "COBRO_OBLIGACION_DINERARIA\nSENTENCIA_SIN_OPOSICION"}
    C.aplicar(r, _RELLENO + "Se aprueba el acuerdo transaccional presentado por las partes.", None, nivel="tribunal")
    assert r["tipos_defecto"] == "COBRO_OBLIGACION_DINERARIA"

    r = {"tipos_defecto": "SENTENCIA_SIN_OPOSICION"}
    C.aplicar(r, _RELLENO + "La demandada contestó la demanda y propuso excepciones.", None, nivel="tribunal")
    assert r["tipos_defecto"] == ""

    # Sin contestación (art. 352): se conserva.
    r = {"tipos_defecto": "SENTENCIA_SIN_OPOSICION"}
    C.aplicar(r, _RELLENO + "La demandada no ha contestado la demanda; a falta de pago y excepciones se resuelve.",
              None, nivel="tribunal")
    assert r["tipos_defecto"] == "SENTENCIA_SIN_OPOSICION"


def test_compuertas_no_actuan_sin_texto():
    r = {"tipos_defecto": "SOLIDARIDAD_PATRONAL"}
    C.aplicar(r, "", None, nivel="tribunal")
    assert r["tipos_defecto"] == "SOLIDARIDAD_PATRONAL"


# ── 4. Posición de la demandada ──────────────────────────────────────────────

def test_demandada_que_no_contesto_lleva_texto_estandar():
    sentencia = _RELLENO + "La parte demandada no ha contestado la demanda ni compareció a la audiencia única."
    r = {"extraccion": EXTRACCION_TRIBUNAL + "", "resumen": "Se tuvo por negada la demanda."}
    C.aplicar(r, sentencia, None, nivel="tribunal")
    secs = _secciones(r["extraccion"])
    assert secs[10] == "• La parte demandada no contestó la demanda ni propuso excepciones. Tampoco compareció a la audiencia."
    assert "Tampoco compareció a la audiencia." in secs[15] and "rancho" not in secs[15]
    assert "NORMATIVA APLICADA:" in secs[15]


def test_negativa_pura_respeta_argumentos_atribuidos():
    con = _RELLENO + "La demandada negó pura y simplemente. La defensa de la demandada manifestó que pagó."
    r = {"extraccion": EXTRACCION_TRIBUNAL}
    C.aplicar(r, con, None, nivel="tribunal")
    assert "rancho" in _secciones(r["extraccion"])[15]

    sin = _RELLENO + "La demandada ha negado pura y simplemente los fundamentos de la demanda."
    r = {"extraccion": EXTRACCION_TRIBUNAL}
    C.aplicar(r, sin, None, nivel="tribunal")
    assert _secciones(r["extraccion"])[10] == "• La parte demandada negó pura y simplemente los fundamentos de hecho y de derecho de la demanda."


# ── 5. Cuantía ────────────────────────────────────────────────────────────────

def test_cuantia_con_formula_inventada_queda_neutra():
    r = {"extraccion": EXTRACCION_TRIBUNAL}
    C.aplicar(r, _RELLENO + "reclama una cuantía de $ 96.246,00", None, nivel="tribunal")
    # La sentencia llama cuantía al monto: el rótulo dice su concepto (auditoría v5).
    assert _secciones(r["extraccion"])[7] == "Cuantía fijada en la demanda: USD 96.246,00"

    r = {"extraccion": EXTRACCION_TRIBUNAL}
    C.aplicar(r, _RELLENO, None, nivel="tribunal")
    assert _secciones(r["extraccion"])[7] == "Valor indicado en la sentencia: USD 96.246,00"

    r = {"extraccion": EXTRACCION_TRIBUNAL}
    C.aplicar(r, _RELLENO + "la cuantía se fija en USD 96.246,00", None, nivel="tribunal")
    assert _secciones(r["extraccion"])[7] == "La cuantía se fija en USD 96.246,00"


# ── 6. Señales "revisar" ─────────────────────────────────────────────────────

def test_senales_revisar():
    sentencia = (_RELLENO + "Competencia según el art. 240 del Código Orgánico de la Función Judicial (Res. 49-99). "
                 "ADMINISTRANDO JUSTICIA, se acepta la demanda. Sin lugar los demás rubros reclamados; sin costas.")
    r = {"resumen": "La jueza es competente (art. 240 de la Constitución).",
         "extraccion": EXTRACCION_TRIBUNAL.replace("• El rancho es un beneficio social.",
                                                   "• Cita los arts. 1453 y ss. CC, que regulan el contrato de mutuo."),
         "jurisprudencia": "1. Jurisprudencia mencionada en el caso:\n• Res. 49-99\nCorte Suprema\nFecha: R.O. 11 de junio de 1999\n“x”"}
    info = C.aplicar(r, sentencia, None, nivel="tribunal")
    s = " | ".join(info["senales"])
    assert "niega algún rubro" in s and "240 de la constitucion" in s and "que regulan el" in s
    assert "Registro Oficial" in s


EXTRACCION_EJECUTIVO = EXTRACCION_TRIBUNAL.replace(
    "7. Cuantía del Caso:", "6. Tipo de Acción:\nProcedimiento ejecutivo — cobro de pagaré\n7. Cuantía del Caso:")


def test_senal_procedimiento_mal_identificado():
    # Fichas A, B y C: ordinario rotulado ejecutivo.
    ordinario = _RELLENO + "Se instaló la audiencia preliminar y luego la audiencia de juicio (art. 289 COGEP)."
    info = C.aplicar({"extraccion": EXTRACCION_EJECUTIVO}, ordinario, None, nivel="tribunal")
    assert any("procedimiento ejecutivo" in x for x in info["senales"])

    ejecutivo = _RELLENO + "Conforme al art. 347 del COGEP, el pagaré es título ejecutivo; audiencia de juicio."
    info = C.aplicar({"extraccion": EXTRACCION_EJECUTIVO}, ejecutivo, None, nivel="tribunal")
    assert not any("procedimiento ejecutivo" in x for x in info["senales"])

    # Al revés: ordinario que aplica los arts. 347–353.
    ext = EXTRACCION_EJECUTIVO.replace("Procedimiento ejecutivo — cobro de pagaré", "Procedimiento ordinario — cobro de dinero")
    info = C.aplicar({"extraccion": ext}, _RELLENO + "a falta de pago y excepciones, art. 352 del COGEP", None, nivel="tribunal")
    assert any("'ordinario'" in x for x in info["senales"])
    info = C.aplicar({"extraccion": ext}, ordinario, None, nivel="tribunal")
    assert not any("'ordinario'" in x for x in info["senales"])


def test_negar_costas_o_excepciones_no_es_senal():
    sentencia = _RELLENO + "ADMINISTRANDO JUSTICIA, se rechazan las excepciones y se acepta la demanda. Sin lugar las costas."
    info = C.aplicar({"extraccion": EXTRACCION_TRIBUNAL}, sentencia, None, nivel="tribunal")
    assert not any("niega algún rubro" in s for s in info["senales"])


# ── Cableado: corte.analizar manda solo la mayoría a los prompts ─────────────

def test_corte_manda_solo_la_mayoria_y_llena_el_voto(monkeypatch):
    import resolucion.ai.corte as corte
    monkeypatch.setattr(corte, "cargar_api_key_openrouter", lambda: "k")
    monkeypatch.setattr(corte, "extraer_texto_pdf", lambda *a, **k: SENTENCIA_CON_VOTO)
    monkeypatch.setattr(corte, "crear_docx_corte", lambda *a, **k: b"docx")
    monkeypatch.setattr(corte, "GPT_PAUSA_SEG", 0)
    monkeypatch.setattr(corte.log, "info", lambda *a, **k: None)
    monkeypatch.setattr(corte.log, "warn", lambda *a, **k: None)
    extraccion = ("2. Jueces que intervinieron:\n• Juez Ponente: Ana Ruiz\n• Otros Jueces: Juan Pérez\n"
                  "11. Decisión Final\nConfirma la sentencia.\n13. Voto Salvado\nNo consta en la sentencia.\n"
                  "14. Argumento del Voto Salvado\nNo consta en la sentencia.\n")
    it = iter([("resumen", "stop"), (extraccion, "stop"), ("juris", "stop"),
               ("Categoría 1: X", "stop"), ("SIN_ESPECIFICAR", "stop")])
    prompts = []
    monkeypatch.setattr(corte, "llamar_qwen", lambda p, *a, **k: prompts.append(p) or next(it))
    r = corte.analizar(b"%PDF", "09359-2022-00070", modelo="qwen/qwen3", nivel="apelacion")
    assert prompts and not any("discrepo" in p for p in prompts)
    assert _secciones(r["extraccion"])[13] == "Juan Carlos Pérez de la Torre"
