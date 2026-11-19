"""
Tests de worker/runner.py — validan que _procesar_con_timeout arma el ctx
correcto y llama a resolucion.orquestador.procesar_causa (no al monolito
viejo resolucion.extractor). Esto es justo lo que se cableó al terminar
el refactor del punto 1.
"""
import worker.runner as runner


def test_procesar_con_timeout_llama_orquestador_con_ctx(monkeypatch):
    llamado = {}

    def _fake_procesar_causa(numero, ctx):
        llamado["numero"] = numero
        llamado["ctx"] = ctx
        return {"Número de juicio": numero, "Estado de Juicio": "Sin resultados"}

    monkeypatch.setattr(runner.orq, "procesar_causa", _fake_procesar_causa)

    ctx = {
        "tribunal": "09501", "nombre_tribunal": "Tribunal X", "ano": "2023",
        "corrida_id": "test", "carpeta_local": None, "carpeta_docx": None,
        "gpt_ok": True, "gpt_cnj_ok": True, "skip_pdf": False,
        "modelo": "gpt-4o", "job_id": 42,
    }

    fila = runner._procesar_con_timeout("09501-2023-00010", ctx)

    assert llamado["numero"] == "09501-2023-00010"
    assert llamado["ctx"] == ctx
    assert fila["Estado de Juicio"] == "Sin resultados"


def test_procesar_con_timeout_propaga_timeout(monkeypatch):
    import time

    def _lento(numero, ctx):
        time.sleep(0.2)
        return {}

    monkeypatch.setattr(runner.orq, "procesar_causa", _lento)
    monkeypatch.setattr(runner, "TIMEOUT_CAUSA_SEG", 0.05)
    monkeypatch.setattr(runner.log, "error", lambda *a, **kw: None)

    ctx = {"job_id": 1}
    try:
        runner._procesar_con_timeout("09501-2023-00010", ctx)
        assert False, "debía levantar TimeoutError"
    except TimeoutError:
        pass


def test_no_quedan_referencias_al_monolito_viejo():
    """
    Guardrail: si alguien reintroduce `import resolucion.extractor as ext`
    en runner.py, este test lo detecta — el punto de entrada correcto es
    resolucion.orquestador desde que se terminó el refactor.
    """
    import inspect
    fuente = inspect.getsource(runner)
    assert "resolucion.extractor" not in fuente
    assert "ext." not in fuente


def test_guarda_en_strapi_a_traves_de_las_capas_cms():
    """
    Guardrail: el guardado en Strapi debe pasar por resolucion/cms/
    (tribunal.py / corte.py), no llamar a STRAPI.guardar_causa/
    guardar_casacion directo — esa es la capa que separa "qué se guarda"
    de "cómo se reporta/loguea el guardado".
    """
    import inspect
    fuente = inspect.getsource(runner)
    assert "cms_tribunal.guardar(" in fuente
    assert "cms_corte.guardar(" in fuente
    assert "STRAPI.guardar_causa(" not in fuente
    assert "STRAPI.guardar_casacion(" not in fuente


def test_revision_particionada_recibe_todos_sus_tribunales(monkeypatch):
    """Regresión: el split por comas de `workers` cortaba "t=a,b,c" y cada
    job de una revisión particionada revisaba solo el primer tribunal."""
    import threading
    import worker.revision_worker as rw
    llamado = {}
    monkeypatch.setattr(runner.jm, "obtener_job", lambda _id: {
        "tribunal": "-", "año": "0000", "checkpoint": 0, "desde": 0, "hasta": None,
        "dry_run": False, "modelo": "-",
        "workers": "revision::diaria:descarga:t=09332,17230,06102"})
    monkeypatch.setattr(rw, "ejecutar_job_revision", lambda *a, **kw: llamado.update(kw))
    runner.ejecutar_job(1, threading.Event(), threading.Event())
    assert llamado["tribunales"] == ["09332", "17230", "06102"]
    assert llamado["descarga"] is True and llamado["modo"] == "diaria" and llamado["anio"] == ""
