PROMPTS DEL SISTEMA — Materia CIVIL Y MERCANTIL (Ecuador)
=====================================================================

Esta carpeta contiene las INSTRUCCIONES que se le envian al modelo de IA
para que analice cada sentencia civil o mercantil y genere las fichas
tecnicas. Cada archivo .txt es un prompt independiente que se puede
editar SIN TOCAR CODIGO.

Es la version para materia CIVIL Y MERCANTIL de la carpeta prompts/ del
extractor tributario. Conserva los mismos nombres de archivo, los mismos
titulos numerados de seccion (el codigo los busca por numero y titulo) y
las mismas reglas anti-fabricacion ("No consta en la sentencia").

DIFERENCIA CENTRAL CON LA VERSION TRIBUTARIA: la materia civil tiene
TRES niveles de decision, no dos.
  1. Primera instancia: Unidad Judicial Civil (o Civil y Mercantil, o
     Multicompetente), juez unipersonal. Decision: Acepta / Acepta
     parcialmente / Rechaza la demanda (o Acepta / Rechaza la
     reconvencion, abandono, nulidad, desistimiento, transaccion,
     conciliacion).
  2. Apelacion: Sala Especializada de lo Civil y Mercantil de la Corte
     Provincial de Justicia, tribunal de 3 jueces. Decision: Confirma /
     Revoca / Reforma la sentencia, o Declara la nulidad.
  3. Casacion: Sala Especializada de lo Civil y Mercantil de la Corte
     Nacional de Justicia. Decision: Casa / No casa / Casa parcialmente
     la sentencia.
Por eso existen dos prompts nuevos (11 y 12) para la apelacion.

ALCANCE DE LA MATERIA: obligaciones y contratos (compraventa, promesa,
arrendamiento e inquilinato, mutuo, mandato, comodato, fianza, prenda,
hipoteca), responsabilidad civil contractual y extracontractual (danos y
perjuicios, dano moral, lucro cesante, dano emergente), nulidad y
rescision (lesion enorme, simulacion, vicios del consentimiento),
resolucion de contrato, propiedad y derechos reales (reivindicacion,
prescripcion adquisitiva, servidumbres, particion, deslinde, posesion y
acciones posesorias, propiedad horizontal), sucesiones, titulos
ejecutivos y procedimiento ejecutivo, monitorio, societario (Ley de
Companias) y mercantil (Codigo de Comercio: seguros, transporte,
compraventa mercantil), insolvencia, medidas cautelares, tercerias,
prescripcion extintiva, caducidad, cosa juzgada, legitimacion,
competencia, nulidad procesal, abandono, carga de la prueba.
NO incluye familia, ninez y adolescencia, laboral, penal, contencioso
administrativo ni contencioso tributario (son otras materias en SATJE).

ATENCION: estos textos los lee directamente el bot. Cualquier cambio que
hagas en estos archivos se aplica la proxima vez que se procese una causa.
NO BORRES ninguno de los archivos, solo modifica su contenido.

