# lacortedice-extractor-constitucional

Extractor de La Corte Dice para **garantías constitucionales** (acción de protección, hábeas corpus,
hábeas data, acceso a la información pública y medidas cautelares autónomas), derivado de
`lacortedice-extractor-civil` el 19-nov-2026. Toda la materia está en `config/materia.py`:
idMateria 9 de SATJE, 328 judicaturas de primera instancia (cualquier juez conoce garantías)
y apelación en la Corte Provincial. No hay casación en la CNJ.

Pendiente: prompts y vocabularios propios (fase de IA con un constitucionalista), hábeas corpus
presentado directamente en la Sala provincial y numeración de 2008–2015.

---

Texto original del extractor civil:

# lacortedice-extractor-civil

Extractor de sentencias **Civil y Mercantil** del Ecuador para La Corte Dice. Derivado de
`lacortedice-extractor` (materia tributaria) en octubre de 2026: mismo motor
SATJE → IA → Strapi, con la materia desacoplada en `config/materia.py` y un
**tercer nivel de instancia (apelación en la Corte Provincial)** que la materia
tributaria no tiene.

| Nivel | Órgano | Detección (SATJE) | Prompts | Strapi |
|---|---|---|---|---|
| 1ª instancia | Unidad Judicial Civil (o Civil y Mercantil / Multicompetente) | `PRIMERA_INSTANCIA_CLAVES` sobre `nombreJudicatura` | 1, 2, 3, 4 (+7, 8, 9, 10 internos) | `causas` |
| Apelación | Sala Especializada de lo Civil y Mercantil de la Corte Provincial | `APELACION_CLAVES` | 1, 11, 12 | `apelaciones` |
| Casación | Sala Especializada de lo Civil y Mercantil de la CNJ | `idJudicatura == 17751` | 1, 5, 6 | `casaciones` |

Marco sustantivo: Código Civil, Código de Comercio, Ley de Compañías, Ley de Inquilinato + COGEP.

## Qué cambió respecto al extractor tributario

Dos commits sobre el historial original (`git log` los muestra):

1. **`refactor(materia)`** — materia-agnóstico. Todo lo que dependía de la
   materia pasó a `config/materia.py` (ids SATJE, judicaturas, claves de
   instancia, frases de decisión, enums de la llamada JSON, administraciones,
   códigos de los prompts 7/8/10, nombres de campo de Strapi, textos de
   fichas y briefs). Añade `resolucion/satje/apelacion.py`,
   `resolucion/extractores/apelacion.py`, `resolucion/cms/apelacion.py`,
   `StrapiClient.guardar_apelacion`, el paso 7b del orquestador y
   `scripts/descubrir_judicaturas.py`. Este commit es **portable al repo
   tributario**: con su `materia.py` el comportamiento es idéntico (los
   mismos tests pasan).
2. **`feat(civil)`** — instancia la materia: `config/materia.py`,
   `prompts/` completos (12 prompts + catálogo + `codigos.json`), ejemplos
   few-shot vaciados, `render.yaml`, este README y tests adaptados.

Lo que **no** cambió: `api.py` (cliente SATJE), `worker/` (jobs, SSE,
scheduler, embeddings, briefs, revisión de novedades, backfills),
`config/pdf_utils.py`, Dockerfile y `main.py` (API FastAPI).

## Prompts

`prompts/` es editable sin tocar código (ver `prompts/README.txt`). Contratos
que el código parsea y que **no se deben romper**:

- Prompt 2: 19 secciones numeradas con esos títulos exactos. Las secciones 9
  y 10 son *Pretensiones de la Parte Actora* / *Excepciones de la Parte
  Demandada* (en tributario eran acto impugnado / otros actos administrativos)
  y van a los campos Strapi `pretensiones` / `excepciones`.
- Prompt 4: termina con `Catálogo Aplicable:` + `CIVIL` y hasta 4
  categorías con el nombre exacto de `catalogo.txt`.
- Prompts 5 y 11: 14 secciones; la sección 11 cierra con una frase canónica
  (`Casa la sentencia. / No casa la sentencia. / Casa parcialmente la sentencia.` en casación;
  `Confirma la sentencia. / Revoca la sentencia. / Reforma la sentencia. / Declara la nulidad.` en apelación).
- Prompts 7, 8 y 10: códigos cerrados; la lista viva está en
  `prompts/codigos.json` y copiada en `config/materia.py` (si cambia uno,
  cambiar el otro: `tests/test_prompts_codigos.py` lo verifica).
- Tipos de acción: Procedimiento ordinario / sumario / ejecutivo / monitorio / voluntario / de ejecución / concursal.

## Antes de la primera corrida real

1. **Judicaturas e idMateria.** `config/materia.py` arranca con
   `JUDICATURAS` vacío e `IDS_MATERIA_SATJE` vacío; mientras tanto el filtro
   de materia funciona por el nombre de la judicatura (más lento porque
   necesita pedir incidentes). Para llenarlos:

   ```bash
   python scripts/descubrir_judicaturas.py --provincia 09 --ano 2024 --filtro CIVIL,MERCANTIL
   ```

   Imprime el bloque listo para pegar. Repetir por provincia (09 Guayas,
   17 Pichincha, 01 Azuay, 13 Manabí…). Un año de una Unidad Judicial de
   esta materia tiene miles de causas (vs. cientos en un TDCT): ajustar
   `MAX_VACIAS_RESOLUCION` y pensar los jobs por rangos de secuencial.

