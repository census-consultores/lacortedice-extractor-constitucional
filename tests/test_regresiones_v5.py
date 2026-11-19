"""
Regresiones de los controles de la ficha detectadas en la auditoría v5
(pilotos/v5/<materia>/comparacion_<materia>.md y reglas_<materia>.md).
Los fragmentos vienen de las fichas y sentencias reales del piloto v5; en el
resumen se repone la oración que el recorte anterior borró.
"""
import re
from pathlib import Path

from resolucion.ai import controles as C
from resolucion.ai._cliente import _decision_perfil, _perfil_caso_texto, normalizar_clasificacion
import worker.strapi_client as sc

_RELLENO = "Antecedentes del proceso contencioso administrativo. " * 20
# Los montos de los casos de prueba constan en la sentencia (regla cero de montos).
_RELLENO += " Valores del expediente: 96246,00; 620229,41; 1865362; 1595,28; 400206,03; 39672,37; 37034,43. "
_PROMPTS = Path(__file__).resolve().parent.parent / "prompts"


def _oraciones(texto):
    return [o for p in re.split(r"\n\s*\n", texto.strip()) for o in C._oraciones(p)]


def _sin_huerfanas(original, recortado):
    """Toda oración que empieza con conector o pronombre conserva la anterior."""
    orig, quedan = _oraciones(original), set(_oraciones(recortado))
    for k, o in enumerate(orig):
        if k and o in quedan and C._RE_DEPENDIENTE.match(C.sin_tildes(o)):
            assert orig[k - 1] in quedan, o[:60]


# ── 1. Recorte del resumen: partes, pretensión y decisión siempre ────────────

_INICIO_00027 = ("El Tribunal Distrital de lo Contencioso Administrativo con sede en el Distrito Metropolitano "
                 "de Quito, Provincia de Pichincha, emitió su sentencia el 28 de noviembre de 2018 en el juicio "
                 "No. 17811-2017-00027.")
# Oraciones que el recorte v5 borró (quién demandó, qué pidió y quiénes contestaron).
_PARTES_00027 = ("El Consorcio H&T demandó al Servicio de Contratación de Obras (SECOB) y al Procurador General "
                 "del Estado el pago de USD 620.229,41 por el rubro “Transporte Sub Base Clase 3” del contrato "
                 "LPI-SECOB-MINEDUC-BID-002-2015, más los daños y perjuicios por lucro cesante, alegando que "
                 "fiscalización y el administrador del contrato aprobaron el rubro como nuevo. El SECOB y la "
                 "Procuraduría General del Estado contestaron que el transporte estaba incluido en el precio del "
                 "rubro “Relleno Compactado Sub Base Clase III” y que no procedía ningún pago adicional.")
_RESTO_00027 = (
    "Señalan que el contratista, al presentar su oferta, tuvo acceso a los estudios del sitio y a las "
    "especificaciones técnicas, por lo que no puede alegar desconocimiento. Además, invocan el principio de buena "
    "fe y el acto propio, al considerar que el contratista no puede desconocer condiciones que aceptó al ofertar. "
    "También alegaron caducidad de la acción, la cual fue desechada en audiencia preliminar, pues en contratación "
    "pública rige un plazo de cinco años conforme al artículo 306 numeral 3 del Código Orgánico General de "
    "Procesos. El Tribunal consideró que el contrato y sus pliegos definen con precisión las obligaciones de las "
    "partes y que, conforme al artículo 1561 del Código Civil, los contratos son ley para los contratantes. "
    "Determinó que el rubro “Transporte Sub Base Clase 3” no puede considerarse nuevo, pues su inclusión en el "
    "rubro “Relleno Compactado Sub Base Clase III” es obligatoria según las especificaciones técnicas, que "
    "establecen que “deben estar considerados en el precio fijado todos los costos necesarios para la terminación "
    "final del rubro” y que “no se conocerán pagos adicionales por ningún concepto”. El Tribunal destacó que el "
    "contratista tuvo la oportunidad de formular preguntas durante la etapa precontractual, pero no lo hizo "
    "respecto al transporte del material. Asimismo, señaló que los pronunciamientos favorables del fiscalizador y "
    "el administrador del contrato no tienen fuerza vinculante si contradicen el contrato, pues la modificación "
    "contractual requiere un acuerdo formal y aprobaciones administrativas. En consecuencia, el Tribunal desechó "
    "la demanda, al considerar que no existe obligación de pagar el rubro reclamado ni los daños y perjuicios, "
    "pues la actuación del SECOB se ajustó al contrato y a la ley. No se ordenó pago alguno, ni costas ni "
    "honorarios.")


