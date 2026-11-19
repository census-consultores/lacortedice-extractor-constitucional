"""
Módulo de consulta a la API pública del E-SATJE 2020
Función Judicial del Ecuador
"""

from __future__ import annotations
import os
import requests
import re
import collections
import threading
import time
from datetime import datetime
from typing import Optional

BASE_CAUSAS = "https://api.funcionjudicial.gob.ec/EXPEL-CONSULTA-CAUSAS-SERVICE/api/consulta-causas"
BASE_CLEX   = "https://api.funcionjudicial.gob.ec/EXPEL-CONSULTA-CAUSAS-CLEX-SERVICE/api/consulta-causas-clex"
BASE_DOC    = "https://api.funcionjudicial.gob.ec/CJ-DOCUMENTO-SERVICE/api/document/query/hba"

_PROXIES         = None  # None = sin proxy; dict = {"http": url, "https": url}
_VERIFY          = True  # False cuando RESIDENTIAL_PROXY_INSECURE=1
_REINTENTOS      = 5     # intentos totales ante errores de red transitorios
_PAUSA_REINTENTO = 2     # segundos entre reintentos


def configurar_proxy(url: str, insecure: bool = False) -> None:
    """Activa el proxy BrightData para todas las llamadas a SATJE."""
    global _PROXIES, _VERIFY
    _PROXIES = {"http": url, "https": url}
    _VERIFY  = not insecure
    if insecure:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# ── Protecciones de la salida a SATJE ──────────────────────────────────────
# Toda consulta a SATJE pasa por _get/_post, así que aquí se controla el ritmo
# del worker entero, sin importar cuántos jobs o hilos haya. MAX_CONCURRENT_JOBS
# limita jobs, no consultas: el índice con 8 hilos y 0,3 s de pausa hacía ~14
# consultas/s y la IP de Render quedó bloqueada (16-22 oct).
#
#
# La pool de Bright Data (9 IP) es compartida por los cuatro workers. El
# bloqueo de oct fue por IP (~14/s desde una sola). Techo de civil: 3 por IP,
# 27/s. El ritmo real lo fijan los jobs a la vez (MAX_CONCURRENT_JOBS): con la
# latencia de ~2 s por consulta, cada job hace ~0,5/s.
# Se cambia aquí, en el código, no por variable de entorno.
_MAX_RPS      = 27.0   # consultas por segundo de este worker (3 por IP)
# Freno de emergencia: mira el ÚLTIMO MINUTO, no fallos seguidos. Con ~20
# consultas en paralelo, 5 fallos seguidos se juntan en un tropiezo de 2 s de
# SATJE: el 26-oct 04:28 frenó civil 17 min mientras tributaria, por las mismas
# IP, seguía respondiendo. Ahora frena solo si la mayoría falla.
_FRENO_FALLOS     = 20    # fallos (red, 403, 429, 5xx) en la ventana, como mínimo…
_FRENO_PROPORCION = 0.5   # …y más de esta fracción de las consultas de la ventana
_FRENO_VENTANA_S  = 60    # segundos que se miran hacia atrás
_FRENO_MIN        = 5     # minutos sin consultar SATJE tras activarse el freno

_ritmo_lock      = threading.Lock()
_proximo_turno   = 0.0
_ventana         = collections.deque()   # (instante, ok) del último minuto
_freno_hasta     = 0.0
_sin_proxy       = False   # True: el proxy es obligatorio y no hay


def _antes_de_consultar() -> None:
    """Freno de emergencia, proxy obligatorio y turno según SATJE_MAX_RPS."""
    global _proximo_turno
    if _sin_proxy:
        raise SatjeNoDisponible("sin proxy configurado: no se consulta SATJE desde la IP del servidor")
    with _ritmo_lock:
        ahora = time.monotonic()
        if _freno_hasta > ahora:
            raise SatjeNoDisponible(
                f"freno de emergencia activo {round((_freno_hasta - ahora) / 60)} min más")
        if _MAX_RPS > 0:
            turno = max(ahora, _proximo_turno)
            _proximo_turno = turno + 1.0 / _MAX_RPS
        else:
            turno = ahora
    espera = turno - time.monotonic()
    if espera > 0:
        time.sleep(espera)