2. **Strapi (CMS).** Este extractor espera un CMS **propio** (no el
   tributario). Content-types necesarios, a partir del esquema tributario:

   - `causas`: igual, renombrando `acto_impugnado` → `pretensiones` y
     `otros_actos_administrativos` → `excepciones`.
   - **`apelaciones`** (nuevo): espejo de `casaciones` — `causa` (relación),
     `instancia`, `fecha_de_emision`, `antecedentes`,
     `argumentos_del_recurrente`, `argumentos_contraparte`,
     `criterio_sala_para_resolver`, `jurisprudencia_mencionada`,
     `medidas_adicionales`, `argumento_juez_salvado`, `observaciones`,
     `file_pdf`, `decision_final` (→ `decisiones-finales-apelacion`), `juez`,
     `jueces`, `voto_salvado`, `recurrentes`, `causal_invocadas`,
     `fecha_inicio_apel`, `fecha_sentencia_apel`, `duracion_dias_apel`.
   - **`decisiones-finales-apelacion`** (nuevo catálogo):
     Confirma la sentencia., Revoca la sentencia., Reforma la sentencia., Declara la nulidad.
   - `decision-finals` (1ª instancia): *con lugar*, *sin lugar*,
     *parcialmente con lugar* (el normalizador mapea "acepta la demanda" →
     con lugar, "rechaza" → sin lugar, "parcialmente" → parcial).
   - `tipo-de-accions`: los tipos de procedimiento de arriba.
   - `categorias`: cargar `prompts/catalogo.txt` (jerarquía padre→hijo).
   - `tribunales`: las judicaturas de 1ª instancia descubiertas en el paso 1.
   - `entidades`: aquí guarda al **demandado canonizado** (empleador /
     demandado), no a 3 administraciones.
   - `casaciones`, `jueces`, `abogados`, `causales` y las tablas propias
     del worker (`worker_*`, `causa_*`) se crean igual que en tributario
     (`worker/job_manager.py::init_db`).

3. **Variables de entorno** (`config/.env.example`): `WORKER_API_KEY`,
   `DATABASE_URL` (Postgres del Strapi propio), `STRAPI_URL`,
   `STRAPI_API_TOKEN`, `OPENAI_API_KEY`, `DEEPINFRA_API_KEY`,
   `OPENROUTER_API_KEY`, `MAX_CONCURRENT_JOBS`.

4. **Render**: `render.yaml` ya apunta al servicio `civil-worker`.

5. **Validar con 5 causas reales** antes de un barrido: crear un job con
   `POST /jobs/lista` (modo `vista_previa`) y revisar las fichas Word de los
   tres niveles. Especialmente: que `detectar_sentencia_apelacion` acierte
   el tipo de actuación con que SATJE etiqueta las sentencias de la Corte
   Provincial en esta materia (se escribió sin acceso a SATJE; la lista
   `_RESOLUCIONES` de `resolucion/satje/apelacion.py` es la primera cosa a
   corregir si falla).

## Flujo de trabajo recomendado: corpus primero, IA después

Bajar y entender son dos operaciones separadas; la primera se hace una vez y
la segunda tantas veces como cambien los prompts.

1. **Descargar el corpus sin IA** — `POST /jobs` con `"solo_descarga": true`.
   Guarda PDF + metadatos (SATJE, COGEP, duración) de los tres niveles con
   `Completitud = "PDF sin análisis"`. Cuesta tiempo de SATJE, no tokens.
2. **Conjunto patrón** — elegir 30–50 causas representativas y extraerlas
   (`POST /reextraer` con `numeros`). Luego
   `python scripts/evaluar_patron.py --generar <numeros>` crea `patron/<numero>.json`
   con lo que la IA sacó; un abogado los **corrige a mano** (son la verdad).
3. **Iterar prompts con número** — editar `prompts/*.txt`, re-extraer el patrón
   (`POST /reextraer` con los mismos `numeros`) y
   `python scripts/evaluar_patron.py --evaluar --etiqueta "p7-v2"`. El puntaje
   por criterio y el historial (`patron/historial.csv`) dicen si la ronda
   mejoró. Cuando dos rondas seguidas no suben, el prompt está listo.
4. **IA sobre todo el corpus** — `POST /reextraer` con
   `"completitud": "PDF sin análisis"` (por tribunal/año y `limite`), primero
   con Qwen; después GPT-4o solo sobre `"completitud": "Parcial - revisar"`.
   Cualquier mejora posterior de prompts se aplica igual, sin volver a SATJE.

`POST /reextraer` acepta `numeros` (lista explícita) o filtro
`tribunal`/`año`/`completitud`/`limite`, más `niveles` (`tribunal`, `corte`,
`apelacion`) y `modelo`.

## Desarrollo

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp config/.env.example config/.env   # y completar
pytest -q                            # 180 verdes + 4 fallos heredados del repo tributario
uvicorn main:app --reload            # API en :8000
```

Los 4 tests que fallan (`test_pdf_utils_abogados` ×3 y
`test_mapear_causa_incluye_duracion_tribunal`) ya fallaban en
`lacortedice-extractor@55ac5ec`: los valores esperados quedaron viejos
respecto al código (el parser de abogados ya quita el "Ab."). No son de esta
derivación.

## Pendientes conocidos

- `JUDICATURAS`, `JUDICATURAS_SLUG` e `IDS_MATERIA_SATJE` vacíos (paso 1).
- Sin ejemplos few-shot en `ejemplos/` (ver `ejemplos/LEEME.txt`).
- `worker/revision_worker.py` sigue llamando `no_tributaria` a la columna
  "es de otra materia" (nombre histórico, semántica correcta).
- Los briefs (`resolucion/briefs.py`) reciben textos de la materia desde
  `materia.py`, pero su lógica de métricas se diseñó para tributario; revisar
  con datos reales qué indicadores tienen sentido aquí.
- Las causas con **reconvención** (civil) cierran la Decisión Final con la
  frase de la demanda principal; la reconvención queda en la línea anterior
  (ver `prompts/README.txt`).
