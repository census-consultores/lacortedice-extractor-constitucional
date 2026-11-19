"""
Tests de worker/scheduler.py.

listar_schedules/crear_schedule/etc. tocan la BD directamente (via jm._conn),
así que se mockean. Lo que SÍ se valida con el BackgroundScheduler real de
APScheduler es el bug que importa: que un schedule activo en BD quede
efectivamente armado en el scheduler al arrancar el server.
"""
import pytest

import worker.scheduler as sched


@pytest.fixture(autouse=True)
def _reset_scheduler():
    sched._scheduler = None
    yield
    if sched._scheduler and sched._scheduler.running:
        sched._scheduler.shutdown(wait=False)
    sched._scheduler = None


def _schedule_activo(id_=1, cron="0 3 * * *"):
    return {
        "id": id_, "nombre": f"schedule-{id_}", "tribunal": "09501", "año": "2023",
        "desde": 1, "hasta": None, "modelo": "gpt-4o", "cron_expr": cron,
        "activo": True, "ultimo_run": None,
    }


def test_init_schedules_registra_los_schedules_activos_de_bd(monkeypatch):
    """
    Regresión: init_schedules() cargaba los schedules de BD y llamaba
    _registrar_job() ANTES de scheduler.start() — como _registrar_job
    aborta si `not _scheduler.running`, en producción el server arrancaba
    con el scheduler vacío aunque hubiera crons activos en la tabla.
    """
    activos = [_schedule_activo(1), _schedule_activo(2)]
    monkeypatch.setattr(sched, "listar_schedules", lambda solo_activos=False: activos)
    monkeypatch.setattr(sched.log, "sistema", lambda *a, **kw: None)

    sched.init_schedules()

    # "vigilante_jobs" lo registra init_schedules() siempre: no viene de la
    # tabla, reconcilia los jobs colgados cada pocos minutos.
    job_ids = {j.id for j in sched._scheduler.get_jobs()} - {"vigilante_jobs"}
    assert job_ids == {"cron-1", "cron-2"}, (
        "los schedules activos en BD deben quedar armados al arrancar el server"
    )


def test_init_schedules_sin_schedules_no_rompe(monkeypatch):
    monkeypatch.setattr(sched, "listar_schedules", lambda solo_activos=False: [])
    monkeypatch.setattr(sched.log, "sistema", lambda *a, **kw: None)

    sched.init_schedules()

    # Sin crons en la tabla solo queda el vigilante interno de jobs colgados.
    assert [j.id for j in sched._scheduler.get_jobs()] == ["vigilante_jobs"]
    assert sched._scheduler.running


def test_registrar_job_usa_cron_expr_como_trigger(monkeypatch):
    monkeypatch.setattr(sched, "listar_schedules", lambda solo_activos=False: [])
    monkeypatch.setattr(sched.log, "sistema", lambda *a, **kw: None)
    sched.init_schedules()

    sched._registrar_job(_schedule_activo(id_=7, cron="30 2 * * 1"))

    job = sched._scheduler.get_job("cron-7")
    assert job is not None
    assert job.name == "schedule-7"


def test_toggle_schedule_desactivar_saca_el_job(monkeypatch):
    monkeypatch.setattr(sched, "listar_schedules", lambda solo_activos=False: [])
    monkeypatch.setattr(sched.log, "sistema", lambda *a, **kw: None)
    sched.init_schedules()
    sched._registrar_job(_schedule_activo(id_=3))
    assert sched._scheduler.get_job("cron-3") is not None

    class _FakeCur:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, *a, **kw): pass
        def fetchone(self):
            row = _schedule_activo(id_=3)
            row["activo"] = False
            return row

    class _FakeConn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def cursor(self, **kw): return _FakeCur()
        def commit(self): pass

    monkeypatch.setattr(sched.jm, "_conn", lambda: _FakeConn())

    sched.toggle_schedule(3, activo=False)

    assert sched._scheduler.get_job("cron-3") is None


def _fake_crud(monkeypatch, previos):
    tabla = list(previos)
    creados, borrados = [], []
    monkeypatch.setattr(sched, "listar_schedules", lambda solo_activos=False: list(tabla))
    monkeypatch.setattr(sched, "eliminar_schedule", lambda i: borrados.append(i))
    monkeypatch.setattr(sched, "crear_schedule", lambda *a, **kw: creados.append((a, kw)))
    return creados, borrados


def test_revision_automatica_crea_el_cron(monkeypatch):
    monkeypatch.setenv("REVISION_AUTOMATICA", '{"cron": "0 6 * * *", "particiones": 4, "descarga": true}')
    creados, borrados = _fake_crud(monkeypatch, [])
    sched.asegurar_revision_automatica()
    (args, kw), = creados
    assert args[0] == sched.NOMBRE_REVISION_AUTO and args[6] == "0 6 * * *"
    assert kw == {"workers": "revision::diaria:descarga:p=4", "categoria": "extraccion"}
    assert borrados == []


def test_revision_automatica_idempotente_y_off(monkeypatch):
    previo = {"id": 7, "nombre": sched.NOMBRE_REVISION_AUTO, "cron_expr": "0 6 * * *",
              "workers": "revision::diaria:descarga:p=4", "activo": True}
    monkeypatch.setenv("REVISION_AUTOMATICA", '{"cron": "0 6 * * *", "particiones": 4}')
    creados, borrados = _fake_crud(monkeypatch, [previo])
    sched.asegurar_revision_automatica()
    assert creados == [] and borrados == []
    monkeypatch.setenv("REVISION_AUTOMATICA", "off")
    sched.asegurar_revision_automatica()
    assert borrados == [7]


def test_revision_automatica_vacia_no_toca_nada(monkeypatch):
    monkeypatch.delenv("REVISION_AUTOMATICA", raising=False)
    creados, borrados = _fake_crud(monkeypatch, [])
    sched.asegurar_revision_automatica()
    assert creados == [] and borrados == []