ARCHIVOS:

  sistema.txt
    Mensaje "de sistema" principal. Define el tono, las reglas generales,
    como tratar los argumentos del actor y del demandado (incluida la
    reconvencion), el voto salvado, los tres niveles de instancia, el
    alcance de la materia y las exclusiones, etc.
    Se envia ANTES de los prompts 1, 3 y 4.

  sistema_neutro.txt
    Mensaje de sistema alternativo con el contexto de informacion publica
    (SATJE, art. 168 de la Constitucion) antepuesto a las directrices. Se
    usa SOLO en el Prompt 2 (Extraccion 19 secciones) para evitar que el
    proveedor bloquee la extraccion por activar filtros de contenido.

  prompt_1_resumen.txt
    Pide un resumen ejecutivo del caso en maximo 350 palabras, en un
    solo parrafo. Identifica organo, partes y su calidad, tipo de
    procedimiento, objeto, decision (incluida la reconvencion si la hay).

  prompt_2_extraccion.txt
    Pide la extraccion de 19 secciones de la sentencia de PRIMERA
    INSTANCIA (instancia, juzgado, juez, partes, tipo de procedimiento
    con la pretension especifica tras un guion largo, cuantia y su
    detalle por rubros, pretensiones (con sublista de la reconvencion),
    excepciones (con sublista de excepciones a la reconvencion), objeto,
    antecedentes, pruebas del actor / del demandado, argumentos,
    decision final con frase canonica, disposiciones adicionales, voto
    salvado). Es el prompt mas largo y el que mas datos saca.
    Secciones 9 y 10 (Pretensiones / Excepciones) reemplazan a "Acto
    impugnado" / "Otros actos administrativos" de la version tributaria.
    Cuando hay reconvencion, la frase canonica FINAL se refiere a la
    demanda principal; lo resuelto sobre la reconvencion va en la linea
    anterior ("Acepta la reconvencion." / "Rechaza la reconvencion.").

  prompt_3_jurisprudencia.txt
    Pide citas textuales de la jurisprudencia mencionada y de los
    criterios del juzgador de primera instancia, sin parafrasear.
    Conserva los dos encabezados que el codigo separa:
    "1. Jurisprudencia mencionada en el caso:" y
    "2. Criterio del tribunal para resolver el caso:".

  prompt_4_clasificacion.txt
    Pide clasificar la sentencia segun el catalogo civil (siguiente
    archivo). Maximo 4 categorias. La linea "Catalogo Aplicable:" va
    SIEMPRE seguida de la palabra CIVIL (unico bloque).

  catalogo.txt
    Catalogo oficial de categorias civiles y mercantiles (24 categorias
    con subcategorias) que se concatena al final del Prompt 4. Misma
    sintaxis que el original (Categoria N / N.<TAB>Nombre /
    Subcategorias 1 / N.<TAB>Nombre). Un solo encabezado: CIVIL.
    El modelo solo puede elegir categorias que aparezcan EXACTAMENTE
    aqui — no inventa ni reformula. Nombres de maximo 70 caracteres
    (limite del parser).

  prompt_5_cnj_extraccion.txt
    Extraccion de 14 secciones de la sentencia de CASACION de la Sala
    de lo Civil y Mercantil de la Corte Nacional de Justicia. La seccion
    4 es "Sala de la Corte Provincial que emitio la sentencia recurrida"
    y la seccion 6 "Decision de las instancias anteriores" (primera y
    segunda instancia). Decision final: Casa / No casa / Casa
    parcialmente la sentencia.

  prompt_6_cnj_jurisprudencia.txt
    Jurisprudencia + criterio de la Sala de lo Civil y Mercantil de la
    Corte Nacional de Justicia (mismos dos encabezados que el prompt 3).

  prompt_11_apelacion_extraccion.txt   (NUEVO)
    Extraccion de 14 secciones de la sentencia de APELACION de la Sala
    de lo Civil y Mercantil de la Corte Provincial. Seccion 4 "Juzgado
    que emitio la sentencia recurrida", seccion 6 "Decision de primera
    instancia", seccion 8 "Causales o agravios invocados" (la apelacion
    no tiene causales tasadas). Decision final: Confirma / Revoca /
    Reforma la sentencia, o Declara la nulidad (la frase se refiere a la
    demanda principal cuando tambien se resuelve la reconvencion).

  prompt_12_apelacion_jurisprudencia.txt   (NUEVO)
    Jurisprudencia + criterio de la Sala Provincial (espejo del 6).

  prompt_7_tipos_defecto.txt
    Clasificacion interna (jurimetria, no se muestra al usuario) del
    EJE/TIPO DE CONTROVERSIA civil o mercantil: 26 codigos en
    MAYUSCULAS_CON_GUION (INCUMPLIMIENTO_CONTRACTUAL,
    COBRO_OBLIGACION_DINERARIA, TITULO_EJECUTIVO_EXCEPCIONES,
    NULIDAD_CONTRATO, LESION_ENORME, RESPONSABILIDAD_EXTRACONTRACTUAL,
    DANO_MORAL, REIVINDICACION, PRESCRIPCION_ADQUISITIVA,
    ACCIONES_POSESORIAS, PARTICION_DESLINDE_SERVIDUMBRES,
    ARRENDAMIENTO_INQUILINATO, SUCESIONES, SOCIETARIO,
    MERCANTIL_SEGUROS_TRANSPORTE, INSOLVENCIA_CONCURSO,
    TERCERIA_MEDIDAS_CAUTELARES, PRESCRIPCION_EXTINTIVA_CADUCIDAD,
    COSA_JUZGADA_TRANSACCION, LEGITIMACION_PERSONERIA, COMPETENCIA,
    NULIDAD_PROCESAL, FALTA_MOTIVACION, ABANDONO,
    CARGA_PRUEBA, VALORACION_PRUEBA, AUTENTICIDAD_DOCUMENTAL, POSESION_BUENA_MALA_FE, MANDATO_RENDICION_CUENTAS, SENTENCIA_SIN_OPOSICION) + SIN_ESPECIFICAR. Una linea por codigo.
    En la version tributaria eran "vicios del acto".

  prompt_8_tipo_prueba.txt
    Clasificacion interna del TIPO DE PRUEBA por lado (ACTOR / DEMANDADO)
    con catalogo civil: ESCRITURA_PUBLICA, TITULO_VALOR,
    CERTIFICADO_REGISTRO_PROPIEDAD, DOCUMENTAL_SOCIETARIA,
    DOCUMENTAL_MUNICIPAL, INFORME_PERICIAL_AVALUO, DOCUMENTAL_CONTABLE,
    DOCUMENTAL_ADMINISTRATIVO, mas los genericos (TESTIMONIAL,
    INSPECCION_JUDICIAL, DOCUMENTO_NOTARIAL, CONTRATO, CORRESPONDENCIA,
    CERTIFICACION_BANCARIA, INFORME_PERICIAL_CONTABLE,
    INFORME_PERICIAL_TECNICO, DECLARACION_TERCERO,
    JURISPRUDENCIA_PRECEDENTE, OTRA_DOCUMENTAL, NO_DETERMINABLE).
    La declaracion de parte y la confesion judicial se agrupan en
    TESTIMONIAL (como en el original). Incluye los campos POR_QUE y
    EVIDENCIA_AUSENTE. Los ejemplos son ilustrativos (inventados), no
    medidos.

  prompt_9_evidencia_ausente.txt
    Clasificacion interna: si el juzgador reprocha a una parte NO haber
    presentado una prueba concreta e identificable. Recibe la sentencia
    completa. Ejemplos ilustrativos de la materia civil.

  prompt_10_extraccion_estructurada.txt
    JSON de 9 campos (eje_decision, prueba_suficiente, tipo_prueba_clave,
    norma_clave, precedente_citado, tesis_contraparte, como_superar,
    clave_resultado, cita_textual). En eje_decision el eje de fondo es
    "fondo civil" y "motivacion de la sentencia" reemplaza a
    "motivacion del acto". La prescripcion ADQUISITIVA como pretension
    no va en "prescripcion/caducidad" (ese eje es solo para la
    prescripcion extintiva y la caducidad).

  codigos.json
    Resumen MAQUINA de todas las listas cerradas que usan los prompts
    (tipos_defecto, tipo_prueba, alias_tipo_prueba, ejes_decision,
    tipo_prueba_clave, decisiones_primera_instancia,
    decisiones_apelacion, decisiones_casacion, tipos_accion). Los codigos
    coinciden EXACTAMENTE con los de los prompts 7, 8 y 10. Sirve para
    actualizar las listas de validacion del codigo (worker/job_manager.py,
    resolucion/ai/tribunal.py, worker/strapi_client.py) sin releer los
    prompts.

