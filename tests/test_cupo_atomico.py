"""El tope de jobs a la vez no se rompe cuando varios lanzan al mismo tiempo
(26-oct: 72 jobs corriendo con tope 10 tras un reinicio con 1.117 pendientes)."""
import threading
import time
from worker import job_manager as jm


def test_muchos_lanzando_a_la_vez_respetan_el_tope(monkeypatch):
    jm._active.clear()
    monkeypatch.setitem(jm._MAX_CAT, "extraccion", 10)
    monkeypatch.setattr(jm, "_actualizar", lambda *a, **k: time.sleep(0.02))   # la escritura lenta del hueco
    import worker.runner as runner
    fin = threading.Event()
    monkeypatch.setattr(runner, "ejecutar_job", lambda j, p, k: fin.wait(5))
    monkeypatch.setattr(jm, "_iniciar_siguiente_pendiente", lambda: None)
    hilos = [threading.Thread(target=jm._lanzar_hilo, args=(i,)) for i in range(60)]
    [h.start() for h in hilos]; [h.join() for h in hilos]
    try:
        assert jm._corriendo_por_categoria("extraccion") == 10
    finally:
        fin.set(); time.sleep(0.1); jm._active.clear()
