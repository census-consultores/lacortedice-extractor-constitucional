"""
Regresiones de la auditoría v6 (pilotos/v6/civil/reglas_civil.md): datos que el
modelo copia de los ejemplos de los prompts, decisión canónica del perfil en
primera instancia y la frase suelta «No consta en la sentencia.» del resumen.
Los fragmentos vienen de las fichas y sentencias reales del piloto v6; los de
los prompts, del texto de los prompts con que se generaron esas fichas.
"""
import re
from pathlib import Path

from resolucion.ai import controles as C
from resolucion.ai._cliente import _decision_perfil, _perfil_caso_texto

_RELLENO = "Antecedentes del proceso civil. " * 20
_PROMPTS = Path(__file__).resolve().parent.parent / "prompts"

# Ejemplos de los prompts v6 que el modelo copió.
_P1_V6 = ("usa el de la demanda y el que corresponde a los artículos citados (art. 289 COGEP = ordinario; "
          "art. 347 COGEP = ejecutivo) y añade \"(la sentencia lo llama también X)\"")
_P2_V6 = ("Ejemplo: \"ABG. CARLOS ENRIQUE OCAÑA PALACIOS, en su calidad de PROCURADOR JUDICIAL\" → \"No consta en "
          "la sentencia (comparece por procurador judicial: Carlos Enrique Ocaña Palacios)\".")
_P3_V6 = ("así lo ha señalado la Primera Sala de lo Civil y Mercantil de la Corte Suprema de Justicia en la "
          "Resolución No. NNN-AAAA, publicada en el Registro Oficial No. NNN de DD de mes de AAAA, al indicar que "
          "'la posesión que sirve de base a la prescripción debe ser exclusiva y excluyente', y la Sala de lo Civil y "
          "Mercantil de la Corte Nacional de Justicia en la sentencia dictada en el juicio No. NNNNN-AAAA-NNNNN ha "
          "reiterado que la mera tenencia no muda en posesión.")
_PROMPTS_V6 = [_P1_V6, _P2_V6, _P3_V6]


def _seccion(ext, n):
    return C._cuerpo(ext, n)


# ── 1. Datos copiados de los ejemplos de los prompts ─────────────────────────

# 17233-2019-06189: el actor comparece por sus propios derechos con su abogado.
_SENT_06189 = _RELLENO + (
    "comparece el actor GUANOTUÑA CAYAMBE JESUS MARCELO, detalla los fundamentos de hecho y derecho de su demanda, "
    "anuncia los medios probatorios que consideran adecuados, las pretensiones que tiene, determinando la cuantía, "
    "y el procedimiento, suscribiendo la demanda, conjuntamente con su abogado patrocinador.")
_EXT_06189 = (
    "5. Partes Procesales:\n"
    "• Actor (Demandante): Guanotuña Cayambe Jesús Marcelo\n"
    "• Abogado del Actor: No consta en la sentencia (comparece por procurador judicial: Carlos Enrique Ocaña "
    "Palacios)\n"
    "• Demandado: Corrales Arellano Myrian Yolanda\n"
    "6. Tipo de Acción:\nprocedimiento ordinario — reivindicación\n")


def test_nombre_del_ejemplo_del_prompt_2_se_quita():
    r = {"extraccion": _EXT_06189}
    info = C.aplicar(r, _SENT_06189, None, nivel="tribunal", prompts=_PROMPTS_V6)
    partes = _seccion(r["extraccion"], 5)
    assert "Ocaña" not in partes
    assert "• Abogado del Actor: No consta en la sentencia\n" in partes + "\n"
    assert "• Actor (Demandante): Guanotuña Cayambe Jesús Marcelo" in partes
    assert any("Carlos Enrique Ocaña Palacios" in s for s in info["senales"])


# 09332-2025-00631: el procurador sí es Carlos Enrique Ocaña Palacios (ya funcionaba).
_SENT_00631 = _RELLENO + ("IDENTIFICACIÓN DE LAS PARTES: ACTORA: ABG. CARLOS ENRIQUE OCAÑA PALACIOS, en su calidad de "
                          "PROCURADOR JUDICIAL, del DR. JAIME MANUEL FLOR RUBIANES, representante jurídico del BANCO "
                          "PICHINCHA.")


