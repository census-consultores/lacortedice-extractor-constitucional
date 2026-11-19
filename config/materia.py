"""
MATERIA — el único archivo que distingue un extractor de otro.

Todo lo que depende de la materia jurídica (qué causas aceptar en SATJE,
cómo se llaman las judicaturas de cada instancia, qué frases canónicas de
decisión existen, qué códigos cerrados usan los prompts 7/8/10, cómo se
etiqueta el estado del juicio) vive AQUÍ. El resto del código es genérico
y lo importa. Los códigos de los prompts 7/8/10 se copian de
prompts/codigos.json (fuente de verdad junto con los .txt).

Esta versión describe las GARANTÍAS CONSTITUCIONALES (Ecuador): acción de protección,
hábeas corpus, hábeas data, acceso a la información pública y medidas cautelares
autónomas. Primera instancia: cualquier juez (328 judicaturas de primera instancia;
el hábeas corpus que se presenta directo en la Sala provincial, art. 44 LOGJCC, queda
para una segunda etapa porque la Sala también es la instancia de apelación); apelación: Corte Provincial.
Lo que sigue (vocabularios, códigos de prompts) es aún el heredado de civil:
se reemplaza en la fase de IA con un constitucionalista.

Texto original:
  1ª instancia  → Unidad Judicial Civil (juez unipersonal)
  apelación     → Sala de lo Civil y Mercantil de la Corte Provincial (3 jueces)
  casación      → Sala Especializada de lo Civil y Mercantil – Corte Nacional de Justicia

JUDICATURAS / JUDICATURAS_SLUG / IDS_MATERIA_SATJE salen del barrido de las
24 provincias con scripts/descubrir_judicaturas.py (2026-10-14).
"""

# ─── Identidad ───────────────────────────────────────────────────────────────
SLUG             = 'constitucional'
NOMBRE           = 'Garantías Constitucionales'
PALABRA_CATALOGO = 'CONSTITUCIONAL'      # lo que el prompt 4 escribe tras "Catálogo Aplicable:"
SERVICIO_RENDER  = 'constitucional-worker'

# ─── Filtro de materia en SATJE ──────────────────────────────────────────────
# idMateria de SATJE para esta materia. VACÍO hasta verificarlo con
# scripts/descubrir_judicaturas.py: mientras tanto se acepta por el nombre de
# la judicatura de primera instancia (más lento: requiere pedir incidentes).
# Cola automática (worker/job_manager.alimentar_cola): unidades propias de la
# materia, que se barren completas; el resto se cubre con el índice rápido.
COLA_ESPECIALIZADAS: tuple = ()   # sin unidades propias: todo entra por el índice rápido
COLA_EXCLUIR: tuple = ()

IDS_MATERIA_SATJE: set = {9}    # garantías constitucionales (mapeo 9-nov-2026; índices civil y penal)
ETIQUETA_OTRA_MATERIA = 'No constitucional'

# ─── Instancias ──────────────────────────────────────────────────────────────
# Tupla = todas sus palabras deben aparecer en nombreJudicatura (normalizado,
# sin tildes, MAYÚSCULAS); basta con que matchee una tupla de la lista.
PRIMERA_INSTANCIA_CLAVES = [("UNIDAD JUDICIAL",), ("JUZGADO",), ("TRIBUNAL DE GARANTIAS PENALES",)]   # cualquier juez conoce garantías; se reconoce sobre todo por el código (JUDICATURAS)
# Apelación: Sala de la Corte Provincial. Lista NO vacía = materia de tres niveles.
APELACION_CLAVES = [("CORTE PROVINCIAL",)]   # cualquier Sala de Corte Provincial que conozca la causa (especializada o multicompetente)
ID_CNJ = "00000"   # sin casación en la CNJ: tras la Corte Provincial sigue la Corte Constitucional (fuera de SATJE)                   # Sala de la Corte Nacional de Justicia (verificado en SATJE)
SALA_CNJ = 'No aplica'
NOMBRE_PRIMERA_INSTANCIA = 'Juez de garantías jurisdiccionales'
NOMBRE_APELACION         = 'Sala de la Corte Provincial (apelación)'

# Etiquetas de "Estado de Juicio"
ESTADO_EN_PRIMERA   = 'En Unidad Judicial'
ESTADO_EN_APELACION = "En Corte Provincial"
ESTADO_EN_CNJ       = "En Corte Nacional"
# Cuando la casación ya se resolvió (sentencia, inadmisión, desistimiento…).
# Vacío = conservar ESTADO_EN_CNJ también para las causas ya resueltas.
ESTADO_TERMINADO_CNJ = "Concluido en Corte Nacional"
# Igual para la apelación: la Sala ya resolvió y no hubo casación.
ESTADO_TERMINADO_APELACION = "Concluido en Corte Provincial"

