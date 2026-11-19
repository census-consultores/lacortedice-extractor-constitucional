"""Errores que encontró la puerta de validación en el piloto civil v2."""
from resolucion.ai._cliente import _decision_perfil
from resolucion.ai import controles


def test_demanda_rechazada_con_reconvencion_aceptada_18331_2020_00450():
    t = ("Acepta parcialmente la demanda.\nEl suscrito Juez rechaza por improcedente la demanda de "
         "prescripción extraordinaria adquisitiva de dominio. Acepta parcialmente la reconvención "
         "propuesta por el señor Héctor Rafael Naranjo Pérez.")
    assert _decision_perfil(t, primera_instancia=True) == "Rechaza la demanda."


def test_acepta_la_demanda_y_desecha_reconvencion_17230_2022_07361():
    t = "Se ACEPTA la demanda de prescripción adquisitiva. Se desecha la reconvención."
    assert _decision_perfil(t, primera_instancia=True) == "Acepta la demanda."


def test_frase_canonica_correcta_no_se_toca():
    assert _decision_perfil("Rechaza la demanda.\nSe rechaza la demanda por falta de prueba.",
                            primera_instancia=True) == "Rechaza la demanda."


def test_reconvencion_inexistente_se_quita_18334_2024_01897():
    ext = ("6. Tipo de Acción:\nprocedimiento ordinario — servidumbre de paso, con reconvención por daño moral\n\n"
           "15. Argumentos de la Parte Demandada:\n• Niega los fundamentos.\n• Reconvención:\n"
           "• Solicita el rechazo de la demanda por improcedente\n\n16. Decisión Final:\nRechaza la demanda.\n")
    r = {"extraccion": ext, "resumen": "El juez rechazó la demanda. La reconvención fue negada."}
    sentencia = "Se RECHAZA la demanda planteada por la señora Rita del Carmen. Sin costas."
    controles.aplicar(r, sentencia, nivel="tribunal")
    assert "reconven" not in r["extraccion"].lower()
    assert "Niega los fundamentos" in r["extraccion"]
    assert "16. Decisión Final" in r["extraccion"]
    assert "reconven" not in r["resumen"].lower()


def test_reconvencion_real_se_conserva():
    ext = "6. Tipo de Acción:\nprocedimiento ordinario — reivindicación, con reconvención por prescripción\n"
    r = {"extraccion": ext, "resumen": ""}
    controles.aplicar(r, "El demandado propuso reconvención por prescripción adquisitiva.", nivel="tribunal")
    assert "con reconvención por prescripción" in r["extraccion"]


def test_voto_salvado_sin_encabezado_en_apelacion_12334_2022_00400():
    ext = ("11. Decisión:\nConfirma la sentencia.\n\n13. Voto Salvado:\nDr. Pérez\n\n"
           "14. Argumento del Voto Salvado:\nLa actora sostuvo que la factura es exigible.\n")
    r = {"extraccion": ext, "resumen": ""}
    controles.aplicar(r, "La Sala confirma la sentencia venida en grado. Notifíquese.", nivel="corte")
    assert "factura" not in r["extraccion"]
    assert r["extraccion"].count("No hubo voto salvado.") == 2


def test_desechandose_excepciones_se_acepta_la_demanda_17233_2018_06346():
    t = ("Acepta la demanda.\nADMINISTRANDO JUSTICIA, desechándose las excepciones planteadas por la "
         "parte demandada, se acepta la demanda y en consecuencia se declara la nulidad de la sentencia.")
    assert _decision_perfil(t, primera_instancia=True) == "Acepta la demanda."


def test_transcripcion_con_voto_salvado_no_cambia_la_decision_09332_2019_03351():
    t = ("Declaro CON LUGAR la demanda.\n"
         "Declaro sin lugar la demanda presentada por JOSÉ EDUARDO IBAÑEZ SANTOS.")
    assert _decision_perfil(t, primera_instancia=True) == "Acepta la demanda."


def test_sin_lugar_la_accion_01613_2020_00108():
    t = "Declara sin lugar la acción de vicios redhibitorios por rebaja de precio.\nRechaza la reconvención."
    assert _decision_perfil(t, primera_instancia=True) == "Rechaza la demanda."


def test_parcialmente_con_lugar_12334_2022_00400():
    t = "ADMINISTRANDO JUSTICIA, esta autoridad declara parcialmente con lugar a la demanda, disponiendo el pago."
    assert _decision_perfil(t, primera_instancia=True) == "Acepta parcialmente la demanda."


def test_apelacion_que_invierte_es_revoca_01613_2020_00108():
    ext = ("6. Decisión de primera instancia\nDeclara sin lugar la acción de vicios redhibitorios.\n\n"
           "11. Decisión Final\nReforma la sentencia.\n\n13. Voto Salvado\nNo hubo voto salvado.\n")
    r = {"extraccion": ext, "resumen": ""}
    sent = ("ADMINISTRANDO JUSTICIA, EN NOMBRE DEL PUEBLO, ACEPTA el recurso de apelación y declara con lugar "
            "la demanda ordinaria de vicios redhibitorios. No hubo voto salvado.")
    controles.aplicar(r, sent, nivel="apelacion")
    assert "11. Decisión Final\nRevoca la sentencia." in r["extraccion"]


def test_apelacion_que_confirma_no_se_toca():
    ext = ("6. Decisión de primera instancia\nRechaza la demanda.\n\n11. Decisión Final\nConfirma la sentencia.\n")
    r = {"extraccion": ext, "resumen": ""}
    controles.aplicar(r, "ADMINISTRANDO JUSTICIA, rechaza el recurso y confirma la sentencia que desechó la demanda.",
                      nivel="apelacion")
    assert "Confirma la sentencia." in r["extraccion"]


def test_revoca_conserva_la_transcripcion_12334_2023_00950():
    ext = ("6. Decisión de primera instancia\nEl juez declaró con lugar la acción ejecutiva.\n\n"
           "11. Decisión Final\nresuelve: ACEPTAR el recurso, REVOCA la sentencia y declara sin lugar la demanda.\n")
    r = {"extraccion": ext, "resumen": ""}
    controles.aplicar(r, "ADMINISTRANDO JUSTICIA, resuelve: ACEPTAR el recurso, REVOCA la sentencia que vino "
                         "en grado y declara sin lugar la demanda.", nivel="apelacion")
    assert "declara sin lugar la demanda.\nRevoca la sentencia." in r["extraccion"]


def test_parcial_del_modelo_contra_con_lugar_total_09332_2019_03351():
    t = ("Acepta parcialmente la demanda.\nDeclaro CON LUGAR la demanda.\n"
         "Declaro sin lugar la demanda presentada por VALPACIFICO S.A. (reconvención).")
    assert _decision_perfil(t, primera_instancia=True) == "Acepta la demanda."


def test_parcialmente_con_lugar_se_conserva():
    t = "Acepta parcialmente la demanda.\nSe declara parcialmente con lugar la demanda."
    assert _decision_perfil(t, primera_instancia=True) == "Acepta parcialmente la demanda."


def test_revoca_sin_administrando_justicia_en_la_sentencia():
    ext = ("6. Decisión de primera instancia\nEl juez declaró sin lugar la demanda de vicios redhibitorios.\n\n"
           "11. Decisión Final\nReforma la sentencia.\nACEPTA el recurso de apelación y declara con lugar la demanda.\n")
    r = {"extraccion": ext, "resumen": ""}
    controles.aplicar(r, "Texto recortado de la sentencia sin la fórmula.", nivel="apelacion")
    assert r["extraccion"].rstrip().endswith("Revoca la sentencia.")
