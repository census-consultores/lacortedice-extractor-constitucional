"""
Análisis de sentencias de CASACIÓN (Corte Nacional de Justicia) con IA.

analizar(pdf_bytes, numero, modelo, job_id) → dict

Ejecuta los prompts 1, 5 y 6, y después la clasificación (prompt 4) y los
tipos de defecto (prompt 7) acotados al objeto del recurso — las mismas
funciones que el tribunal (tribunal.clasificar_y_tipificar). Soporta OpenAI y Qwen.

El mismo módulo sirve para las sentencias de APELACIÓN (Corte Provincial)
en materias de tres niveles: analizar(..., nivel="apelacion") usa los
prompts 11 y 12 en vez de 5 y 6 (ver NIVELES).

Retorna:
  resumen, extraccion, jurisprudencia, categorias, tipos_defecto,
  docx_bytes, pdf_truncado,
  validacion_ok, validacion_problemas, error
"""

import re
import time

from resolucion.ai._cliente import (
    cargar_api_key_openai, cargar_api_key_openrouter, es_modelo_qwen,
    cargar_prompt, extraer_texto_pdf,
    llamar_openai, llamar_qwen, crear_docx_corte,
    GPT_MAX_TOKENS, GPT_MAX_CHARS_INPUT, QWEN_MAX_CHARS_INPUT,
    GPT_PAUSA_SEG, EJEMPLOS_CORTE_DIR, MIN_CHARS_RESUMEN,
    elegir_salida_reintento, sin_linea_cortada,
)
from config.pdf_utils import cargar_ejemplos_few_shot
from resolucion.ai.tribunal import (clasificar_y_tipificar, _codigos_controversia, secciones_faltantes,
                                    recortar_seccion_incompleta, instruccion_continuacion)
from resolucion.ai import controles, regla_cero, verificador_argumentos
from worker import logger as log

# ── Prompts ───────────────────────────────────────────────────────────────────
# Ejemplos few-shot: se leen una sola vez al importar (no son prompts
# editables, son archivos de referencia aparte).
_EJEMPLOS = {
    "casacion":  cargar_ejemplos_few_shot(EJEMPLOS_CORTE_DIR),
    "apelacion": cargar_ejemplos_few_shot(EJEMPLOS_CORTE_DIR.parent / "apelacion"),
}


# Niveles que comparten este módulo. "casacion" = Corte Nacional (prompts 5 y 6);
# "apelacion" = Corte Provincial (prompts 11 y 12), solo en materias de tres niveles.
NIVELES = {
    "casacion": {
        "extraccion":     "prompt_5_cnj_extraccion.txt",
        "jurisprudencia": "prompt_6_cnj_jurisprudencia.txt",
        "etiqueta":       "CNJ",
        "titulo_doc":     "casación",
        "recurso":        "CASACIÓN",
        "ejemplos":       True,
    },
    "apelacion": {
        "extraccion":     "prompt_11_apelacion_extraccion.txt",
        "jurisprudencia": "prompt_12_apelacion_jurisprudencia.txt",
        "etiqueta":       "APELACION",
        "titulo_doc":     "apelación",
        "recurso":        "APELACIÓN",
        "ejemplos":       True,
    },
}


def _cargar_prompts_editables(nivel: str = "casacion"):
    """
    Recarga los 5 prompts editables en cada extracción (en vez de una sola
    vez al importar el módulo) — así una edición hecha desde la webapp
    aplica en la próxima causa procesada, sin reiniciar el worker.
    """
    cfg = NIVELES[nivel]
    sistema        = cargar_prompt("sistema.txt")
    sistema_neutro = cargar_prompt("sistema_neutro.txt")
    p1_resumen     = cargar_prompt("prompt_1_resumen.txt")
    p5_extrac      = cargar_prompt(cfg["extraccion"])
    p6_jurisp      = cargar_prompt(cfg["jurisprudencia"])

    if cfg["ejemplos"] and _EJEMPLOS[nivel]:
        doc = cfg["titulo_doc"]
        p5_extrac = p5_extrac.rstrip() + (
            "\n\nEJEMPLOS DE REFERENCIA (sigue este mismo formato y nivel de detalle "
            f"para la sentencia de {doc} que viene al final; sus datos son de otra causa "
            "y nunca se copian):\n\n"
            + _EJEMPLOS[nivel]
            + f"\nSENTENCIA DE {doc.upper()} A ANALIZAR:\n"
        )
    return sistema, sistema_neutro, p1_resumen, p5_extrac, p6_jurisp


