"""Mapeo de volumen (worker/mapeo_worker.py): búsqueda del último secuencial y estimación."""
from worker import mapeo_worker as mw


def test_ultimo_secuencial_encuentra_el_borde():
    for ultimo in (0, 1, 2, 63, 64, 65, 1000, 37_421, 99_999):
        llamadas = []
        def existe(n):
            llamadas.append(n)
            return n <= ultimo
        assert mw.ultimo_secuencial(existe) == ultimo
        assert len(llamadas) < 40


def test_contar_par_tolera_huecos_cortos(monkeypatch):
    # 120 causas con los números 50 y 51 anulados.
    existentes = set(range(1, 121)) - {50, 51}
    monkeypatch.setattr(mw, "_consultar",
                        lambda numero, pausa: [{"idMateria": 1, "nombreDelito": "ROBO"}]
                        if int(numero.split("-")[-1]) in existentes else [])
    ultimo, muestras = mw.contar_par("09286", "2024", 0)
    assert ultimo == 120
    assert len(muestras) == 10 - sum(s in (50, 51) for s in mw.posiciones_muestra(120))


def test_se_cuenta_excluye_apelacion():
    assert mw.se_cuenta("Unidad Judicial Penal con sede en el cantón Guayaquil", mw.CLAVES_PENAL)
    assert not mw.se_cuenta("Sala Penal de la Corte Provincial de Justicia del Guayas", mw.CLAVES_PENAL)
    assert not mw.se_cuenta("Unidad Judicial Civil Cuenca", mw.CLAVES_PENAL)


def test_estimar_reparte_por_muestra():
    r = mw.estimar([{"prefijo": "09286", "nombre": "U. Penal", "anio": "2024", "ultimo": 1000,
                     "muestras": [{"id": 1, "m": "PENAL", "d": "ROBO"}] * 3 + [{"id": 2, "m": "TRANSITO", "d": "X"}]}])
    assert r["total_numeros"] == 1000
    assert r["por_materia_estimado"][0] == {"clave": "1 PENAL", "causas": 750}
