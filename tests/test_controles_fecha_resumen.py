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


def test_sin_linea_cortada():
    from resolucion.ai._cliente import sin_linea_cortada
    assert sin_linea_cortada("• Primera completa.\n• La antigua máxima Pas de nullité sans grief recuerda que las nulidades no tienen por finalidad satisfacer pr") == "• Primera completa."
    assert sin_linea_cortada("• Completa.\n• Otra completa.") == "• Completa.\n• Otra completa."


def test_voto_no_se_corta_por_firmado_en_minusculas():
    from resolucion.ai import controles as C
    voto = ("VOTO SALVADO DE: JUEZ X\nAntecedentes: el contrato fue firmado por las partes contratantes. " +
            "Relleno. " * 50 + "DECISIÓN: se confirma la sentencia.\nFirmado por JUEZ X")
    assert C.extracto_voto(voto, 2000).endswith("se confirma la sentencia.")


def test_sin_repetidas_quita_bucle():
    from resolucion.ai._cliente import sin_repetidas
    r = "• Resolución 20-2004 de la Corte Nacional de Justicia, Registro Oficial 413."
    t = "\n".join([r, "• Otra cita distinta y suficientemente larga para contar.", r, r])
    assert sin_repetidas(t).count("20-2004") == 1


def test_monto_ocr_con_espacio_antes_de_la_o():
    from resolucion.ai import fidelidad as f
    assert f._digitos("36,382.1 O") == f._digitos("36.382,10")