def test_recorte_17811_00027_conserva_partes_y_pretension():
    original = f"{_INICIO_00027} {_PARTES_00027} {_RESTO_00027}"
    r = {"resumen": original}
    info = C.aplicar(r, _RELLENO, None, nivel="tribunal")
    res = r["resumen"]
    assert len(res.split()) <= 350
    # v5: "…00027. Señalan que…" (sin quién demandó ni qué pidió).
    assert res.startswith(_INICIO_00027 + " El Consorcio H&T demandó al Servicio de Contratación de Obras")
    assert res.endswith("No se ordenó pago alguno, ni costas ni honorarios.")
    assert "Señalan que" not in res   # sale con su antecedente, no queda suelta
    _sin_huerfanas(original, res)
    assert not any("máximo 350" in s for s in info["senales"])


_CAS_00027 = (
    "La Sala Especializada de lo Contencioso Administrativo de la Corte Nacional de Justicia, integrada por los "
    "jueces Álvaro Ojeda Hidalgo (ponente), Iván Rodrigo Larco Ortuño y Patricio Secaira Durango, resolvió el 9 de "
    "marzo de 2020 el recurso de casación No. 17811-2017-00027, interpuesto por la procuradora común y "
    "representante legal del Consorcio H&T contra la sentencia de mayoría del Tribunal Distrital de lo Contencioso "
    "Administrativo de Quito, de 28 de noviembre de 2018, que desechó la demanda del Consorcio H&T contra el "
    "Servicio de Contratación de Obras (SECOB) y el Procurador General del Estado. "
    # Oración del caso 2 que el recorte v5 borró.
    "La Sala determinó que se configuró el caso 2 del artículo 268 del Código Orgánico General de Procesos, "
    "porque el Tribunal no explicó por qué los informes de fiscalización, las planillas de avance de obra, la "
    "orden de cambio y el acta del 25 de abril de 2016, que aprobaron el rubro nuevo, carecían de valor frente a "
    "las especificaciones técnicas del contrato, de modo que la sentencia recurrida no cumplió con el estándar "
    "de motivación exigido por la Constitución. "
    "Asimismo, se configuró el caso quinto, pues se aplicaron indebidamente los artículos 1561 y 1562 del Código "
    "Civil, al equiparar la pretensión de pago por un rubro autorizado con una modificación unilateral del "
    "contrato, cuando dicha ejecución se enmarca en las estipulaciones contractuales sobre eventos compensables. "
    "El contrato LPI-SECOB-MINUDUC-BID-002-2015, financiado por el BID, rige por sus propias cláusulas, "
    "especialmente el numeral 44 de las Condiciones Generales (eventos compensables) y el CGC 12.1 de las "
    "Condiciones Especiales, que permiten la creación de rubros nuevos si son solicitados por fiscalización y "
    "aprobados por el Administrador del Contrato, requisitos cumplidos mediante informes técnicos y el acta del "
    "25 de abril de 2016. Por tanto, la Sala casó la sentencia recurrida, aceptó parcialmente la demanda y ordenó "
    "al SECOB pagar al Consorcio H&T la suma de USD 400.206,03 por el rubro \"Transporte Subclase Clase 3\", según "
    "consta en las planillas de avance de obra, orden de cambio y rubros nuevos, en el plazo de 30 días desde la "
    "ejecutoria de la sentencia. Se desechó la pretensión de indemnización por daños y perjuicios por falta de "
    "prueba.")