def test_nombre_que_esta_en_la_sentencia_se_conserva():
    ext = ("5. Partes Procesales:\n"
           "• Actor (Demandante): Banco Pichincha Compañía Anónima, representado por el Dr. Jaime Manuel Flor "
           "Rubianes, a través del Abg. Carlos Enrique Ocaña Palacios en calidad de Procurador Judicial\n"
           "• Abogado del Actor: No consta en la sentencia (comparece por procurador judicial: Carlos Enrique Ocaña "
           "Palacios)\n6. Tipo de Acción:\nprocedimiento ordinario — cobro\n")
    r = {"extraccion": ext}
    info = C.aplicar(r, _SENT_00631, None, nivel="tribunal", prompts=_PROMPTS_V6)
    assert r["extraccion"] == ext
    assert not any("ejemplo del prompt" in s for s in info["senales"])


# 17230-2025-00693: marcador NNN-AAAA y órganos del ejemplo del prompt 3.
_SENT_00693 = _RELLENO + (
    "promesa de pago materializada en un instrumento privado, que, según el Art. 486 del Código de Comercio, debe "
    "contener la indicación de ser a la orden.(1ª. Sala, 21 de febrero de 1975). Vale citar la siguiente "
    "jurisprudencia: publicada en a G.J.S. VII, No. 6, p. 598, dice: \"El pagaré a la orden debe contener una "
    "obligación incondicional de pagar una suma determinada\". Se garantiza el derecho a la defensa consagrado en el "
    "artículo 76 de la Norma suprema.")
_JURIS_00693 = (
    "1. Jurisprudencia mencionada en el caso:\n"
    "•\tResolución No. NNN-AAAA\n"
    "Primera Sala de lo Civil y Mercantil de la Corte Suprema de Justicia\n"
    "Fecha: 21 de febrero de 1975\n"
    "“el pagaré a la orden debe contener la indicación de ser a la orden”\n"
    "•\tSentencia publicada en la G.J.S. VII, No. 6, p. 598\n"
    "Sala de lo Civil y Mercantil de la Corte Nacional de Justicia\n"
    "Fecha: R.O. No consta en la sentencia\n"
    "“El pagaré a la orden debe contener una obligación incondicional de pagar una suma determinada”\n"
    "2. Criterio del tribunal para resolver el caso:\n•\t“En el caso sub judice…” (Considerando 5.2)\n")


def test_marcador_y_organos_del_ejemplo_del_prompt_3_se_quitan():
    r = {"jurisprudencia": _JURIS_00693}
    info = C.aplicar(r, _SENT_00693, None, nivel="tribunal", prompts=_PROMPTS_V6)
    j = C._cuerpo(r["jurisprudencia"], 1)
    assert "NNN" not in j and "Corte Suprema" not in j and "Corte Nacional" not in j
    # Cada viñeta conserva sus cuatro líneas; los datos reales se quedan.
    assert j.split("\n")[:4] == ["•\tNo consta en la sentencia", "No consta en la sentencia",
                                 "Fecha: 21 de febrero de 1975",
                                 "“el pagaré a la orden debe contener la indicación de ser a la orden”"]
    assert "•\tSentencia publicada en la G.J.S. VII, No. 6, p. 598\nNo consta en la sentencia\n" in j
    assert "2. Criterio del tribunal" in r["jurisprudencia"]
    assert sum("ejemplo del prompt" in s for s in info["senales"]) == 3


# 09332-2024-00429: «art. 289» del ejemplo del prompt 1 en el resumen.
_SENT_00429 = _RELLENO + ("se procedió a convocar la Audiencia Preliminar de conformidad con lo dispuesto en el "
                          "Art. 292 del Código Orgánico General de Procesos.")
_RESUMEN_00429 = (
    "La Unidad Judicial Civil con sede en el cantón Guayaquil emitió la sentencia en el juicio número "
    "09332-2024-00429, sobre la demanda promovida por Banco de la Producción S.A. Produbanco en contra de Abel "
    "Antonio Sarmiento Gómez, en calidad de deudor. La acción se tramitó en procedimiento ordinario conforme al "
    "artículo 289 del COGEP. Se convocó a audiencia preliminar conforme al art. 292 del COGEP. Se rechazó la demanda.")


def test_articulo_del_ejemplo_del_prompt_1_se_quita_del_resumen():
    r = {"resumen": _RESUMEN_00429}
    C.aplicar(r, _SENT_00429, None, nivel="tribunal", prompts=_PROMPTS_V6)
    assert "289" not in r["resumen"]
    assert "art. 292 del COGEP" in r["resumen"] and r["resumen"].startswith("La Unidad Judicial Civil")


def test_sin_prompts_no_cambia_nada():
    r = {"extraccion": _EXT_06189}
    C.aplicar(r, _SENT_06189, None, nivel="tribunal")
    assert "Ocaña" in r["extraccion"]