def _registrar_respuesta(ok: bool) -> None:
    """Si en el último minuto fallaron al menos _FRENO_FALLOS consultas (red,
    403, 429, 5xx) y son la mayoría, se frenan todas las consultas del worker
    _FRENO_MIN minutos, para no seguir golpeando a SATJE cuando empieza a
    rechazar (así se agravó el bloqueo de laboral el 24 y 25 de octubre)."""
    global _freno_hasta
    with _ritmo_lock:
        ahora = time.monotonic()
        _ventana.append((ahora, ok))
        while _ventana and _ventana[0][0] < ahora - _FRENO_VENTANA_S:
            _ventana.popleft()
        if ok:
            return
        fallos = sum(1 for _, b in _ventana if not b)
        if fallos >= _FRENO_FALLOS and fallos > _FRENO_PROPORCION * len(_ventana):
            _freno_hasta = ahora + _FRENO_MIN * 60
            _ventana.clear()
            print(f"  [WARN SATJE] {fallos} fallos en {_FRENO_VENTANA_S} s: "
                  f"freno de emergencia {_FRENO_MIN:g} min", flush=True)


def _pedir(metodo, url: str, **kwargs) -> requests.Response:
    """Reintentos ante errores de red, cada intento con turno y control."""
    ultimo = None
    for i in range(_REINTENTOS):
        _antes_de_consultar()
        try:
            r = metodo(url, proxies=_PROXIES, verify=_VERIFY, **kwargs)
        except Exception as e:
            ultimo = e
            _registrar_respuesta(False)
        else:
            _registrar_respuesta(not (r.status_code in (403, 429) or r.status_code >= 500))
            return r
        if i < _REINTENTOS - 1:
            time.sleep(_PAUSA_REINTENTO)
    raise ultimo


def _get(url: str, **kwargs) -> requests.Response:
    """GET a SATJE con ritmo, freno y reintentos ante errores de red."""
    return _pedir(requests.get, url, **kwargs)


def _post(url: str, **kwargs) -> requests.Response:
    """POST a SATJE con ritmo, freno y reintentos ante errores de red."""
    return _pedir(requests.post, url, **kwargs)


def activar_proxy_desde_entorno() -> tuple[bool, str]:
    """Enciende el proxy de Bright Data para SATJE. Existía configurar_proxy()
    pero nadie la llamaba: civil, laboral y administrativo salían directo desde
    Render, que SATJE bloquea por IP.

    Sin proxy: en producción (RENDER, que Render define siempre) se BLOQUEAN
    las consultas, para que un error de configuración no vuelva a exponer la IP
    del servidor; en local se sigue directo."""
    global _sin_proxy
    from config.config import cargar_proxy_brightdata
    url, insecure = cargar_proxy_brightdata()
    if url:
        configurar_proxy(url, insecure)
        _sin_proxy = False
        return True, f"proxy SATJE activo vía {url.split('@')[-1]} · máx {_MAX_RPS:g} consultas/s"
    obligatorio = bool(os.environ.get("RENDER"))
    _sin_proxy = obligatorio
    if obligatorio:
        return False, "SIN PROXY: consultas a SATJE bloqueadas hasta configurar BDATA_DATACENTER_PROXY_URL"
    return False, "sin proxy (local) — SATJE directo"


def verificar_proxy(numero: str = "09332-2024-00010") -> tuple[bool, str]:
    """Una consulta real a SATJE para comprobar la salida al arrancar."""
    t0 = time.monotonic()
    try:
        buscar_causa(numero)
        return True, f"SATJE responde en {time.monotonic() - t0:.1f}s"
    except Exception as e:
        return False, f"SATJE no responde por la salida configurada: {type(e).__name__}: {e}"

HEADERS = {
    "Content-Type": "application/json",
    "Origin": "https://procesosjudiciales.funcionjudicial.gob.ec",
    "Referer": "https://procesosjudiciales.funcionjudicial.gob.ec/",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
}


class SatjeNoDisponible(Exception):
    """SATJE no respondió tras todos los reintentos. Se levanta en vez de
    devolver una lista vacía para que la causa quede como error (SATJE_ERROR)
    y se reprocese, y no como inexistente, "sin actuaciones" o "en trámite"."""