def test_recorte_casacion_no_deja_asimismo_sin_el_caso_anterior():
    r = {"resumen": _CAS_00027}
    info = C.aplicar(r, _RELLENO, None, nivel="corte")
    res = r["resumen"]
    # v5: "…Procurador General del Estado. Asimismo, se configuró el caso quinto…"
    assert not re.search(r"Estado\. Asimismo", res)
    _sin_huerfanas(_CAS_00027, res)
    if res == _CAS_00027:   # si no puede recortar sin romper el texto, lo deja y marca revisar
        assert any("máximo 350" in s for s in info["senales"])
    else:
        assert len(res.split()) <= 350


_RESUMEN_00274 = (
    "El Tribunal Distrital de lo Contencioso Administrativo con sede en Cuenca, integrado por las juezas Diana "
    "Vintimilla Zea y Paúl Jiménez Larrivá, y el juez Gonzalo Urgilés León, emitió sentencia el 1 de noviembre de "
    "2022 en el juicio 01803-2021-00274. "
    # Oración que el recorte v5 borró: sin ella, "Esta última" no tiene antecedente.
    "El ex Director de Fiscalización del Gobierno Autónomo Descentralizado demandó a la Contraloría General del "
    "Estado, y se contó con la Procuraduría General del Estado. "
    "Esta última intervino en defensa del interés del Estado. El objeto del proceso fue la nulidad de la "
    "Resolución No. 19033, de 2 de febrero de 2021, emitida por el Director Nacional de Responsabilidades, mediante "
    "la cual se confirmó una responsabilidad civil culposa solidaria por 18 653,62 USD contra el accionante, por "
    "haber autorizado una ampliación de plazo de 30 días al contratista del proyecto “Acondicionamiento Ambiental "
    "de la Quebrada Ortega”, cuando, según la Contraloría, solo procedía una prórroga de 14 días, lo que habría "
    "impedido el cobro de multas por retraso. La acción se sustentó en falta de motivación del acto impugnado, "
    "alegando que la suspensión de obras obedeció a condiciones climáticas justificadas, respaldadas con datos del "
    "INAMHI y registros del libro de obra. La Procuraduría General del Estado compareció en defensa del acto "
    "administrativo. La Contraloría argumentó que no existió falta de motivación, pues la resolución se basó en el "
    "análisis del libro de obra, donde se evidenció que los trabajos no fueron suspendidos en su totalidad y que "
    "participaron entre 9 y 36 personas en los días cuestionados, además de que la normativa de control interno "
    "408-25 prohíbe tomar en cuenta certificaciones pluviométricas externas sin respaldo en el libro de obra. El "
    "Tribunal consideró que la resolución impugnada está debidamente motivada, pues se ajusta a la cláusula décimo "
    "primera y décimo segunda del contrato, así como a la norma 408-25, que exige registros detallados de "
    "suspensión por lluvia, los cuales no fueron cumplidos. Señaló que no se acreditó la paralización total de "
    "actividades ni el cumplimiento de los requisitos formales para justificar la prórroga, por lo que la "
    "ampliación excesiva generó un perjuicio económico a la entidad auditada. En consecuencia, se declaró sin "
    "lugar la demanda, manteniéndose la validez de la Resolución No. 19033. No se concedieron costas.")


def test_recorte_01803_00274_esta_ultima_conserva_su_antecedente():
    r = {"resumen": _RESUMEN_00274}
    C.aplicar(r, _RELLENO, None, nivel="tribunal")
    res = r["resumen"]
    assert len(res.split()) <= 350
    assert "demandó a la Contraloría General del Estado" in res
    assert "Estado. Esta última" in res   # v5: "…01803-2021-00274. Esta última intervino…"
    assert res.endswith("No se concedieron costas.")
    _sin_huerfanas(_RESUMEN_00274, res)