def test_prompts_sin_datos_de_ejemplo():
    for nombre in ("prompt_1_resumen.txt", "prompt_2_extraccion.txt", "prompt_3_jurisprudencia.txt",
                   "prompt_5_cnj_extraccion.txt", "prompt_6_cnj_jurisprudencia.txt",
                   "prompt_11_apelacion_extraccion.txt", "prompt_12_apelacion_jurisprudencia.txt"):
        texto = (_PROMPTS / nombre).read_text(encoding="utf-8")
        for dato in ("Ocaña", "OCAÑA", "NNN", "AAAA", "art. 289", "Art. 289", "39.672", "1975", "Los Ceibos",
                     "R.O.No.99", "[nombre]", "[cantón]", "[provincia]", "R.O. [fecha]"):
            assert dato not in texto, (nombre, dato)


# ── 2. Decisión canónica del perfil en primera instancia ─────────────────────

_DEC_06189 = (
    "a) En el juicio principal: se acepta la demanda:\n"
    "1. Se concede a favor del señor Jesús Marcelo Guanotuña Cayambe la reivindicación del segundo piso del "
    "inmueble ubicado en el lote 374 de la Urbanización Nueva Aurora.\n"
    "2. Una vez ejecutoriada la sentencia, el actor notificará a la demandada, quien tendrá un término de veinte "
    "días para desocupar el bien.\n"
    "3. No se consideran prestaciones mutuas, por no haberse solicitado ni demostrado la existencia de mejoras "
    "útiles.\n"
    "b) En la reconvención: se rechaza la demanda por falta de prueba.\n"
    "c) No se condena en costas ni honorarios profesionales, por no acreditarse litigio abusivo, temerario o "
    "malicioso.\n"
    "Acepta la demanda.\n"
    "Rechaza la reconvención.")
_DEC_00626 = (
    "Declara parcialmente con lugar la demanda, presentada por el Sr. Carlos Roberto Zambrano Arnao, y ordena que el "
    "demandado, el Sr. Edgar Santiago Zambrano Arnao:\n"
    "1.- Rinda cuentas de:\n"
    "1.4.- No a lugar la rendición de cuentas de los envíos, giros y transacciones de dinero por parte de Carlos "
    "Roberto Zambrano Arnao a favor de Edgar Santiago Zambrano Arnao, por un total de USD$43.949,18\n"
    "2.- Se aceptan las cuentas presentadas por el demandado respecto a los puntos 1.1., 1.2. y 1.3., sin que se "
    "verifique valor en dinero a favor del actor\n"
    "3.- Sin costas ni honorarios que regular\n"
    "Rechaza la demanda.")
_DEC_00631 = (
    "Se declara con lugar la demanda planteada por el Abg. Carlos Enrique Ocaña Palacios, en su calidad de "
    "Procurador Judicial del Dr. Jaime Manuel Flor Rubianes, representante jurídico del Banco Pichincha Compañía "
    "Anónima, en contra de la Sra. Lourdes Holanda Mite Palma, y se ordena el pago de la suma de USD 5.226,57 más el "
    "interés legal y de mora establecido por el Banco Central, hasta su cancelación. Sin lugar los demás rubros "
    "reclamados. Sin costas que regular.\n"
    "Acepta la demanda.")
# Ya funcionaban.
_DEC_00454 = (
    "Se acepta parcialmente la demanda presentada por la COOPERATIVA DE AHORRO Y CREDITO CREDI YA LTDA. No ha lugar a "
    "la comisión reclamada, por cuanto se verifica que el pagaré base de la demanda no ha circulado. No se dispone el "
    "pago de costas procesales, ni honorarios que regular.\n"
    "Acepta parcialmente la demanda.")
_DEC_00448 = (
    "Se aprueba el acuerdo celebrado entre el doctor Juan Carlos Cueva Serrano, en calidad de Procurador Judicial "
    "del Banco Diners Club del Ecuador S.A., y la señora Gabriela Rendón Basabe.\n"
    "Aprueba la transacción.")
_DEC_CASACION = ("La Sala casa la sentencia y, en su lugar, acepta la demanda.\nAcepta la demanda.\n"
                 "Casa la sentencia.")


def test_perfil_prioriza_la_demanda_sobre_la_reconvencion():
    assert _decision_perfil(_DEC_06189, primera_instancia=True) == "Acepta la demanda."


def test_perfil_respeta_parcialmente_con_lugar():
    assert _decision_perfil(_DEC_00626, primera_instancia=True) == "Acepta parcialmente la demanda."


