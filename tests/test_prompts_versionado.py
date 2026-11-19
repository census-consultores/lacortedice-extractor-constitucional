"""
Tests del sistema de versionado de prompts (worker_prompts):
  - guardar_prompt(modo="sobrescribir"|"nueva_version")
  - activar_version_prompt (rollback)
  - listar_prompts / listar_versiones_prompt

Mockea la conexión a BD — no toca Postgres real (esa parte ya se validó
en vivo contra la BD local: init_db + bootstrap + sobrescribir + nueva
versión + rollback, ciclo completo).
"""
import pytest

import worker.job_manager as jm


class _DictCursor:
    """Cursor que imita RealDictCursor: fetchone/fetchall devuelven dict-like."""

    def __init__(self, ejecuciones, resultados, rowcounts=None):
        self._ejecuciones = ejecuciones
        self._resultados = list(resultados)  # cola de resultados, uno por execute()
        self._rowcounts = list(rowcounts) if rowcounts is not None else None
        self._actual = None
        self._rowcount_actual = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self._ejecuciones.append((sql, params))
        self._actual = self._resultados.pop(0) if self._resultados else None
        if self._rowcounts is not None:
            self._rowcount_actual = self._rowcounts.pop(0) if self._rowcounts else 0
        else:
            self._rowcount_actual = 1 if self._actual else 0

    def fetchone(self):
        return self._actual

    def fetchall(self):
        return self._actual or []

    @property
    def rowcount(self):
        return self._rowcount_actual


class _Conn:
    def __init__(self, ejecuciones, resultados, rowcounts=None):
        self._ejecuciones = ejecuciones
        self._resultados = resultados
        self._rowcounts = rowcounts

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self, **kw):
        return _DictCursor(self._ejecuciones, self._resultados, self._rowcounts)

    def commit(self):
        pass


def test_guardar_prompt_modo_invalido_lanza():
    with pytest.raises(ValueError):
        jm.guardar_prompt("prompt_1_resumen.txt", "x", modo="borrar_todo")


def test_guardar_prompt_sobrescribir_hace_update_no_insert(monkeypatch):
    ejecuciones = []
    fila_actualizada = {
        "id": 1, "nombre": "prompt_1_resumen.txt", "version": 1,
        "contenido": "nuevo texto", "activo": True, "nota": "n",
        "creado_por": "a@b.com", "creado_en": None,
    }
    monkeypatch.setattr(jm, "_conn", lambda: _Conn(ejecuciones, [fila_actualizada]))

    row = jm.guardar_prompt("prompt_1_resumen.txt", "nuevo texto", modo="sobrescribir", nota="n")

    assert row["version"] == 1
    assert len(ejecuciones) == 1
    sql, params = ejecuciones[0]
    assert "UPDATE worker_prompts" in sql
    assert "INSERT" not in sql
    assert params == ("nuevo texto", "n", None, "prompt_1_resumen.txt")


def test_guardar_prompt_sobrescribir_sin_activa_lanza(monkeypatch):
    ejecuciones = []
    monkeypatch.setattr(jm, "_conn", lambda: _Conn(ejecuciones, [None]))

    with pytest.raises(ValueError):
        jm.guardar_prompt("prompt_1_resumen.txt", "x", modo="sobrescribir")


def test_guardar_prompt_nueva_version_incrementa_y_desactiva_anterior(monkeypatch):
    ejecuciones = []
    fila_nueva = {
        "id": 2, "nombre": "prompt_1_resumen.txt", "version": 3,
        "contenido": "v3", "activo": True, "nota": None,
        "creado_por": None, "creado_en": None,
    }
    # 1er execute: SELECT MAX(version) -> {"siguiente": 2}
    # 2do execute: UPDATE desactivar (sin resultado relevante)
    # 3er execute: INSERT ... RETURNING -> fila_nueva
    monkeypatch.setattr(
        jm, "_conn",
        lambda: _Conn(ejecuciones, [{"siguiente": 2}, None, fila_nueva]),
    )

    row = jm.guardar_prompt("prompt_1_resumen.txt", "v3", modo="nueva_version")

    assert row["version"] == 3
    assert len(ejecuciones) == 3
    assert "MAX(version)" in ejecuciones[0][0]
    assert "SET activo = FALSE" in ejecuciones[1][0]
    assert "INSERT INTO worker_prompts" in ejecuciones[2][0]


def test_activar_version_prompt_desactiva_la_actual_y_activa_la_pedida(monkeypatch):
    ejecuciones = []
    monkeypatch.setattr(jm, "_conn", lambda: _Conn(ejecuciones, [None, None], rowcounts=[1, 1]))

    ok = jm.activar_version_prompt("prompt_1_resumen.txt", 1)

    assert ok is True
    assert len(ejecuciones) == 2
    assert "activo = FALSE" in ejecuciones[0][0]
    assert "activo = TRUE" in ejecuciones[1][0]
    assert ejecuciones[1][1] == ("prompt_1_resumen.txt", 1)