def test_recorte_sin_salida_no_recorta_y_marca_revisar():
    # Toda oración depende de la anterior, que está protegida: no hay qué quitar sin romperlo.
    resumen = (_INICIO_00027 + " " + " ".join(
        f"Además, el actor alegó el hecho número {i} con detalle suficiente." for i in range(50))
        + " En consecuencia, el Tribunal desechó la demanda.")
    r = {"resumen": resumen}
    info = C.aplicar(r, _RELLENO, None, nivel="tribunal")
    assert r["resumen"] == resumen
    assert any("máximo 350" in s for s in info["senales"])


# Civil 17230-2024-00448 (C): el recorte v5 borró la oración de la parte actora.
_RESUMEN_C = (
    "La Unidad Judicial Civil con sede en la parroquia Iñaquito del Distrito Metropolitano de Quito, Provincia de "
    "Pichincha, emitió la sentencia en el juicio número 17230-2024-00448 el 30 de julio de 2024. "
    "La parte actora es Banco Diners Club del Ecuador S.A., representado por su procurador judicial. "
    "La parte demandada es Gabriela Rendón Basabe, actuando por sus propios derechos. No consta reconvención. El "
    "objeto de la controversia consiste en el cobro de una obligación dineraria derivada del incumplimiento del "
    "contrato de emisión y uso de tarjeta de crédito Diners Club, por un valor de USD 39.672,37, más intereses de "
    "mora sobre el capital de USD 37.034,43, costas procesales y honorarios profesionales, demanda presentada en "
    "procedimiento ordinario (la sentencia lo llama también ejecutivo, al referirse al Art. 289 del COGEP). Durante "
    "el proceso, las partes presentaron un acuerdo transaccional suscrito y ratificado judicialmente, mediante el "
    "cual la demandada se compromete a pagar la deuda en 60 cuotas mensuales y consecutivas, según lo detallado en "
    "el acuerdo a fs. 249-250. Se establece que el incumplimiento de hasta cinco días en cualquiera de las cuotas "
    "permitirá al banco exigir el pago inmediato de la totalidad de la deuda. La juez declara su competencia "
    "conforme al Art. 240 de la COFJ, Art. 9 del COGEP y la cláusula de sumisión de la demandada al fuero de Quito. "
    "Se acredita la personería del actor mediante procuración certificada a fs. 225-232, y se verifica la validez "
    "del proceso por haberse observado las garantías del debido proceso (Art. 76 Constitución) y las solemnidades "
    "procesales. El acuerdo transaccional se considera válido, libre, voluntario e incondicional, ajustado al Art. "
    "2348 del Código Civil y al Art. 235 del COGEP, que permite su homologación y el fin del juicio. Por tanto, se "
    "aprueba el acuerdo, se dispone la conclusión del juicio y el fiel cumplimiento de lo pactado, con posibilidad "
    "de ejecución forzosa en caso de incumplimiento, conforme al Art. 371 del COGEP. Verificado el pago total, se "
    "dispondrá el archivo del expediente. Las notificaciones se realizarán electrónicamente según el Art. 66 del "
    "COGEP, y la firma electrónica de la sentencia tiene validez legal conforme al Art. 14 de la Ley de Comercio "
    "Electrónico.")

