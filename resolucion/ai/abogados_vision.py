"""
Fallback de identificación de abogados vía visión (Qwen-VL, DeepInfra).

Cuando prompt_2_extraccion.txt no trae "Abogado del Actor"/"Abogado del
Demandado" en el texto de la sentencia (config.pdf_utils.buscar_abogados
devuelve vacío), se recurre a leer los escritos más antiguos del
expediente — la mayoría son PDFs escaneados, sin capa de texto — con un
modelo de visión, pidiéndole que primero determine DE QUÉ PARTE es el
escrito (anclado a los nombres ya conocidos de actor/demandado) y recién
después busque la firma del abogado que lo patrocina.

Validado empíricamente contra ground truth real (Strapi) antes de
construirlo:
  - Regex sobre notificaciones:        42% acierto
  - IA sobre notificaciones:           25% acierto (peor, no es problema
    de herramienta — el dato de origen es ambiguo)
  - Tabla de comparecientes en actas:   8% acierto, 71% sin acta útil
  - Visión + autoidentificación de parte, sobre escritos:  87.5% (GPT-4o),
    80% (Qwen2.5-VL-32B, sobre las llamadas que no dieron timeout)

Corre en el worker en background — sin usuario esperando una respuesta
en vivo — así que timeout generoso y reintentos son aceptables a cambio
de un costo ~15x menor que GPT-4o vía DeepInfra. Deliberadamente SOLO
Qwen, sin fallback a GPT: la idea es que corra 24/7 de forma barata: si
falla una vez, se reintenta la corrida completa igual que cualquier otro
error del pipeline.
"""
import json

import httpx

from resolucion.ai._cliente import cargar_api_key_deepinfra, DEEPINFRA_BASE_URL
from config.pdf_utils import pdf_a_imagenes_b64
from worker import logger as log

MODELO_VISION = "Qwen/Qwen2.5-VL-32B-Instruct"
MAX_PAGINAS_POR_ESCRITO = 6
MAX_ESCRITOS_A_PROBAR = 3
TIMEOUT_SEG = 300  # generoso a propósito — corre en background, no en vivo
REINTENTOS = 2


def _vacio(v) -> bool:
    s = (v or "").strip().lower()
    return not s or "no consta" in s or len(s) < 4


def _prompt_identificacion(actor: str, demandado: str) -> str:
    from config import materia as MATERIA
    return f"""Este es un escrito judicial ecuatoriano de una causa en materia {MATERIA.NOMBRE.lower()}.

PARTE ACTORA (demandante): {actor or "no especificado"}
PARTE DEMANDADA: {demandado or "no especificado"}

Instrucciones:
1. Primero determina DE QUE PARTE es este escrito, leyendo como se identifica quien lo presenta (ej: "Yo, [actor], compareciendo por mis propios derechos..." vs "[procurador], en mi calidad de procurador judicial de [demandado]..."). Decidi explicitamente: es un escrito de la parte ACTORA o de la parte DEMANDADA, o no se puede determinar.
2. Busca la firma del abogado que patrocina a ESA parte especificamente (con Ab./Dr. y numero de matricula/foro).
3. Si no podes determinar con confianza de que parte es, o no hay firma de abogado legible, decilo.

Responde SOLO este JSON:
{{"de_que_parte": "actor" o "demandado" o "no_determinado", "abogado": "nombre o no consta", "evidencia": "frase textual donde se identifica de que parte es el escrito"}}"""


def _identificar_de_escrito(
    pdf_bytes: bytes, actor: str, demandado: str, job_id: int = None, causa: str = None
) -> dict:
    """
    Convierte un escrito a imágenes y le pide al modelo de visión que
    identifique de qué parte es y quién lo patrocina. Reintenta ante
    timeout/error de red — no ante una respuesta válida que diga
    "no_determinado" (eso es una respuesta real, no una falla).

    Retorna {} si no se pudo obtener una respuesta tras los reintentos.
    """
    api_key = cargar_api_key_deepinfra()
    if not api_key:
        log.warn("DEEPINFRA_API_KEY no configurada — fallback de abogados no disponible", job_id=job_id, causa=causa)
        return {}

    try:
        imagenes = pdf_a_imagenes_b64(pdf_bytes, max_paginas=MAX_PAGINAS_POR_ESCRITO, dpi=150)
    except Exception as e:
        log.warn(f"No se pudo convertir escrito a imágenes: {type(e).__name__}: {e}", job_id=job_id, causa=causa)
        return {}
    if not imagenes:
        return {}

    content = [{"type": "text", "text": _prompt_identificacion(actor, demandado)}]
    for img in imagenes:
        content.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img}"}})

    for intento in range(REINTENTOS + 1):
        try:
            resp = httpx.post(
                f"{DEEPINFRA_BASE_URL}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": MODELO_VISION,
                    "temperature": 0,
                    "max_tokens": 300,
                    "messages": [{"role": "user", "content": content}],
                    "extra_body": {"enable_thinking": False},
                },
                timeout=TIMEOUT_SEG,
            )
            if resp.status_code != 200:
                log.warn(
                    f"Qwen-VL HTTP {resp.status_code} (intento {intento + 1}/{REINTENTOS + 1})",
                    job_id=job_id, causa=causa,
                )
                continue
            raw = resp.json()["choices"][0]["message"]["content"].strip()
            if "```" in raw:
                raw = raw.split("```")[1]
                raw = raw[4:] if raw.lstrip().lower().startswith("json") else raw
                raw = raw.strip()
            return json.loads(raw)
        except Exception as e:
            log.warn(
                f"Qwen-VL {type(e).__name__}: {e} (intento {intento + 1}/{REINTENTOS + 1})",
                job_id=job_id, causa=causa,
            )
    return {}


def completar_abogados_con_escritos(
    escritos_pdf_bytes: list,
    actor: str,
    demandado: str,
    falta_actor: bool,
    falta_demandado: bool,
    max_escritos: int = MAX_ESCRITOS_A_PROBAR,
    job_id: int = None,
    causa: str = None,
) -> dict:
    """
    Recorre hasta max_escritos PDFs (bytes ya descargados, del más
    antiguo al más reciente) buscando el/los abogado(s) que falten.
    Para en cuanto encuentra todo lo que faltaba o se acaban los
    escritos a probar.

    Retorna dict con SOLO las claves que se pudieron completar:
      {"abogado_actor": "..."} y/o {"abogado_demandado": "..."}
    """
    resultado = {}
    if not falta_actor and not falta_demandado:
        return resultado

    for pdf_bytes in escritos_pdf_bytes[:max_escritos]:
        if not falta_actor and not falta_demandado:
            break
        r = _identificar_de_escrito(pdf_bytes, actor, demandado, job_id=job_id, causa=causa)
        parte = r.get("de_que_parte")
        abogado = (r.get("abogado") or "").strip()
        if _vacio(abogado):
            continue
        if parte == "actor" and falta_actor and "abogado_actor" not in resultado:
            resultado["abogado_actor"] = abogado
            falta_actor = False
        elif parte == "demandado" and falta_demandado and "abogado_demandado" not in resultado:
            resultado["abogado_demandado"] = abogado
            falta_demandado = False

    return resultado
