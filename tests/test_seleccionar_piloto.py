"""El selector del piloto no repite una causa entre tipos y pasa los parámetros a la consulta."""
from scripts import seleccionar_piloto as sp


class CursorFalso:
    def __init__(self): self.llamadas = []
    def execute(self, sql, params): self.llamadas.append(params)
    def fetchall(self):
        consulta, minimo, vistos, limite = self.llamadas[-1]
        return [(n, 50000, True, False) for n in ("A", "B", "C") if n not in vistos][:limite]


def test_no_repite_causas_y_respeta_excluidas():
    cur = CursorFalso()
    filas = sp.seleccionar(cur, 1, 30000, ["A"])
    numeros = [f["numero"] for f in filas]
    assert numeros == ["B", "C"] and len(set(numeros)) == len(numeros)
    assert cur.llamadas[0][1] == 30000 and len(cur.llamadas) == len(sp.TIPOS)
