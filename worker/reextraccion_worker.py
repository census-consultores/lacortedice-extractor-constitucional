"""
Worker de RE-EXTRACCIÓN: corre la IA sobre los PDFs YA guardados (Strapi),
sin volver a SATJE.

Es la pieza que separa "bajar" de "entender": el corpus se descarga una
vez (POST /jobs con solo_descarga=true) y la extracción se repite tantas
veces como cambien los prompts o el modelo, leyendo el PDF desde Strapi.

Selección de causas (POST /reextraer → workers="reextraccion:…"):
  - una lista explícita de números (worker_lista_items del job), o
  - un filtro SQL sobre worker_causa_datos: tribunal/año del job ("-" = todos),
    completitud (p. ej. "PDF sin análisis", "Parcial - revisar") y límite.

Niveles: tribunal (prompts 1–4 + 7/8/9/10), corte (5/6), apelacion (11/12).
Cada nivel se procesa solo si la causa tiene PDF de ese nivel guardado.

El resultado se persiste igual que una extracción normal (cms_tribunal /
cms_corte / cms_apelacion + registrar_caso), reemplazando la ficha anterior.
"""
import re
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

import httpx

from config.config import PROJECT_ROOT, TRIBUNALES
from resolucion.cms import tribunal as cms_tribunal
from resolucion.cms import corte as cms_corte
from resolucion.cms import apelacion as cms_apelacion
from resolucion.extractores.tribunal import extraer as extraer_tribunal
from resolucion.extractores.corte import extraer as extraer_corte
from resolucion.extractores.apelacion import extraer as extraer_apelacion
from worker import job_manager as jm
from worker import logger as log
from worker.lista_worker import _persistir_extras_tribunal, _doc_id
from worker.runner import _subir_archivos_strapi
from worker.strapi_client import StrapiClient, STRAPI_URL

PAUSA_SEG = 1.0
NIVELES_VALIDOS = ("tribunal", "corte", "apelacion")


def _now():
    return datetime.now(timezone.utc)


def parsear_marca(marca: str) -> dict:
    """
    "reextraccion[:niveles=tribunal,corte][:completitud=PDF sin análisis][:limite=200]"
    → {"niveles": [...], "completitud": str|None, "limite": int|None}
    """
    partes = marca.split(":")[1:]
    out = {"niveles": list(NIVELES_VALIDOS), "completitud": None, "limite": None}
    for p in partes:
        if p.startswith("niveles="):
            niv = [n.strip() for n in re.split(r"[,+]", p[8:]) if n.strip() in NIVELES_VALIDOS]
            if niv:
                out["niveles"] = niv
        elif p.startswith("completitud="):
            out["completitud"] = p[12:].strip() or None
        elif p.startswith("limite=") and p[7:].isdigit():
            out["limite"] = int(p[7:])
    return out


def _url_absoluta(url: str) -> str:
    if not url:
        return ""
    if url.startswith("http://") or url.startswith("https://"):
        return url
    return STRAPI_URL.rstrip("/") + "/" + url.lstrip("/")


def descargar_pdf_guardado(url: str, job_id: int = None, causa: str = None) -> bytes | None:
    """Baja un PDF desde la URL que Strapi devolvió al subirlo. Reintenta 3 veces."""
    url = _url_absoluta(url)
    if not url:
        return None
    for intento in range(3):
        try:
            r = httpx.get(url, timeout=60, follow_redirects=True)
            if r.status_code == 200 and r.content:
                return r.content
            log.warn(f"PDF guardado respondió HTTP {r.status_code} (intento {intento + 1}/3)",
                     job_id=job_id, causa=causa)
        except Exception as e:
            log.warn(f"PDF guardado: {type(e).__name__}: {e} (intento {intento + 1}/3)",
                     job_id=job_id, causa=causa)
        time.sleep(2 * (intento + 1))
    return None


def _sin_fechas(fila: dict) -> dict:
    """Postgres devuelve DATE/TIMESTAMP como objetos date/datetime, que no se
    pueden serializar a JSON al guardar en Strapi: se pasan a texto ISO."""
    from datetime import date as _date
    return {k: (v.isoformat() if isinstance(v, _date) else v) for k, v in fila.items()}


def _fila_base(datos: dict, causa: dict) -> dict:
    """Arma la fila con la identidad y los metadatos ya conocidos (sin IA)."""
    fila = _sin_fechas(_fila_base_cruda(datos, causa))
    # Igual que el orquestador: sin esto guardar_causa escribía tipo_actor y
    # actor_normalizado vacíos y borraba los que dejó la descarga.
    if fila.get("Actor"):
        from resolucion.clasificar_actor import clasificar_tipo_actor, normalizar_nombre_actor
        tipo = clasificar_tipo_actor(fila["Actor"])
        fila["Tipo de Actor"] = tipo
        fila["Actor Normalizado"] = normalizar_nombre_actor(fila["Actor"], tipo)
    return fila


