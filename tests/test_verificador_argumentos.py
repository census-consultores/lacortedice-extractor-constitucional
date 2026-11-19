from resolucion.ai import verificador_argumentos as V

EXT = ("8. Causales o agravios invocados\n• Falta de motivación\n\n"
       "9. Argumentos del Recurrente\n• Alega que no se valoró la prueba.\n• El Tribunal considera que la letra es exigible.\n\n"
       "10. Argumentos de la Contraparte\n• Los testigos declararon que vivía allí.\n\n"
       "11. Decisión Final\nConfirma la sentencia.\n")


def test_vinetas_y_prompt():
    v = V.vinetas(EXT)
    assert v == {9: ["• Alega que no se valoró la prueba.", "• El Tribunal considera que la letra es exigible."],
                 10: ["• Los testigos declararon que vivía allí."]}
    assert "[9.2] (Argumentos del Recurrente) El Tribunal considera" in V.prompt(v)


def test_aplicar_quita_lo_del_tribunal_y_deja_no_consta():
    v = V.vinetas(EXT)
    ver = V.leer_veredictos('ok [{"id":"9.1","veredicto":"PARTE"},{"id":"9.2","veredicto":"TRIBUNAL"},'
                            '{"id":"10.1","veredicto":"OTRO"}]')
    ext, q = V.aplicar(EXT, v, ver)
    assert "Alega que no se valoró" in ext
    assert "El Tribunal considera" not in ext and "testigos" not in ext
    assert "10. Argumentos de la Contraparte\n\nNo consta en la sentencia." in ext
    assert "11. Decisión Final\nConfirma la sentencia." in ext
    assert len(q) == 2


def test_salida_ilegible_no_cambia_nada():
    v = V.vinetas(EXT)
    ext, q = V.aplicar(EXT, v, V.leer_veredictos("no es json"))
    assert ext == EXT and q == []


def test_respuesta_cortada_o_con_texto_igual_se_lee():
    sal = 'Aquí va:\n```json\n[{"id": "9.1", "veredicto": "PARTE", "cita": "x"}, {"id": "9.2", "veredicto": "TRIBUNAL"}, {"id": "10.1", "verec'
    assert {k: v[0] for k, v in V.leer_veredictos(sal).items()} == {"9.1": "PARTE", "9.2": "TRIBUNAL"}


# Puerta civil v6: PARTE exige una cita literal ubicada en el bloque del recurso.
SENT = ("TERCERO: DE LA DEMANDA. Los actores señalan que compraron una vivienda con vicios ocultos "
        "en las paredes y la piscina de la casa. " + "Relleno sin relevancia alguna. " * 120 +
        "OCTAVO: FUNDAMENTACION DEL RECURSO. La parte actora fundamenta el recurso de apelacion en que "
        "la resolucion dictada no se encuentra debidamente motivada conforme la Constitucion.")


def test_cita_del_recurso_es_valida():
    assert V.cita_valida("la resolución dictada no se encuentra debidamente motivada", V._plano(SENT))


def test_cita_de_la_demanda_o_inventada_no_vale():
    plana = V._plano(SENT)
    assert not V.cita_valida("compraron una vivienda con vicios ocultos en las paredes", plana)
    assert not V.cita_valida("el inmueble fue entregado sin licencia municipal vigente", plana)


def test_parte_sin_cita_valida_sale():
    ext = ("9. Argumentos del Recurrente\n• La sentencia no está motivada.\n• Hay vicios ocultos en la piscina.\n"
           "10. Argumentos de la Contraparte\nNo consta.\n11. Decisión Final\nConfirma la sentencia.\n")
    v = V.vinetas(ext)
    ver = V.leer_veredictos('[{"id":"9.1","veredicto":"PARTE","cita":"la resolucion dictada no se encuentra debidamente motivada"},'
                           '{"id":"9.2","veredicto":"PARTE","cita":"compraron una vivienda con vicios ocultos en las paredes"}]')
    nuevo, q = V.aplicar(ext, v, ver, SENT)
    assert "no está motivada" in nuevo and "piscina" not in nuevo
    assert q[0][1] == "SIN_CITA"
