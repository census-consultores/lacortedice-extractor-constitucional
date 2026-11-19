"""
La primera instancia la dicta un juez unipersonal (Unidad Judicial): lo que lee
el modelo o ve el usuario en esa ficha no dice «tribunal». La apelación (Sala de
la Corte Provincial, tres jueces) sí lo conserva. Y el órgano de un precedente
nunca se deduce: si la sentencia no lo nombra, «No consta en la sentencia».
"""
import io
import re

from docx import Document

import resolucion.ai.tribunal as trib
import worker.strapi_client as sc
from config.config import PROMPTS_DIR
from resolucion.ai._cliente import crear_docx_tribunal

_PRIMERA = ["prompt_2_extraccion.txt", "prompt_3_jurisprudencia.txt", "prompt_4_clasificacion.txt",
            "prompt_7_tipos_defecto.txt", "prompt_8_tipo_prueba.txt", "prompt_9_evidencia_ausente.txt"]
# Nombres propios que no son el órgano de la causa.
_PERMITIDO = re.compile(r"tribunales de conciliaci[oó]n y arbitraje", re.I)


def _leer(nombre):
    return (PROMPTS_DIR / nombre).read_text(encoding="utf-8")


def test_prompts_de_primera_instancia_no_dicen_tribunal():
    for nombre in _PRIMERA:
        texto = _PERMITIDO.sub("", _leer(nombre))
        assert not re.search(r"tribunal", texto, re.I), nombre


def test_prompts_compartidos_solo_dicen_tribunal_para_la_apelacion():
    # sistema y prompt 1 sirven a las tres instancias: «tribunal» solo describe la Sala provincial.
    for nombre in ("prompt_1_resumen.txt", "sistema.txt", "sistema_neutro.txt"):
        for m in re.finditer(r"tribunal.{0,20}", _leer(nombre), re.I):
            assert m.group(0).lower().startswith("tribunal de tres jueces"), (nombre, m.group(0))


def test_prompt_2_pide_el_juez_de_la_causa_sin_inventar_otros():
    texto = _leer("prompt_2_extraccion.txt")
    assert "2. Unidad Judicial que emite la sentencia:" in texto
    assert "• Juez de la causa:" in texto
    assert "Juez Ponente" not in texto and "máximo dos" not in texto


def test_apelacion_conserva_tribunal():
    assert "Criterio del tribunal para resolver el caso" in _leer("prompt_12_apelacion_jurisprudencia.txt")


def test_word_de_primera_instancia_dice_juzgador():
    doc = Document(io.BytesIO(crear_docx_tribunal("1", "R.", "E.", "J.", "C.")))
    textos = [p.text for p in doc.paragraphs]
    assert "III. JURISPRUDENCIA Y CRITERIOS DEL JUZGADOR" in textos
    assert not any("TRIBUNAL" in t.upper() for t in textos)


def test_entradas_de_prompts_8_y_10_dicen_juzgador(monkeypatch):
    enviados = []
    monkeypatch.setattr(trib, "cargar_api_key_openai", lambda: "k")
    monkeypatch.setattr(trib, "cargar_prompt", lambda n: "")
    monkeypatch.setattr(trib, "llamar_openai", lambda prompt, *a, **k: (enviados.append(prompt), ("{}", "stop"))[1])
    trib.clasificar_tipo_prueba("obj", "pruebas", "", "criterio", modelo="gpt-4o")
    trib.extraccion_estructurada("obj", "arg", "p", "p", "j", "c" * 250, True, modelo="gpt-4o")
    assert len(enviados) == 2
    assert "CRITERIO DEL JUZGADOR PARA RESOLVER" in enviados[0]
    assert "RAZONAMIENTO DEL JUZGADOR" in enviados[1]
    assert not any("TRIBUNAL" in e for e in enviados)


def test_parser_lee_rotulos_nuevos_y_viejos():
    assert sc.StrapiClient._parsear_jueces_seccion("• Juez de la causa: Ana Ruiz") == ("Ana Ruiz", ["Ana Ruiz"])
    assert sc.StrapiClient._parsear_jueces_seccion("• Juez Ponente: Ana Ruiz")[0] == "Ana Ruiz"
    patron = dict(sc._TITULOS_TRIBUNAL)[2]
    assert patron.search("Unidad Judicial que emite la sentencia:")
    assert patron.search("Juzgado o Tribunal que emite la sentencia:")


def test_organo_del_precedente_nunca_se_deduce():
    for nombre in ("prompt_3_jurisprudencia.txt", "prompt_6_cnj_jurisprudencia.txt",
                   "prompt_12_apelacion_jurisprudencia.txt"):
        texto = _leer(nombre)
        assert "2009" not in texto, nombre
        assert "nunca lo deduzcas" in texto, nombre
