"""Índice rápido (worker/indice_worker.py): un corte breve de SATJE no abandona el par."""
import threading
from worker import indice_worker as iw


def _preparar(monkeypatch, respuestas):
    guardados = []
    monkeypatch.setattr(iw, "_progreso", lambda p, a: {"terminado": False, "ultimo": 0})
    monkeypatch.setattr(iw, "_guardar", lambda p, a, lote, ultimo, term: guardados.append((len(lote), ultimo, term)))
    monkeypatch.setattr(iw, "ESPERA_CORTE_S", 0)
    monkeypatch.setattr(iw, "MIN_ANTES_DE_CORTAR", 1)
    def cab(numero):
        r = respuestas.pop(0) if respuestas else None
        if isinstance(r, Exception):
            raise r
        return r
    monkeypatch.setattr(iw, "consultar_cabecera", cab)
    return guardados


def test_reintenta_tras_un_corte_breve(monkeypatch):
    # causa, dos errores de red, causa, y luego vacías hasta cortar.
    g = _preparar(monkeypatch, [{"idMateria": 27}, OSError(), OSError(), {"idMateria": 27}])
    consultados, hallados = iw.barrer_par("01283", "2019", 0, 2, threading.Event())
    assert hallados == 2 and g[-1][2] is True      # el par terminó, no se abandonó


def test_abandona_si_satje_sigue_caido(monkeypatch):
    g = _preparar(monkeypatch, [{"idMateria": 27}] + [OSError()] * (iw.REINTENTOS_CORTE + 1))
    consultados, hallados = iw.barrer_par("01283", "2019", 0, 2, threading.Event())
    assert g[-1] == (1, 1, False)                   # guarda lo hecho y queda pendiente