# ─── Judicaturas de primera instancia (prefijo del número de juicio → nombre) ─
# Los prefijos son los 5 dígitos iniciales del número de causa en SATJE.
# Se llenan con:  python scripts/descubrir_judicaturas.py --provincia 09 --ano 2024
# (el script imprime este bloque listo para pegar). Ejemplo de forma:
#   "09XXX": "Unidad Judicial de Trabajo con sede en el cantón Guayaquil",
# 155 judicaturas de primera instancia (Civil + Multicompetente no penal), barrido de las 24 provincias, año 2024.
JUDICATURAS: dict = {
    "01201": "Unidad Judicial de Familia, Mujer, Niñez y Adolescencia (01201)",
    "01202": "Unidad Judicial de Familia, Mujer, Niñez y Adolescencia (01202)",
    "01203": "Unidad Judicial de Familia, Mujer, Niñez y Adolescencia (01203)",
    "01204": "Unidad Judicial de Familia, Mujer, Niñez y Adolescencia (01204)",
    "01281": "Unidad Judicial Penal (01281)",
    "01282": "Unidad Judicial Penal (01282)",
    "01283": "Unidad Judicial Penal (01283)",
    "01331": "Unidad Judicial Civil (01331)",
    "01332": "Unidad Judicial Civil (01332)",
    "01333": "Unidad Judicial Civil (01333)",
    "01371": "Unidad Judicial de Trabajo (01371)",
    "01610": "Unidad Judicial Multicompetente (01610)",
    "01612": "Unidad Judicial Multicompetente (01612)",
    "01613": "Unidad Judicial Multicompetente (01613)",
    "01617": "Unidad Judicial Multicompetente (01617)",
    "01618": "Unidad Judicial Multicompetente (01618)",
    "01619": "Unidad Judicial Multicompetente (01619)",
    "01658": "Unidad Judicial Multicompetente (01658)",
    "01904": "Tribunal de Garantías Penales (01904)",
    "02202": "Unidad Judicial de Familia, Mujer, Niñez y Adolescencia (02202)",
    "02241": "Tribunal de Garantías Penales (02241)",
    "02281": "Unidad Judicial Penal (02281)",
    "02305": "Unidad Judicial Multicompetente (02305)",
    "02308": "Unidad Judicial Multicompetente (02308)",
    "02331": "Unidad Judicial Civil (02331)",
    "02332": "Unidad Judicial Multicompetente (02332)",
    "02333": "Unidad Judicial Multicompetente (02333)",
    "02334": "Unidad Judicial Multicompetente (02334)",
    "02335": "Unidad Judicial Multicompetente (02335)",
    "03201": "Unidad Judicial de Familia, Mujer, Niñez y Adolescencia (03201)",
    "03203": "Unidad Judicial de Familia, Mujer, Niñez y Adolescencia (03203)",
    "03204": "Unidad Judicial Multicompetente (03204)",
    "03257": "Unidad Judicial Multicompetente (03257)",
    "03281": "Unidad Judicial Multicompetente (03281)",
    "03282": "Unidad Judicial Multicompetente (03282)",
    "03283": "Unidad Judicial Penal (03283)",
    "03331": "Unidad Judicial Multicompetente (03331)",
    "03332": "Unidad Judicial Civil (03332)",
    "03333": "Unidad Judicial Civil (03333)",
    "03334": "Unidad Judicial Multicompetente (03334)",
    "04243": "Tribunal de Garantías Penales (04243)",
    "04281": "Unidad Judicial Penal (04281)",
    "04306": "Unidad Judicial Multicompetente (04306)",
    "04332": "Unidad Judicial Multicompetente (04332)",
    "04333": "Unidad Judicial Civil (04333)",
    "04334": "Unidad Judicial Multicompetente (04334)",
    "04335": "Unidad Judicial Multicompetente (04335)",
    "04951": "Unidad Judicial de Familia, Mujer, Niñez y Adolescencia (04951)",
    "05151": "Unidad Judicial Multicompetente (05151)",
    "05202": "Unidad Judicial de Familia, Mujer, Niñez y Adolescencia (05202)",
    "05241": "Tribunal de Garantías Penales (05241)",
    "05254": "Unidad Judicial Multicompetente (05254)",
    "05282": "Unidad Judicial Multicompetente (05282)",
    "05283": "Unidad Judicial Penal (05283)",
    "05304": "Unidad Judicial Multicompetente (05304)",
    "05307": "Unidad Judicial Multicompetente (05307)",
    "05331": "Unidad Judicial Multicompetente (05331)",
    "05332": "Unidad Judicial Multicompetente (05332)",
    "05333": "Unidad Judicial Civil (05333)",
    "05334": "Unidad Judicial Multicompetente (05334)",
    "05335": "Unidad Judicial Multicompetente (05335)",
    "05371": "Unidad Judicial de Trabajo (05371)",
    "06101": "Unidad Judicial de Familia, Mujer, Niñez y Adolescencia (06101)",
    "06102": "Unidad Judicial Multicompetente (06102)",
    "06171": "Tribunal de Garantías Penales (06171)",
    "06282": "Unidad Judicial Penal (06282)",
    "06308": "Unidad Judicial Multicompetente (06308)",
    "06310": "Unidad Judicial Multicompetente (06310)",
    "06332": "Unidad Judicial Multicompetente (06332)",
    "06333": "Unidad Judicial Multicompetente (06333)",
    "06334": "Unidad Judicial Multicompetente (06334)",
    "06335": "Unidad Judicial Civil (06335)",
    "06336": "Unidad Judicial Multicompetente (06336)",
    "06352": "Unidad Judicial de Trabajo (06352)",
    "07171": "Tribunal de Garantías Penales (07171)",
    "07201": "Unidad Judicial de Familia, Mujer, Niñez y Adolescencia (07201)",
    "07205": "Unidad Judicial de Adolescentes Infractores (07205)",
    "07206": "Unidad Judicial de Familia, Mujer, Niñez y Adolescencia (07206)",
    "07257": "Unidad Judicial Multicompetente (07257)",
    "07258": "Unidad Judicial Multicompetente (07258)",
    "07259": "Unidad Judicial Multicompetente (07259)",
    "07281": "Unidad Judicial Multicompetente (07281)",
    "07283": "Unidad Judicial Penal (07283)",
    "07307": "Unidad Judicial Civil (07307)",
    "07308": "Unidad Judicial Multicompetente (07308)",
    "07309": "Unidad Judicial Multicompetente (07309)",
    "07310": "Unidad Judicial Multicompetente (07310)",
    "07312": "Unidad Judicial Multicompetente (07312)",
    "07317": "Unidad Judicial Multicompetente (07317)",
    "07331": "Unidad Judicial Multicompetente (07331)",
    "07332": "Unidad Judicial Multicompetente (07332)",
    "07333": "Unidad Judicial Civil (07333)",
    "07334": "Unidad Judicial Civil (07334)",
    "07335": "Unidad Judicial Multicompetente (07335)",
    "07371": "Unidad Judicial de Trabajo (07371)",
    "07710": "Unidad Judicial de Flagrancia (07710)",
    "07711": "Unidad Judicial de Flagrancia (07711)",
    "07712": "Unidad Judicial de Flagrancia (07712)",
    "08201": "Unidad Judicial de Familia, Mujer, Niñez y Adolescencia (08201)",
    "08244": "Tribunal de Garantías Penales (08244)",
    "08256": "Unidad Judicial Multicompetente (08256)",
    "08257": "Unidad Judicial Multicompetente (08257)",
    "08281": "Unidad Judicial Multicompetente (08281)",
    "08282": "Unidad Judicial Penal (08282)",
    "08304": "Unidad Judicial Multicompetente (08304)",
    "08308": "Unidad Judicial Multicompetente (08308)",
    "08309": "Unidad Judicial Multicompetente (08309)",
    "08331": "Unidad Judicial Civil (08331)",
    "08332": "Unidad Judicial Multicompetente (08332)",
    "08371": "Unidad Judicial de Trabajo (08371)",
    "09201": "Unidad Judicial de Familia, Mujer, Niñez y Adolescencia (09201)",
    "09202": "Unidad Judicial de Familia, Mujer, Niñez y Adolescencia (09202)",
    "09204": "Unidad Judicial Multicompetente (09204)",
    "09208": "Unidad Judicial de Familia, Mujer, Niñez y Adolescencia (09208)",
    "09209": "Unidad Judicial de Familia, Mujer, Niñez y Adolescencia (09209)",
    "09210": "Unidad Judicial de Familia, Mujer, Niñez y Adolescencia (09210)",
    "09211": "Unidad Judicial Multicompetente (09211)",
    "09212": "Unidad Judicial Multicompetente (09212)",
    "09266": "Unidad Judicial Multicompetente (09266)",
    "09267": "Unidad Judicial Multicompetente (09267)",
    "09272": "Unidad Judicial Multicompetente (09272)",
    "09281": "Unidad Judicial de Flagrancia (09281)",
    "09284": "Unidad Judicial Penal (09284)",
    "09285": "Unidad Judicial Penal (09285)",
    "09286": "Unidad Judicial Penal (09286)",
    "09287": "Unidad Judicial Penal (09287)",
    "09288": "Unidad Judicial Penal (09288)",
    "09290": "Unidad Judicial Multicompetente (09290)",
    "09292": "Unidad Judicial de Flagrancia (09292)",
    "09315": "Unidad Judicial Civil (09315)",
    "09318": "Unidad Judicial Multicompetente (09318)",
    "09319": "Unidad Judicial Multicompetente (09319)",
    "09320": "Unidad Judicial Multicompetente (09320)",
    "09322": "Unidad Judicial Multicompetente (09322)",
    "09326": "Unidad Judicial Multicompetente (09326)",
    "09327": "Unidad Judicial Multicompetente (09327)",
    "09330": "Unidad Judicial Civil (09330)",
    "09332": "Unidad Judicial Civil (09332)",
    "09333": "Unidad Judicial Multicompetente (09333)",
    "09334": "Unidad Judicial Multicompetente (09334)",
    "09335": "Unidad Judicial de Trabajo (09335)",
    "09337": "Unidad Judicial Multicompetente (09337)",
    "09356": "Unidad Judicial de Trabajo (09356)",
    "09359": "Unidad Judicial de Trabajo (09359)",
    "09901": "Tribunal de Garantías Penales (09901)",
    "09914": "Tribunal de Garantías Penales (09914)",
    "09915": "Tribunal de Garantías Penales (09915)",
    "09969": "Unidad Judicial de Familia, Mujer, Niñez y Adolescencia (09969)",
    "10201": "Unidad Judicial de Adolescentes Infractores (10201)",
    "10203": "Unidad Judicial de Adolescentes Infractores (10203)",
    "10243": "Tribunal de Garantías Penales (10243)",
    "10281": "Unidad Judicial Penal (10281)",
    "10282": "Unidad Judicial Penal (10282)",
    "10309": "Unidad Judicial Multicompetente (10309)",
    "10310": "Unidad Judicial Multicompetente (10310)",
    "10311": "Unidad Judicial Civil (10311)",
    "10333": "Unidad Judicial Multicompetente (10333)",
    "10334": "Unidad Judicial Multicompetente (10334)",
    "11203": "Unidad Judicial de Familia, Mujer, Niñez y Adolescencia (11203)",
    "11258": "Unidad Judicial Multicompetente (11258)",
    "11282": "Unidad Judicial Penal (11282)",
    "11308": "Unidad Judicial Multicompetente (11308)",
    "11310": "Unidad Judicial Multicompetente (11310)",
    "11313": "Unidad Judicial Multicompetente (11313)",
    "11314": "Unidad Judicial Multicompetente (11314)",
    "11317": "Unidad Judicial Multicompetente (11317)",
    "11318": "Unidad Judicial Multicompetente (11318)",
    "11320": "Unidad Judicial Multicompetente (11320)",
    "11331": "Unidad Judicial Multicompetente (11331)",
    "11332": "Unidad Judicial Multicompetente (11332)",
    "11333": "Unidad Judicial Civil (11333)",
    "11335": "Unidad Judicial Multicompetente (11335)",
    "11336": "Unidad Judicial Multicompetente (11336)",
    "11337": "Unidad Judicial Multicompetente (11337)",
    "11371": "Unidad Judicial de Trabajo (11371)",
    "11904": "Tribunal de Garantías Penales (11904)",
    "12201": "Unidad Judicial de Familia, Mujer, Niñez y Adolescencia (12201)",
    "12203": "Unidad Judicial de Familia, Mujer, Niñez y Adolescencia (12203)",
    "12242": "Tribunal de Garantías Penales (12242)",
    "12244": "Tribunal de Garantías Penales (12244)",
    "12281": "Unidad Judicial Multicompetente (12281)",
    "12282": "Unidad Judicial Penal (12282)",
    "12283": "Unidad Judicial Penal (12283)",
    "12309": "Unidad Judicial Multicompetente (12309)",
    "12310": "Unidad Judicial Multicompetente (12310)",
    "12312": "Unidad Judicial Multicompetente (12312)",
    "12313": "Unidad Judicial Multicompetente (12313)",
    "12331": "Unidad Judicial de Trabajo (12331)",
    "12332": "Unidad Judicial Multicompetente (12332)",
    "12333": "Unidad Judicial Multicompetente (12333)",
    "12334": "Unidad Judicial Civil (12334)",
    "12335": "Unidad Judicial Multicompetente (12335)",
    "12336": "Unidad Judicial Multicompetente (12336)",
    "12371": "Unidad Judicial de Trabajo (12371)",
    "13176": "Tribunal de Garantías Penales (13176)",
    "13177": "Tribunal de Garantías Penales (13177)",
    "13203": "Otra judicatura (13203)",
    "13204": "Otra judicatura (13204)",
    "13205": "Unidad Judicial de Adolescentes Infractores (13205)",
    "13244": "Tribunal de Garantías Penales (13244)",
    "13245": "Tribunal de Garantías Penales (13245)",
    "13253": "Unidad Judicial Multicompetente (13253)",
    "13259": "Unidad Judicial Multicompetente (13259)",
    "13266": "Unidad Judicial Multicompetente (13266)",
    "13267": "Unidad Judicial Multicompetente (13267)",
    "13281": "Unidad Judicial Multicompetente (13281)",
    "13282": "Unidad Judicial Penal (13282)",
    "13283": "Unidad Judicial Penal (13283)",
    "13284": "Unidad Judicial Penal (13284)",
    "13313": "Unidad Judicial Multicompetente (13313)",
    "13314": "Unidad Judicial Multicompetente (13314)",
    "13315": "Unidad Judicial Multicompetente (13315)",
    "13317": "Unidad Judicial de Adolescentes Infractores (13317)",
    "13320": "Unidad Judicial Multicompetente (13320)",
    "13322": "Unidad Judicial Multicompetente (13322)",
    "13331": "Unidad Judicial Multicompetente (13331)",
    "13332": "Unidad Judicial Multicompetente (13332)",
    "13333": "Unidad Judicial Multicompetente (13333)",
    "13334": "Unidad Judicial Civil (13334)",
    "13335": "Unidad Judicial Multicompetente (13335)",
    "13336": "Unidad Judicial Civil (13336)",
    "13337": "Unidad Judicial Civil (13337)",
    "13338": "Unidad Judicial de Adolescentes Infractores (13338)",
    "13339": "Unidad Judicial Multicompetente (13339)",
    "13340": "Unidad Judicial Multicompetente (13340)",
    "13354": "Unidad Judicial de Trabajo (13354)",
    "13371": "Otra judicatura (13371)",
    "13959": "Unidad Judicial de Adolescentes Infractores (13959)",
    "14201": "Unidad Judicial de Familia, Mujer, Niñez y Adolescencia (14201)",
    "14241": "Tribunal de Garantías Penales (14241)",
    "14254": "Unidad Judicial Multicompetente (14254)",
    "14255": "Unidad Judicial Penal (14255)",
    "14256": "Unidad Judicial Multicompetente (14256)",
    "14302": "Unidad Judicial Multicompetente (14302)",
    "14304": "Unidad Judicial Multicompetente (14304)",
    "14305": "Unidad Judicial Multicompetente (14305)",
    "14307": "Unidad Judicial Civil (14307)",
    "15123": "Unidad Judicial Multicompetente (15123)",
    "15241": "Tribunal de Garantías Penales (15241)",
    "15281": "Unidad Judicial Penal (15281)",
    "15301": "Unidad Judicial Civil (15301)",
    "15951": "Unidad Judicial de Familia, Mujer, Niñez y Adolescencia (15951)",
    "16171": "Tribunal de Garantías Penales (16171)",
    "16201": "Unidad Judicial de Familia, Mujer, Niñez y Adolescencia (16201)",
    "16281": "Unidad Judicial Penal (16281)",
    "16331": "Unidad Judicial Civil (16331)",
    "17159": "Otra judicatura (17159)",
    "17160": "Unidad Judicial Penal (17160)",
    "17201": "Unidad Judicial de Familia, Mujer, Niñez y Adolescencia (17201)",
    "17203": "Unidad Judicial de Familia, Mujer, Niñez y Adolescencia (17203)",
    "17204": "Unidad Judicial de Familia, Mujer, Niñez y Adolescencia (17204)",
    "17205": "Unidad Judicial de Familia, Mujer, Niñez y Adolescencia (17205)",
    "17230": "Unidad Judicial Civil (17230)",
    "17231": "Unidad Judicial Civil (17231)",
    "17232": "Unidad Judicial Multicompetente (17232)",
    "17233": "Unidad Judicial Civil (17233)",
    "17240": "Tribunal de Garantías Penales (17240)",
    "17250": "Tribunal de Garantías Penales (17250)",
    "17267": "Unidad Judicial Multicompetente (17267)",
    "17282": "Unidad Judicial de Flagrancia (17282)",
    "17283": "Unidad Judicial de Flagrancia (17283)",
    "17291": "Unidad Judicial Penal (17291)",
    "17292": "Unidad Judicial Multicompetente (17292)",
    "17293": "Unidad Judicial Penal (17293)",
    "17294": "Unidad Judicial Penal (17294)",
    "17295": "Unidad Judicial Penal (17295)",
    "17296": "Unidad Judicial Penal (17296)",
    "17297": "Unidad Judicial Penal (17297)",
    "17314": "Unidad Judicial Civil (17314)",
    "17315": "Unidad Judicial Multicompetente (17315)",
    "17316": "Unidad Judicial Multicompetente (17316)",
    "17322": "Unidad Judicial Multicompetente (17322)",
    "17371": "Unidad Judicial de Trabajo (17371)",
    "17460": "Unidad Judicial de Flagrancia (17460)",
    "17951": "Unidad Judicial de Familia, Mujer, Niñez y Adolescencia (17951)",
    "17952": "Unidad Judicial de Familia, Mujer, Niñez y Adolescencia (17952)",
    "17953": "Unidad Judicial de Familia, Mujer, Niñez y Adolescencia (17953)",
    "17981": "Unidad Judicial de Familia, Mujer, Niñez y Adolescencia (17981)",
    "17983": "Unidad Judicial de Familia, Mujer, Niñez y Adolescencia (17983)",
    "17985": "Unidad Judicial de Familia, Mujer, Niñez y Adolescencia (17985)",
    "17986": "Unidad Judicial de Familia, Mujer, Niñez y Adolescencia (17986)",
    "18151": "Otra judicatura (18151)",
    "18171": "Tribunal de Garantías Penales (18171)",
    "18202": "Unidad Judicial de Familia, Mujer, Niñez y Adolescencia (18202)",
    "18282": "Unidad Judicial Penal (18282)",
    "18331": "Unidad Judicial Multicompetente (18331)",
    "18332": "Unidad Judicial Multicompetente (18332)",
    "18333": "Unidad Judicial Multicompetente (18333)",
    "18334": "Unidad Judicial Civil (18334)",
    "18335": "Unidad Judicial Multicompetente (18335)",
    "18371": "Unidad Judicial de Trabajo (18371)",
    "18461": "Otra judicatura (18461)",
    "19254": "Unidad Judicial Multicompetente (19254)",
    "19281": "Unidad Judicial Multicompetente (19281)",
    "19303": "Unidad Judicial Multicompetente (19303)",
    "19304": "Unidad Judicial Multicompetente (19304)",
    "19331": "Unidad Judicial Multicompetente (19331)",
    "19332": "Unidad Judicial Multicompetente (19332)",
    "19333": "Unidad Judicial Multicompetente (19333)",
    "19901": "Tribunal de Garantías Penales (19901)",
    "20331": "Unidad Judicial Multicompetente (20331)",
    "20332": "Unidad Judicial Multicompetente (20332)",
    "20333": "Unidad Judicial Multicompetente (20333)",
    "20571": "Unidad Judicial de Violencia contra la Mujer o Miembros del Núcleo Familiar (20571)",
    "21201": "Unidad Judicial de Familia, Mujer, Niñez y Adolescencia (21201)",
    "21241": "Tribunal de Garantías Penales (21241)",
    "21282": "Unidad Judicial Multicompetente (21282)",
    "21331": "Unidad Judicial de Trabajo (21331)",
    "21332": "Unidad Judicial Multicompetente (21332)",
    "21333": "Unidad Judicial Multicompetente (21333)",
    "21334": "Unidad Judicial Multicompetente (21334)",
    "21371": "Unidad Judicial de Trabajo (21371)",
    "22201": "Unidad Judicial de Familia, Mujer, Niñez y Adolescencia (22201)",
    "22241": "Tribunal de Garantías Penales (22241)",
    "22252": "Unidad Judicial Multicompetente (22252)",
    "22281": "Unidad Judicial Multicompetente (22281)",
    "22301": "Unidad Judicial Civil (22301)",
    "22302": "Unidad Judicial Multicompetente (22302)",
    "22303": "Unidad Judicial Multicompetente (22303)",
    "23171": "Tribunal de Garantías Penales (23171)",
    "23201": "Unidad Judicial de Adolescentes Infractores (23201)",
    "23281": "Unidad Judicial Penal (23281)",
    "23303": "Unidad Judicial Multicompetente (23303)",
    "23331": "Unidad Judicial Civil (23331)",
    "24201": "Unidad Judicial de Familia, Mujer, Niñez y Adolescencia (24201)",
    "24202": "Unidad Judicial Multicompetente (24202)",
    "24281": "Unidad Judicial Penal (24281)",
    "24331": "Unidad Judicial Civil (24331)",
}
JUDICATURAS_SLUG: dict = {
    "01201": "familia-01201",
    "01202": "familia-01202",
    "01203": "familia-01203",
    "01204": "familia-01204",
    "01281": "penal-01281",
    "01282": "penal-01282",
    "01283": "penal-01283",
    "01331": "civil-01331",
    "01332": "civil-01332",
    "01333": "civil-01333",
    "01371": "trabajo-01371",
    "01610": "multi-01610",
    "01612": "multi-01612",
    "01613": "multi-01613",
    "01617": "multi-01617",
    "01618": "multi-01618",
    "01619": "multi-01619",
    "01658": "multi-01658",
    "01904": "tgp-01904",
    "02202": "familia-02202",
    "02241": "tgp-02241",
    "02281": "penal-02281",
    "02305": "multi-02305",
    "02308": "multi-02308",
    "02331": "civil-02331",
    "02332": "multi-02332",
    "02333": "multi-02333",
    "02334": "multi-02334",
    "02335": "multi-02335",
    "03201": "familia-03201",
    "03203": "familia-03203",
    "03204": "multi-03204",
    "03257": "multi-03257",
    "03281": "multi-03281",
    "03282": "multi-03282",
    "03283": "penal-03283",
    "03331": "multi-03331",
    "03332": "civil-03332",
    "03333": "civil-03333",
    "03334": "multi-03334",
    "04243": "tgp-04243",
    "04281": "penal-04281",
    "04306": "multi-04306",
    "04332": "multi-04332",
    "04333": "civil-04333",
    "04334": "multi-04334",
    "04335": "multi-04335",
    "04951": "familia-04951",
    "05151": "multi-05151",
    "05202": "familia-05202",
    "05241": "tgp-05241",
    "05254": "multi-05254",
    "05282": "multi-05282",
    "05283": "penal-05283",
    "05304": "multi-05304",
    "05307": "multi-05307",
    "05331": "multi-05331",
    "05332": "multi-05332",
    "05333": "civil-05333",
    "05334": "multi-05334",
    "05335": "multi-05335",
    "05371": "trabajo-05371",
    "06101": "familia-06101",
    "06102": "multi-06102",
    "06171": "tgp-06171",
    "06282": "penal-06282",
    "06308": "multi-06308",
    "06310": "multi-06310",
    "06332": "multi-06332",
    "06333": "multi-06333",
    "06334": "multi-06334",
    "06335": "civil-06335",
    "06336": "multi-06336",
    "06352": "trabajo-06352",
    "07171": "tgp-07171",
    "07201": "familia-07201",
    "07205": "adolescentes-07205",
    "07206": "familia-07206",
    "07257": "multi-07257",
    "07258": "multi-07258",
    "07259": "multi-07259",
    "07281": "multi-07281",
    "07283": "penal-07283",
    "07307": "civil-07307",
    "07308": "multi-07308",
    "07309": "multi-07309",
    "07310": "multi-07310",
    "07312": "multi-07312",
    "07317": "multi-07317",
    "07331": "multi-07331",
    "07332": "multi-07332",
    "07333": "civil-07333",
    "07334": "civil-07334",
    "07335": "multi-07335",
    "07371": "trabajo-07371",
    "07710": "flagrancia-07710",
    "07711": "flagrancia-07711",
    "07712": "flagrancia-07712",
    "08201": "familia-08201",
    "08244": "tgp-08244",
    "08256": "multi-08256",
    "08257": "multi-08257",
    "08281": "multi-08281",
    "08282": "penal-08282",
    "08304": "multi-08304",
    "08308": "multi-08308",
    "08309": "multi-08309",
    "08331": "civil-08331",
    "08332": "multi-08332",
    "08371": "trabajo-08371",
    "09201": "familia-09201",
    "09202": "familia-09202",
    "09204": "multi-09204",
    "09208": "familia-09208",
    "09209": "familia-09209",
    "09210": "familia-09210",
    "09211": "multi-09211",
    "09212": "multi-09212",
    "09266": "multi-09266",
    "09267": "multi-09267",
    "09272": "multi-09272",
    "09281": "flagrancia-09281",
    "09284": "penal-09284",
    "09285": "penal-09285",
    "09286": "penal-09286",
    "09287": "penal-09287",
    "09288": "penal-09288",
    "09290": "multi-09290",
    "09292": "flagrancia-09292",
    "09315": "civil-09315",
    "09318": "multi-09318",
    "09319": "multi-09319",
    "09320": "multi-09320",
    "09322": "multi-09322",
    "09326": "multi-09326",
    "09327": "multi-09327",
    "09330": "civil-09330",
    "09332": "civil-09332",
    "09333": "multi-09333",
    "09334": "multi-09334",
    "09335": "trabajo-09335",
    "09337": "multi-09337",
    "09356": "trabajo-09356",
    "09359": "trabajo-09359",
    "09901": "tgp-09901",
    "09914": "tgp-09914",
    "09915": "tgp-09915",
    "09969": "familia-09969",
    "10201": "adolescentes-10201",
    "10203": "adolescentes-10203",
    "10243": "tgp-10243",
    "10281": "penal-10281",
    "10282": "penal-10282",
    "10309": "multi-10309",
    "10310": "multi-10310",
    "10311": "civil-10311",
    "10333": "multi-10333",
    "10334": "multi-10334",
    "11203": "familia-11203",
    "11258": "multi-11258",
    "11282": "penal-11282",
    "11308": "multi-11308",
    "11310": "multi-11310",
    "11313": "multi-11313",
    "11314": "multi-11314",
    "11317": "multi-11317",
    "11318": "multi-11318",
    "11320": "multi-11320",
    "11331": "multi-11331",
    "11332": "multi-11332",
    "11333": "civil-11333",
    "11335": "multi-11335",
    "11336": "multi-11336",
    "11337": "multi-11337",
    "11371": "trabajo-11371",
    "11904": "tgp-11904",
    "12201": "familia-12201",
    "12203": "familia-12203",
    "12242": "tgp-12242",
    "12244": "tgp-12244",
    "12281": "multi-12281",
    "12282": "penal-12282",
    "12283": "penal-12283",
    "12309": "multi-12309",
    "12310": "multi-12310",
    "12312": "multi-12312",
    "12313": "multi-12313",
    "12331": "trabajo-12331",
    "12332": "multi-12332",
    "12333": "multi-12333",
    "12334": "civil-12334",
    "12335": "multi-12335",
    "12336": "multi-12336",
    "12371": "trabajo-12371",
    "13176": "tgp-13176",
    "13177": "tgp-13177",
    "13203": "otra-13203",
    "13204": "otra-13204",
    "13205": "adolescentes-13205",
    "13244": "tgp-13244",
    "13245": "tgp-13245",
    "13253": "multi-13253",
    "13259": "multi-13259",
    "13266": "multi-13266",
    "13267": "multi-13267",
    "13281": "multi-13281",
    "13282": "penal-13282",
    "13283": "penal-13283",
    "13284": "penal-13284",
    "13313": "multi-13313",
    "13314": "multi-13314",
    "13315": "multi-13315",
    "13317": "adolescentes-13317",
    "13320": "multi-13320",
    "13322": "multi-13322",
    "13331": "multi-13331",
    "13332": "multi-13332",
    "13333": "multi-13333",
    "13334": "civil-13334",
    "13335": "multi-13335",
    "13336": "civil-13336",
    "13337": "civil-13337",
    "13338": "adolescentes-13338",
    "13339": "multi-13339",
    "13340": "multi-13340",
    "13354": "trabajo-13354",
    "13371": "otra-13371",
    "13959": "adolescentes-13959",
    "14201": "familia-14201",
    "14241": "tgp-14241",
    "14254": "multi-14254",
    "14255": "penal-14255",
    "14256": "multi-14256",
    "14302": "multi-14302",
    "14304": "multi-14304",
    "14305": "multi-14305",
    "14307": "civil-14307",
    "15123": "multi-15123",
    "15241": "tgp-15241",
    "15281": "penal-15281",
    "15301": "civil-15301",
    "15951": "familia-15951",
    "16171": "tgp-16171",
    "16201": "familia-16201",
    "16281": "penal-16281",
    "16331": "civil-16331",
    "17159": "otra-17159",
    "17160": "penal-17160",
    "17201": "familia-17201",
    "17203": "familia-17203",
    "17204": "familia-17204",
    "17205": "familia-17205",
    "17230": "civil-17230",
    "17231": "civil-17231",
    "17232": "multi-17232",
    "17233": "civil-17233",
    "17240": "tgp-17240",
    "17250": "tgp-17250",
    "17267": "multi-17267",
    "17282": "flagrancia-17282",
    "17283": "flagrancia-17283",
    "17291": "penal-17291",
    "17292": "multi-17292",
    "17293": "penal-17293",
    "17294": "penal-17294",
    "17295": "penal-17295",
    "17296": "penal-17296",
    "17297": "penal-17297",
    "17314": "civil-17314",
    "17315": "multi-17315",
    "17316": "multi-17316",
    "17322": "multi-17322",
    "17371": "trabajo-17371",
    "17460": "flagrancia-17460",
    "17951": "familia-17951",
    "17952": "familia-17952",
    "17953": "familia-17953",
    "17981": "familia-17981",
    "17983": "familia-17983",
    "17985": "familia-17985",
    "17986": "familia-17986",
    "18151": "otra-18151",
    "18171": "tgp-18171",
    "18202": "familia-18202",
    "18282": "penal-18282",
    "18331": "multi-18331",
    "18332": "multi-18332",
    "18333": "multi-18333",
    "18334": "civil-18334",
    "18335": "multi-18335",
    "18371": "trabajo-18371",
    "18461": "otra-18461",
    "19254": "multi-19254",
    "19281": "multi-19281",
    "19303": "multi-19303",
    "19304": "multi-19304",
    "19331": "multi-19331",
    "19332": "multi-19332",
    "19333": "multi-19333",
    "19901": "tgp-19901",
    "20331": "multi-20331",
    "20332": "multi-20332",
    "20333": "multi-20333",
    "20571": "violencia-20571",
    "21201": "familia-21201",
    "21241": "tgp-21241",
    "21282": "multi-21282",
    "21331": "trabajo-21331",
    "21332": "multi-21332",
    "21333": "multi-21333",
    "21334": "multi-21334",
    "21371": "trabajo-21371",
    "22201": "familia-22201",
    "22241": "tgp-22241",
    "22252": "multi-22252",
    "22281": "multi-22281",
    "22301": "civil-22301",
    "22302": "multi-22302",
    "22303": "multi-22303",
    "23171": "tgp-23171",
    "23201": "adolescentes-23201",
    "23281": "penal-23281",
    "23303": "multi-23303",
    "23331": "civil-23331",
    "24201": "familia-24201",
    "24202": "multi-24202",
    "24281": "penal-24281",
    "24331": "civil-24331",
}