# Civil 17230-2025-00693 (D): el recorte v5 borró la identificación de ambas partes.
_RESUMEN_D = (
    "Unidad Judicial Civil con sede en la Parroquia Iñaquito del Distrito Metropolitano de Quito, provincia de "
    "Pichincha, emitió la sentencia en el proceso ejecutivo número 17230-2025-00693, el 30 de septiembre de 2025. "
    "La parte actora es la Cooperativa de Ahorro y Crédito Uniblock y Servicios Ltda., representada por su gerente "
    "general, Gilberto Fabián Proaño Amores, y la parte demandada es Nathaly Carolina Burgos Puetate, en calidad de "
    "deudora principal. "
    "El objeto del litigio es el cobro de una obligación dineraria derivada de un pagaré a la orden con "
    "vencimientos sucesivos, suscrito el 10 de abril de 2024, por un monto inicial de 4.300,00 dólares, "
    "encontrándose la deudora en mora desde la sexta cuota, vencida el 10 de octubre de 2024. La actora fundamenta "
    "su pretensión en los artículos 186 y 187 del Código de Comercio, así como en los artículos 347.5, 348 y 349 "
    "del COGEP, alegando que el pagaré constituye título ejecutivo válido. La demandada fue citada en legal forma "
    "mediante tres boletas fijadas entre el 22 y el 24 de julio de 2025, pero no compareció ni cumplió con la "
    "obligación ni propuso excepciones previstas en el artículo 353 del COGEP. La sentencia considera que, conforme "
    "al artículo 352 del COGEP, ante la falta de pago y de excepciones, procede dictar sentencia condenando al "
    "cumplimiento de la obligación. Se valora como prueba el pagaré a la orden (fs. 01), que reúne los requisitos "
    "del artículo 186 del Código de Comercio, y documentos complementarios como la tabla de amortización (fs. 02), "
    "certificación de plazo vencido (fs. 04) y liquidación de crédito (fs. 05). Se declara que el título es "
    "ejecutivo por naturaleza, conforme a la Disposición General Décimo Primera del Código Orgánico Monetario y "
    "Financiero. La sentencia acepta la demanda y ordena a BURGOS PUETATE NATHALY CAROLINA pagar inmediatamente a "
    "la cooperativa la suma de 3.792,75 dólares por concepto de saldo de capital, más intereses legales de plazo y "
    "de mora, calculados desde la fecha de vencimiento según las regulaciones del organismo regulador monetario y "
    "financiero, liquidados pericialmente hasta su total cancelación. No se concede el pago de costas procesales "
    "por no acreditarse los presupuestos del artículo 284 del COGEP.")


def test_recorte_civil_conserva_la_identificacion_de_las_partes():
    for original, partes, final in (
            (_RESUMEN_C, ("La parte actora es Banco Diners Club", "La parte demandada es Gabriela Rendón Basabe"),
             "Ley de Comercio Electrónico."),
            (_RESUMEN_D, ("La parte actora es la Cooperativa de Ahorro y Crédito Uniblock",),
             "artículo 284 del COGEP.")):
        r = {"resumen": original}
        info = C.aplicar(r, _RELLENO, None, nivel="tribunal")
        res = r["resumen"]
        assert len(res.split()) <= 350 and res.endswith(final)
        assert all(p in res for p in partes), res[:300]
        _sin_huerfanas(original, res)
        assert not any("máximo 350" in s for s in info["senales"])


# ── 2. Rebeldía: solo cuando la DEMANDADA no contestó ───────────────────────

_EXT_DEMANDADA = """14. Argumentos de la Parte Actora:
• El acto no está motivado.
15. Argumentos de la Parte Demandada:
• El acto goza de presunción de legitimidad.
NORMATIVA APLICADA:
• Art. 229 COA
"""

# Laboral 09359-2022-00069 (el mismo patrón se da con la Procuraduría en lo contencioso administrativo).
_PGE_NO_COMPARECE = (
    "quienes comparecen a través de su defensa técnica con poder de procuración judicial. Y de la Procuraduría "
    "General del Estado, que pese haber sido citada no ha comparecido a proceso. CUARTO: ... De su parte la "
    "accionada al comparecer solicita que se declare totalmente sin lugar la demanda en razón de haber cumplido "
    "con la ley y anexando prueba que fundamentan su negativa. QUINTO: EXCEPCIÓN PREVIA. Por cuanto la parte "
    "accionada propone la excepción previa de transacción, se corre traslado a la contraparte.")


def _seccion_15(sentencia):
    r = {"extraccion": _EXT_DEMANDADA}
    C.aplicar(r, _RELLENO + sentencia, None, nivel="tribunal")
    return sc._parsear_secciones(r["extraccion"])[15]


