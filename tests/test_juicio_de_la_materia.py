"""Un número de causa puede ser varios juicios en SATJE (12312-2019-00113:
tránsito "…00113" y civil "…00113G"). Se elige el de la materia y se usa su
idJuicio exacto para todo lo demás."""
import api
from config import materia as MATERIA

MAT = next(iter(MATERIA.IDS_MATERIA_SATJE))


def test_elige_el_juicio_de_la_materia_aunque_no_sea_el_primero():
    otro = {"idJuicio": "12312201900113 ", "idMateria": 999}
    propio = {"idJuicio": "12312201900113G", "idMateria": MAT}
    assert api.elegir_de_la_materia([otro, propio]) is propio


def test_sin_juicio_de_la_materia_devuelve_el_primero():
    otro = {"idJuicio": "X", "idMateria": 999}
    assert api.elegir_de_la_materia([otro]) is otro
    assert api.elegir_de_la_materia([]) is None


def test_id_juicio_conserva_el_sufijo():
    assert api.id_juicio_de({"idJuicio": "12312201900113G "}, "12312-2019-00113") == "12312201900113G"
    assert api.id_juicio_de(None, "12312-2019-00113") == api.normalizar_numero("12312-2019-00113").strip()


def test_el_orquestador_consulta_el_juicio_elegido(monkeypatch):
    from resolucion import orquestador as orq
    pedidos = []
    monkeypatch.setattr(orq, "buscar_causa", lambda n: {"idJuicio": "12312201900113G", "idMateria": MAT})
    monkeypatch.setattr(orq, "obtener_incidentes", lambda idj: pedidos.append(idj) or [])
    orq.procesar_causa("12312-2019-00113", {"tribunal": "12312", "job_id": None})
    assert pedidos == ["12312201900113G"]


def test_entre_dos_de_la_materia_prefiere_el_juicio_real():
    depre = {"idJuicio": "02332201700064G", "idMateria": MAT, "nombreDelito": "DEPRECATORIO"}
    real = {"idJuicio": "02332201700064", "idMateria": MAT, "nombreDelito": "COBRO DE PAGARÉ A LA ORDEN"}
    assert api.elegir_de_la_materia([depre, real]) is real


def test_entre_dos_juicios_reales_el_de_mas_instancias(monkeypatch):
    insp = {"idJuicio": "01613201800605G", "idMateria": MAT, "nombreDelito": "INSPECCIÓN PREPARATORIA"}
    cobro = {"idJuicio": "01613201800605", "idMateria": MAT, "nombreDelito": "COBRO DE DINERO"}
    incs = {"01613201800605G": [1, 2], "01613201800605": [1]}
    monkeypatch.setattr(api, "obtener_incidentes", lambda idj: incs[idj])
    assert api.elegir_de_la_materia([insp, cobro]) is insp
    incs.update({"01613201800605G": [1], "01613201800605": [1]})
    assert api.elegir_de_la_materia([insp, cobro]) is cobro      # empate: el sin sufijo
