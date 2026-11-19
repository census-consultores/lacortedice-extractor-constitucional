"""Revisión diaria de novedades: tres niveles, inventario desde
worker_causa_datos y modo descarga (sin IA)."""
from worker import job_manager as jm
from worker import revision_worker as rw


def _info(**kw):
    base = {"con_sentencia": True, "cerrada_sin_sentencia": False,
            "apelacion_resuelta": False, "casacion_resuelta": False, "en_corte": False}
    base.update(kw)
    return base


def test_decidir_tres_niveles():
    n = "09332-2024-00001"
    # Falta todo
    assert rw._decidir(n, _info(), {})[:3] == (True, False, False)
    # Tenemos la causa; aparece la apelación resuelta
    assert rw._decidir(n, _info(apelacion_resuelta=True), {n: (True, False, False)})[:3] == (False, True, False)
    # Tenemos causa y apelación; aparece la casación
    assert rw._decidir(n, _info(apelacion_resuelta=True, casacion_resuelta=True),
                       {n: (True, True, False)})[:3] == (False, False, True)
    # Completa
    assert rw._decidir(n, _info(apelacion_resuelta=True, casacion_resuelta=True),
                       {n: (True, True, True)})[3] == "ya la tenemos"
    # En trámite: no se encola
    assert rw._decidir(n, _info(con_sentencia=False), {})[:3] == (False, False, False)


def test_encolar_descarga_crea_jobs_solo_descarga(monkeypatch):
    creados = []
    monkeypatch.setattr(jm, "crear_job", lambda **kw: creados.append(kw) or {"id": len(creados)})
    r = rw._encolar([("09332-2024-00007", True, False, False),
                     ("09332-2024-00003", False, True, False),
                     ("17230-2023-00010", False, False, True)], job_id=1, descarga=True)
    assert r == {"id": [1, 2]}
    assert all(j["solo_descarga"] and j["workers"] == "reintento" for j in creados)
    assert [(j["tribunal"], j["año"], j["desde"], j["hasta"]) for j in creados] == \
        [("09332", "2024", 3, 7), ("17230", "2023", 10, 10)]
    assert '"numeros": [7, 3]' in creados[0]["parametros"]


def test_encolar_lista_reextrae_causa_si_falta_apelacion(monkeypatch):
    capturado = {}
    monkeypatch.setattr(jm, "crear_job_lista", lambda items, **kw: capturado.update(items=items) or {"id": 9})
    rw._encolar([("09332-2024-00003", False, True, False)], job_id=1, descarga=False)
    assert capturado["items"] == [("09332-2024-00003", True, False)]