NOTA PARA QUIEN ADAPTE EL CODIGO:
  - Las listas de codigos validos del worker (tipos de defecto, tipos de
    prueba y alias), los enums de decision final y tipo de accion de los
    structured outputs, y los disparadores de "Se caso / No se caso"
    estan HARDCODEADOS en Python con los valores tributarios. Deben
    reemplazarse por los de codigos.json.
  - El parser de clasificacion busca "Catalogo Aplicable:" seguido de
    SRI/SENAE/GAD; debe aceptar CIVIL.
  - Los tipos de accion de esta materia se escriben en minusculas y
    pueden ir seguidos de un guion largo y la pretension especifica
    ("procedimiento ordinario — reivindicacion"); el normalizador debe
    quedarse con la parte anterior al guion para el enum.
  - La decision de primera instancia admite dos frases adicionales
    ("Acepta la reconvencion." / "Rechaza la reconvencion.") que pueden
    aparecer en la linea ANTERIOR a la frase canonica final.
  - Hace falta un tercer flujo (prompts 11 y 12) para las sentencias de
    apelacion, con su propio normalizador de decision (Confirma / Revoca
    / Reforma / Nulidad).

COMO EDITAR:

  Opcion 1 (recomendada): usar el Inspector Visual.
    Doble-click en "Inspector Visual.command" -> pestana "Editor de
    Prompts" -> editar -> guardar. La app valida que no se haya borrado
    nada por error.

  Opcion 2: abrir el .txt directamente con TextEdit, Notepad, Word.
    Guardar manteniendo la codificacion UTF-8.

TIP: si quieres probar un cambio antes de aplicarlo a todas las
causas, guarda una copia del archivo original (ej. sistema.txt.backup)
para poder revertir.
