"""jm._conn reutiliza conexiones y descarta las que fallaron o se cerraron."""
import queue

import pytest

from worker import job_manager as jm


class _FakeConn:
    def __init__(self):
        self.closed = 0
        self.commits = self.rollbacks = 0

    def __enter__(self):
        return self

    def __exit__(self, tipo, *_):
        if tipo:
            self.rollbacks += 1
        else:
            self.commits += 1
        return False

    def close(self):
        self.closed = 1


@pytest.fixture
def conexiones(monkeypatch):
    creadas = []

    def connect(_url):
        creadas.append(_FakeConn())
        return creadas[-1]

    monkeypatch.setattr(jm.psycopg2, "connect", connect)
    monkeypatch.setattr(jm, "_IDLE", queue.LifoQueue())
    return creadas


def test_reutiliza_y_hace_commit(conexiones):
    with jm._conn() as a:
        pass
    with jm._conn() as b:
        pass
    assert a is b and len(conexiones) == 1 and a.commits == 2


def test_descarta_tras_excepcion(conexiones):
    with pytest.raises(ValueError):
        with jm._conn() as a:
            raise ValueError
    assert a.rollbacks == 1 and a.closed
    with jm._conn() as b:
        pass
    assert b is not a


def test_reemplaza_conexion_cerrada(conexiones):
    with jm._conn() as a:
        pass
    a.closed = 2  # el servidor la cortó
    with jm._conn() as b:
        pass
    assert b is not a and len(conexiones) == 2
