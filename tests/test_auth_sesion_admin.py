from unittest import mock
import pytest
from fastapi import HTTPException
import main


def _resp(code, activo=True):
    r = mock.Mock(status_code=code)
    r.json.return_value = {"data": {"isActive": activo}}
    return r


def test_clave_del_worker_sigue_valiendo(monkeypatch):
    monkeypatch.setattr(main, "WORKER_API_KEY", "k")
    main.verificar_key(x_worker_key="k", authorization=None)


def test_sesion_admin_valida(monkeypatch):
    monkeypatch.setattr(main, "WORKER_API_KEY", "k")
    main._SESIONES_ADMIN.clear()
    with mock.patch("requests.get", return_value=_resp(200)) as g:
        main.verificar_key(x_worker_key=None, authorization="Bearer abc")
        main.verificar_key(x_worker_key=None, authorization="Bearer abc")
    assert g.call_count == 1   # la segunda usa la caché


@pytest.mark.parametrize("resp", [_resp(401), _resp(200, activo=False)])
def test_sesion_invalida_o_inactiva(monkeypatch, resp):
    monkeypatch.setattr(main, "WORKER_API_KEY", "k")
    main._SESIONES_ADMIN.clear()
    with mock.patch("requests.get", return_value=resp), pytest.raises(HTTPException):
        main.verificar_key(x_worker_key="otra", authorization="Bearer xyz")


def test_sin_nada_401(monkeypatch):
    monkeypatch.setattr(main, "WORKER_API_KEY", "k")
    with pytest.raises(HTTPException):
        main.verificar_key(x_worker_key=None, authorization=None)
