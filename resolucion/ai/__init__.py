"""
Capa de abstracción de IA.

Punto de entrada único para análisis de sentencias.
Despacha a OpenAI o Qwen según el nombre del modelo.

Uso:
    from resolucion.ai import analizar_sentencia, analizar_casacion

    resultado = analizar_sentencia(pdf_bytes, numero, demandado="...", modelo="gpt-4o", job_id=3)
    resultado = analizar_casacion(pdf_bytes, numero, modelo="gpt-4o", job_id=3)
"""

from functools import partial

from resolucion.ai.tribunal import analizar as analizar_sentencia
from resolucion.ai.corte import analizar as analizar_casacion

# Apelación (Corte Provincial): mismo motor que casación con los prompts 11/12.
analizar_apelacion = partial(analizar_casacion, nivel="apelacion")

__all__ = ["analizar_sentencia", "analizar_casacion", "analizar_apelacion"]