def normalizar_numero(numero: str) -> str:
    """Convierte '09501-2024-00021' → '09501202400021' (sin guiones)."""
    return numero.strip().replace("-", "")


def elegir_de_la_materia(resultados: list) -> Optional[dict]:
    """Un mismo número puede ser VARIOS juicios en SATJE (36% de una muestra
    del índice): en los juzgados multicompetentes, 12312-2019-00113 es una
    contravención de tránsito y también "12312201900113G", una inspección
    civil. SATJE los devuelve del más nuevo al más viejo, así que el primero
    no es necesariamente el de la materia. Se elige el de la materia; si no hay
    ninguno, el primero (el llamador decide que es de otra materia)."""
    if not resultados:
        return None
    from config import materia as _MAT
    if _MAT.IDS_MATERIA_SATJE:
        propios = [r for r in resultados if r.get("idMateria") in _MAT.IDS_MATERIA_SATJE]
        if propios:
            # Dos juicios de la materia con el mismo número (5,2% de una muestra
            # de 400): casi siempre uno es un DEPRECATORIO (diligencia encargada
            # por otro juzgado) y el otro el juicio real; 11333-2023-00062 es
            # "OTROS" (…G) y un cobro con apelación y casación (sin G). Se
            # prefiere el que no es deprecatorio y, a igualdad, el sin sufijo.
            reales = [r for r in propios
                      if "DEPRECATORIO" not in (r.get("nombreDelito") or "").upper()] or propios
            if len(reales) > 1:
                # Dos juicios reales (01613-2018-00605: inspección preparatoria
                # con apelación "…G" y un cobro de dinero sin ella). Hasta que la
                # plataforma guarde los dos, se toma el de más instancias.
                return max(reales, key=lambda r: (
                    len(obtener_incidentes((r.get("idJuicio") or "").strip()) or []),
                    not (r.get("idJuicio") or "").strip().upper().endswith("G"),
                ))
            return reales[0]
    return resultados[0]


def id_juicio_de(causa: Optional[dict], numero: str) -> str:
    """El idJuicio exacto del juicio elegido, con su sufijo ("…00113G").
    normalizar_numero(numero) lo pierde y apunta al OTRO juicio del número."""
    return ((causa or {}).get("idJuicio") or normalizar_numero(numero)).strip()


def buscar_causa(numero: str) -> Optional[dict]:
    """Busca una causa por número. Retorna el juicio de la materia (ver
    elegir_de_la_materia) o None. Usar id_juicio_de() para las consultas
    siguientes, nunca normalizar_numero().

    Reintenta cuando el SATJE responde 200 con lista vacía (ocurre con
    frecuencia en picos de carga aunque la causa sí existe).
    """
    id_juicio = normalizar_numero(numero)
    body = {
        "numeroCausa": id_juicio,
        "actor": {"cedulaActor": "", "nombreActor": ""},
        "demandado": {"cedulaDemandado": "", "nombreDemandado": ""},
        "provincia": "",
        "canton": None,
        "materia": None,
        "dependencia": None,
        "numeroFiscalia": "",
        "first": 1,
        "pageSize": 10,
        "recaptcha": "verdad",
    }
    respondio = False
    for intento in range(_REINTENTOS):
        try:
            r = _post(
                f"{BASE_CAUSAS}/informacion/buscarCausas?page=1&size=10",
                headers=HEADERS,
                json=body,
                timeout=(8, 20),
            )
            r.raise_for_status()
            data = r.json()
            respondio = True
            if data:
                return elegir_de_la_materia(data)
            # Respuesta vacía — puede ser SATJE inestable, reintentar
            if intento < _REINTENTOS - 1:
                time.sleep(_PAUSA_REINTENTO)
        except Exception as e:
            print(f"  [ERROR buscarCausa intento {intento+1}] {numero}: {e}")
            if intento < _REINTENTOS - 1:
                time.sleep(_PAUSA_REINTENTO)
    if not respondio:
        raise SatjeNoDisponible(f"SATJE sin respuesta en buscarCausas para {numero}")
    return None


