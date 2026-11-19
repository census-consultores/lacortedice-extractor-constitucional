from worker import numeracion


def test_desde_2016_cinco_digitos(monkeypatch):
    monkeypatch.setattr(numeracion, "_cuatro", {("09111", "2008")})
    assert numeracion.numero("09332", 2024, 10) == "09332-2024-00010"
    assert numeracion.numero("09111", "2016", 139) == "09111-2016-00139"


def test_juzgado_antiguo_cuatro_digitos(monkeypatch):
    monkeypatch.setattr(numeracion, "_cuatro", {("09111", "2008")})
    assert numeracion.numero("09111", "2008", 139) == "09111-2008-0139"
    assert numeracion.numero("09301", "2010", 1) == "09301-2010-00001"   # no mapeado: 5
    assert numeracion.numero("09301", "2010", 1, digs=4) == "09301-2010-0001"