def test_perfil_parcial_si_niega_rubros():
    assert _decision_perfil(_DEC_00631, primera_instancia=True) == "Acepta parcialmente la demanda."


def test_perfil_casos_que_ya_funcionaban():
    assert _decision_perfil(_DEC_00454, primera_instancia=True) == "Acepta parcialmente la demanda."
    assert _decision_perfil(_DEC_00448, primera_instancia=True) == "Aprueba la transacción."
    # En casación manda la frase de casación, aunque la nueva sentencia acepte la demanda.
    assert _decision_perfil(_DEC_CASACION) == "Casa la sentencia."


def test_perfil_tribunal_usa_la_decision_sobre_la_demanda():
    ext = ("6. Tipo de Acción:\nprocedimiento ordinario — reivindicación\n7. Cuantía del Caso:\nCuantía "
           f"indeterminada\n16. Decisión Final:\n{_DEC_06189}\n17. Disposiciones Adicionales:\nNo constan.\n")
    assert "• Decisión: Acepta la demanda." in _perfil_caso_texto(ext, "")


# ── 3. Frase suelta «No consta en la sentencia.» al final del resumen ────────

_RESUMEN_00631 = (
    "La Unidad Judicial Civil con sede en el cantón Guayaquil emitió la sentencia en el juicio 09332-2025-00631. "
    "No se accedieron los rubros de costas procesales ni recargo por expensas, conforme al art. 284 del COGEP, por no "
    "haberse configurado la conducta prevista en el art. 286 del mismo cuerpo legal. La sentencia no expone la "
    "valoración de la prueba más allá de lo señalado. No consta en la sentencia.")


def test_frase_suelta_no_consta_se_quita_del_resumen():
    r = {"resumen": _RESUMEN_00631}
    C.aplicar(r, _RELLENO + "Sentencia.", None, nivel="tribunal")
    assert r["resumen"].endswith("La sentencia no expone la valoración de la prueba más allá de lo señalado.")
    assert "No consta en la sentencia" not in r["resumen"]


def test_no_consta_dentro_de_una_oracion_se_conserva():
    texto = ("La Unidad Judicial Civil emitió la sentencia en el juicio 09332-2025-00631. La fecha de la citación "
             "no consta en la sentencia.")
    r = {"resumen": texto}
    C.aplicar(r, _RELLENO + "Sentencia.", None, nivel="tribunal")
    assert r["resumen"] == texto


# ── Cableado: corte.analizar compara con los prompts cargados y el Word recibe la jurisprudencia corregida ──

def test_corte_quita_datos_del_prompt_cargado_y_el_word_los_recibe(monkeypatch):
    import resolucion.ai.corte as corte
    sentencia = _RELLENO + "La Sala Especializada de lo Civil y Mercantil resuelve. NO CASA la sentencia."
    monkeypatch.setattr(corte, "cargar_api_key_openrouter", lambda: "k")
    monkeypatch.setattr(corte, "extraer_texto_pdf", lambda *a, **k: sentencia)
    monkeypatch.setattr(corte, "cargar_prompt", lambda nombre: _P3_V6 if nombre.startswith("prompt_6") else "X")
    word = {}
    monkeypatch.setattr(corte, "crear_docx_corte", lambda *a, **k: word.setdefault("juris", a[3]) and b"docx")
    monkeypatch.setattr(corte, "GPT_PAUSA_SEG", 0)
    monkeypatch.setattr(corte.log, "info", lambda *a, **k: None)
    monkeypatch.setattr(corte.log, "warn", lambda *a, **k: None)
    juris = ("1. Jurisprudencia mencionada en el caso:\n•\tResolución No. NNN-AAAA\n"
             "Primera Sala de lo Civil y Mercantil de la Corte Suprema de Justicia\nFecha: No consta en la sentencia\n"
             "“la posesión debe ser exclusiva”\n2. Criterio del tribunal para resolver el caso:\n•\t“Criterio.”\n")
    it = iter([("resumen", "stop"), ("11. Decisión Final\nNo casa la sentencia.\n", "stop"), (juris, "stop"),
               ("Categoría 1: X", "stop"), ("SIN_ESPECIFICAR", "stop")])
    monkeypatch.setattr(corte, "llamar_qwen", lambda *a, **k: next(it))
    r = corte.analizar(b"%PDF", "17711-2020-00001", modelo="qwen/qwen3")
    assert "NNN" not in r["jurisprudencia"] and "Corte Suprema" not in r["jurisprudencia"]
    assert word["juris"] == r["jurisprudencia"]
    assert any("ejemplo del prompt" in p for p in r["validacion_problemas"]) and not r["validacion_ok"]