def obtener_incidentes(id_juicio: str) -> list:
    """Obtiene la lista de judicaturas e incidentes de una causa.

    Verifica que cada incidente tenga lstIncidenteJudicatura poblado.
    Si alguno llega vacío, el payload llegó incompleto (la API no envía
    Content-Length, por lo que el truncamiento es silencioso). Esos
    incidentes se filtran para evitar consultas con idMovimiento=0.
    Reintenta si la lista llega completamente vacía (SATJE inestable).
    """
    respondio = False
    for intento in range(_REINTENTOS):
        try:
            r = _get(
                f"{BASE_CLEX}/informacion/getIncidenteJudicatura/{id_juicio}",
                headers=HEADERS,
                timeout=(8, 20),
            )
            r.raise_for_status()
            incidentes = r.json() or []
            respondio = True
        except Exception as e:
            print(f"  [ERROR getIncidenteJudicatura intento {intento+1}] {id_juicio}: {e}")
            if intento < _REINTENTOS - 1:
                time.sleep(_PAUSA_REINTENTO)
            continue

        completos = []
        for inc in incidentes:
            if inc.get("lstIncidenteJudicatura"):
                completos.append(inc)
            else:
                print(f"  [WARN] Incidente sin datos: {inc.get('nombreJudicatura', inc.get('idJudicatura', '?'))} — payload incompleto, se omite")

        if completos:
            return completos

        # Lista vacía o todos incompletos — reintentar si quedan intentos
        if intento < _REINTENTOS - 1:
            time.sleep(_PAUSA_REINTENTO)

    if not respondio:
        raise SatjeNoDisponible(f"SATJE sin respuesta en getIncidenteJudicatura para {id_juicio}")
    return []


def obtener_actuaciones(id_juicio: str, incidente_data: dict) -> list:
    """Obtiene las actuaciones de un incidente específico."""
    lst = incidente_data.get("lstIncidenteJudicatura", [])
    if not lst:
        print(f"  [WARN] obtener_actuaciones llamado con lstIncidenteJudicatura vacío — se omite")
        return []
    incidente = lst[0]
    body = {
        "idMovimientoJuicioIncidente": incidente.get("idMovimientoJuicioIncidente", 0),
        "idJuicio": id_juicio,
        "idJudicatura": incidente_data.get("idJudicatura", ""),
        "idIncidenteJudicatura": incidente.get("idIncidenteJudicatura", 0),
        "aplicativo": "",
        "nombreJudicatura": incidente_data.get("nombreJudicatura", ""),
        "incidente": incidente.get("incidente", 1),
    }
    ultimo = None
    for intento in (1, 2):
        try:
            r = _post(
                f"{BASE_CAUSAS}/informacion/actuacionesJudiciales",
                headers=HEADERS,
                json=body,
                timeout=(8, 30),
            )
            r.raise_for_status()
            return r.json() or []
        except Exception as e:
            ultimo = e
            print(f"  [ERROR actuacionesJudiciales intento {intento}]: {e}")
            if intento == 1:
                time.sleep(15)
    raise SatjeNoDisponible(
        f"SATJE sin respuesta en actuacionesJudiciales para {id_juicio}: {ultimo}")


def obtener_anexos(actuacion: dict) -> list:
    """
    Obtiene los documentos adjuntos (anexos) de una actuación tipo ESCRITO.
    Retorna lista de dicts con UUID, nombreArchivo, descripcion.
    """
    body = {
        "TablaReferencia": actuacion.get("ieTablaReferencia", ""),
        "IdTablaReferencia": actuacion.get("codigo", 0),
        "IdIndiceElectronico": 0,
        "idmovimientojuicioincidente": actuacion.get("idMovimientoJuicioIncidente", 0),
        "tipoActuacion": actuacion.get("tipo", "").strip(),
    }
    try:
        r = _post(
            f"{BASE_CLEX}/datos/anexos",
            headers=HEADERS,
            json=body,
            timeout=(8, 20),
        )
        r.raise_for_status()
        data = r.json()
        return [a for a in (data or []) if a is not None]
    except Exception as e:
        print(f"  [ERROR datos/anexos]: {e}")
        return []


