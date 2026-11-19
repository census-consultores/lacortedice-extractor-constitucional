"""Un job que revienta debe quedar en 'error' con el traceback en la base.

Sin esto la fila queda en 'corriendo' con 0 procesadas y sin una sola linea de
log — el traceback se va por stderr y nadie lo ve. Paso con la revision del
2026-10-23 (jobs 278-281) y con los briefs 259/268/277.
"""
import threading
import pytest

from worker import job_manager as jm


@pytest.fixture
def espias(monkeypatch):
    cambios, logs = [], []
    monkeypatch.setattr(jm, "_actualizar", lambda job_id, **c: cambios.append((job_id, c)))
    monkeypatch.setattr(jm, "_iniciar_siguiente_pendiente", lambda: None)
    from worker import logger as log
    monkeypatch.setattr(log, "error", lambda m, **k: logs.append(m))
    return cambios, logs


def _correr(monkeypatch, explotar):
    from worker import runner
    monkeypatch.setattr(runner, "ejecutar_job", explotar)
    jm._lanzar_hilo(4242)
    with jm._lock:
        entrada = jm._active.get(4242)
    if entrada:
        entrada["thread"].join(timeout=10)
    for t in threading.enumerate():
        if t.name == "job-4242-extraccion":
            t.join(timeout=10)


def test_excepcion_marca_error_y_deja_traceback(monkeypatch, espias):
    cambios, logs = espias

    def explota(job_id, pause_ev, kill_ev):
        raise ValueError("revento el arranque")

    _correr(monkeypatch, explota)

    estados = [c.get("estado") for _, c in cambios]
    assert "corriendo" in estados, "deberia marcarse corriendo al lanzar"
    assert "error" in estados, f"deberia quedar en 'error', quedo en {estados}"
    assert any("ValueError" in m and "revento el arranque" in m for m in logs)
    assert any("Traceback" in m for m in logs), "el traceback debe llegar a la base"


def test_memory_error_tambien_se_registra(monkeypatch, espias):
    """MemoryError no hereda de Exception: se captura BaseException a proposito."""
    cambios, logs = espias

    def explota(job_id, pause_ev, kill_ev):
        raise MemoryError()

    _correr(monkeypatch, explota)

    assert "error" in [c.get("estado") for _, c in cambios]
    assert any("MemoryError" in m for m in logs)


def test_job_sano_no_se_marca_error(monkeypatch, espias):
    cambios, _ = espias
    _correr(monkeypatch, lambda job_id, pause_ev, kill_ev: None)
    assert "error" not in [c.get("estado") for _, c in cambios]


def test_asegurar_tabla_serializa_el_ddl():
    """Los 4 jobs de la revision entran a la vez; CREATE TABLE IF NOT EXISTS
    en paralelo revienta 3 de 4 (UniqueViolation en pg_type_typname_nsp_index)."""
    from pathlib import Path
    from worker import revision_worker
    src = Path(revision_worker.__file__).read_text(encoding="utf-8")
    cabeza = src.split("CREATE TABLE IF NOT EXISTS worker_revision_estado")[0]
    assert "pg_advisory_xact_lock" in cabeza[-800:], \
        "el DDL debe ir detras de un advisory lock"


# ── Vigilante de jobs estancados ─────────────────────────────────────────────
# OJO: aqui NO se marca 'error' a los jobs sin hilo. De esos se encarga
# rescatar_huerfanos(), que los devuelve a 'pendiente' para retomarlos desde su
# checkpoint. El hueco que cubre este vigilante es el contrario: un hilo vivo
# que dejo de avanzar, al que rescatar_huerfanos da por sano.

from contextlib import contextmanager


def _fingir_base(monkeypatch, filas):
    """filas = [(job_id, min_sin_latir)]"""
    class _Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, *a, **k): pass
        def fetchall(self): return filas
    class _Conn:
        def cursor(self, *a, **k): return _Cur()
    @contextmanager
    def _conn():
        yield _Conn()
    monkeypatch.setattr(jm, "_conn", _conn)


def _hilo_vivo(job_id):
    import threading as _th
    fin = _th.Event()
    h = _th.Thread(target=lambda: fin.wait(30), daemon=True)
    h.start()
    with jm._lock:
        jm._active[job_id] = {"thread": h, "pause": None, "kill": None,
                              "categoria": "extraccion"}
    return fin


def _con_hilo(job_id):
    import threading as _th
    kill = _th.Event()
    h = _th.Thread(target=lambda: kill.wait(30), daemon=True)
    h.start()
    with jm._lock:
        jm._active[job_id] = {"thread": h, "pause": _th.Event(), "kill": kill,
                              "categoria": "extraccion"}
    return kill