# ─── Vocabulario cerrado de decisiones ───────────────────────────────────────
DECISIONES_PRIMERA_INSTANCIA = [
    "acepta la demanda",
    "acepta parcialmente la demanda",
    "rechaza la demanda",
    "acepta la reconvención",
    "rechaza la reconvención",
    "declara el abandono",
    "declara la nulidad",
    "acepta el desistimiento",
    "aprueba la transacción",
    "aprueba la conciliación",
    "declara con lugar",
    "declara sin lugar",
    "declara parcialmente con lugar",
    "no ha lugar"
]
DECISIONES_ENUM_IA = [
    "Acepta la demanda",
    "Acepta parcialmente la demanda",
    "Rechaza la demanda",
    "No consta"
]
DECISIONES_APELACION = [
    "Confirma la sentencia.",
    "Revoca la sentencia.",
    "Reforma la sentencia.",
    "Declara la nulidad."
]
DECISIONES_CASACION  = [
    "Casa la sentencia.",
    "No casa la sentencia.",
    "Casa parcialmente la sentencia.",
    # Auto de inadmisión del conjuez (art. 270 COGEP). Está en el catálogo
    # decisiones-finales-casacion del CMS; va al final para no mover el mapeo
    # posicional de las tres primeras en strapi_client.
    "Inadmite el recurso de casación."
]

