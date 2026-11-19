"""
Tests de worker/job_manager.py::registrar_caso.

No usan una BD real (eso ya se validó a mano contra la Postgres local al
construir la feature de abogados) — acá se mockea la conexión y se valida
lo que de verdad importa para no repetir el bug que encontramos: que la
cantidad de placeholders %s en cada INSERT coincida con la cantidad de
parámetros que se le pasan. psycopg2 no lo valida hasta ejecutar contra
una BD real, así que un desfase pasa el import/syntax check sin ruido.
"""
import worker.job_manager as jm


class _CapturaCursor:
    def __init__(self, ejecuciones):
        self._ejecuciones = ejecuciones

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self._ejecuciones.append((sql, params or ()))


class _CapturaConn:
    def __init__(self, ejecuciones):
        self._ejecuciones = ejecuciones

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self, **kw):
        return _CapturaCursor(self._ejecuciones)

    def commit(self):
        pass


def test_registrar_caso_placeholders_coinciden_con_params(monkeypatch):
    ejecuciones = []
    monkeypatch.setattr(jm, "_conn", lambda: _CapturaConn(ejecuciones))

    fila = {
        "Número de juicio": "09501-2023-00010",
        "_tribunal_codigo": "09501",
        "Tribunal": "Tribunal de Prueba",
        "Año": "2023",
        "Estado de Juicio": "Archivado",
        "Resultado tribunal": "Con sentencia.",
        "Completitud": "Completo",
        "Cuantía": "123.45",
        "Abogado del Actor": "Ab. Juan Pérez",
        "Abogado del Demandado": "Dra. María López",
        "_auditoria": {"cuantia": True, "abogados": True},
    }
    jm.registrar_caso(job_id=1, fila=fila)

    assert len(ejecuciones) == 3, "espera 3 statements: worker_causas, worker_causa_datos, worker_caso_log"
    for sql, params in ejecuciones:
        n_placeholders = sql.count("%s")
        assert n_placeholders == len(params), (
            f"desfase placeholders/params: {n_placeholders} vs {len(params)} en:\n{sql[:200]}"
        )


def test_registrar_caso_sin_auditoria_igual_registra_worker_causa_datos(monkeypatch):
    """
    estado_juicio/resultado/resultado_cnj vienen de SATJE, no de IA — deben
    guardarse en worker_causa_datos aunque no haya corrido la extraccion IA
    (dry_run, o cualquier corrida sin _auditoria). Antes esto se omitia por
    completo y esos campos quedaban vacios en /admin/registros sin necesidad.
    """
    ejecuciones = []
    monkeypatch.setattr(jm, "_conn", lambda: _CapturaConn(ejecuciones))

    fila = {
        "Número de juicio": "09501-2023-00010",
        "_tribunal_codigo": "09501",
        "Estado de Juicio": "Sin resultados",
        "Resultado tribunal": "Con sentencia.",
        "Resultado en Corte": "Casa la sentencia.",
    }
    jm.registrar_caso(job_id=1, fila=fila)

    # worker_causas + worker_causa_datos + worker_caso_log — las 3 siempre.
    assert len(ejecuciones) == 3
    for sql, params in ejecuciones:
        assert sql.count("%s") == len(params)

    sql_causa_datos, params_causa_datos = ejecuciones[1]
    assert "worker_causa_datos" in sql_causa_datos
    assert "Sin resultados" in params_causa_datos
    assert "Con sentencia." in params_causa_datos
    assert "Casa la sentencia." in params_causa_datos
    # Campos que SI dependen de IA (decision_final, etc.) siguen vacios/None
    # sin auditoria — eso es correcto, no un bug.
    assert params_causa_datos[8] in (None, "")  # decision_final


def test_lanzar_hilo_no_relanza_job_con_hilo_vivo(monkeypatch):
    """Un job que ya corre no arranca un segundo hilo (causaba trabajo duplicado)."""
    import threading
    import worker.runner as runner
    liberar, llamadas = threading.Event(), []

    def _ejecutar(job_id, pause_ev, kill_ev):
        llamadas.append(job_id)
        liberar.wait(5)

    monkeypatch.setattr(runner, "ejecutar_job", _ejecutar)
    monkeypatch.setattr(jm, "_actualizar", lambda *a, **k: None)
    monkeypatch.setattr(jm, "_iniciar_siguiente_pendiente", lambda: None)
    try:
        jm._lanzar_hilo(999001)
        jm._lanzar_hilo(999001)
        hilo = jm._active[999001]["thread"]
    finally:
        liberar.set()
    hilo.join(5)
    assert llamadas == [999001]