def _vigilar(monkeypatch, filas, job_id=None):
    cambios = []
    _fingir_base(monkeypatch, filas)
    monkeypatch.setattr(jm, "_actualizar", lambda j, **c: cambios.append((j, c)))
    kill = _con_hilo(job_id) if job_id else None
    try:
        return jm.vigilar_jobs_estancados(), cambios, kill
    finally:
        if kill:
            kill.set()
            with jm._lock:
                jm._active.pop(job_id, None)


def test_no_mata_durante_la_pausa_de_satje(monkeypatch):
    """SATJE_BACKOFF_MIN llega a 30: estar quieto media hora es legitimo.
    Un timeout de 15 min, como se penso al principio, matacaria jobs sanos."""
    r, cambios, _ = _vigilar(monkeypatch, [(1, 31.0)], job_id=1)
    assert r == {"avisados": [], "matados": []} and cambios == []


def test_avisa_cuando_se_agota_la_escalera(monkeypatch):
    """La escalera entera (2+5+10+20+30) son 67 min; a los 45 ya conviene avisar."""
    r, cambios, _ = _vigilar(monkeypatch, [(2, 50.0)], job_id=2)
    assert r["avisados"] == [2] and r["matados"] == [] and cambios == []


def test_mata_al_estancado_de_horas(monkeypatch):
    """El caso real de civil: 21,7 h sin avanzar, ocupando un slot."""
    r, cambios, kill = _vigilar(monkeypatch, [(3, 1302.0)], job_id=3)
    assert r["matados"] == [3]
    assert kill.is_set(), "hay que pedirle al hilo que termine, no solo marcarlo"
    assert cambios and cambios[0][1]["estado"] == "error"


def test_no_toca_al_job_sin_hilo(monkeypatch):
    """Territorio de rescatar_huerfanos: lo devuelve a 'pendiente' para
    retomarlo desde el checkpoint. Marcarlo aqui perderia el avance."""
    with jm._lock:
        jm._active.pop(900, None)
    r, cambios, _ = _vigilar(monkeypatch, [(900, 6000.0)])
    assert r == {"avisados": [], "matados": []} and cambios == []


def test_reanudar_respeta_el_tope_de_slots():
    """reanudar_job llamaba a _lanzar_hilo directo, que no mira slots:
    reanudando a mano varios pausados se arrancaban todos. Medido: 10
    reanudados con tope 4 -> 10 hilos vivos. Asi llego civil a 18."""
    from pathlib import Path
    src = Path(jm.__file__).read_text(encoding="utf-8")
    cuerpo = src[src.index("def reanudar_job("):src.index("def matar_job(")]
    assert "_corriendo_por_categoria" in cuerpo and "_max_cat" in cuerpo, \
        "reanudar_job debe comprobar el tope antes de lanzar"


def test_la_extraccion_tambien_late():
    """reportar_progreso hace su propio UPDATE (no pasa por _actualizar). Sin
    sellar latido ahi, el timeout mataria toda extraccion sana."""
    from pathlib import Path
    src = Path(jm.__file__).read_text(encoding="utf-8")
    cuerpo = src[src.index("def reportar_progreso("):src.index("def registrar_caso(")]
    assert "latido = NOW()" in cuerpo


def test_alter_type_va_despues_del_add_column():
    """init_db() abortaba sobre una base nueva.

    worker_cron_schedules se crea SIN la columna `workers` (no esta en su
    CREATE TABLE; la agrega un ADD COLUMN mas abajo). Con el ALTER ... TYPE
    TEXT por delante, Postgres responde UndefinedColumn, init_db() revienta y
    el worker no arranca. Comprobado contra Postgres 18 con el esquema vacio.
    """
    from pathlib import Path
    src = Path(jm.__file__).read_text(encoding="utf-8")
    for tabla in ("worker_jobs", "worker_cron_schedules"):
        add = src.find(f'ALTER TABLE {tabla} ADD COLUMN IF NOT EXISTS workers')
        tipo = src.find(f'ALTER TABLE {tabla} ALTER COLUMN workers TYPE TEXT')
        if tipo == -1:
            continue
        assert add != -1, f"{tabla}: hay ALTER TYPE pero no ADD COLUMN"
        assert add < tipo, (
            f"{tabla}: el ALTER ... TYPE corre antes del ADD COLUMN; "
            f"init_db() aborta contra una base nueva"
        )