def _fila_base_cruda(datos: dict, causa: dict) -> dict:
    numero = datos["numero"]
    tribunal = (causa or {}).get("tribunal_codigo") or (numero.split("-")[0] if "-" in numero else "")
    return {
        "Número de juicio":      numero,
        "Tribunal":              (causa or {}).get("tribunal_nombre") or TRIBUNALES.get(tribunal, tribunal),
        "_tribunal_codigo":      tribunal,
        "Año":                   (causa or {}).get("año") or (numero.split("-")[1] if numero.count("-") >= 2 else ""),
        "Fecha de ingreso":      (causa or {}).get("fecha_ingreso") or "",
        "Actor":                 (causa or {}).get("actor") or "",
        "Demandado":             (causa or {}).get("demandado") or "",
        "Estado de Juicio":      datos.get("estado_juicio") or "",
        "Resultado tribunal":    datos.get("resultado") or "",
        "Resultado en Corte":    datos.get("resultado_cnj") or "",
        "Resultado en Apelación": datos.get("resultado_apel") or "",
        "Completitud":           datos.get("completitud") or "",
        "Completitud CNJ":       datos.get("completitud_cnj") or "",
        "Completitud Apelación": datos.get("completitud_apel") or "",
        "_fecha_inicio_caso":    (causa or {}).get("fecha_inicio_caso"),
        "_fecha_sentencia_caso": (causa or {}).get("fecha_sentencia_caso"),
        "_duracion_dias":        (causa or {}).get("duracion_dias"),
        "_fecha_inicio_cnj":     (causa or {}).get("fecha_inicio_cnj"),
        "_fecha_sentencia_cnj":  (causa or {}).get("fecha_sentencia_cnj"),
        "_duracion_dias_cnj":    (causa or {}).get("duracion_dias_cnj"),
        # Las URLs ya conocidas se conservan para que registrar_caso no las pise con NULL
        "_url_pdf_tribunal":     datos.get("url_pdf_tribunal") or "",
        "_url_pdf_cnj":          datos.get("url_pdf_cnj") or "",
        "_url_pdf_apel":         datos.get("url_pdf_apel") or "",
        "Observaciones":         "",
        "Categorización":        "",
    }


