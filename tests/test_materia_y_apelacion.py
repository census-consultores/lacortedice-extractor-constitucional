"""
Tests de la capa de MATERIA (config/materia.py) y del tercer nivel de
apelación (resolucion/satje/apelacion.py). No tocan la red.

Cubren el contrato que los extractores derivados (laboral, civil) deben
respetar: reconocer instancias por nombre de judicatura, filtrar causas de
otra materia, detectar la sentencia de apelación entre actuaciones y
normalizar la decisión de segunda instancia.
"""
import pytest

from config import materia as M
from resolucion.satje import apelacion as apel
from resolucion.satje.tribunal import determinar_estado, es_tribunal_primera_instancia
from worker.strapi_client import (
    _clasificar_decision_apelacion, _extraer_administracion_catalogo,
    _TITULOS_TRIBUNAL, _TITULOS_CNJ,
)


# ── materia.py: reconocimiento de instancias ──────────────────────────────────

def test_primera_instancia_usa_claves_de_materia(monkeypatch):
    monkeypatch.setattr(M, "PRIMERA_INSTANCIA_CLAVES", [("UNIDAD JUDICIAL", "TRABAJO")])
    assert es_tribunal_primera_instancia("UNIDAD JUDICIAL DE TRABAJO CON SEDE EN EL CANTÓN GUAYAQUIL")
    assert not es_tribunal_primera_instancia("TRIBUNAL DISTRITAL DE LO CONTENCIOSO TRIBUTARIO")
    assert not es_tribunal_primera_instancia("SALA ESPECIALIZADA DE LO LABORAL DE LA CORTE PROVINCIAL")


def test_apelacion_desactivada_sin_claves(monkeypatch):
    monkeypatch.setattr(M, "APELACION_CLAVES", [])
    assert not M.tiene_apelacion()
    assert not M.es_apelacion("SALA ESPECIALIZADA DE LO LABORAL DE LA CORTE PROVINCIAL DE GUAYAS")
    assert apel.encontrar_incidente_apelacion([{"nombreJudicatura": "CORTE PROVINCIAL"}]) is None


def test_apelacion_reconoce_sala_provincial(monkeypatch):
    monkeypatch.setattr(M, "APELACION_CLAVES", [("CORTE PROVINCIAL", "LABORAL")])
    assert M.es_apelacion("SALA ESPECIALIZADA DE LO LABORAL DE LA CORTE PROVINCIAL DE JUSTICIA DEL GUAYAS")
    assert not M.es_apelacion("SALA DE LO CIVIL DE LA CORTE PROVINCIAL DE JUSTICIA DEL GUAYAS")
    incs = [
        {"idJudicatura": "09359", "nombreJudicatura": "UNIDAD JUDICIAL DE TRABAJO"},
        {"idJudicatura": "09100", "nombreJudicatura": "SALA ESPECIALIZADA DE LO LABORAL DE LA CORTE PROVINCIAL DE JUSTICIA DEL GUAYAS"},
    ]
    assert apel.encontrar_incidente_apelacion(incs)["idJudicatura"] == "09100"


def test_es_de_la_materia_por_id():
    assert M.es_de_la_materia({"idMateria": next(iter(M.IDS_MATERIA_SATJE))}) if M.IDS_MATERIA_SATJE else True


def test_es_de_la_materia_por_judicatura_cuando_no_hay_ids(monkeypatch):
    monkeypatch.setattr(M, "IDS_MATERIA_SATJE", set())
    monkeypatch.setattr(M, "PRIMERA_INSTANCIA_CLAVES", [("UNIDAD JUDICIAL", "CIVIL")])
    incs = [{"nombreJudicatura": "UNIDAD JUDICIAL CIVIL CON SEDE EN EL CANTÓN CUENCA"}]
    assert M.es_de_la_materia({"idMateria": 99}, incs)
    assert not M.es_de_la_materia({"idMateria": 99}, [{"nombreJudicatura": "UNIDAD JUDICIAL PENAL"}])


# ── estado de juicio con tres niveles ─────────────────────────────────────────

def test_determinar_estado_prefiere_cnj_luego_apelacion(monkeypatch):
    monkeypatch.setattr(M, "APELACION_CLAVES", [("CORTE PROVINCIAL",)])
    cnj  = {"idJudicatura": M.ID_CNJ, "nombreJudicatura": "CORTE NACIONAL"}
    prov = {"idJudicatura": "09100", "nombreJudicatura": "SALA DE LA CORTE PROVINCIAL"}
    uj   = {"idJudicatura": "09359", "nombreJudicatura": "UNIDAD JUDICIAL"}
    assert determinar_estado([uj, prov, cnj], "Con sentencia.") == M.ESTADO_EN_CNJ
    assert determinar_estado([uj, prov], "Con sentencia.") == M.ESTADO_EN_APELACION
    assert determinar_estado([uj], "Con sentencia.") == "Archivado"


# ── detección de la resolución de apelación ───────────────────────────────────

def test_detectar_sentencia_apelacion_ignora_tramite():
    acts = [
        {"tipo": "RAZON"},
        {"tipo": "OFICIO"},
        {"tipo": "ESCRITO"},
        {"tipo": "CONVOCATORIA A AUDIENCIA"},
        {"tipo": "SENTENCIA (RESOLUCION)", "uuid": "abc"},
        {"tipo": "ACTA DE SORTEO"},
    ]
    desc, act = apel.detectar_sentencia_apelacion(acts)
    assert desc == apel.CON_SENTENCIA
    assert act["uuid"] == "abc"
    assert desc in apel.DESCRIPCIONES_SUBSTANTIVAS


