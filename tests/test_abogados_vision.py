"""
Tests de resolucion/ai/abogados_vision.py — el fallback de identificación
de abogados por visión (Qwen-VL) sobre escritos escaneados.

Todo mockeado: ni pdf_a_imagenes_b64 ni httpx tocan la red real.
"""
import resolucion.ai.abogados_vision as av


def _resp_ok(payload, status=200):
    class _R:
        status_code = status
        def json(self):
            return {"choices": [{"message": {"content": payload}}]}
        text = ""
    return _R()


# ── _identificar_de_escrito ─────────────────────────────────────────────────

def test_identificar_de_escrito_exito(monkeypatch):
    monkeypatch.setattr(av, "cargar_api_key_deepinfra", lambda: "fake-key")
    monkeypatch.setattr(av, "pdf_a_imagenes_b64", lambda pdf, **kw: ["img1base64"])

    payload = '{"de_que_parte": "actor", "abogado": "Ab. Juan Perez", "evidencia": "Yo, Juan..."}'
    monkeypatch.setattr(av.httpx, "post", lambda *a, **kw: _resp_ok(payload))

    r = av._identificar_de_escrito(b"pdf-bytes", "Juan Perez", "SRI")
    assert r["de_que_parte"] == "actor"
    assert r["abogado"] == "Ab. Juan Perez"


def test_identificar_de_escrito_parsea_json_con_markdown_fence(monkeypatch):
    monkeypatch.setattr(av, "cargar_api_key_deepinfra", lambda: "fake-key")
    monkeypatch.setattr(av, "pdf_a_imagenes_b64", lambda pdf, **kw: ["img1"])

    payload = '```json\n{"de_que_parte": "demandado", "abogado": "Dra. Maria Lopez", "evidencia": "x"}\n```'
    monkeypatch.setattr(av.httpx, "post", lambda *a, **kw: _resp_ok(payload))

    r = av._identificar_de_escrito(b"pdf-bytes", "Juan Perez", "SRI")
    assert r["abogado"] == "Dra. Maria Lopez"


def test_identificar_de_escrito_sin_api_key(monkeypatch):
    monkeypatch.setattr(av, "cargar_api_key_deepinfra", lambda: "")
    assert av._identificar_de_escrito(b"x", "A", "B") == {}


def test_identificar_de_escrito_sin_imagenes(monkeypatch):
    monkeypatch.setattr(av, "cargar_api_key_deepinfra", lambda: "fake-key")
    monkeypatch.setattr(av, "pdf_a_imagenes_b64", lambda pdf, **kw: [])
    assert av._identificar_de_escrito(b"x", "A", "B") == {}


def test_identificar_de_escrito_reintenta_y_se_recupera(monkeypatch):
    monkeypatch.setattr(av, "cargar_api_key_deepinfra", lambda: "fake-key")
    monkeypatch.setattr(av, "pdf_a_imagenes_b64", lambda pdf, **kw: ["img1"])

    llamadas = {"n": 0}
    def _post(*a, **kw):
        llamadas["n"] += 1
        if llamadas["n"] == 1:
            return _resp_ok("", status=500)
        return _resp_ok('{"de_que_parte": "actor", "abogado": "Ab. X", "evidencia": "y"}')
    monkeypatch.setattr(av.httpx, "post", _post)

    r = av._identificar_de_escrito(b"x", "A", "B")
    assert llamadas["n"] == 2
    assert r["abogado"] == "Ab. X"


def test_identificar_de_escrito_agota_reintentos(monkeypatch):
    monkeypatch.setattr(av, "cargar_api_key_deepinfra", lambda: "fake-key")
    monkeypatch.setattr(av, "pdf_a_imagenes_b64", lambda pdf, **kw: ["img1"])
    monkeypatch.setattr(av.httpx, "post", lambda *a, **kw: _resp_ok("", status=500))

    assert av._identificar_de_escrito(b"x", "A", "B") == {}


# ── completar_abogados_con_escritos ─────────────────────────────────────────

def test_completar_ambos_de_distintos_escritos(monkeypatch):
    respuestas = [
        {"de_que_parte": "demandado", "abogado": "Dra. Lopez"},
        {"de_que_parte": "actor", "abogado": "Ab. Perez"},
    ]
    monkeypatch.setattr(av, "_identificar_de_escrito", lambda *a, **kw: respuestas.pop(0))

    r = av.completar_abogados_con_escritos(
        [b"pdf1", b"pdf2"], actor="Perez", demandado="SRI",
        falta_actor=True, falta_demandado=True,
    )
    assert r == {"abogado_demandado": "Dra. Lopez", "abogado_actor": "Ab. Perez"}


def test_completar_para_apenas_encuentra_todo(monkeypatch):
    llamados = {"n": 0}
    def _fake(*a, **kw):
        llamados["n"] += 1
        return {"de_que_parte": "actor", "abogado": "Ab. Perez"}
    monkeypatch.setattr(av, "_identificar_de_escrito", _fake)

    r = av.completar_abogados_con_escritos(
        [b"pdf1", b"pdf2", b"pdf3"], actor="Perez", demandado="SRI",
        falta_actor=True, falta_demandado=False,
    )
    assert r == {"abogado_actor": "Ab. Perez"}
    assert llamados["n"] == 1  # no sigue probando escritos si ya no falta nada


def test_completar_ignora_no_determinado(monkeypatch):
    respuestas = [
        {"de_que_parte": "no_determinado", "abogado": "no consta"},
        {"de_que_parte": "actor", "abogado": "Ab. Perez"},
    ]
    monkeypatch.setattr(av, "_identificar_de_escrito", lambda *a, **kw: respuestas.pop(0))

    r = av.completar_abogados_con_escritos(
        [b"pdf1", b"pdf2"], actor="Perez", demandado="SRI",
        falta_actor=True, falta_demandado=False,
    )
    assert r == {"abogado_actor": "Ab. Perez"}


def test_completar_sin_nada_que_completar_no_llama_ia(monkeypatch):
    monkeypatch.setattr(av, "_identificar_de_escrito", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("no deberia llamarse")))

    r = av.completar_abogados_con_escritos(
        [b"pdf1"], actor="Perez", demandado="SRI",
        falta_actor=False, falta_demandado=False,
    )
    assert r == {}


def test_completar_respeta_max_escritos(monkeypatch):
    llamados = {"n": 0}
    def _fake(*a, **kw):
        llamados["n"] += 1
        return {"de_que_parte": "demandado", "abogado": "no consta"}
    monkeypatch.setattr(av, "_identificar_de_escrito", _fake)

    av.completar_abogados_con_escritos(
        [b"p1", b"p2", b"p3", b"p4", b"p5"], actor="A", demandado="B",
        falta_actor=True, falta_demandado=True, max_escritos=2,
    )
    assert llamados["n"] == 2
