"""Avisos por correo cuando un job falla.

Existe porque el 23-10-2026 el extractor tributario estuvo 8,6 h sin hacer
nada y nadie se entero: el fallo quedaba en un log que solo ve quien entra a
mirarlo. Lo mismo con los 18 jobs de civil, trabados 21,7 h.

Usa smtplib de la libreria estandar contra el MISMO buzon SMTP que ya usa el
CMS (Titan), asi que no agrega dependencias ni un servicio mas que mantener.

Quien recibe y como se llama el extractor van aqui en el codigo, no en el
entorno: son dos datos estables y asi no hay que configurar nada al desplegar.
El nombre sale de config/materia.py, asi que este archivo es identico en los
tres extractores.

Lo unico que SI tiene que estar en el entorno son las credenciales SMTP, que
no pueden vivir en el repositorio:
    SMTP_USER / SMTP_PASS        obligatorias; sin ellas no se manda nada
    SMTP_HOST / SMTP_PORT / SMTP_FROM_EMAIL   opcionales (por defecto, Titan)
"""
import os
import smtplib
import threading
import time
from email.message import EmailMessage

from worker import logger as log

# Quien se entera de los fallos. Editar aqui para cambiarlo.
DESTINATARIOS = [
    "a.vasconez@censusconsultores.com.ec",
    "j.arias@censusconsultores.com.ec",
]

def _nombre_materia() -> str:
    """'civil', 'laboral', ... desde config/materia.py. El extractor tributario
    no tiene ese modulo (es de una sola materia), asi que cae a 'tributario'."""
    try:
        from config.materia import SLUG
        return SLUG
    except Exception:
        return "tributario"

NOMBRE     = _nombre_materia()
ESPERA_MIN = 360   # no repetir el aviso del mismo job antes de 6 h

_SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.titan.email")
_SMTP_PORT = int(os.environ.get("SMTP_PORT", "465"))
_SMTP_USER = os.environ.get("SMTP_USER", "")
_SMTP_PASS = os.environ.get("SMTP_PASS", "")
_DESDE     = os.environ.get("SMTP_FROM_EMAIL", "info@lacortedicetributaria.com")

# Para no mandar el mismo aviso cada 5 min mientras el problema sigue.
_ultimo_aviso: dict = {}
_lock = threading.Lock()


def _toca_avisar(clave: str) -> bool:
    ahora = time.monotonic()
    with _lock:
        previo = _ultimo_aviso.get(clave)
        if previo is not None and (ahora - previo) < ESPERA_MIN * 60:
            return False
        _ultimo_aviso[clave] = ahora
    return True


def configurado() -> bool:
    return bool(DESTINATARIOS and _SMTP_USER and _SMTP_PASS)


def avisar(asunto: str, cuerpo: str, clave: str = "") -> bool:
    """Manda el aviso. Nunca lanza: un fallo de correo no puede tumbar un job.

    `clave` agrupa avisos repetidos (normalmente el id del job): mientras no
    pasen ALERTAS_ESPERA_MIN, el mismo no se vuelve a mandar.
    """
    if not DESTINATARIOS:
        return False
    if clave and not _toca_avisar(clave):
        return False
    if not (_SMTP_USER and _SMTP_PASS):
        log.sistema("WARN", f"Alerta sin enviar (falta SMTP_USER/SMTP_PASS): {asunto}")
        return False

    msg = EmailMessage()
    msg["Subject"] = f"[{NOMBRE}] {asunto}"
    msg["From"]    = _DESDE
    msg["To"]      = ", ".join(DESTINATARIOS)
    msg.set_content(cuerpo)

    try:
        with smtplib.SMTP_SSL(_SMTP_HOST, _SMTP_PORT, timeout=20) as s:
            s.login(_SMTP_USER, _SMTP_PASS)
            s.send_message(msg)
        log.sistema("INFO", f"Alerta enviada a {len(DESTINATARIOS)} destinatario(s): {asunto}")
        return True
    except Exception as e:
        # Se traga el error a proposito: el aviso es secundario al trabajo.
        log.sistema("ERROR", f"No se pudo enviar la alerta ({type(e).__name__}: {e}): {asunto}")
        return False


def avisar_en_hilo(asunto: str, cuerpo: str, clave: str = "") -> None:
    """Igual que avisar(), sin bloquear a quien llama. El SMTP puede tardar."""
    threading.Thread(target=avisar, args=(asunto, cuerpo, clave),
                     daemon=True, name="alerta").start()