def test_detectar_sentencia_apelacion_no_confunde_casacion_concedida():
    acts = [{"tipo": "CONCESION DE RECURSO DE CASACION"}, {"tipo": "DESISTIMIENTO DEL RECURSO"}]
    desc, _ = apel.detectar_sentencia_apelacion(acts)
    assert desc.startswith("Art. 238")
    assert desc not in apel.DESCRIPCIONES_SUBSTANTIVAS


def test_detectar_sentencia_apelacion_vacio():
    assert apel.detectar_sentencia_apelacion([{"tipo": "ESCRITO"}]) == ("", None)


# ── normalización de la decisión de segunda instancia ─────────────────────────

@pytest.mark.parametrize("texto,esperado", [
    ("Se CONFIRMA en todas sus partes la sentencia venida en grado", "Confirma la sentencia."),
    ("se revoca la sentencia subida en grado y se acepta la demanda", "Revoca la sentencia."),
    ("REFORMA la sentencia en cuanto al monto", "Reforma la sentencia."),
    ("Acepta parcialmente el recurso y modifica la liquidación", "Reforma la sentencia."),
    ("declara la nulidad de todo lo actuado", "Declara la nulidad."),
    ("rechaza el recurso de apelación interpuesto", "Confirma la sentencia."),
    ("", None),
    ("No consta en la sentencia", None),
])
def test_clasificar_decision_apelacion(texto, esperado):
    assert _clasificar_decision_apelacion(texto) == esperado


# ── parsers que deben aceptar títulos de otras materias ───────────────────────

def test_titulos_tribunal_aceptan_pretensiones_y_excepciones():
    t = dict(_TITULOS_TRIBUNAL)
    assert t[9].search("9. Pretensiones de la Parte Actora:")
    assert t[9].search("9. Acto Impugnado:")
    assert t[10].search("10. Excepciones de la Parte Demandada:")
    assert t[10].search("10. Otros Actos Administrativos Relacionados:")


def test_titulos_cnj_aceptan_apelacion_y_otras_materias():
    t = dict(_TITULOS_CNJ)
    assert t[4].search("4. Tribunal Contencioso que emitió la sentencia recurrida")
    assert t[4].search("4. Sala de la Corte Provincial que emitió la sentencia recurrida")
    assert t[4].search("4. Juzgado que emitió la sentencia recurrida")
    assert t[6].search("6. Decisión del Tribunal Distrital en primera instancia")
    assert t[6].search("6. Decisión de las instancias anteriores")
    assert t[6].search("6. Decisión de primera instancia")


def test_administracion_catalogo_acepta_palabra_de_materia(monkeypatch):
    monkeypatch.setattr(M, "ADMINISTRACIONES", {})
    monkeypatch.setattr(M, "PALABRA_CATALOGO", "LABORAL")
    assert _extraer_administracion_catalogo("Catálogo Aplicable:\nLABORAL\n\nCategoría 1:") == "LABORAL"
    assert _extraer_administracion_catalogo("Catálogo Aplicable: SRI") is None


# ── Tipos reales de SATJE para la resolución de apelación (2026-10-14) ───────
import pytest as _pytest
from resolucion.satje import apelacion as _ap


@_pytest.mark.parametrize("tipo,esperado", [
    ("ACEPTAR RECURSO DE APELACION (RESOLUCION)", _ap.ACEPTA_APELACION),
    ("RECHAZAR RECURSO DE APELACION (RESOLUCION)", _ap.RECHAZA_APELACION),
    ("SENTENCIA Y/O RESOLUCION (RESOLUCION)", _ap.CON_SENTENCIA),
    ("RESOLUCION DE LA APELACION (ART. 147 COGEP) (RESOLUCION)", _ap.CON_SENTENCIA),
    ("SENTENCIA (RESOLUCION)", _ap.CON_SENTENCIA),
])
def test_detecta_resolucion_apelacion_tipos_reales(tipo, esperado):
    acts = [
        {"tipo": "ARCHIVO DEL PROCESO (AUTO INTERLOCUTORIO)"},
        {"tipo": "ADMITIR RECURSO DE CASACION (TEMPORALIDAD) (AUTO INTERLOCUTORIO)"},
        {"tipo": "RECHAZAR ACLARACIÓN Y/O AMPLIACIÓN (AUTO INTERLOCUTORIO)"},
        {"tipo": tipo.replace("(RESOLUCION)", "(RAZON DE NOTIFICACION)")},
        {"tipo": tipo},
    ]
    desc, act = _ap.detectar_sentencia_apelacion(acts)
    assert desc == esperado and act["tipo"] == tipo
    assert desc in _ap.DESCRIPCIONES_SUBSTANTIVAS


def test_apelacion_resolucion_a_secas_es_sentencia():
    from resolucion.satje.apelacion import detectar_sentencia_apelacion, CON_SENTENCIA
    r, a = detectar_sentencia_apelacion([{"tipo": "RESOLUCIÓN (RESOLUCION)", "fecha": "2026-07-02"}])
    assert r == CON_SENTENCIA


def test_conflicto_de_competencia_no_es_la_apelacion():
    from resolucion.satje.apelacion import detectar_sentencia_apelacion
    r, a = detectar_sentencia_apelacion([{"tipo": "RESOLUCION DE CONFLICTO DE COMPETENCIA (RESOLUCION)", "fecha": "2016-10-21"}])
    assert r == "" and a is None