# ── Validación post-GPT para casaciones ───────────────────────────────────────

def _norm(s: str) -> str:
    return (s or "").upper().replace("Á","A").replace("É","E").replace("Í","I")\
        .replace("Ó","O").replace("Ú","U").replace("Ñ","N")


# Encabezado de la sección 11 ("11. Decisión Final", "**11. DECISIÓN FINAL**",
# o sin número cuando el modelo lo omite), sobre el texto ya pasado por _norm.
_RE_DECISION_FINAL = re.compile(r"^[\s#*_>\-]*(?:11\s*[.)\-:]?\s*)?DECISION\s+FINAL", re.M)


def _validar(resultado: dict) -> tuple:
    """Valida la extracción de una casación. Retorna (ok, [problemas])."""
    problemas = []

    resumen = (resultado.get("resumen") or "").strip()
    if not resumen:
        problemas.append("Resumen vacío — GPT no generó texto")
    elif len(resumen) < MIN_CHARS_RESUMEN:
        problemas.append(f"Resumen muy corto ({len(resumen)} chars)")
    elif resumen.lower().startswith("[error"):
        problemas.append(f"Resumen contiene error GPT: {resumen[:80]}")

    extraccion = (resultado.get("extraccion") or "").strip()
    if not extraccion:
        problemas.append("Extracción vacía — el prompt 5 no generó texto")
    elif len(extraccion) < 500:
        problemas.append(f"Extracción muy corta ({len(extraccion)} chars)")
    else:
        e_norm = _norm(extraccion)
        etiquetas = ["CAUSAL", "RECURRENTE"]
        faltantes = [t for t in etiquetas if t not in e_norm]
        # La sección 11 tiene que existir como encabezado: "Decisión de
        # primera instancia" (sección 6) no cuenta como decisión final.
        if not _RE_DECISION_FINAL.search(e_norm):
            faltantes.append("DECISION FINAL")
        if faltantes:
            problemas.append(f"Extracción sin etiquetas clave: {', '.join(faltantes)}")

    jurisprudencia = (resultado.get("jurisprudencia") or "").strip()
    if not jurisprudencia:
        problemas.append("Jurisprudencia vacía")
    elif len(jurisprudencia) < 100:
        problemas.append(f"Jurisprudencia muy corta ({len(jurisprudencia)} chars)")
    elif jurisprudencia.lower().startswith("[error"):
        problemas.append(f"Jurisprudencia contiene error GPT: {jurisprudencia[:80]}")

    return (len(problemas) == 0, problemas)


# ── Función principal ─────────────────────────────────────────────────────────