def descargar_pdf(uuid: str) -> Optional[bytes]:
    """
    Descarga el PDF dado su UUID. Usa _get() que ya incluye reintentos
    silenciosos. No reintenta en 404 (documento no existe en SATJE).
    """
    if not uuid:
        return None
    headers_doc = {k: v for k, v in HEADERS.items() if k != "Content-Type"}
    try:
        r = _get(f"{BASE_DOC}?code={uuid}", headers=headers_doc, timeout=(8, 20))
        r.raise_for_status()
        content_type = r.headers.get("Content-Type", "")
        if (content_type.startswith("application/pdf") or
                content_type.startswith("application/octet-stream") or
                r.content[:4] == b'%PDF'):
            return r.content
        return None
    except requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            return None
        print(f"  (Error de descarga PDF)")
        return None
    except Exception:
        print(f"  (Error de descarga PDF)")
        return None


def _es_razon_notificacion(tipo: str) -> bool:
    """Detecta si una actuación es solo una constancia de notificación (RAZON),
    no el documento real (AUTO, SENTENCIA, RESOLUCION, etc.)."""
    t = tipo.strip().upper()
    return (
        t == "RAZON" or
        t.endswith("(RAZON)") or
        t.endswith("(RAZON DE NOTIFICACION)")
    )


def _fecha_iso(fecha_str: str) -> Optional[datetime]:
    """Convierte string ISO a datetime, o None si falla."""
    if not fecha_str:
        return None
    try:
        return datetime.fromisoformat(fecha_str.replace("+00:00", ""))
    except Exception:
        return None


def limpiar_html(html: str) -> str:
    """Elimina tags HTML y decodifica entidades."""
    if not html:
        return ""
    texto = re.sub(r"<[^>]+>", " ", html)
    reemplazos = {
        "&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"',
        "&#39;": "'", "&nbsp;": " ", "&aacute;": "á", "&eacute;": "é",
        "&iacute;": "í", "&oacute;": "ó", "&uacute;": "ú", "&ntilde;": "ñ",
        "&Aacute;": "Á", "&Eacute;": "É", "&Iacute;": "Í", "&Oacute;": "Ó",
        "&Uacute;": "Ú", "&Ntilde;": "Ñ", "&ordm;": "°",
    }
    for ent, char in reemplazos.items():
        texto = texto.replace(ent, char)
    texto = re.sub(r"\s+", " ", texto).strip()
    return texto[:2000] + "..." if len(texto) > 2000 else texto


def _seleccionar_mejor_incidente(id_juicio: str, incidentes: list) -> tuple:
    """
    Entre todos los incidentes (subcausas), devuelve el que tiene
    la actuación más reciente, junto con sus actuaciones.
    Retorna (incidente_data, actuaciones).
    """
    mejor_incidente = None
    mejor_actuaciones = []
    mejor_fecha = None

    for inc in incidentes:
        acts = obtener_actuaciones(id_juicio, inc)
        if not acts:
            continue
        # Usar la fecha de la primera actuación real (ignorando RAZONES)
        ultima_real = next(
            (a for a in acts if not _es_razon_notificacion(a.get("tipo", ""))),
            acts[0]
        )
        fecha = _fecha_iso(ultima_real.get("fecha", ""))
        if fecha is None:
            continue
        if mejor_fecha is None or fecha > mejor_fecha:
            mejor_fecha = fecha
            mejor_incidente = inc
            mejor_actuaciones = acts

    return mejor_incidente, mejor_actuaciones


