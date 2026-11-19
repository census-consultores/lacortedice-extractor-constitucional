"""
Tests del mecanismo genérico de backfill por lote:
  - worker/job_manager.py: validación de campo, estimador de tiempo,
    registro de intentos/stats (BD mockeada).
  - worker/backfill_worker.py: loop de ejecución del lote y el handler
    de abogados (SATJE + visión mockeados).
"""
import pytest

import worker.job_manager as jm
import worker.backfill_worker as bw
from config import materia as _MAT
_ID_MATERIA = next(iter(_MAT.IDS_MATERIA_SATJE), 36)


class _Cursor:
    def __init__(self, ejecuciones, resultado=None):
        self._ejecuciones = ejecuciones
        self._resultado = resultado

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self._ejecuciones.append((sql, params))

    def fetchone(self):
        return self._resultado

    def fetchall(self):
        return self._resultado or []

    @property
    def rowcount(self):
        return 1


class _Conn:
    def __init__(self, ejecuciones, resultado=None):
        self._ejecuciones = ejecuciones
        self._resultado = resultado

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self, **kw):
        return _Cursor(self._ejecuciones, self._resultado)

    def commit(self):
        pass


# ── job_manager: validación y estimador ─────────────────────────────────────

def test_validar_campo_backfill_rechaza_invalido():
    with pytest.raises(ValueError):
        jm._validar_campo_backfill("no_existe")


def test_validar_campo_backfill_acepta_valido():
    jm._validar_campo_backfill("abogados")  # no debe lanzar


def test_estimar_tiempo_sin_historial(monkeypatch):
    ejecuciones = []
    monkeypatch.setattr(jm, "_conn", lambda: _Conn(ejecuciones, resultado=None))

    r = jm.estimar_tiempo_backfill("abogados", 100)
    assert r["confiable"] is False
    assert r["segundos_estimados"] is None


def test_estimar_tiempo_con_historial(monkeypatch):
    ejecuciones = []
    fila = {"total_procesados": 20, "total_segundos": 1000.0}  # 50s/causa
    monkeypatch.setattr(jm, "_conn", lambda: _Conn(ejecuciones, resultado=fila))

    r = jm.estimar_tiempo_backfill("abogados", 100)
    assert r["confiable"] is True
    assert r["segundos_promedio_por_causa"] == 50.0
    assert r["segundos_estimados"] == 5000


def test_registrar_intento_backfill_ejecuta_upsert(monkeypatch):
    ejecuciones = []
    monkeypatch.setattr(jm, "_conn", lambda: _Conn(ejecuciones))

    jm.registrar_intento_backfill("09501-2023-00010", "abogados")

    assert len(ejecuciones) == 1
    sql, params = ejecuciones[0]
    assert "worker_backfill_intentos" in sql
    assert params == ("09501-2023-00010", "abogados")


def test_registrar_stats_backfill_ejecuta_upsert(monkeypatch):
    ejecuciones = []
    monkeypatch.setattr(jm, "_conn", lambda: _Conn(ejecuciones))

    jm.registrar_stats_backfill("abogados", procesados=10, segundos=500.0)

    assert len(ejecuciones) == 1
    sql, params = ejecuciones[0]
    assert "worker_backfill_stats" in sql
    assert params == ("abogados", 10, 500.0)


def test_registrar_stats_backfill_no_hace_nada_si_cero_procesados(monkeypatch):
    ejecuciones = []
    monkeypatch.setattr(jm, "_conn", lambda: _Conn(ejecuciones))

    jm.registrar_stats_backfill("abogados", procesados=0, segundos=0)

    assert ejecuciones == []


def test_actualizar_abogados_causa_ambos(monkeypatch):
    ejecuciones = []
    monkeypatch.setattr(jm, "_conn", lambda: _Conn(ejecuciones))

    jm.actualizar_abogados_causa("09501-2023-00010", abogado_actor="Ab. X", abogado_demandado="Ab. Y")

    sql, params = ejecuciones[0]
    assert "abogado_actor = %s" in sql
    assert "abogado_demandado = %s" in sql
    assert "Ab. X" in params and "Ab. Y" in params


def test_actualizar_abogados_causa_sin_nada_no_ejecuta(monkeypatch):
    ejecuciones = []
    monkeypatch.setattr(jm, "_conn", lambda: _Conn(ejecuciones))

    n = jm.actualizar_abogados_causa("09501-2023-00010")

    assert n == 0
    assert ejecuciones == []


# ── backfill_worker: loop del lote ──────────────────────────────────────────

class _EventoNuncaSeteado:
    def is_set(self):
        return False


