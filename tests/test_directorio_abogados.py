"""Directorio de abogados: extracción sin IA desde el texto de las sentencias."""
from scripts.directorio_abogados import Directorio, clave_nombre, extraer


def _por_nombre(texto):
    return {a["nombre"]: a for a in extraer(texto)}


def test_razon_de_notificacion_satje():
    texto = ("notifiqué la SENTENCIA que antecede a: GARCIA LOPEZ MARIA en el casillero "
             "electrónico No.0912345678 correo electrónico abgjperez@gmail.com, "
             "estudio.perez@hotmail.com. del Dr./Ab. PEREZ RODRIGUEZ JUAN CARLOS; "
             "a: BANCO XYZ S.A. en el correo electrónico mlopez@lopezabogados.ec. "
             "del Dr./Ab. LOPEZ MORA MARTHA; Certifico: ")
    a = _por_nombre(texto)
    assert a["PEREZ RODRIGUEZ JUAN CARLOS"]["emails"] == {"abgjperez@gmail.com",
                                                          "estudio.perez@hotmail.com"}
    assert a["LOPEZ MORA MARTHA"]["emails"] == {"mlopez@lopezabogados.ec"}


def test_cuerpo_con_matricula_y_telefono():
    texto = ("comparece patrocinado por el Ab. Juan Carlos Pérez Rodríguez, matrícula "
             "09-2010-123 del Foro de Abogados, celular 0991234567, quien señala el "
             "correo jcperez@gmail.com para sus notificaciones.")
    a = _por_nombre(texto)["Juan Carlos Pérez Rodríguez"]
    assert a["matricula"] == "09-2010-123"
    assert a["telefono"] == "0991234567"
    assert a["emails"] == {"jcperez@gmail.com"}


def test_mayusculas_cortan_el_nombre():
    a = _por_nombre("el AB. PEREZ LOPEZ JUAN EN CALIDAD DE PROCURADOR JUDICIAL, MATRICULA 1234")
    assert a["PEREZ LOPEZ JUAN"]["matricula"] == "1234"


def test_excluye_funcionarios_y_dominios_publicos():
    texto = ("Dr. Pedro Andrade Salazar, JUEZ DE LA UNIDAD JUDICIAL CIVIL. "
             "Ab. Rosa Vera Ortiz, Secretaria. "
             "el Ab. Luis Mena Paz, defensor público, correo lmena@defensoria.gob.ec. "
             "La Ab. Ana Ríos Cano señala ana.rios@funcionjudicial.gob.ec")
    # Ana usa un correo del Estado para notificaciones: es funcionaria y también sale.
    assert _por_nombre(texto) == {}


def test_correo_lejano_no_se_asigna():
    texto = "Ab. Juan Pérez Mora comparece." + " texto" * 100 + " actor@gmail.com"
    assert _por_nombre(texto)["Juan Pérez Mora"]["emails"] == set()


def test_mismo_abogado_en_otro_orden_y_matricula_une():
    assert clave_nombre("PÉREZ RODRÍGUEZ JUAN") == clave_nombre("Juan Pérez Rodríguez")
    d = Directorio()
    d.agregar("09332-2024-00001", "tribunal", "Ab. Juan Pérez Rodríguez, Mat. 09-2010-123.", "09332", "2024")
    d.agregar("09332-2024-00002", "apelacion", "del Dr./Ab. PEREZ RODRIGUEZ JUAN; ", "09332", "2024")
    d.agregar("09332-2025-00003", "tribunal", "Ab. Juan Carlos Pérez, Mat. 09-2010-123, correo jp@gmail.com", "09332", "2025")
    filas = d.filas("civil")
    assert len(filas) == 1
    f = filas[0]
    assert f["n_causas"] == 3 and f["emails"] == ["jp@gmail.com"]
    assert f["matriculas"] == ["09-2010-123"] and f["anio_min"] == "2024"


def test_ruido_de_una_sola_causa_se_descarta():
    d = Directorio()
    d.agregar("1", "tribunal", "el Dr. Mario Salas Ruiz dijo", "", "")
    assert d.filas("civil") == []


