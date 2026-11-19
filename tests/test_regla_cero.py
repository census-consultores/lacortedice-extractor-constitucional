import pytest
from resolucion.ai import regla_cero as R


def test_ficha_con_error_api_no_se_guarda():
    with pytest.raises(R.FichaConErrorAPI):
        R.exigir_sin_error_api({"resumen": "[Error Qwen: Error code: 402]"})
    R.exigir_sin_error_api({"resumen": "ok", "extraccion": "1. x"})


def test_montos_sin_respaldo_se_quitan():
    r = {"resumen": "Se condenó al pago. La liquidación suma $2.276,89 por desahucio.",
         "extraccion": "8. Detalle:\n• Desahucio: $410,52\n• Saldo calculado: $2.276,89\n• Pensión: $15.000\n9. Otro:\nx"}
    a, s = [], []
    R.montos_sin_respaldo(r, "se fija en USD 410,52 y quince mil dolares", a, s)
    assert "2.276,89" not in r["resumen"] and "Se condenó al pago." in r["resumen"]
    assert "410,52" in r["extraccion"] and "2.276,89" not in r["extraccion"] and "15.000" in r["extraccion"]
    assert a and s


def test_sin_texto_no_se_quita_nada():
    r = {"resumen": "Pago de $2.276,89."}
    R.montos_sin_respaldo(r, "", [], [])
    assert "2.276,89" in r["resumen"]


def test_pedidos_tipicos_no_pedidos_se_quitan():
    ext = ("9. Pretensiones de la Parte Actora:\n• Pago del saldo\n• Condenar al pago de costas y honorarios\n"
           "• Intereses de mora sobre el capital\n10. Excepciones:\nx")
    sent = "el actor demanda el pago del saldo e intereses de mora sobre el capital. sin costas ni honorarios que regular"
    out = R.pedidos_tipicos_no_pedidos(ext, 9, sent, [], [])
    assert "costas" not in out.split("10.")[0] and "Intereses de mora" in out and "saldo" in out