TIPOS_ACCION = [
    "procedimiento ordinario",
    "procedimiento sumario",
    "procedimiento ejecutivo",
    "procedimiento monitorio",
    "procedimiento voluntario",
    "procedimiento de ejecución",
    "procedimiento concursal"
]
TIPOS_ACCION_ENUM_IA = [
    "Procedimiento ordinario",
    "Procedimiento sumario",
    "Procedimiento ejecutivo",
    "Procedimiento monitorio",
    "Procedimiento voluntario",
    "Procedimiento de ejecución",
    "Procedimiento concursal",
    "No consta"
]

# ─── Catálogo único (sin administraciones) ───────────────────────────────────
ADMINISTRACIONES: dict = {}
ADMINISTRACION_POR_DEFECTO = ""

# ─── Códigos cerrados de los prompts internos (7, 8, 10) — de prompts/codigos.json
CODIGOS_TIPOS_DEFECTO = {
    "INCUMPLIMIENTO_CONTRACTUAL",
    "COBRO_OBLIGACION_DINERARIA",
    "TITULO_EJECUTIVO_EXCEPCIONES",
    "NULIDAD_CONTRATO",
    "LESION_ENORME",
    "RESPONSABILIDAD_EXTRACONTRACTUAL",
    "DANO_MORAL",
    "REIVINDICACION",
    "PRESCRIPCION_ADQUISITIVA",
    "ACCIONES_POSESORIAS",
    "PARTICION_DESLINDE_SERVIDUMBRES",
    "ARRENDAMIENTO_INQUILINATO",
    "SUCESIONES",
    "SOCIETARIO",
    "MERCANTIL_SEGUROS_TRANSPORTE",
    "INSOLVENCIA_CONCURSO",
    "TERCERIA_MEDIDAS_CAUTELARES",
    "PRESCRIPCION_EXTINTIVA_CADUCIDAD",
    "COSA_JUZGADA_TRANSACCION",
    "LEGITIMACION_PERSONERIA",
    "COMPETENCIA",
    "NULIDAD_PROCESAL",
    "FALTA_MOTIVACION",
    "ABANDONO",
    "CARGA_PRUEBA",
    "VALORACION_PRUEBA",
    "AUTENTICIDAD_DOCUMENTAL",
    "POSESION_BUENA_MALA_FE",
    "MANDATO_RENDICION_CUENTAS",
    "SENTENCIA_SIN_OPOSICION",
    "SIN_ESPECIFICAR",
}
# Rótulo legible (con tildes) de cada código en la ficha Word; sin entrada → el código "humanizado".
ETIQUETAS_TIPOS_DEFECTO = {
    "INCUMPLIMIENTO_CONTRACTUAL": "Incumplimiento contractual",
    "COBRO_OBLIGACION_DINERARIA": "Cobro de obligación dineraria",
    "TITULO_EJECUTIVO_EXCEPCIONES": "Título ejecutivo y excepciones",
    "NULIDAD_CONTRATO": "Nulidad del contrato",
    "LESION_ENORME": "Lesión enorme",
    "RESPONSABILIDAD_EXTRACONTRACTUAL": "Responsabilidad extracontractual",
    "DANO_MORAL": "Daño moral",
    "REIVINDICACION": "Reivindicación",
    "PRESCRIPCION_ADQUISITIVA": "Prescripción adquisitiva",
    "ACCIONES_POSESORIAS": "Acciones posesorias",
    "PARTICION_DESLINDE_SERVIDUMBRES": "Partición, deslinde y servidumbres",
    "ARRENDAMIENTO_INQUILINATO": "Arrendamiento e inquilinato",
    "SUCESIONES": "Sucesiones",
    "SOCIETARIO": "Societario",
    "MERCANTIL_SEGUROS_TRANSPORTE": "Mercantil, seguros y transporte",
    "INSOLVENCIA_CONCURSO": "Insolvencia y concurso",
    "TERCERIA_MEDIDAS_CAUTELARES": "Tercería y medidas cautelares",
    "PRESCRIPCION_EXTINTIVA_CADUCIDAD": "Prescripción extintiva y caducidad",
    "COSA_JUZGADA_TRANSACCION": "Cosa juzgada y transacción",
    "LEGITIMACION_PERSONERIA": "Legitimación y personería",
    "COMPETENCIA": "Competencia",
    "NULIDAD_PROCESAL": "Nulidad procesal",
    "FALTA_MOTIVACION": "Falta de motivación",
    "ABANDONO": "Abandono",
    "CARGA_PRUEBA": "Carga de la prueba",
    "VALORACION_PRUEBA": "Valoración de la prueba",
    "AUTENTICIDAD_DOCUMENTAL": "Autenticidad de la prueba documental",
    "POSESION_BUENA_MALA_FE": "Posesión de buena o mala fe",
    "MANDATO_RENDICION_CUENTAS": "Mandato y rendición de cuentas",
    "SENTENCIA_SIN_OPOSICION": "Sentencia sin oposición",
    "SIN_ESPECIFICAR": "Sin especificar",
}
CODIGOS_TIPO_PRUEBA = {
    "ESCRITURA_PUBLICA",
    "DOCUMENTO_NOTARIAL",
    "CONTRATO",
    "TITULO_VALOR",
    "CERTIFICADO_REGISTRO_PROPIEDAD",
    "DOCUMENTAL_SOCIETARIA",
    "DOCUMENTAL_MUNICIPAL",
    "DOCUMENTAL_ADMINISTRATIVO",
    "DOCUMENTAL_CONTABLE",
    "CERTIFICACION_BANCARIA",
    "CORRESPONDENCIA",
    "INFORME_PERICIAL_AVALUO",
    "INFORME_PERICIAL_CONTABLE",
    "INFORME_PERICIAL_TECNICO",
    "INSPECCION_JUDICIAL",
    "TESTIMONIAL",
    "DECLARACION_TERCERO",
    "JURISPRUDENCIA_PRECEDENTE",
    "OTRA_DOCUMENTAL",
    "NO_DETERMINABLE",
}
ALIAS_TIPO_PRUEBA = {
    "ESCRITURA": "ESCRITURA_PUBLICA",
    "ESCRITURAS_PUBLICAS": "ESCRITURA_PUBLICA",
    "ESCRITURA_COMPRAVENTA": "ESCRITURA_PUBLICA",
    "ESCRITURA_HIPOTECA": "ESCRITURA_PUBLICA",
    "ESCRITURA_PROMESA": "ESCRITURA_PUBLICA",
    "TESTAMENTO": "ESCRITURA_PUBLICA",
    "INSTRUMENTO_PUBLICO": "ESCRITURA_PUBLICA",
    "ACTA_NOTARIAL": "DOCUMENTO_NOTARIAL",
    "DECLARACION_JURAMENTADA": "DOCUMENTO_NOTARIAL",
    "RECONOCIMIENTO_FIRMA": "DOCUMENTO_NOTARIAL",
    "PODER": "DOCUMENTO_NOTARIAL",
    "PODER_ESPECIAL": "DOCUMENTO_NOTARIAL",
    "PROTOCOLIZACION": "DOCUMENTO_NOTARIAL",
    "POSESION_EFECTIVA": "DOCUMENTO_NOTARIAL",
    "DESAHUCIO_NOTARIAL": "DOCUMENTO_NOTARIAL",
    "NOTARIAL": "DOCUMENTO_NOTARIAL",
    "CONTRATO_PRIVADO": "CONTRATO",
    "DOCUMENTO_PRIVADO": "CONTRATO",
    "CONTRATO_ARRENDAMIENTO": "CONTRATO",
    "CONTRATO_INQUILINATO": "CONTRATO",
    "CONTRATO_PROMESA": "CONTRATO",
    "PROMESA_COMPRAVENTA": "CONTRATO",
    "CONTRATO_COMPRAVENTA": "CONTRATO",
    "CONTRATO_MUTUO": "CONTRATO",
    "CONTRATO_OBRA": "CONTRATO",
    "CONTRATO_SERVICIOS": "CONTRATO",
    "CONTRATO_SEGURO": "CONTRATO",
    "POLIZA": "CONTRATO",
    "POLIZA_SEGURO": "CONTRATO",
    "RECONOCIMIENTO_DEUDA": "CONTRATO",
    "CONVENIO_PAGO": "CONTRATO",
    "TRANSACCION": "CONTRATO",
    "PAGARE": "TITULO_VALOR",
    "PAGARE_A_LA_ORDEN": "TITULO_VALOR",
    "LETRA_DE_CAMBIO": "TITULO_VALOR",
    "LETRA_CAMBIO": "TITULO_VALOR",
    "CHEQUE": "TITULO_VALOR",
    "CHEQUES": "TITULO_VALOR",
    "FACTURA_NEGOCIABLE": "TITULO_VALOR",
    "TITULO_EJECUTIVO": "TITULO_VALOR",
    "TITULOS_VALORES": "TITULO_VALOR",
    "CERTIFICADO_REGISTRO": "CERTIFICADO_REGISTRO_PROPIEDAD",
    "CERTIFICADO_PROPIEDAD": "CERTIFICADO_REGISTRO_PROPIEDAD",
    "CERTIFICADO_GRAVAMENES": "CERTIFICADO_REGISTRO_PROPIEDAD",
    "HISTORIA_DOMINIO": "CERTIFICADO_REGISTRO_PROPIEDAD",
    "REGISTRO_PROPIEDAD": "CERTIFICADO_REGISTRO_PROPIEDAD",
    "REGISTRO_DE_LA_PROPIEDAD": "CERTIFICADO_REGISTRO_PROPIEDAD",
    "REGISTRO_MERCANTIL": "CERTIFICADO_REGISTRO_PROPIEDAD",
    "CERTIFICADO_REGISTRO_MERCANTIL": "CERTIFICADO_REGISTRO_PROPIEDAD",
    "INSCRIPCION_REGISTRAL": "CERTIFICADO_REGISTRO_PROPIEDAD",
    "SUPERINTENDENCIA_COMPANIAS": "DOCUMENTAL_SOCIETARIA",
    "NOMBRAMIENTO": "DOCUMENTAL_SOCIETARIA",
    "NOMBRAMIENTO_REPRESENTANTE_LEGAL": "DOCUMENTAL_SOCIETARIA",
    "ACTA_JUNTA": "DOCUMENTAL_SOCIETARIA",
    "ACTA_JUNTA_GENERAL": "DOCUMENTAL_SOCIETARIA",
    "ESTATUTOS": "DOCUMENTAL_SOCIETARIA",
    "CERTIFICADO_EXISTENCIA_LEGAL": "DOCUMENTAL_SOCIETARIA",
    "CERTIFICADO_CUMPLIMIENTO_OBLIGACIONES": "DOCUMENTAL_SOCIETARIA",
    "SOCIETARIA": "DOCUMENTAL_SOCIETARIA",
    "CATASTRO": "DOCUMENTAL_MUNICIPAL",
    "AVALUO_CATASTRAL": "DOCUMENTAL_MUNICIPAL",
    "CERTIFICADO_AVALUO_CATASTRO": "DOCUMENTAL_MUNICIPAL",
    "CLAVE_CATASTRAL": "DOCUMENTAL_MUNICIPAL",
    "IMPUESTO_PREDIAL": "DOCUMENTAL_MUNICIPAL",
    "LINEA_FABRICA": "DOCUMENTAL_MUNICIPAL",
    "MUNICIPAL": "DOCUMENTAL_MUNICIPAL",
    "MUNICIPIO": "DOCUMENTAL_MUNICIPAL",
    "REGISTRO_CIVIL": "DOCUMENTAL_ADMINISTRATIVO",
    "PARTIDA_NACIMIENTO": "DOCUMENTAL_ADMINISTRATIVO",
    "PARTIDA_DEFUNCION": "DOCUMENTAL_ADMINISTRATIVO",
    "CERTIFICADO_DEFUNCION": "DOCUMENTAL_ADMINISTRATIVO",
    "PARTE_POLICIAL": "DOCUMENTAL_ADMINISTRATIVO",
    "ANT": "DOCUMENTAL_ADMINISTRATIVO",
    "MATRICULA_VEHICULO": "DOCUMENTAL_ADMINISTRATIVO",
    "EXPEDIENTE_FISCALIA": "DOCUMENTAL_ADMINISTRATIVO",
    "ACTO_ADMINISTRATIVO": "DOCUMENTAL_ADMINISTRATIVO",
    "OFICIO": "DOCUMENTAL_ADMINISTRATIVO",
    "FACTURAS": "DOCUMENTAL_CONTABLE",
    "FACTURA": "DOCUMENTAL_CONTABLE",
    "ESTADOS_FINANCIEROS": "DOCUMENTAL_CONTABLE",
    "BALANCES": "DOCUMENTAL_CONTABLE",
    "LIBROS_CONTABLES": "DOCUMENTAL_CONTABLE",
    "COMPROBANTES_PAGO": "DOCUMENTAL_CONTABLE",
    "COMPROBANTE_EGRESO": "DOCUMENTAL_CONTABLE",
    "RECIBOS": "DOCUMENTAL_CONTABLE",
    "TABLA_AMORTIZACION": "DOCUMENTAL_CONTABLE",
    "ESTADO_DE_CUENTA": "CERTIFICACION_BANCARIA",
    "ESTADOS_DE_CUENTA_BANCARIOS": "CERTIFICACION_BANCARIA",
    "TRANSFERENCIA_BANCARIA": "CERTIFICACION_BANCARIA",
    "DEPOSITO_BANCARIO": "CERTIFICACION_BANCARIA",
    "CERTIFICADO_BANCARIO": "CERTIFICACION_BANCARIA",
    "CHEQUE_DEVUELTO": "CERTIFICACION_BANCARIA",
    "BURO_CREDITO": "CERTIFICACION_BANCARIA",
    "CORREO_ELECTRONICO": "CORRESPONDENCIA",
    "CORREOS": "CORRESPONDENCIA",
    "WHATSAPP": "CORRESPONDENCIA",
    "MENSAJES": "CORRESPONDENCIA",
    "CAPTURAS_PANTALLA": "CORRESPONDENCIA",
    "CARTA": "CORRESPONDENCIA",
    "CARTAS": "CORRESPONDENCIA",
    "REQUERIMIENTO": "CORRESPONDENCIA",
    "NOTIFICACION": "CORRESPONDENCIA",
    "PUBLICACION_PRENSA": "CORRESPONDENCIA",
    "REDES_SOCIALES": "CORRESPONDENCIA",
    "AVALUO": "INFORME_PERICIAL_AVALUO",
    "AVALUO_PERICIAL": "INFORME_PERICIAL_AVALUO",
    "PERITAJE_AVALUO": "INFORME_PERICIAL_AVALUO",
    "INFORME_AVALUO": "INFORME_PERICIAL_AVALUO",
    "PERITO_AVALUADOR": "INFORME_PERICIAL_AVALUO",
    "PERICIAL_CONTABLE": "INFORME_PERICIAL_CONTABLE",
    "LIQUIDACION_PERICIAL": "INFORME_PERICIAL_CONTABLE",
    "INFORME_PERICIAL_FINANCIERO": "INFORME_PERICIAL_CONTABLE",
    "PERICIAL_TECNICO": "INFORME_PERICIAL_TECNICO",
    "PERICIAL_TECNICA": "INFORME_PERICIAL_TECNICO",
    "INFORME_PERICIAL": "INFORME_PERICIAL_TECNICO",
    "INFORME_PERICIAL_GRAFOLOGICO": "INFORME_PERICIAL_TECNICO",
    "INFORME_PERICIAL_GRAFOTECNICO": "INFORME_PERICIAL_TECNICO",
    "PERICIA_GRAFOTECNICA": "INFORME_PERICIAL_TECNICO",
    "INFORME_PERICIAL_DOCUMENTOLOGICO": "INFORME_PERICIAL_TECNICO",
    "INFORME_PERICIAL_TOPOGRAFICO": "INFORME_PERICIAL_TECNICO",
    "INFORME_PERICIAL_INGENIERIA": "INFORME_PERICIAL_TECNICO",
    "INFORME_PERICIAL_MEDICO": "INFORME_PERICIAL_TECNICO",
    "INFORME_PERICIAL_PSICOLOGICO": "INFORME_PERICIAL_TECNICO",
    "INFORME_PERICIAL_INFORMATICO": "INFORME_PERICIAL_TECNICO",
    "INFORME_PERICIAL_TRANSITO": "INFORME_PERICIAL_TECNICO",
    "INSPECCION": "INSPECCION_JUDICIAL",
    "INSPECCION_OCULAR": "INSPECCION_JUDICIAL",
    "DECLARACION_PARTE": "TESTIMONIAL",
    "DECLARACION_DE_PARTE": "TESTIMONIAL",
    "CONFESION_JUDICIAL": "TESTIMONIAL",
    "CONFESION": "TESTIMONIAL",
    "JURAMENTO": "TESTIMONIAL",
    "DECLARACION_TESTIGOS": "TESTIMONIAL",
    "TESTIGOS": "TESTIMONIAL",
    "TESTIMONIO": "TESTIMONIAL",
    "CERTIFICACION_TERCERO": "DECLARACION_TERCERO",
    "CERTIFICADO_ADMINISTRADOR": "DECLARACION_TERCERO",
    "JURISPRUDENCIA": "JURISPRUDENCIA_PRECEDENTE",
    "PRECEDENTE": "JURISPRUDENCIA_PRECEDENTE",
    "FOTOGRAFIAS": "OTRA_DOCUMENTAL",
    "FOTOS": "OTRA_DOCUMENTAL",
    "VIDEOS": "OTRA_DOCUMENTAL",
    "PLANOS": "OTRA_DOCUMENTAL",
    "LEVANTAMIENTO_PLANIMETRICO": "OTRA_DOCUMENTAL",
    "SENTENCIA_OTRO_PROCESO": "OTRA_DOCUMENTAL",
    "COPIAS_PROCESO": "OTRA_DOCUMENTAL",
    "CEDULA": "OTRA_DOCUMENTAL",
    "DOCUMENTAL": "OTRA_DOCUMENTAL",
}
EJES_DECISION = {
    "prueba",
    "motivación de la sentencia",
    "prescripción/caducidad",
    "nulidad/procedimiento",
    "requisitos/plazos",
    "fondo civil",
    "competencia",
    "otro",
}
TIPO_PRUEBA_CLAVE = {
    "documental",
    "pericial contable",
    "pericial técnica",
    "inspección",
    "testimonial",
    "ninguna",
    "no_determinable",
}

