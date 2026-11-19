from resolucion.ai import controles as C


def test_hora_se_quita_solo_en_lineas_de_fecha():
    assert C.sin_hora("6 de diciembre de 2024, a las 15h30") == "6 de diciembre de 2024"
    assert C.sin_hora("Fecha de la sentencia recurrida: 24 de julio de 2024, a las 16h57") == \
        "Fecha de la sentencia recurrida: 24 de julio de 2024"
    cita = "• El testigo dijo que el 3 de mayo de 2020 a las 15h00 vio el choque"
    assert C.sin_hora(cita) == cita


def test_resumen_largo_se_recorta_lo_posible():
    largo = " ".join(f"Oración intermedia número {i} con varias palabras de relleno." for i in range(80))
    texto = "Primer párrafo del caso entre Juan y Pedro.\n\n" + largo + "\n\nLa Sala confirma la sentencia."
    out = C.recortar_palabras(texto, 350)
    assert C._contar(out) <= 350 and out.endswith("confirma la sentencia.")
