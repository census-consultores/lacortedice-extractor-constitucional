"""
Configuración compartida de pytest: agrega la raíz del proyecto al path
para que los tests puedan hacer `import resolucion.x` / `import worker.x`
igual que el código de producción.
"""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


import pytest as _pytest

# Los tests de controles anteriores a la regla de fidelidad usan sentencias
# sintéticas que no contienen los montos ni las citas de sus fichas de ejemplo;
# la fidelidad tiene sus propios tests (test_fidelidad.py).
_SIN_FIDELIDAD = ("test_controles", "test_regresiones_v4", "test_regresiones_v5", "test_regresiones_v6")


@_pytest.fixture(autouse=True)
def _fidelidad_en_tests_antiguos(request, monkeypatch):
    if request.module.__name__.rsplit(".", 1)[-1].startswith(_SIN_FIDELIDAD):
        from resolucion.ai import fidelidad
        monkeypatch.setattr(fidelidad, "ACTIVA", False)