# ─── Nombres de campo en Strapi para las secciones 9 y 10 del prompt 2 ───────
CAMPO_STRAPI_SEC9  = 'pretensiones'      # 9. Pretensiones de la Parte Actora
CAMPO_STRAPI_SEC10 = 'excepciones'      # 10. Excepciones de la Parte Demandada

# ─── Textos que aparecen en fichas Word, briefs y Excel ──────────────────────
TITULO_CLASIFICACION_DOCX = 'IV. CLASIFICACIÓN JURÍDICO-CIVIL'
FRASE_PARTE_FAVORECIDA    = 'Tasa favorable al actor (demanda aceptada)'
DESCRIPCION_ENTIDADES     = 'personas y empresas demandadas ante los jueces civiles'
DESCRIPCION_CNJ_BRIEF     = 'jueces de la Sala de lo Civil y Mercantil de la CNJ que conocen recursos de casación en materia civil'
TESIS_CONTRAPARTE_DESC    = 'el argumento central de la parte demandada'

# Excel / Drive
CARPETA_DRIVE_RESOLUCION = 'Sentencias Civiles'
CARPETA_DRIVE_APERTURA   = 'Demandas Civiles'


# ─── Controles deterministas de la ficha (resolucion/ai/controles.py) ────────
# Los regex se aplican sobre texto en minúsculas y SIN tildes: escríbelos así.

