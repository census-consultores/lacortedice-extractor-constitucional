"""Una caída de SATJE no debe confundirse con 'sin actuaciones' ni con causa inexistente."""
import pytest
import requests

import api


def _caido(*a, **k):
    raise requests.exceptions.ReadTimeout("Read timed out.")


class _Resp:
    def __init__(self, data):
        self._d = data
    def raise_for_status(self):
        pass
    def json(self):
        return self._d


@pytest.fixture(autouse=True)
def _sin_pausas(monkeypatch):
    monkeypatch.setattr(api.time, "sleep", lambda *_: None)


def test_actuaciones_levanta_si_satje_no_responde(monkeypatch):
    monkeypatch.setattr(api, "_post", _caido)
    inc = {"idJudicatura": "09332", "lstIncidenteJudicatura": [{"idMovimientoJuicioIncidente": 1}]}
    with pytest.raises(api.SatjeNoDisponible):
        api.obtener_actuaciones("09332202500002", inc)


def test_actuaciones_vacias_reales_siguen_siendo_lista(monkeypatch):
    monkeypatch.setattr(api, "_post", lambda *a, **k: _Resp([]))
    inc = {"idJudicatura": "09332", "lstIncidenteJudicatura": [{"idMovimientoJuicioIncidente": 1}]}
    assert api.obtener_actuaciones("09332202500002", inc) == []


def test_buscar_causa_levanta_si_satje_no_responde(monkeypatch):
    monkeypatch.setattr(api, "_post", _caido)
    with pytest.raises(api.SatjeNoDisponible):
        api.buscar_causa("09332-2025-00002")


def test_buscar_causa_inexistente_devuelve_none(monkeypatch):
    monkeypatch.setattr(api, "_post", lambda *a, **k: _Resp([]))
    assert api.buscar_causa("09332-2025-99999") is None


def test_incidentes_levanta_si_satje_no_responde(monkeypatch):
    monkeypatch.setattr(api, "_get", _caido)
    with pytest.raises(api.SatjeNoDisponible):
        api.obtener_incidentes("09332202500002")


def test_fila_base_reextraccion_sin_objetos_fecha():
    """Las fechas que vienen de Postgres se guardan como texto ISO (JSON serializable)."""
    import json
    from datetime import date
    from worker.reextraccion_worker import _fila_base
    fila = _fila_base({"numero": "09359-2024-00556"},
                      {"fecha_inicio_caso": date(2024, 3, 1), "fecha_sentencia_caso": date(2024, 9, 2)})
    assert fila["_fecha_inicio_caso"] == "2024-03-01"
    json.dumps(fila)


def test_marca_reextraccion_conserva_todos_los_niveles():
    """El runner parte 'workers' por comas: los niveles deben sobrevivir a eso."""
    from worker.reextraccion_worker import parsear_marca
    marca = "reextraccion:niveles=" + "+".join(["tribunal", "apelacion", "corte"])
    activos = [w.strip() for w in marca.split(",") if w.strip()]
    assert parsear_marca(activos[0])["niveles"] == ["tribunal", "apelacion", "corte"]


def test_normalizar_clasificacion_pone_nombre_y_numero_del_catalogo():
    """Toma el número real del catálogo escrito junto al nombre y completa el
    nombre cuando el modelo solo pone el número (vale para cualquier materia)."""
    from resolucion.ai._cliente import normalizar_clasificacion, _catalogo_estructurado
    cats = _catalogo_estructurado()
    a, b = sorted(cats)[-1], sorted(cats)[1]
    sub_a = cats[a][1][0]
    texto = (f"Catálogo Aplicable:\nX\nCategoría 1:\n{a}. {cats[a][0]}\n• Subcategoría:\n1. {sub_a}\n"
             f"Categoría {b}:\n• Subcategoría:\n{cats[b][1][0]}\n")
    _, res = normalizar_clasificacion(texto)
    assert res[0][:2] == (a, cats[a][0]) and res[0][2] == [sub_a]
    assert res[1][:2] == (b, cats[b][0])


def test_quitar_cedulas_conserva_ruc():
    from resolucion.ai._cliente import quitar_cedulas
    t = quitar_cedulas("Juan Pérez, con cédula de identidad #0801011313, contra X Cía. Ltda., RUC 1790142981001")
    assert "0801011313" not in t and "1790142981001" in t