def reextraer_una(datos: dict, causa: dict, niveles: list, modelo: str,
                  flujo_proveedores: str, job_id: int, strapi: StrapiClient,
                  carpeta_pdfs: Path, carpeta_words: Path) -> list:
    """Procesa una causa. Devuelve la lista de acciones hechas (strings)."""
    numero = datos["numero"]
    fila = _fila_base(datos, causa)
    hecho = []
    causa_doc_id = None

    # ── Tribunal ───────────────────────────────────────────────────────────
    if "tribunal" in niveles:
        url = datos.get("url_pdf_tribunal")
        if not url:
            hecho.append("tribunal omitido (sin PDF guardado)")
        else:
            pdf = descargar_pdf_guardado(url, job_id=job_id, causa=numero)
            if not pdf:
                hecho.append("tribunal FALLÓ (no se pudo leer el PDF guardado)")
            else:
                fila["Resultado tribunal"] = fila["Resultado tribunal"] or "Con sentencia."
                campos = extraer_tribunal(
                    numero=numero, pdf_bytes=pdf, demandado=fila["Demandado"],
                    modelo=modelo, job_id=job_id, nombre_tribunal=fila["Tribunal"],
                    ano=fila["Año"], carpeta_pdfs=carpeta_pdfs, carpeta_docx=carpeta_words,
                    flujo_proveedores=flujo_proveedores,
                )
                for k in ("Cuantía", "Tipo de Acción", "Objeto Controversia", "Decisión Final",
                          "Resumen Ejecutivo", "Categorización", "Abogado del Actor",
                          "Abogado del Demandado", "_extraccion", "_jurisprudencia",
                          "_tipos_defecto", "_evidencia_ausente", "Origen Cuantía",
                          "Fecha Emisión", "PDF Truncado", "Modo Extracción", "Completitud",
                          "Observaciones", "_auditoria"):
                    if k in campos:
                        fila[k] = campos[k]
                # El PDF ya está en Strapi: no se vuelve a subir, solo el Word nuevo.
                if campos.get("_docx_bytes_tribunal"):
                    fila["_docx_bytes_tribunal"]  = campos["_docx_bytes_tribunal"]
                    fila["_docx_nombre_tribunal"] = campos.get("_docx_nombre_tribunal", f"{numero}_ficha_tecnica.docx")
                    fila["Análisis IA - Sentencias Tribunal"] = fila["_docx_nombre_tribunal"]
                if campos.get("error"):
                    hecho.append(f"tribunal ERROR IA ({campos['error'][:60]})")
                else:
                    _subir_archivos_strapi(fila, strapi, job_id)
                    _relink_pdf(fila, strapi, numero, "tribunal")
                    fila = _sin_fechas(fila)
                    causa_doc_id = _doc_id(cms_tribunal.guardar(fila, strapi, job_id))
                    if causa_doc_id:
                        _persistir_extras_tribunal(fila, causa_doc_id, job_id, modelo, flujo_proveedores)
                        hecho.append("tribunal ✓")
                    else:
                        hecho.append("tribunal FALLÓ (Strapi no devolvió documentId)")

    if ("corte" in niveles or "apelacion" in niveles) and not causa_doc_id:
        existente = strapi._buscar_causa(numero)
        causa_doc_id = existente.get("documentId") if existente else None
        if not causa_doc_id:
            hecho.append("corte/apelación omitidas (la causa no existe en Strapi)")
            return hecho

    # ── Apelación ─────────────────────────────────────────────────────────
    if "apelacion" in niveles:
        url = datos.get("url_pdf_apel")
        if not url:
            hecho.append("apelación omitida (sin PDF guardado)")
        else:
            pdf = descargar_pdf_guardado(url, job_id=job_id, causa=numero)
            if not pdf:
                hecho.append("apelación FALLÓ (no se pudo leer el PDF guardado)")
            else:
                campos = extraer_apelacion(pdf_bytes=pdf, numero=numero, modelo=modelo, job_id=job_id,
                                           carpeta_pdfs=carpeta_pdfs, carpeta_docx=carpeta_words,
                                           flujo_proveedores=flujo_proveedores)
                fila.update({k: v for k, v in campos.items() if k.startswith("_") or k.startswith("Completitud")})
                fila.pop("_pdf_bytes_apel", None)      # ya está en Strapi
                if campos.get("error"):
                    hecho.append(f"apelación ERROR IA ({campos['error'][:60]})")
                else:
                    _subir_archivos_strapi(fila, strapi, job_id)
                    _relink_pdf(fila, strapi, numero, "apel")
                    cms_apelacion.guardar(_sin_fechas(fila), causa_doc_id, strapi, job_id)
                    hecho.append("apelación ✓")

    # ── Corte (casación) ───────────────────────────────────────────────────
    if "corte" in niveles:
        url = datos.get("url_pdf_cnj")
        if not url:
            hecho.append("corte omitida (sin PDF guardado)")
        else:
            pdf = descargar_pdf_guardado(url, job_id=job_id, causa=numero)
            if not pdf:
                hecho.append("corte FALLÓ (no se pudo leer el PDF guardado)")
            else:
                campos = extraer_corte(pdf_bytes=pdf, numero=numero, modelo=modelo, job_id=job_id,
                                       carpeta_pdfs=carpeta_pdfs, carpeta_docx=carpeta_words,
                                       flujo_proveedores=flujo_proveedores)
                fila.update({k: v for k, v in campos.items() if k.startswith("_") or k.startswith("Completitud")})
                fila.pop("_pdf_bytes_cnj", None)
                if campos.get("error"):
                    hecho.append(f"corte ERROR IA ({campos['error'][:60]})")
                else:
                    _subir_archivos_strapi(fila, strapi, job_id)
                    _relink_pdf(fila, strapi, numero, "cnj")
                    cms_corte.guardar(_sin_fechas(fila), causa_doc_id, strapi, job_id)
                    hecho.append("corte ✓")

    try:
        jm.registrar_caso(job_id, fila)
    except Exception as e_reg:
        log.error(f"registrar_caso — {type(e_reg).__name__}: {e_reg}", job_id=job_id, causa=numero)
    return hecho