# Número de cada sección que tocan los controles, según los prompts de la
# materia (2 = tribunal; 5 y 11 = corte). Una clave ausente apaga ese control.
SECCIONES_CONTROLES = {
    "tribunal": {"jueces": 3, "tipo_accion": 6, "cuantia": 7, "pretensiones": 9, "excepciones": 10, "arg_demandado": 15,
                 "decision": 16, "voto": 18, "voto_arg": 19},
    "corte":    {"jueces": 2, "decision_previa": 6, "decision": 11, "voto": 13, "voto_arg": 14},
}
# Primera instancia en unidades judiciales civiles (juez unipersonal): sin
# "Otros Jueces" ni voto salvado.
PRIMERA_INSTANCIA_UNIPERSONAL = True

MAX_PALABRAS_RESUMEN      = 350
MAX_PALABRAS_VOTO_SALVADO = 1200

# Compuertas: (códigos del prompt 7, categorías del catálogo —número = toda la
# categoría; texto = una subcategoría por su nombre exacto—, regex que la
# sentencia debe contener). Si no aparece, se quitan del perfil y del CMS.
# Casos reales: /home/claude/pilotos/v3/civil/reglas_civil.md (patrones 2 y 12).
COMPUERTAS_PALABRA_CLAVE = [
    ({"PRESCRIPCION_EXTINTIVA_CADUCIDAD"},
     {22, "Prescripción de la acción cambiaria y ejecutiva"},          r"prescri|caducid"),
    ({"LESION_ENORME"},
     {"Lesión enorme y rescisión", "Lesión enorme en la compraventa de inmuebles"}, r"lesion enorme"),
    ({"PRESCRIPCION_ADQUISITIVA"}, set(),
     r"prescripcion adquisitiva|adquisitiva (extraordinaria |ordinaria )?de dominio|usucapi"),
    ({"REIVINDICACION"},           set(), r"reivindic"),
    ({"SUCESIONES"},               {15},  r"suces|herenc|hereder|testament|causante|legitimari"),
    ({"DANO_MORAL"},               {10},  r"moral"),
    ({"ARRENDAMIENTO_INQUILINATO"}, {6},  r"arrend|inquilin"),
    ({"INSOLVENCIA_CONCURSO"},     {20},  r"insolven|concurso de acreedores|quiebra"),
    ({"ABANDONO"},                 set(), r"abandon"),
]
# Compuertas inversas: se quitan si la sentencia SÍ contiene la regex (salvo que
# contenga la segunda). SENTENCIA_SIN_OPOSICION no cabe si se aprobó una
# transacción/conciliación o si la demandada contestó (reglas_civil, patrón 10).
COMPUERTAS_EXCLUYENTES = [
    ({"SENTENCIA_SIN_OPOSICION"}, set(),
     r"aprueb\w*[^.]{0,120}(transacci|conciliaci|acuerdo)|(transacci|conciliaci|acuerdo)\w*[^.]{0,120}aprueb", None),
    ({"SENTENCIA_SIN_OPOSICION"}, set(),
     r"contest\w*\s+(a\s+)?la\s+demanda|(propus|propone|dedu[jc]|formul|plante)\w*\s+(las\s+)?(siguientes\s+)?excepciones",
     r"no (ha |habia |hubiere |hubiera )?(dado )?contesta\w*|rebeld|falta de pago y excepciones"),
]