def test_notificacion_correo_de_parte_sin_abogado_no_se_pega():
    from scripts.directorio_abogados import extraer_notificacion
    texto = ("a: PEREZ JUAN en el casillero electrónico No.0912345678 correo electrónico "
             "jperez@gmail.com. del Dr./Ab. MORA VERA ANA; a: LOPEZ ROSA en el correo "
             "electrónico rosa.lopez@hotmail.com; Certifico.")
    a = {x["nombre"]: x for x in extraer_notificacion(texto)}
    assert a["MORA VERA ANA"]["emails"] == {"jperez@gmail.com"}


def test_actuaciones_satje_y_directorio_con_menciones():
    from scripts.abogados_satje import menciones_de_actuaciones
    acts = [
        {"tipo": "RAZON DE NOTIFICACION", "actividad":
         "<p>a: X S.A. en el correo electrónico <b>legal@estudio.ec</b> del Dr./Ab. "
         "CANO RUIZ PEDRO; a: Y en el correo y@gmail.com del Dr./Ab. CANO RUIZ PEDRO;</p>"},
        {"tipo": "AUTO", "actividad": "Agréguese al proceso."},
    ]
    n, menciones = menciones_de_actuaciones(acts)
    assert n == 1 and len(menciones) == 1
    assert menciones[0]["emails"] == ["legal@estudio.ec", "y@gmail.com"]
    d = Directorio()
    d.agregar("17230-2024-00001", "satje", "", "17230", "2024",
              menciones=[dict(m, emails=set(m["emails"])) for m in menciones])
    assert d.filas("civil")[0]["emails"] == ["legal@estudio.ec", "y@gmail.com"]


def test_correo_propio_del_abogado_va_primero():
    from collections import Counter
    from scripts.directorio_abogados import ordenar_correos
    c = Counter({"kloja@jep.coop": 5, "wvintimilla@jep.coop": 2, "abwilsonvintimilla@gmail.com": 1})
    assert ordenar_correos(clave_nombre("WILSON RODRIGO VINTIMILLA RIERA"), c) == [
        "wvintimilla@jep.coop", "abwilsonvintimilla@gmail.com", "kloja@jep.coop"]


def test_abogados_satje_envia_por_tandas(monkeypatch):
    """No crea un Future por causa de golpe (OOM del 13-nov en civil)."""
    import sys
    import scripts.abogados_satje as s
    enviados, maximo = [0], [0]

    def procesar(n):
        enviados[0] += 1
        return {"n": 0, "menciones": [], "error": None, "seg": 0}

    import concurrent.futures as cf
    real_map = cf.ThreadPoolExecutor.map

    def map_(self, fn, it):
        lote = list(it)
        maximo[0] = max(maximo[0], len(lote))
        return real_map(self, fn, lote)

    monkeypatch.setattr(cf.ThreadPoolExecutor, "map", map_)
    monkeypatch.setattr(s, "procesar", procesar)
    monkeypatch.setattr(s, "pendientes", lambda *a: iter([str(i) for i in range(230)]))
    monkeypatch.setattr(s, "contar_pendientes", lambda *a: 230)
    monkeypatch.setitem(sys.modules, "api", type(sys)("api"))
    sys.modules["api"].activar_proxy_desde_entorno = lambda: (True, "proxy")

    class _Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, *a): pass

    class _Conn(_Cur):
        def cursor(self): return _Cur()

    from worker import job_manager as jm
    monkeypatch.setattr(jm, "_conn", lambda: _Conn())

    class _Candado:                     # pg_try_advisory_lock → libre
        autocommit = False
        def cursor(self):
            c = _Cur()
            c.fetchone = lambda: (True,)
            return c
    import psycopg2
    monkeypatch.setattr(psycopg2, "connect", lambda *a, **k: _Candado())
    monkeypatch.setattr(sys, "argv", ["x", "--hilos", "3"])
    s.main()
    assert enviados[0] == 230 and maximo[0] <= s.TANDA


