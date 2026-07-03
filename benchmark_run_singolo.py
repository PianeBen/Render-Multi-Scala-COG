"""
benchmark_run_singolo.py  (v2 — banda via netstat -e thread)
-------------------------------------------------------------
Script companion per benchmark_render_qgis.py.
Esegue UN SOLO caricamento + rendering di un progetto QGIS e misura
in parallelo la banda di rete consumata usando `netstat -e` campionato
in un thread Python — nessun privilegio admin richiesto.

Stampa su stdout una singola riga JSON con tutti i risultati.
NON lanciare direttamente — chiamato da benchmark_render_qgis.py.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time


# ---------------------------------------------------------------------------
# CAMPIONATORE BANDA — thread Python, solo netstat -e (no admin)
# ---------------------------------------------------------------------------

class BandaCampionatore(threading.Thread):
    """
    Campiona `netstat -e` ogni <intervallo_ms> millisecondi in un thread
    daemon. Calcola byte ricevuti/inviati delta tra ogni campione.

    netstat -e su Windows stampa:
        Interface Statistics
                           Received       Sent
        Bytes              1234567890     9876543210
        ...
    Leggiamo solo la riga "Bytes" — nessun privilegio richiesto.
    """

    PATTERN_BYTES = re.compile(
        r"Bytes?\s+([\d]+)\s+([\d]+)", re.IGNORECASE  # EN: Bytes / IT: Byte
    )

    def __init__(self, intervallo_ms: int = 500):
        super().__init__(daemon=True)
        self.intervallo_s  = intervallo_ms / 1000.0
        self.campioni: list[dict] = []
        self._stop_event   = threading.Event()
        self._errore: str  = ""

    # --- API pubblica ---

    def stop(self):
        self._stop_event.set()

    @property
    def errore(self) -> str:
        return self._errore

    # --- Lettura netstat ---

    def _leggi_bytes(self) -> tuple[int, int] | None:
        """Ritorna (bytes_ricevuti, bytes_inviati) o None in caso di errore."""
        try:
            out = subprocess.check_output(
                ["netstat", "-e"],
                stderr=subprocess.DEVNULL,
                timeout=3,
                text=True,
                creationflags=subprocess.CREATE_NO_WINDOW  # no finestra cmd visibile
            )
        except Exception as e:
            self._errore = str(e)
            return None

        m = self.PATTERN_BYTES.search(out)
        if not m:
            # Mostra le prime righe utili per diagnosticare la lingua del SO
            righe_utili = [r.strip() for r in out.splitlines() if r.strip()][:6]
            self._errore = (
                "Pattern 'Bytes/Byte' non trovato in netstat -e. "
                f"Prime righe output: {righe_utili}"
            )
            return None

        return int(m.group(1)), int(m.group(2))

    # --- Thread principale ---

    def run(self):
        prev = self._leggi_bytes()
        if prev is None:
            return
        prev_rx, prev_tx = prev
        prev_t = time.perf_counter()

        while not self._stop_event.is_set():
            time.sleep(self.intervallo_s)
            curr = self._leggi_bytes()
            if curr is None:
                break
            curr_rx, curr_tx = curr
            curr_t = time.perf_counter()

            dt = curr_t - prev_t
            if dt <= 0:
                dt = self.intervallo_s

            delta_rx = max(0, curr_rx - prev_rx)   # max(0) evita wrap-around
            delta_tx = max(0, curr_tx - prev_tx)

            self.campioni.append({
                "t_s":     round(curr_t, 3),
                "rx_MB":   round(delta_rx / 1_048_576, 4),
                "tx_MB":   round(delta_tx / 1_048_576, 4),
                "rx_MBs":  round(delta_rx / 1_048_576 / dt, 4),
                "tx_MBs":  round(delta_tx / 1_048_576 / dt, 4),
            })

            prev_rx, prev_tx = curr_rx, curr_tx
            prev_t = curr_t

    def statistiche(self) -> dict:
        """Aggregati totali e medi calcolati sui campioni."""
        if not self.campioni:
            return {
                "n_campioni":     0,
                "rx_totale_MB":   0.0,
                "tx_totale_MB":   0.0,
                "rx_media_MBs":   0.0,
                "tx_media_MBs":   0.0,
                "rx_picco_MBs":   0.0,
                "tx_picco_MBs":   0.0,
            }
        rx_vals  = [c["rx_MBs"] for c in self.campioni]
        tx_vals  = [c["tx_MBs"] for c in self.campioni]
        return {
            "n_campioni":   len(self.campioni),
            "rx_totale_MB": round(sum(c["rx_MB"] for c in self.campioni), 3),
            "tx_totale_MB": round(sum(c["tx_MB"] for c in self.campioni), 3),
            "rx_media_MBs": round(sum(rx_vals) / len(rx_vals), 4),
            "tx_media_MBs": round(sum(tx_vals) / len(tx_vals), 4),
            "rx_picco_MBs": round(max(rx_vals), 4),
            "tx_picco_MBs": round(max(tx_vals), 4),
        }


# ---------------------------------------------------------------------------
# BOOTSTRAP QGIS
# ---------------------------------------------------------------------------

def bootstrap_qgis():
    osgeo4w = os.environ.get("OSGEO4W_ROOT", r"C:\Program Files\QGIS 3.40.15")
    qgis_python = os.path.join(osgeo4w, "apps", "qgis", "python")
    for p in [qgis_python, os.path.join(qgis_python, "plugins")]:
        if p not in sys.path and os.path.isdir(p):
            sys.path.insert(0, p)
    os.environ.setdefault("QGIS_PREFIX_PATH",
                          os.path.join(osgeo4w, "apps", "qgis"))

    from qgis.core import QgsApplication
    app = QgsApplication([], False)
    app.initQgis()
    return app


# ---------------------------------------------------------------------------
# CARICAMENTO + RENDERING con misura banda in parallelo
# ---------------------------------------------------------------------------

def esegui_render(project_path: str, width: int, height: int,
                  intervallo_banda_ms: int) -> dict:
    from qgis.core import (
        QgsProject, QgsMapSettings,
        QgsMapRendererParallelJob, QgsRectangle
    )
    from PyQt5.QtCore import QSize, QEventLoop

    project = QgsProject.instance()
    project.clear()

    # ---- FASE 1: caricamento progetto ----
    banda_load = BandaCampionatore(intervallo_banda_ms)
    banda_load.start()

    t_load_start = time.perf_counter()
    ok = project.read(project_path)
    t_load_end   = time.perf_counter()

    banda_load.stop()
    banda_load.join(timeout=3)

    if not ok:
        return {"esito": "ERRORE", "msg": f"Impossibile aprire: {project_path}"}

    t_load    = round(t_load_end - t_load_start, 3)
    stat_load = banda_load.statistiche()

    # ---- Configura MapSettings ----
    ms   = QgsMapSettings()
    root = project.layerTreeRoot()
    layer_ids = [n.layerId() for n in root.findLayers() if n.isVisible()]
    layers    = [project.mapLayer(lid) for lid in layer_ids
                 if project.mapLayer(lid)]

    ms.setLayers(layers)
    ms.setOutputSize(QSize(width, height))
    ms.setDestinationCrs(project.crs())
    ms.setOutputDpi(96)

    view_settings = project.viewSettings()
    extent = view_settings.defaultViewExtent()
    if extent.isNull() or extent.isEmpty():
        extent = QgsRectangle()
        for lyr in layers:
            extent.combineExtentWith(lyr.extent())
    ms.setExtent(extent)

    # ---- FASE 2: rendering ----
    banda_render = BandaCampionatore(intervallo_banda_ms)
    banda_render.start()

    loop = QEventLoop()
    job  = QgsMapRendererParallelJob(ms)

    t_render_start = time.perf_counter()
    job.start()
    job.finished.connect(loop.quit)
    loop.exec_()
    t_render_end = time.perf_counter()

    banda_render.stop()
    banda_render.join(timeout=3)

    t_render     = round(t_render_end - t_render_start, 3)
    stat_render  = banda_render.statistiche()
    errors       = job.errors()

    # ---- Avvisi campionatore ----
    avvisi = []
    for nome, campionatore in [("load", banda_load), ("render", banda_render)]:
        if campionatore.errore:
            avvisi.append(f"{nome}: {campionatore.errore}")

    return {
        "esito":        "OK",
        # tempi
        "t_load_s":     t_load,
        "t_render_s":   t_render,
        # layer info
        "n_layers":     len(layers),
        "crs":          project.crs().authid(),
        "extent":       (f"{extent.xMinimum():.1f},{extent.yMinimum():.1f},"
                         f"{extent.xMaximum():.1f},{extent.yMaximum():.1f}"),
        # banda fase caricamento
        "load_rx_MB":       stat_load["rx_totale_MB"],
        "load_rx_media_MBs":stat_load["rx_media_MBs"],
        "load_rx_picco_MBs":stat_load["rx_picco_MBs"],
        # banda fase rendering
        "render_rx_MB":       stat_render["rx_totale_MB"],
        "render_rx_media_MBs":stat_render["rx_media_MBs"],
        "render_rx_picco_MBs":stat_render["rx_picco_MBs"],
        # diagnostica
        "n_campioni_load":   stat_load["n_campioni"],
        "n_campioni_render": stat_render["n_campioni"],
        "avvisi":            avvisi,
        "render_errors":     errors[:3] if errors else [],
        # serie temporale banda (opzionale, per grafici)
        "serie_load":   banda_load.campioni,
        "serie_render": banda_render.campioni,
    }


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--progetto",          required=True)
    parser.add_argument("--width",             type=int, default=1920)
    parser.add_argument("--height",            type=int, default=1080)
    parser.add_argument("--intervallo-banda",  type=int, default=500,
                        help="Intervallo campionamento banda in ms (default 500)")
    parser.add_argument("--no-serie",          action="store_true",
                        help="Ometti serie temporale dal JSON (output più compatto)")
    args = parser.parse_args()

    app = bootstrap_qgis()

    try:
        result = esegui_render(
            args.progetto, args.width, args.height,
            args.intervallo_banda
        )
    except Exception as e:
        result = {"esito": "ERRORE", "msg": str(e)}

    # Rimuovi serie temporale se non richiesta (alleggerisce stdout)
    if args.no_serie or result.get("esito") != "OK":
        result.pop("serie_load",   None)
        result.pop("serie_render", None)

    # Una sola riga JSON su stdout — il processo padre la legge
    print(json.dumps(result), flush=True)

    app.exitQgis()
    sys.exit(0)


if __name__ == "__main__":
    main()