# Posición de la demandada (tribunal): disparadores en la sentencia y texto
# estándar que reemplaza a las viñetas del modelo. Sin cita del art. 157 COGEP:
# en el ejecutivo la falta de excepciones se rige por el art. 352.
RE_NO_CONTESTO = (r"no (ha |habia |hubiere |hubiera )?(dado )?contesta\w* (a )?la demanda"
                  r"|(declar|tuv|tien|encuentr)\w*[^.]{0,40}rebeld"
                  # "no comparece a proceso", "sin que la demandada haya comparecido al proceso",
                  # "el demandado no compareció" (no la audiencia: RE_NO_COMPARECIO_AUDIENCIA).
                  r"|\bno\s+(ha\s+|haber\s+|hubiere\s+|hubiera\s+)?compar(ec|eci)\w*\s+(a\s+|al\s+)?(proceso|juicio|la\s+causa)"
                  r"|sin\s+que\s+[^.]{0,40}haya\s+comparecido\s+(a\s+|al\s+)?(proceso|juicio|la\s+causa)"
                  r"|(demandad|accionad)\w*[^.]{0,80}\bno\s+(ha\s+|hubiere\s+|hubiera\s+)?compar(ec|eci)\w*(?![^.]{0,60}audiencia)"
                  r"|\ben\s+rebeldia\b"
                  r"|a falta de pago y (de )?excepciones")