def consultar_causa(numero: str, descripcion: str = "", api_key: str = None) -> dict:
    """
    Consulta completa de una causa.
    - Si hay varias subcausas, elige la que tiene la actuación más reciente.
    - Para actuaciones tipo ESCRITO, descarga el Anexo (documento real)
      en lugar de la FE DE PRESENTACION.
    """
    resultado = {
        "numero": numero,
        "descripcion": descripcion,
        "estado": "ERROR",
        "actor": "",
        "demandado": "",
        "delito": "",
        "judicatura": "",
        "ultima_fecha": "",
        "ultimo_tipo": "",
        "ultima_actividad": "",
        "pdf_bytes": None,
        "pdf_nombre": "",
        "pdf_resumen": "",
        "drive_url": "",
        "error": "",
    }

    causa = buscar_causa(numero)
    if not causa:
        resultado["error"] = "Causa no encontrada"
        return resultado
    id_juicio = id_juicio_de(causa, numero)
    print(f"  Consultando: {numero} ({id_juicio})")

    resultado["delito"] = causa.get("nombreDelito", "")

    incidentes = obtener_incidentes(id_juicio)
    if not incidentes:
        resultado["error"] = "Sin incidentes disponibles"
        return resultado

    if len(incidentes) == 1:
        incidente_elegido = incidentes[0]
        actuaciones = obtener_actuaciones(id_juicio, incidente_elegido)
    else:
        print(f"  {len(incidentes)} subcausas — comparando fechas...")
        incidente_elegido, actuaciones = _seleccionar_mejor_incidente(id_juicio, incidentes)
        if incidente_elegido is None:
            resultado["error"] = "Sin actuaciones disponibles en ninguna subcausa"
            return resultado

    resultado["judicatura"] = (
        incidente_elegido.get("nombreJudicatura", "") +
        f" ({incidente_elegido.get('ciudad', '')})"
    )

    # Extraer actor y demandado
    lst_inc = incidente_elegido.get("lstIncidenteJudicatura", [])
    if lst_inc:
        actores = lst_inc[0].get("lstLitiganteActor", [])
        demandados = lst_inc[0].get("lstLitiganteDemandado", [])
        if actores:
            resultado["actor"] = actores[0].get("nombresLitigante", "")
        if demandados:
            resultado["demandado"] = demandados[0].get("nombresLitigante", "")

    if not actuaciones:
        resultado["error"] = "Sin actuaciones disponibles"
        return resultado

    # La última actuación real es la primera que NO es RAZON de notificación
    ultima = next(
        (a for a in actuaciones if not _es_razon_notificacion(a.get("tipo", ""))),
        actuaciones[0]  # fallback si todas fueran RAZON (improbable)
    )
    fecha_raw = ultima.get("fecha", "")
    fecha_dt = _fecha_iso(fecha_raw)
    resultado["ultima_fecha"] = fecha_dt.strftime("%d/%m/%Y %H:%M") if fecha_dt else fecha_raw[:10]
    resultado["ultimo_tipo"] = ultima.get("tipo", "").strip()
    resultado["ultima_actividad"] = limpiar_html(ultima.get("actividad", ""))
    resultado["estado"] = "OK"

    # Buscar PDF SOLO de la última actuación reportada (no de actuaciones anteriores)
    # Esto evita adjuntar documentos de semanas/meses atrás
    act = ultima
    if act.get("ieDocumentoAdjunto") == "S":
        en_escritos_primera = act.get("ieTablaReferencia", "") == "EscritosPrimera"

        if en_escritos_primera:
            # EscritosPrimera: el uuid propio es solo la FE DE PRESENTACION (recibo).
            # El documento real está en datos/anexos.
            print(f"  Buscando documento en datos/anexos (codigo={act.get('codigo')})...")
            anexos = obtener_anexos(act)
            for anexo in anexos:
                uuid = anexo.get("UUID") or anexo.get("uuid", "")
                if not uuid:
                    continue
                nombre = anexo.get("nombreArchivo") or anexo.get("descripcion") or "documento"
                print(f"  Descargando anexo: {nombre}")
                pdf = descargar_pdf(uuid)
                if pdf:
                    resultado["pdf_bytes"] = pdf
                    nombre_base = nombre if nombre.lower().endswith(".pdf") else f"{nombre}.pdf"
                    resultado["pdf_nombre"] = f"{id_juicio}_{nombre_base}"
                break
        else:
            # ProvPrimera u otra tabla: descargar directamente con el UUID
            uuid = act.get("uuid", "")
            if uuid:
                nombre = act.get("nombreArchivo", "documento") or "documento"
                print(f"  Descargando PDF: {nombre}")
                pdf = descargar_pdf(uuid)
                if pdf:
                    resultado["pdf_bytes"] = pdf
                    nombre_base = nombre if nombre.lower().endswith(".pdf") else f"{nombre}.pdf"
                    resultado["pdf_nombre"] = f"{id_juicio}_{nombre_base}"
    else:
        print(f"  Última actuación sin documento adjunto")

    # Resumen ejecutivo desactivado (activar poniendo api_key en config.json)
    # if resultado["pdf_bytes"]:
    #     from pdf_resumen import extraer_resumen
    #     resultado["pdf_resumen"] = extraer_resumen(resultado["pdf_bytes"], api_key=api_key)

    return resultado