def analizar(
    pdf_bytes: bytes,
    numero: str,
    modelo: str = "gpt-4o",
    job_id: int = None,
    flujo_proveedores: str = "alibaba",
    nivel: str = "casacion",
) -> dict:
    """
    Analiza una sentencia de casación de la Corte Nacional con los prompts 1, 5 y 6.

    Parámetros:
      pdf_bytes — contenido del PDF como bytes
      numero    — número de juicio
      modelo    — nombre del modelo; "qwen" en el nombre → usa DeepInfra
      job_id    — id del job para logs (None en scripts locales)

    Retorna dict con resumen, extraccion, jurisprudencia, docx_bytes, error.
    """
    es_qwen   = es_modelo_qwen(modelo)
    max_chars = QWEN_MAX_CHARS_INPUT if es_qwen else GPT_MAX_CHARS_INPUT
    _sistema, _sistema_neutro, _p1_resumen, _p5_extrac, _p6_jurisp = _cargar_prompts_editables(nivel)
    ETQ = NIVELES[nivel]["etiqueta"]
    TIT = NIVELES[nivel]["titulo_doc"]

    resultado = {
        "resumen": "", "extraccion": "", "jurisprudencia": "",
        "categorias": "", "tipos_defecto": "",
        "docx_bytes": None, "pdf_truncado": False,
        "validacion_ok": False, "validacion_problemas": [], "error": "",
    }

    # ── API key ───────────────────────────────────────────────────────────────
    if es_qwen:
        api_key = cargar_api_key_openrouter()
        if not api_key:
            resultado["error"] = "OPENROUTER_API_KEY no configurada en .env"
            log.error(resultado["error"], job_id=job_id, causa=numero)
            return resultado
    else:
        api_key = cargar_api_key_openai()
        if not api_key:
            resultado["error"] = "OPENAI_API_KEY no configurada en .env"
            log.error(resultado["error"], job_id=job_id, causa=numero)
            return resultado

    # ── Texto del PDF ─────────────────────────────────────────────────────────
    texto = extraer_texto_pdf(pdf_bytes, job_id=job_id, causa=numero)
    if not texto:
        resultado["error"] = f"No se pudo extraer texto del PDF de {TIT} (PDF vacío, protegido o corrompido)"
        log.error(resultado["error"], job_id=job_id, causa=numero)
        return resultado

    resultado["pdf_truncado"] = len(texto) >= max_chars
    # Los prompts reciben solo el voto de mayoría; el voto salvado llena las
    # secciones 13-14 en controles.aplicar().
    texto_rec, voto = controles.separar_voto(texto[:max_chars])
    if voto:
        log.info(f"IA-{ETQ}: voto separado de la mayoría ({len(voto['texto'])} chars)", job_id=job_id, causa=numero)
    log.info(
        f"IA-{ETQ} [{modelo.split('/')[-1]}]: {len(texto)} chars del PDF de {TIT}"
        + (" (truncado)" if resultado["pdf_truncado"] else ""),
        job_id=job_id, causa=numero,
    )

    truncados = []

    def _llamar(prompt_base: str, max_tokens: int, temperatura: float = None) -> tuple:
        if es_qwen:
            from resolucion.ai._cliente import resolver_orden_proveedores
            return llamar_qwen(prompt_base + texto_rec, _sistema_neutro, max_tokens,
                               api_key, modelo, job_id=job_id, causa=numero,
                               orden_proveedores=resolver_orden_proveedores(flujo_proveedores),
                               temperatura=temperatura)
        return llamar_openai(prompt_base + texto_rec, _sistema, max_tokens,
                             api_key, modelo, job_id=job_id, causa=numero)

    # ── Prompt 1: Resumen ─────────────────────────────────────────────────────
    log.info(f"IA-{ETQ} [1/3] Resumen ejecutivo", job_id=job_id, causa=numero)
    resumen, fr1 = _llamar(_p1_resumen, GPT_MAX_TOKENS["resumen"])
    if fr1 == "length": truncados.append("Resumen")
    resultado["resumen"] = resumen
    time.sleep(GPT_PAUSA_SEG)

    def _reintentar_si_trunca(prompt_base: str, max_tokens: int, salida: str,
                              fin: str, paso: str) -> tuple:
        # Igual que en tribunal: truncar casi siempre es un bucle de repetición
        # (piloto laboral 09359-2022-00070: la apelación perdió de la 10 a la 14
        # y la jurisprudencia de casación se cortó a media cita). Se reintenta
        # completo con algo de temperatura, hasta 2 veces; si todos truncan se
        # queda el intento con menos repetición, no el más largo.
        _reint = 0
        while fin == "length" and _reint < 2:
            _reint += 1
            log.warn(f"IA-{ETQ} {paso} truncada (probable bucle de repetición) "
                     f"— reintentando completo (intento {_reint})", job_id=job_id, causa=numero)
            nueva, fin_n = _llamar(prompt_base, max_tokens, temperatura=0.35)
            if not nueva or nueva.startswith("[Error") or "Lo siento" in nueva[:120]:
                break
            if elegir_salida_reintento(salida or "", nueva, fin_n):
                salida = nueva
            fin = fin_n
        return salida, fin

    # ── Prompt 5: Extracción 14 secciones ────────────────────────────────────
    log.info(f"IA-{ETQ} [2/3] Extracción {TIT} (14 secciones)", job_id=job_id, causa=numero)
    extraccion, fr2 = _llamar(_p5_extrac, GPT_MAX_TOKENS["extraccion"])
    extraccion, fr2 = _reintentar_si_trunca(_p5_extrac, GPT_MAX_TOKENS["extraccion"],
                                            extraccion, fr2, "[2/3] Extracción")
    # Igual que en tribunal: si aun así faltan secciones finales, se pide la
    # continuación una vez (puerta civil v7, 18334-2023-01146: perdió de la 12 a la 14).
    if fr2 == "length" and extraccion and not extraccion.startswith("[Error"):
        faltan = secciones_faltantes(extraccion, 14)
        if faltan:
            log.warn(f"IA-{ETQ} Extracción cortada: faltan secciones {faltan[0]}–14 — pidiendo continuación",
                     job_id=job_id, causa=numero)
            cont, fr_c = _llamar(_p5_extrac + instruccion_continuacion(faltan[0], 14), GPT_MAX_TOKENS["extraccion"])
            if cont and not cont.startswith("[Error"):
                extraccion = recortar_seccion_incompleta(extraccion, faltan[0]) + "\n" + cont.strip()
                fr2 = fr_c
    if fr2 == "length":
        truncados.append(f"Extracción {ETQ}")
        extraccion = sin_linea_cortada(extraccion)
    if extraccion.startswith("[Error"):
        log.warn(
            f"Prompt 5 falló tras agotar reintentos: {extraccion[:80]} — "
            "sin extracción para esta causa",
            job_id=job_id, causa=numero,
        )
        extraccion = ""
    elif extraccion and "Lo siento" in extraccion[:120]:
        log.warn(
            f"Prompt 5 rechazado por filtro de contenido: {extraccion[:80]}",
            job_id=job_id, causa=numero,
        )
        extraccion = ""
    resultado["extraccion"] = extraccion
    time.sleep(GPT_PAUSA_SEG)

    # ── Verificación de los argumentos de las partes (puerta civil v3) ────────
    vin = verificador_argumentos.vinetas(extraccion) if extraccion else {}
    if vin:
        salida, _ = _llamar(verificador_argumentos.prompt(vin), 6000, temperatura=0)
        veredictos = verificador_argumentos.leer_veredictos(salida)
        if not veredictos:
            log.warn(f"IA-{ETQ} verificación de argumentos: respuesta ilegible, no se toca la ficha",
                     job_id=job_id, causa=numero)
        resultado["extraccion"], quitadas = verificador_argumentos.aplicar(extraccion, vin, veredictos, texto_rec)
        for n, v, l in quitadas:
            log.info(f"IA-{ETQ} verificación: se quitó de la sección {n} ({v}): {l[:120]}",
                     job_id=job_id, causa=numero)
        time.sleep(GPT_PAUSA_SEG)

    # ── Prompt 6: Jurisprudencia ──────────────────────────────────────────────
    log.info(f"IA-{ETQ} [3/3] Jurisprudencia y criterios de {TIT}", job_id=job_id, causa=numero)
    jurisprudencia, fr3 = _llamar(_p6_jurisp, GPT_MAX_TOKENS["jurisprudencia"])
    jurisprudencia, fr3 = _reintentar_si_trunca(_p6_jurisp, GPT_MAX_TOKENS["jurisprudencia"],
                                                jurisprudencia, fr3, "[3/3] Jurisprudencia")
    if fr3 == "length":
        truncados.append(f"Jurisprudencia {ETQ}")
        jurisprudencia = sin_linea_cortada(jurisprudencia)
    resultado["jurisprudencia"] = jurisprudencia
    time.sleep(GPT_PAUSA_SEG)

    # ── Prompts 4 y 7: categorías y puntos controvertidos del RECURSO ────────
    # Mismas funciones que el tribunal; el prefijo acota la clasificación a lo
    # que la Sala resolvió al decidir el recurso.
    REC = NIVELES[nivel]["recurso"]
    prefijo = (
        f"ESTA ES UNA SENTENCIA DE {REC}. Clasifica el objeto del RECURSO de {TIT}: "
        "solo los temas que la Sala efectivamente resolvió al decidirlo (no toda la "
        "controversia de primera instancia), con las mismas reglas que siguen.\n\n"
    )
    clas = clasificar_y_tipificar(_llamar, cargar_prompt("prompt_4_clasificacion.txt"),
                                  cargar_prompt("prompt_7_tipos_defecto.txt"), texto_rec,
                                  prefijo=prefijo, etiqueta=f"IA-{ETQ}",
                                  job_id=job_id, numero=numero)
    resultado["categorias"]    = clas["categorias"]
    resultado["tipos_defecto"] = clas["tipos_defecto"]
    truncados += [f"{t} {ETQ}" for t in clas["truncados"]]

    # ── Controles deterministas (antes del Word y de lo que se guarda) ────────
    controles_info = controles.aplicar(resultado, texto_rec, voto, nivel=nivel,
                                      prompts=[_p1_resumen, _p5_extrac, _p6_jurisp])
    for ajuste in controles_info["ajustes"]:
        log.info(f"IA-{ETQ} control: {ajuste}", job_id=job_id, causa=numero)

    # ── Generar Word ──────────────────────────────────────────────────────────
    regla_cero.exigir_sin_error_api(resultado)
    log.info(f"IA-{ETQ}: Generando Ficha Técnica de {TIT} Word", job_id=job_id, causa=numero)
    docx = crear_docx_corte(numero, resultado["resumen"], resultado["extraccion"], resultado["jurisprudencia"],
                             job_id=job_id, causa=numero, nivel=nivel,
                             clasificacion=resultado["categorias"],
                             perfil={"controversias": _codigos_controversia(resultado["tipos_defecto"])})
    if docx:
        resultado["docx_bytes"] = docx
    else:
        log.warn(f"No se pudo generar el Word de {TIT}", job_id=job_id, causa=numero)

    # ── Validación ────────────────────────────────────────────────────────────
    ok, problemas = _validar(resultado)
    for t in truncados:
        problemas.append(f"Respuesta truncada (max_tokens): {t}")
    problemas += controles_info["senales"]
    if truncados or controles_info["senales"]:
        ok = False
    resultado["validacion_ok"]        = ok
    resultado["validacion_problemas"] = problemas
    if not ok:
        log.warn(
            f"Validación {ETQ}: {len(problemas)} problema(s): {'; '.join(problemas)}",
            job_id=job_id, causa=numero,
        )

    return resultado