RE_NO_COMPARECIO_AUDIENCIA = r"\b(no|ni) (ha |hubiere |hubiera )?comparec\w*[^.]{0,60}audiencia"
# RE_NO_CONTESTO solo cuenta si la demandada no contestó: no se aplica si la
# sentencia recoge su contestación o sus excepciones ("la accionada al
# comparecer solicita…", "propone la excepción previa…"), ni si la frase habla
# de un tercero ("la Procuraduría… no ha comparecido a proceso").
RE_CONTESTO_DEMANDADA = (r"(demandad|accionad)\w*[^.;]{0,60}\bal\s+comparecer"
                         r"|(?<!no )(?<!ni )(?<!sin )\bcontest(o|a|aron|an)\s+(a\s+)?la\s+demanda"
                         r"|(?<!no )(?<!ni )(?<!sin )\b(propone|proponen|propuso|propusieron)\s+(la\s+|las\s+|como\s+)?excepci")
RE_SUJETO_TERCERO = r"procuradur|\bpge\b|tercer|coadyuv"
RE_NEGATIVA_PURA = r"negativa pura y simple|negad[oa] pura y simplemente|nieg\w* pura y simplemente"
# Si la sentencia atribuye argumentos propios a la demandada, no se reemplazan.
RE_ATRIBUCION_DEMANDADA = (r"(demandad|accionad)\w*[^.]{0,80}"
                           r"\b(manifest|aleg|sostien|sostuv|argument|afirm|indic|senal|expres|refut|impugn)\w*")
TEXTO_NO_CONTESTO = "La parte demandada no contestó la demanda ni propuso excepciones."
TEXTO_NO_COMPARECIO = "Tampoco compareció a la audiencia."
TEXTO_NEGATIVA_PURA = ("La parte demandada negó pura y simplemente los fundamentos de hecho y de "
                       "derecho de la demanda.")

# Cuantía: fórmulas que el modelo antepone sin que la sentencia las use.
FORMULAS_CUANTIA = [r"(la cuantia )?se fija en", r"fija(da)? (la cuantia )?en", r"asciende a",
                    r"ascendi[oa] a", r"capital adeudado"]
ROTULO_CUANTIA_NEUTRO = "Valor indicado en la sentencia:"
# Cuando la sentencia llama "cuantía" al monto (la fija el actor en la demanda).
ROTULO_CUANTIA_DEMANDA = "Cuantía fijada en la demanda:"

# Señal "revisar": expresiones que el modelo pone sin que la sentencia las use
# (p. ej. "art. 240 de la Constitución" cuando la sentencia cita el COFJ, o una
# explicación inventada de lo que regula una norma).
NOMBRES_VIGILADOS = [r"art(iculo|\.)?\s*240,? de la constitucion",
                     r"que (regulan?|permiten?|establecen?|disponen?) (el|la|los|las)\b",
                     r"se infiere|implicit", r"derechos indisponibles"]
# Rubros cuya negativa en la parte resolutiva no hace parcial la aceptación.
NEGATIVAS_QUE_NO_CUENTAN = r"costas|honorario|tasa|gasto|excepci"

# Señales por sección: (clave de SECCIONES_CONTROLES, regex en esa sección de la
# ficha, regex que la sentencia contiene, regex que NO contiene o None, mensaje).
# Procedimiento mal identificado (reglas_civil, patrón 1): un ordinario
# (audiencia preliminar/de juicio, art. 289) rotulado ejecutivo, y al revés.
_RE_ARTS_EJECUTIVO = r"\bart(iculos?|s?\.?)\s*(34[7-9]|35[0-3])\b"
SENALES_SECCION = [
    ("tipo_accion", r"procedimiento ejecutivo",
     r"audiencia preliminar|audiencia de juicio|\bart(iculo|\.)?\s*289\b",
     r"\bart(iculos?|s?\.?)\s*347\b",
     "Revisar: el tipo de acción dice 'procedimiento ejecutivo' y la sentencia sigue el ordinario "
     "(audiencia preliminar/de juicio o art. 289) sin citar el art. 347"),
    ("tipo_accion", r"ordinari", _RE_ARTS_EJECUTIVO, None,
     "Revisar: el tipo de acción dice 'ordinario' y la sentencia aplica los arts. 347–353 (ejecutivo)"),
]


# ─── Helpers genéricos (no editar al derivar) ────────────────────────────────

def _norm(s: str) -> str:
    return (s or "").upper()\
        .replace("Á", "A").replace("É", "E").replace("Í", "I")\
        .replace("Ó", "O").replace("Ú", "U").replace("Ñ", "N")


def _matchea(nombre_judicatura: str, claves: list) -> bool:
    n = _norm(nombre_judicatura)
    return any(all(k in n for k in grupo) for grupo in claves)


def es_primera_instancia(nombre_judicatura: str) -> bool:
    """True si la judicatura es un órgano de primera instancia de la materia."""
    return _matchea(nombre_judicatura, PRIMERA_INSTANCIA_CLAVES)


def es_apelacion(nombre_judicatura: str) -> bool:
    """True si la judicatura es la Sala de Corte Provincial de la materia."""
    return bool(APELACION_CLAVES) and _matchea(nombre_judicatura, APELACION_CLAVES)


def tiene_apelacion() -> bool:
    return bool(APELACION_CLAVES)


def es_de_la_materia(causa: dict, incidentes: list | None = None) -> bool:
    """
    Decide si una causa de SATJE pertenece a la materia.
    - Si IDS_MATERIA_SATJE está configurado, manda el idMateria.
    - Si no (materia aún sin ids verificados), se acepta cuando alguno de los
      incidentes tiene una judicatura de primera instancia de la materia.
    """
    if IDS_MATERIA_SATJE:
        return causa.get("idMateria") in IDS_MATERIA_SATJE
    for inc in incidentes or []:
        if es_primera_instancia(inc.get("nombreJudicatura", "")):
            return True
    return False
