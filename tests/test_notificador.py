"""Aviso por correo cuando un job falla.

Existe porque el 23-10-2026 el extractor tributario estuvo 8,6 h sin hacer
nada y el usuario se entero por casualidad, al entrar a mirar. El fallo
quedaba en un log que nadie lee.
"""
import importlib
import pytest


@pytest.fixture
def notif(monkeypatch):
    monkeypatch.setenv("SMTP_USER", "u")
    monkeypatch.setenv("SMTP_PASS", "p")
    from worker import notificador
    importlib.reload(notificador)
    monkeypatch.setattr(notificador, "DESTINATARIOS",
                        ["uno@ejemplo.com", "dos@ejemplo.com"])
    enviados = []

    class _SMTP:
        def __init__(self, host, port, timeout=None): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def login(self, u, p): pass
        def send_message(self, msg): enviados.append(msg)

    monkeypatch.setattr(notificador.smtplib, "SMTP_SSL", _SMTP)
    return notificador, enviados


def test_manda_a_todos_los_destinatarios(notif):
    n, enviados = notif
    assert n.avisar("job 166 abortado", "detalle") is True
    msg = enviados[0]
    assert msg["To"] == "uno@ejemplo.com, dos@ejemplo.com"
    # El asunto lleva el nombre de la materia de ESTE repo, no uno fijo.
    assert msg["Subject"] == f"[{n.NOMBRE}] job 166 abortado"


def test_no_repite_el_mismo_aviso(notif):
    """El vigilante corre cada 5 min: sin esto, un problema que dura toda la
    noche manda un correo cada 5 minutos."""
    n, enviados = notif
    assert n.avisar("a", "b", clave="job:7") is True
    assert n.avisar("a", "b", clave="job:7") is False
    assert len(enviados) == 1


def test_claves_distintas_si_se_mandan(notif):
    n, enviados = notif
    n.avisar("a", "b", clave="job:7")
    n.avisar("c", "d", clave="job:8")
    assert len(enviados) == 2


def test_sin_destinatarios_no_manda(notif, monkeypatch):
    """Con la lista vacia no se manda nada, en vez de reventar."""
    n, enviados = notif
    monkeypatch.setattr(n, "DESTINATARIOS", [])
    assert n.avisar("a", "b") is False
    assert enviados == []


def test_un_smtp_caido_no_tumba_el_job(notif, monkeypatch):
    """El aviso es secundario al trabajo: si el correo falla, se traga."""
    n, _ = notif
    def revienta(*a, **k):
        raise OSError("smtp caido")
    monkeypatch.setattr(n.smtplib, "SMTP_SSL", revienta)
    assert n.avisar("a", "b", clave="x") is False   # no lanza


def test_un_solo_correo_por_ronda_de_estancados():
    """Con 18 jobs trabados a la vez —lo que paso en civil— mandar uno por job
    serian 18 correos inutiles."""
    from pathlib import Path
    from worker import job_manager as jm
    src = Path(jm.__file__).read_text(encoding="utf-8")
    nombre = "def vigilar_jobs_estancados(" if "def vigilar_jobs_estancados(" in src \
             else "def vigilar_jobs_colgados("
    cuerpo = src[src.index(nombre):src.index("def recuperar_jobs_interrumpidos(")]
    assert cuerpo.count("avisar_en_hilo") == 1, "un solo envio por ronda, no uno por job"
    # Y fuera del bucle: dentro, seria un correo por job aunque solo haya una
    # llamada escrita. Se comprueba que el envio va despues del `for`.
    assert cuerpo.index("avisar_en_hilo") > cuerpo.rindex("    for "), \
        "el envio debe ir DESPUES del bucle, no dentro"


def test_el_nombre_identifica_al_extractor():
    """El asunto dice que extractor aviso. Sale de config/materia.py donde
    existe; el tributario no tiene ese modulo (es de una sola materia) y cae
    al respaldo 'tributario'. En los dos casos el nombre no puede ir vacio."""
    from worker import notificador
    try:
        from config.materia import SLUG
        assert notificador.NOMBRE == SLUG
    except ModuleNotFoundError:
        assert notificador.NOMBRE == "tributario"


def test_destinatarios_en_el_codigo():
    """Van hardcodeados a proposito: son estables y asi no hay que configurar
    nada al desplegar. Las credenciales SMTP si van por entorno."""
    from worker import notificador
    importlib.reload(notificador)
    assert notificador.DESTINATARIOS, "la lista no puede quedar vacia"
    assert all("@" in d for d in notificador.DESTINATARIOS)