def test_ejecutar_job_backfill_procesa_todo_el_lote(monkeypatch):
    pendientes = [
        {"numero": "09501-2023-00001", "actor": "A", "demandado": "SRI"},
        {"numero": "09501-2023-00002", "actor": "B", "demandado": "SRI"},
    ]
    monkeypatch.setattr(jm, "listar_pendientes_backfill", lambda campo, lote, modo="completar", **kw: pendientes)
    intentos = []
    monkeypatch.setattr(jm, "registrar_intento_backfill", lambda numero, campo: intentos.append(numero))
    monkeypatch.setattr(jm, "registrar_stats_backfill", lambda *a, **kw: None)
    monkeypatch.setattr(jm, "_actualizar", lambda *a, **kw: None)
    monkeypatch.setattr(jm, "_emitir_sse", lambda *a, **kw: None)

    llamados = []
    monkeypatch.setattr(
        bw, "_HANDLERS",
        {"abogados": lambda numero, actor, demandado, job_id: (
            llamados.append(numero) or {"actualizado": True, "detalle": "ok"}
        )},
    )

    bw.ejecutar_job_backfill(1, "abogados", 100, _EventoNuncaSeteado(), _EventoNuncaSeteado())

    assert llamados == ["09501-2023-00001", "09501-2023-00002"]
    assert intentos == ["09501-2023-00001", "09501-2023-00002"]


def test_ejecutar_job_backfill_para_en_kill(monkeypatch):
    pendientes = [{"numero": "09501-2023-00001", "actor": "A", "demandado": "SRI"}]
    monkeypatch.setattr(jm, "listar_pendientes_backfill", lambda campo, lote, modo="completar", **kw: pendientes)
    monkeypatch.setattr(jm, "registrar_stats_backfill", lambda *a, **kw: None)
    estados = []
    monkeypatch.setattr(jm, "_actualizar", lambda job_id, **kw: estados.append(kw.get("estado")))

    class _Kill:
        def is_set(self):
            return True

    # firma: ejecutar_job_backfill(job_id, campo, lote, pause_ev, kill_ev)
    bw.ejecutar_job_backfill(1, "abogados", 100, _EventoNuncaSeteado(), _Kill())

    assert "cancelado" in estados


def test_ejecutar_job_backfill_sin_handler_marca_error(monkeypatch):
    estados = []
    monkeypatch.setattr(jm, "_actualizar", lambda job_id, **kw: estados.append(kw.get("estado")))
    bw.ejecutar_job_backfill(1, "campo_sin_handler", 100, _EventoNuncaSeteado(), _EventoNuncaSeteado())
    assert estados == ["error"]


def test_ejecutar_job_backfill_error_en_handler_no_frena_el_lote(monkeypatch):
    pendientes = [
        {"numero": "09501-2023-00001", "actor": "A", "demandado": "SRI"},
        {"numero": "09501-2023-00002", "actor": "B", "demandado": "SRI"},
    ]
    monkeypatch.setattr(jm, "listar_pendientes_backfill", lambda campo, lote, modo="completar", **kw: pendientes)
    monkeypatch.setattr(jm, "registrar_intento_backfill", lambda *a, **kw: None)
    monkeypatch.setattr(jm, "registrar_stats_backfill", lambda *a, **kw: None)
    monkeypatch.setattr(jm, "_actualizar", lambda *a, **kw: None)
    monkeypatch.setattr(jm, "_emitir_sse", lambda *a, **kw: None)

    def _handler(numero, actor, demandado, job_id):
        if numero.endswith("00001"):
            raise RuntimeError("boom")
        return {"actualizado": True, "detalle": "ok"}

    monkeypatch.setattr(bw, "_HANDLERS", {"abogados": _handler})

    # No debe lanzar — el error de una causa no debe frenar el lote.
    bw.ejecutar_job_backfill(1, "abogados", 100, _EventoNuncaSeteado(), _EventoNuncaSeteado())


# ── backfill_worker: handler de abogados ────────────────────────────────────

def test_backfill_abogados_causa_no_encontrada(monkeypatch):
    monkeypatch.setattr(bw, "buscar_causa", lambda numero: None)
    r = bw._backfill_abogados_una_causa("09501-2023-00010", "A", "SRI", job_id=1)
    assert r["actualizado"] is False
    assert "no encontrada" in r["detalle"]


def test_backfill_abogados_sin_escritos(monkeypatch):
    monkeypatch.setattr(bw, "buscar_causa", lambda numero: {"idMateria": _ID_MATERIA})
    monkeypatch.setattr(bw, "obtener_incidentes", lambda id_juicio: [{"idJudicatura": "09501"}])
    monkeypatch.setattr(bw, "obtener_escritos_pdf", lambda *a, **kw: [])
    r = bw._backfill_abogados_una_causa("09501-2023-00010", "A", "SRI", job_id=1)
    assert r["actualizado"] is False
    assert "sin escritos" in r["detalle"]


def test_backfill_abogados_exito_actualiza_bd(monkeypatch):
    monkeypatch.setattr(bw, "buscar_causa", lambda numero: {"idMateria": _ID_MATERIA})
    monkeypatch.setattr(bw, "obtener_incidentes", lambda id_juicio: [{"idJudicatura": "09501"}])
    monkeypatch.setattr(bw, "obtener_escritos_pdf", lambda *a, **kw: [b"pdf1"])
    monkeypatch.setattr(
        bw, "completar_abogados_con_escritos",
        lambda *a, **kw: {"abogado_actor": "Ab. Juan Pérez"},
    )
    actualizados = {}
    monkeypatch.setattr(
        jm, "actualizar_abogados_causa",
        lambda numero, **kw: actualizados.update(kw),
    )

    r = bw._backfill_abogados_una_causa("09501-2023-00010", "Juan Pérez", "SRI", job_id=1)

    assert r["actualizado"] is True
    assert actualizados["abogado_actor"] == "Ab. Juan Pérez"