def analizar_solo_jurisprudencia_cnj(pdf_bytes: bytes, numero: str, modelo: str = "gpt-4o",
                                      job_id: int = None, flujo_proveedores: str = "alibaba") -> dict:
    """
    Corre ÚNICAMENTE el prompt 6 (jurisprudencia y criterios de casación)
    sobre una sentencia de casación ya conocida — equivalente a
    tribunal.analizar_solo_jurisprudencia, para el backfill de casaciones
    donde ese campo quedó en "No consta".

    Retorna {"jurisprudencia": str, "error": str}.
    """
    resultado = {"jurisprudencia": "", "error": ""}
    es_qwen   = es_modelo_qwen(modelo)
    max_chars = QWEN_MAX_CHARS_INPUT if es_qwen else GPT_MAX_CHARS_INPUT

    if es_qwen:
        api_key = cargar_api_key_openrouter()
        if not api_key:
            resultado["error"] = "OPENROUTER_API_KEY no configurada en .env"
            log.error(resultado["error"], job_id=job_id, causa=numero)
            return resultado
    else:
        api_key = cargar_api_key_openai()
        if not api_key:
            resultado["error"] = "OPENAI_API_KEY no configurada en .env"
            log.error(resultado["error"], job_id=job_id, causa=numero)
            return resultado

    texto = extraer_texto_pdf(pdf_bytes, job_id=job_id, causa=numero)
    if not texto:
        resultado["error"] = "No se pudo extraer texto del PDF de casación (PDF vacío, protegido o corrompido)"
        log.error(resultado["error"], job_id=job_id, causa=numero)
        return resultado
    texto_rec, _ = controles.separar_voto(texto[:max_chars])

    sistema        = cargar_prompt("sistema.txt")
    sistema_neutro = cargar_prompt("sistema_neutro.txt")
    p6_jurisp      = cargar_prompt("prompt_6_cnj_jurisprudencia.txt")

    log.info(f"IA-CNJ [solo jurisprudencia] {modelo.split('/')[-1]}", job_id=job_id, causa=numero)
    if es_qwen:
        from resolucion.ai._cliente import resolver_orden_proveedores
        texto_ia, finish = llamar_qwen(
            p6_jurisp + texto_rec, sistema_neutro, GPT_MAX_TOKENS["jurisprudencia"],
            api_key, modelo, job_id=job_id, causa=numero,
            orden_proveedores=resolver_orden_proveedores(flujo_proveedores),
        )
    else:
        texto_ia, finish = llamar_openai(
            p6_jurisp + texto_rec, sistema, GPT_MAX_TOKENS["jurisprudencia"],
            api_key, modelo, None, job_id=job_id, causa=numero,
        )
    if finish == "error" or texto_ia.startswith("[Error"):
        resultado["error"] = texto_ia
        return resultado
    resultado["jurisprudencia"] = texto_ia
    return resultado