def test_procuraduria_que_no_comparece_no_es_rebeldia_de_la_demandada():
    assert "no contestó la demanda" not in _seccion_15(_PGE_NO_COMPARECE)
    # Sin la contestación de la demandada, la frase sobre la Procuraduría tampoco basta.
    solo_pge = _PGE_NO_COMPARECE.split("CUARTO")[0]
    assert "no contestó la demanda" not in _seccion_15(solo_pge)


def test_demandada_que_no_contesta_sigue_con_el_texto_estandar():
    # Laboral 09359-2024-00556: el control ya funcionaba.
    s = "Una vez citados los accionados no contestaron la demanda.- Con lo que se trabó la litis."
    assert "no contestó la demanda" in _seccion_15(s)


# ── 3. Perfil del caso: decisión (categorías: test_regresiones_v5_materia.py)

_DECISION_CAS_00349 = (
    "Por las consideraciones expuestas, esta Sala Especializada de lo Contencioso Administrativo, administrando "
    "justicia, en nombre del pueblo soberano del Ecuador y por autoridad de la Constitución y las leyes de la "
    "República, rechaza el recurso de casación interpuesto, consecuentemente, NO CASA la sentencia expedida por el "
    "Tribunal Distrital No. 3 de lo Contencioso Administrativo con sede en el cantón Cuenca, el jueves 20 de agosto "
    "del 2020, a las 11h53.\nNotifíquese, publíquese y devuélvase.")


def test_decision_del_perfil_no_es_la_formula_de_cierre():
    assert _decision_perfil(_DECISION_CAS_00349) == "No casa la sentencia."
    ext = "11. Decisión Final\n" + _DECISION_CAS_00349 + "\n12. Medidas Adicionales\nNo consta en la sentencia.\n"
    perfil = _perfil_caso_texto(ext, "", secciones={"decision": 11})
    assert "• Decisión: No casa la sentencia." in perfil and "Notifíquese" not in perfil


def test_decision_canonica_al_final_se_conserva():
    assert _decision_perfil(_DECISION_CAS_00349 + "\nNo casa la sentencia.") == "No casa la sentencia."
    assert _decision_perfil("La Sala acepta parcialmente.\nInadmite el recurso de casación.") == (
        "Inadmite el recurso de casación.")
    assert _decision_perfil("Por lo expuesto, se niega la demanda.\nRechaza la demanda.") == "Rechaza la demanda."


# ── 4. Prompt 4: solo el resultado final ─────────────────────────────────────

def test_prompt_4_pide_solo_el_resultado_final():
    texto = (_PROMPTS / "prompt_4_clasificacion.txt").read_text(encoding="utf-8")
    assert "Entrega solo el resultado final" in texto and "no escribas notas" in texto


# ── 5. Voto salvado: extracto por oración y mención en plural ────────────────

def test_extracto_del_voto_empieza_en_una_oracion():
    # Laboral 09359-2022-00070 apelación: el corte caía en "fs. 169 a 178 indicó que:".
    voto = ("VOTO SALVADO DE: DRA. YANINA MIREYA PEÑA CORREA\n"
            "Analizando estas contradicciones, que también se constituyen en afirmaciones que debieron de ser "
            "probadas por parte de CRUZ GUILLEN GALO FERNANDO, encontramos que la demandada en la contestación a "
            "la demanda que obra de fs. 169 a 178 indicó que: “…la liquidación final de haberes del Sr. Galo "
            "Fernando Cruz Guillen…”. Ante tal hecho probado, este tribunal de impugnación encuentra que la "
            "liquidación corresponde a la realidad procesal probada. Sin costas.")
    ext = C.extracto_voto(voto, 30)
    assert ext.startswith("[…] Ante tal hecho probado") and "169 a 178" not in ext
    # Si el corte cae a pocas palabras del inicio de la oración, se completa la oración.
    total = len(voto.split("\n", 1)[1].split())
    assert C.extracto_voto(voto, total - 3).startswith("Analizando estas contradicciones")
    assert not C._RE_INICIO_ORACION.search("de fs. 169 a 178") and not C._RE_INICIO_ORACION.search("dispone: “Art")