# ── Consulta de la Corte Constitucional del Ecuador ────────────────────
# Para casos con tipo_caso = "Corte Constitucional". Devuelve exactamente
# el mismo formato que consultar_causa() arriba para que monitor.py y
# email_sender.py funcionen sin cambios.

def consultar_causa_cc(numero: str, descripcion: str = "", api_key: str = None) -> dict:
    """Consulta una causa en la Corte Constitucional del Ecuador.
    Mismo formato de retorno que consultar_causa() (SATJE).
    """
    resultado = {
        "numero": numero,
        "descripcion": descripcion,
        "estado": "ERROR",
        "actor": "",
        "demandado": "",
        "delito": "",
        "judicatura": "",
        "ultima_fecha": "",
        "ultimo_tipo": "",
        "ultima_actividad": "",
        "pdf_bytes": None,
        "pdf_nombre": "",
        "pdf_resumen": "",
        "drive_url": "",
        "error": "",
    }

    print(f"  Consultando CC: {numero}")

    try:
        from corte_constitucional import obtener_expediente_por_numero
        data = obtener_expediente_por_numero(numero)
    except Exception as e:
        resultado["error"] = f"Error consultando CC: {e}"
        return resultado

    instancias = data.get("instancias") or []
    if not instancias:
        resultado["error"] = data.get("mensaje") or "Causa sin movimientos en CC"
        return resultado

    inst = instancias[0]
    actuaciones = inst.get("actuaciones") or []
    if not actuaciones:
        resultado["error"] = "Causa sin actuaciones en CC"
        return resultado

    resultado["judicatura"] = inst.get("judicatura", "Corte Constitucional del Ecuador")

    ficha = data.get("ficha") or {}
    motivo = (ficha.get("motivo") or "").strip()
    # Extraer actor del motivo si es posible (la API no expone actor explicitamente)
    resultado["actor"] = motivo[:100] if motivo else ""
    resultado["demandado"] = "Corte Constitucional"

    ultima = actuaciones[0]  # ya vienen ordenadas desc por fecha
    resultado["ultima_fecha"] = ultima.get("fecha", "")
    resultado["ultimo_tipo"] = ultima.get("tipo", "")
    resultado["ultima_actividad"] = ultima.get("actividad", "") or ultima.get("tipo", "")
    resultado["estado"] = "OK"

    # Descargar PDF del último movimiento si lo tiene
    pdf_url = ""
    documentos = ultima.get("documentos") or []
    for doc in documentos:
        if doc.get("es_principal") and doc.get("url"):
            pdf_url = doc["url"]
            break
    if not pdf_url and ultima.get("pdf_url"):
        pdf_url = ultima["pdf_url"]

    if pdf_url:
        try:
            print(f"  Descargando PDF CC: {pdf_url[:80]}...")
            r = _get(pdf_url, timeout=(8, 60), headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            })
            r.raise_for_status()
            if r.content:
                resultado["pdf_bytes"] = r.content
                # Nombre: numero_TIPO.pdf
                tipo_safe = re.sub(r"[^a-zA-Z0-9_-]+", "_", ultima.get("tipo", "movimiento"))[:50]
                num_safe = numero.replace("/", "_").replace(" ", "")
                resultado["pdf_nombre"] = f"{num_safe}_{tipo_safe}.pdf"
                # La URL publica de CC sirve directamente como link permanente
                resultado["cc_url"] = pdf_url
        except Exception as e:
            print(f"  ⚠ Error descargando PDF CC: {e}")

    # Marca de fuente para que el cache distinga CC de SATJE
    resultado["fuente"] = "cc"
    return resultado
