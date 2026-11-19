"""
Formato del número de causa en SATJE según el año y el juzgado.

Desde 2016 (y en las unidades judiciales creadas en 2014–2015) el secuencial
tiene 5 dígitos: 09332-2024-00010. En el sistema anterior, los juzgados
antiguos usan 4: 09111-2008-0139 existe y 09111-2008-00139 no (prueba del
19-nov-2026). Qué pares (prefijo, año) usan 4 dígitos lo descubre
scripts/mapeo_antiguo.py y queda en worker_mapeo_antiguo_par.digitos.
"""
import threading

_lock = threading.Lock()
_cuatro: set | None = None
ANIO_NUEVO_SISTEMA = 2016


def _cargar() -> set:
    from worker import job_manager as jm
    try:
        with jm._conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""SELECT prefijo, anio FROM worker_mapeo_antiguo_par
                                WHERE digitos = 4""")
                return {(p, str(a)) for p, a in cur.fetchall()}
    except Exception:
        return set()          # tabla aún no creada: todo con 5 dígitos


def digitos(prefijo: str, anio) -> int:
    if int(anio) >= ANIO_NUEVO_SISTEMA:
        return 5
    global _cuatro
    with _lock:
        if _cuatro is None:
            _cuatro = _cargar()
        return 4 if (str(prefijo), str(anio)) in _cuatro else 5


def recargar() -> None:
    global _cuatro
    with _lock:
        _cuatro = None


def numero(prefijo: str, anio, n: int, digs: int | None = None) -> str:
    d = digs or digitos(prefijo, anio)
    return f"{prefijo}-{anio}-{n:0{d}d}"