def test_votos_salvados_en_plural_se_quitan_del_resumen():
    # Civil 17230-2025-00693: "La sentencia no menciona votos salvados."
    r = {"resumen": "La sentencia acepta la demanda. No se concede el pago de costas. "
                    "La sentencia no menciona votos salvados."}
    C.aplicar(r, _RELLENO, None, nivel="tribunal")
    assert r["resumen"] == "La sentencia acepta la demanda. No se concede el pago de costas."


# ── 6. Cuantía: rótulo con concepto ──────────────────────────────────────────

_EXT_CUANTIA_00274 = """6. Tipo de Acción:
Acción subjetiva
7. Cuantía del Caso:
La cuantía se fija en 18 653,62 USD
8. Detalle de la Cuantía:
• Valor de la responsabilidad civil culposa confirmada: USD 18 653,62
No consta el desglose específico de este monto en la sentencia
"""


def test_cuantia_toma_el_concepto_del_detalle():
    r = {"extraccion": _EXT_CUANTIA_00274}
    C.aplicar(r, _RELLENO + "confirmó la responsabilidad civil culposa por USD 18.653,62.", None, nivel="tribunal")
    assert sc._parsear_secciones(r["extraccion"])[7] == (
        "Valor de la responsabilidad civil culposa confirmada: 18 653,62 USD")


def test_cuantia_sin_concepto_conserva_el_rotulo_neutro():
    r = {"extraccion": _EXT_CUANTIA_00274.replace("USD 18 653,62", "No consta")}
    C.aplicar(r, _RELLENO, None, nivel="tribunal")
    assert sc._parsear_secciones(r["extraccion"])[7] == "Valor indicado en la sentencia: 18 653,62 USD"


# ── 7. Jurisprudencia: sin marcadores entre corchetes ───────────────────────

def test_marcadores_entre_corchetes_se_quitan():
    # Civil 17230-2025-00693: "Resolución No. [No. 6]" por "G.J.S. VII, No. 6".
    juris = ("1. Jurisprudencia mencionada en el caso:\n•\tResolución No. [No. 6]\n"
             "Gaceta Judicial de la Sala de lo Civil y Mercantil (G.J.S. VII)\nFecha: [fecha]\n"
             "“[…] el pagaré a la orden debe contener una obligación incondicional [sic] de pagar "
             "[énfasis añadido]”\n"
             "•\tSentencia No. 1158-17-EP/21\nCorte Constitucional\nFecha: 20 de octubre de 2021\n"
             "“cédula [número de identificación omitido] y fojas [27]”\n"
             "2. Criterio del tribunal para resolver el caso:\n• “[El actor] no probó.”\n")
    r = {"jurisprudencia": juris}
    info = C.aplicar(r, "", None, nivel="tribunal")
    j = r["jurisprudencia"]
    assert "Resolución No. 6\n" in j and "Fecha: No consta en la sentencia" in j
    assert "[…]" in j and "[sic]" in j and "[número de identificación omitido]" in j and "[27]" in j
    assert "[énfasis añadido]" in j   # el fragmento citado no se toca
    assert "[El actor]" in j   # la sección 2 no se toca
    assert any("corchetes" in s for s in info["senales"])


def test_ejemplos_del_prompt_3_sin_corchetes():
    texto = (_PROMPTS / "prompt_3_jurisprudencia.txt").read_text(encoding="utf-8")
    ejemplos = (texto[texto.index("Ejemplo de párrafo mixto"):texto.index("Nota: en la misma sentencia")]
                + texto[texto.index("Ejemplo (ilustrativo"):])
    assert not re.findall(r"\[(?!\.\.\.\])[^\]]*\]", ejemplos)
    assert "nunca escribas corchetes" in texto