def _relink_pdf(fila: dict, strapi: StrapiClient, numero: str, nivel: str) -> None:
    """
    Conserva el enlace al PDF que ya está en Strapi: guardar_causa /
    guardar_casacion / guardar_apelacion solo escriben `file_pdf` si la fila
    trae `_file_id_pdf_<nivel>`; como aquí el PDF no se re-sube, se busca el
    id del archivo existente por su URL. Si no se encuentra, no se toca.
    """
    key_id, key_url = f"_file_id_pdf_{nivel}", f"_url_pdf_{nivel}"
    if fila.get(key_id) or not fila.get(key_url):
        return
    try:
        resp = httpx.get(f"{STRAPI_URL}/api/upload/files",
                         params={"filters[url][$eq]": fila[key_url], "fields[0]": "id"},
                         headers=strapi._headers, timeout=30)
        data = resp.json()
        if isinstance(data, list) and data:
            fila[key_id] = data[0].get("id")
    except Exception as e:
        log.warn(f"relink PDF {nivel}: {type(e).__name__}: {e}", causa=numero)


def ejecutar_job_reextraccion(job_id: int, pause_ev, kill_ev, modelo: str,
                              flujo_proveedores: str = "alibaba", marca: str = "reextraccion"):
    job = jm.obtener_job(job_id) or {}
    opts = parsear_marca(marca)
    niveles = opts["niveles"]

    # Selección: lista explícita del job, o filtro sobre el registro canónico
    items = jm.listar_items_lista(job_id, solo_pendientes=True)
    if items:
        numeros = [i["juicio"].strip() for i in items if i.get("juicio")]
        item_por_numero = {i["juicio"].strip(): i for i in items}
    else:
        numeros = jm.listar_numeros_reextraer(
            tribunal=job.get("tribunal") if job.get("tribunal") not in (None, "", "-") else None,
            anio=job.get("año") if job.get("año") not in (None, "", "-") else None,
            completitud=opts["completitud"], niveles=niveles, limite=opts["limite"],
        )
        item_por_numero = {}

    if not numeros:
        log.info("Re-extracción: ninguna causa cumple el filtro", job_id=job_id)
        jm._actualizar(job_id, estado="completado", completado_en=_now())
        return

    corrida_id = f"reextraccion-{job_id}"
    carpeta_pdfs  = PROJECT_ROOT / "resultados" / "pdfs"  / "resolucion" / corrida_id
    carpeta_words = PROJECT_ROOT / "resultados" / "words" / "resolucion" / corrida_id
    carpeta_pdfs.mkdir(parents=True, exist_ok=True)
    carpeta_words.mkdir(parents=True, exist_ok=True)

    strapi = StrapiClient()
    jm._actualizar(job_id, estado="corriendo", iniciado_en=_now(), hasta=len(numeros))
    log.info(f"Iniciando RE-EXTRACCIÓN — {len(numeros)} causas, niveles={','.join(niveles)}, modelo={modelo}",
             job_id=job_id)

    procesadas = exitosas = errores = 0
    for idx, numero in enumerate(numeros):
        if kill_ev.is_set():
            jm._actualizar(job_id, estado="cancelado", completado_en=_now())
            return
        if pause_ev.is_set():
            jm._actualizar(job_id, estado="pausado")
            return
        item = item_por_numero.get(numero)
        try:
            datos = jm.obtener_causa_datos(numero)
            causa = jm.obtener_causa(numero)
            if not datos:
                hecho = ["omitida (no está en el registro canónico: hay que descargarla primero)"]
            else:
                hecho = reextraer_una(datos, causa, niveles, modelo, flujo_proveedores,
                                      job_id, strapi, carpeta_pdfs, carpeta_words)
            detalle = " · ".join(hecho) or "sin acción"
            if any("FALLÓ" in h or "ERROR" in h for h in hecho):
                errores += 1
                if item: jm.actualizar_item_lista(item["id"], "error", detalle)
            elif any("✓" in h for h in hecho):
                exitosas += 1
                if item: jm.actualizar_item_lista(item["id"], "procesado", detalle)
            else:
                if item: jm.actualizar_item_lista(item["id"], "omitido", detalle)
            log.info(f"{numero} → {detalle}", job_id=job_id, causa=numero)
        except Exception as e:
            errores += 1
            if item: jm.actualizar_item_lista(item["id"], "error", f"{type(e).__name__}: {e}")
            log.error(f"{numero} → {type(e).__name__}: {e}\n{traceback.format_exc()}",
                      job_id=job_id, causa=numero)
        procesadas += 1
        jm._actualizar(job_id, checkpoint=idx + 1, total_procesadas=procesadas,
                       exitosas=exitosas, errores=errores)
        time.sleep(PAUSA_SEG)

    jm._actualizar(job_id, estado="completado", completado_en=_now())
    log.info(f"Re-extracción terminada: {exitosas} ok · {errores} con error · {procesadas} procesadas",
             job_id=job_id)
