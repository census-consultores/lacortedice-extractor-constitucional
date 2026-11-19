"""
Tests de resolucion/satje/corte.py — detección de sentencias de casación (CNJ).
"""
from resolucion.satje.corte import (
    detectar_sentencia_cnj,
    encontrar_incidente_cnj,
    DESCRIPCIONES_SUBSTANTIVAS,
)
from config.config import ID_CNJ


def test_encontrar_incidente_cnj_presente():
    incidentes = [
        {"idJudicatura": "09501"},
        {"idJudicatura": ID_CNJ, "nombreJudicatura": "CNJ"},
    ]
    inc = encontrar_incidente_cnj(incidentes)
    assert inc is not None
    assert inc["idJudicatura"] == ID_CNJ


def test_encontrar_incidente_cnj_ausente():
    incidentes = [{"idJudicatura": "09501"}]
    assert encontrar_incidente_cnj(incidentes) is None


def test_detectar_sentencia_cnj_casa(monkeypatch):
    monkeypatch.setattr("resolucion.satje.corte._es_razon_notificacion", lambda tipo: False)
    actuaciones = [
        {"tipo": "SENTENCIA CASANDO SENTENCIA Y/O AUTO INTERLOCUTORIO (RESOLUCION)"},
    ]
    desc, act = detectar_sentencia_cnj(actuaciones)
    assert desc == "Casa la sentencia."
    assert desc in DESCRIPCIONES_SUBSTANTIVAS
    assert act is actuaciones[0]


def test_detectar_sentencia_cnj_no_casa(monkeypatch):
    monkeypatch.setattr("resolucion.satje.corte._es_razon_notificacion", lambda tipo: False)
    actuaciones = [{"tipo": "NO CASAR SENTENCIA Y/O AUTO INTERLOCUTORIO (RESOLUCION)"}]
    desc, act = detectar_sentencia_cnj(actuaciones)
    assert desc == "No casa la sentencia."


def test_detectar_sentencia_cnj_ignora_excluidos(monkeypatch):
    monkeypatch.setattr("resolucion.satje.corte._es_razon_notificacion", lambda tipo: False)
    actuaciones = [
        {"tipo": "ACCION EXTRAORDINARIA DE PROTECCION"},  # excluido explícitamente
        {"tipo": "OFICIO"},                                # excluido explícitamente
        {"tipo": "NO CASAR SENTENCIA Y/O AUTO INTERLOCUTORIO (RESOLUCION)"},
    ]
    desc, act = detectar_sentencia_cnj(actuaciones)
    assert desc == "No casa la sentencia."
    assert act == actuaciones[2]


def test_detectar_sentencia_cnj_sin_resolucion(monkeypatch):
    monkeypatch.setattr("resolucion.satje.corte._es_razon_notificacion", lambda tipo: False)
    actuaciones = [{"tipo": "ACTA DE SORTEO"}]
    desc, act = detectar_sentencia_cnj(actuaciones)
    assert desc == ""
    assert act is None


def test_detectar_sentencia_cnj_omite_notificaciones(monkeypatch):
    # Una razón de notificación nunca debe considerarse resolución, aunque
    # su texto matchee alguna keyword por casualidad.
    monkeypatch.setattr("resolucion.satje.corte._es_razon_notificacion", lambda tipo: True)
    actuaciones = [{"tipo": "NO CASAR SENTENCIA Y/O AUTO INTERLOCUTORIO (RESOLUCION)"}]
    desc, act = detectar_sentencia_cnj(actuaciones)
    assert desc == ""
    assert act is None