def test_consolidado_fusiona_materias_y_filtra_seguro():
    from scripts.directorio_abogados import _filtros, fusionar
    f = fusionar("CANO PEDRO RUIZ", "CANO RUIZ PEDRO", 3, "2023", "2025", [
        {"materia": "civil", "n": 2, "emails": ["legal@estudio.ec", "pcano@gmail.com"],
         "matriculas": [], "telefonos": [], "tribunales": {"09332": 1, "17230": 1}, "causas": ["a"]},
        {"materia": "laboral", "n": 1, "emails": ["pcano@gmail.com"],
         "matriculas": [], "telefonos": [], "tribunales": {"01331": 1}, "causas": ["b"]},
    ])
    assert f["correo"] == "pcano@gmail.com" and f["otros_correos"] == ["legal@estudio.ec"]
    assert f["materias"] == {"civil": 2, "laboral": 1} and f["provincias"] == ["01", "09", "17"]
    where, params = _filtros("Pérez' OR 1=1", "civil", "09", True)
    assert "%s" in where and "OR 1=1" not in where       # el texto va como parámetro
    assert params[1:3] == ["civil", "09"] and "%PEREZ'%" in params


def test_solo_se_excluye_lo_publico():
    import re
    from scripts.directorio_abogados import INSTITUCION_REGEX, extraer_notificacion
    texto = ("a: PEREZ JUAN correo provjudiciales@coop.fin.ec del Dr./Ab. FISCALIA GENERAL DEL ESTADO; "
             "a: LOPEZ ANA correo x@gmail.com del Dr./Ab. DEFENSORÍA PÚBLICA AZUAY PENAL; "
             "a: BANCO X correo legal@coop.fin.ec del Dr./Ab. COOPERATIVA JARDIN AZUAYO; "
             "a: MORA LUIS correo jvera@fiscalia.gob.ec del Dr./Ab. VERA CANO JOSE; "
             "a: MORA LUIS correo lmora@gmail.com del Dr./Ab. VERA CANO LUIS;")
    nombres = [a["nombre"] for a in extraer_notificacion(texto)]
    assert nombres == ["COOPERATIVA JARDIN AZUAYO", "VERA CANO LUIS"]
    assert re.search(INSTITUCION_REGEX, clave_nombre("FISCALIA GENERAL DEL ESTADO"))
    assert re.search(INSTITUCION_REGEX, clave_nombre("DEFENSORÍA PÚBLICA AZUAY PENAL"))
    assert not re.search(INSTITUCION_REGEX, clave_nombre("COOPERATIVA JARDIN AZUAYO"))


def test_arranque_automatico_de_abogados(monkeypatch, tmp_path):
    import subprocess
    import main
    lanzados, avisos = [], []

    class Log:
        def sistema(self, nivel, msg): avisos.append((nivel, msg))

    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kw: lanzados.append(cmd))
    monkeypatch.setattr("builtins.open", lambda *a, **k: (tmp_path / "log").open("ab"))
    for valor in ("", "off"):
        monkeypatch.setenv("ABOGADOS_AUTOMATICO", valor)
        main._lanzar_abogados(Log())
    assert lanzados == []
    monkeypatch.setenv("ABOGADOS_AUTOMATICO", "seis")
    main._lanzar_abogados(Log())
    assert lanzados == [] and avisos[-1][0] == "ERROR"
    monkeypatch.setenv("ABOGADOS_AUTOMATICO", "6")
    main._lanzar_abogados(Log())
    assert lanzados[-1][-3:] == ["scripts/abogados_satje.py", "--hilos", "6"]


def test_con_freno_activo_espera_y_no_anota_error(monkeypatch):
    import sys
    import time as _t
    import scripts.abogados_satje as s
    api = type(sys)("api")
    api._freno_hasta = 0
    monkeypatch.setitem(sys.modules, "api", api)
    llamadas, dormido = [], []

    def consultar(n):
        llamadas.append(n)
        if len(llamadas) == 1:                 # la 1.ª consulta activa el freno y falla
            api._freno_hasta = _t.monotonic() + 30
            raise RuntimeError("freno de emergencia activo")
        return 2, ["ok"]

    def dormir(seg):
        dormido.append(seg)
        api._freno_hasta = 0                   # el freno se suelta mientras espera

    monkeypatch.setattr(s, "consultar", consultar)
    monkeypatch.setattr(s.time, "sleep", dormir)
    assert s.consultar_esperando_freno("x") == (2, ["ok"])
    assert len(llamadas) == 2 and dormido and dormido[0] <= 60
