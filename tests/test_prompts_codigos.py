"""
Coherencia entre prompts/codigos.json (lo que ven los abogados al editar
prompts) y config/materia.py (lo que valida el código). Si un código se
agrega o renombra en los prompts 7/8/10 hay que reflejarlo en los dos.
"""
import json
from pathlib import Path

from config import materia as M

COD = json.loads((Path(__file__).resolve().parent.parent / "prompts" / "codigos.json").read_text(encoding="utf-8"))


def test_tipos_defecto_coinciden():
    assert set(COD["tipos_defecto"]) == M.CODIGOS_TIPOS_DEFECTO


def test_tipo_prueba_coinciden():
    assert set(COD["tipo_prueba"]) == M.CODIGOS_TIPO_PRUEBA
    for alias, canon in COD["alias_tipo_prueba"].items():
        assert canon in M.CODIGOS_TIPO_PRUEBA, alias
    assert COD["alias_tipo_prueba"] == M.ALIAS_TIPO_PRUEBA


def test_ejes_y_tipo_prueba_clave():
    assert set(COD["ejes_decision"]) == M.EJES_DECISION
    assert set(COD["tipo_prueba_clave"]) == M.TIPO_PRUEBA_CLAVE


def test_decisiones_apelacion_y_casacion():
    assert COD["decisiones_apelacion"] == M.DECISIONES_APELACION
    assert COD["decisiones_casacion"] == M.DECISIONES_CASACION


def test_codigos_aparecen_en_los_prompts():
    base = Path(__file__).resolve().parent.parent / "prompts"
    p7 = (base / "prompt_7_tipos_defecto.txt").read_text(encoding="utf-8")
    p8 = (base / "prompt_8_tipo_prueba.txt").read_text(encoding="utf-8")
    for c in M.CODIGOS_TIPOS_DEFECTO:
        assert c in p7, c
    for c in M.CODIGOS_TIPO_PRUEBA:
        assert c in p8, c


def test_todas_las_subcategorias_del_catalogo_llegan_a_strapi():
    # Un nombre que el parser descarta (p. ej. por largo) nunca se guarda en el CMS.
    import re
    from worker.strapi_client import _parsear_nombres_categorias
    base = Path(__file__).resolve().parent.parent / "prompts"
    nombres = [l.split("\t", 1)[1].strip()
               for l in (base / "catalogo.txt").read_text(encoding="utf-8").splitlines()
               if re.match(r"^\d+\.\t", l)]
    texto = "\n".join(f"• Subcategoría: {n}" for n in nombres)
    assert _parsear_nombres_categorias(texto) == nombres


def test_compuertas_nombran_subcategorias_y_codigos_existentes():
    # Las subcategorías de las compuertas se comparan por nombre exacto.
    cat = (Path(__file__).resolve().parent.parent / "prompts" / "catalogo.txt").read_text(encoding="utf-8")
    for cods, cats, *_ in M.COMPUERTAS_PALABRA_CLAVE + M.COMPUERTAS_EXCLUYENTES:
        assert cods <= M.CODIGOS_TIPOS_DEFECTO, cods
        for c in cats:
            if isinstance(c, str):
                assert f"\t{c}\n" in cat, c
