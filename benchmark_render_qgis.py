r"""
benchmark_render_qgis.py  (v3 - run isolati + banda via netstat)
------------------------------------------------------------------
Benchmark rendering QGIS con cache sempre fredda + misura banda:
ogni run viene eseguito in un sottoprocesso Python separato
(benchmark_run_singolo.py), garantendo GDAL block cache vuota.
La banda di rete viene campionata dentro il sottoprocesso con
`netstat -e` — nessun privilegio amministrativo richiesto, quindi
funziona anche su rete aziendale con utente standard.

Uso da OSGeo4W Shell / cmd:
    "C:\Program Files\QGIS 3.40.15\bin\python-qgis-ltr.bat" ^
        benchmark_render_qgis.py ^
        --nocog "\\server\share\GISDIS_FVG_ReteNOCOG.qgs" ^
        --cog   "\\server\share\GISDIS_FVG_Rete.qgs" ^
        --runs  5 ^
        --width 1920 --height 1080 ^
        --output "C:\\temp\\risultati"

REQUISITI:
  - benchmark_run_singolo.py nella stessa cartella di questo script
  - Esecuzione con python-qgis-ltr.bat (imposta OSGEO4W_ROOT e PATH)
  - netstat deve essere disponibile in PATH (di default lo è su Windows)
  - Per flush cache SMB tra i run: eseguire come Amministratore (opzionale)
    oppure usare il wrapper run_benchmark.bat incluso

NOTA SU netstat -e:
  Misura il traffico TOTALE della scheda di rete, non solo quello di QGIS.
  Su una postazione "pulita" (poco altro traffico di rete durante il test)
  è una buona approssimazione del consumo di banda del caricamento progetto.
  Se altri processi generano traffico significativo durante il benchmark
  (sync OneDrive, Teams, browser), i valori saranno gonfiati: chiudere le
  app non necessarie durante l'esecuzione per risultati più puliti.
"""

import argparse
import csv
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from statistics import mean, stdev

# ---------------------------------------------------------------------------
# FLUSH CACHE SMB (opzionale, richiede privilegi admin)
# ---------------------------------------------------------------------------

def flush_cache_smb(abilitato: bool):
    """
    Riavvia il servizio Workstation per svuotare la cache SMB.
    Richiede esecuzione come Amministratore.
    Se non disponibile, stampa un avviso e continua senza bloccare.
    """
    if not abilitato:
        return

    print("  [FLUSH] Riavvio servizio Workstation...", flush=True)
    try:
        subprocess.run(
            ["net", "stop", "Workstation", "/y"],
            capture_output=True, check=True, timeout=30
        )
        time.sleep(1)
        subprocess.run(
            ["net", "start", "Workstation"],
            capture_output=True, check=True, timeout=30
        )
        print("  [FLUSH] Cache SMB svuotata.", flush=True)
        time.sleep(2)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        print(f"  [FLUSH] Non riuscito (serve admin?): {e}", flush=True)
        print("  [FLUSH] Continuo senza flush — misure a caldo.", flush=True)


# ---------------------------------------------------------------------------
# RUN SINGOLO ISOLATO IN SOTTOPROCESSO
# ---------------------------------------------------------------------------

CAMPI_VUOTI_ERRORE = {
    "t_load_s": -1, "t_render_s": -1, "n_layers": 0,
    "crs": "N/A", "extent": "N/A",
    "load_rx_MB": 0, "load_rx_media_MBs": 0, "load_rx_picco_MBs": 0,
    "render_rx_MB": 0, "render_rx_media_MBs": 0, "render_rx_picco_MBs": 0,
    "n_campioni_load": 0, "n_campioni_render": 0,
    "avvisi": [], "render_errors": []
}


def esegui_run_isolato(python_bat: str,
                       script_singolo: str,
                       path_progetto: str,
                       width: int,
                       height: int,
                       intervallo_banda_ms: int,
                       salva_serie: bool,
                       timeout: int = 300) -> dict:
    """
    Lancia benchmark_run_singolo.py in un processo Python separato.
    Ogni invocazione parte con GDAL block cache completamente vuota.
    Legge l'ultima riga JSON valida da stdout.
    """
    cmd = [
        python_bat,
        script_singolo,
        "--progetto",         path_progetto,
        "--width",             str(width),
        "--height",            str(height),
        "--intervallo-banda",  str(intervallo_banda_ms),
    ]
    if not salva_serie:
        cmd.append("--no-serie")

    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        return {"esito": "TIMEOUT", **CAMPI_VUOTI_ERRORE}
    except Exception as e:
        return {"esito": "ERRORE", "msg": str(e), **CAMPI_VUOTI_ERRORE}

    righe = [r.strip() for r in proc.stdout.splitlines() if r.strip()]
    for riga in reversed(righe):
        try:
            return json.loads(riga)
        except json.JSONDecodeError:
            continue

    return {
        "esito": "PARSE_ERROR",
        **CAMPI_VUOTI_ERRORE,
        "stderr": proc.stderr[-500:] if proc.stderr else "",
        "stdout": proc.stdout[-500:] if proc.stdout else ""
    }


# ---------------------------------------------------------------------------
# BENCHMARK PER PROGETTO
# ---------------------------------------------------------------------------

def benchmark_progetto(nome: str,
                       path: str,
                       runs: int,
                       python_bat: str,
                       script_singolo: str,
                       width: int,
                       height: int,
                       flush_smb: bool,
                       pausa_tra_run: int,
                       intervallo_banda_ms: int,
                       salva_serie: bool) -> dict:

    risultati = []
    serie_completa = []   # solo se salva_serie=True

    print(f"\n{'='*58}", flush=True)
    print(f"  PROGETTO : {nome}", flush=True)
    print(f"  Path     : {path}", flush=True)
    print(f"  Runs     : {runs}  |  {width}x{height} px", flush=True)
    print(f"  Cache    : {'flush SMB abilitato' if flush_smb else 'NO flush (misure a caldo)'}", flush=True)
    print(f"  Banda    : campionamento ogni {intervallo_banda_ms} ms via netstat -e", flush=True)
    print(f"{'='*58}", flush=True)

    for i in range(1, runs + 1):
        print(f"\n  --- Run {i}/{runs} ---", flush=True)

        flush_cache_smb(flush_smb)

        metriche = esegui_run_isolato(
            python_bat, script_singolo, path, width, height,
            intervallo_banda_ms, salva_serie
        )

        esito = metriche.get("esito", "ERRORE")

        if esito == "OK":
            print(f"  Caricamento : {metriche['t_load_s']} s  |  "
                  f"banda RX: {metriche['load_rx_MB']} MB "
                  f"(media {metriche['load_rx_media_MBs']} MB/s, "
                  f"picco {metriche['load_rx_picco_MBs']} MB/s)", flush=True)
            print(f"  Rendering   : {metriche['t_render_s']} s  "
                  f"({metriche['n_layers']} layer)  |  "
                  f"banda RX: {metriche['render_rx_MB']} MB "
                  f"(media {metriche['render_rx_media_MBs']} MB/s)", flush=True)
            if metriche.get("avvisi"):
                print(f"  Avvisi banda: {metriche['avvisi']}", flush=True)
            if metriche.get("render_errors"):
                print(f"  Warnings render: {metriche['render_errors']}", flush=True)
        else:
            print(f"  ESITO: {esito}", flush=True)
            if metriche.get("stderr"):
                print(f"  STDERR: {metriche['stderr']}", flush=True)

        riga_csv = {
            "progetto":            nome,
            "run":                 i,
            "esito":               esito,
            "t_load_s":            metriche.get("t_load_s", -1),
            "t_render_s":          metriche.get("t_render_s", -1),
            "n_layers":            metriche.get("n_layers", 0),
            "crs":                 metriche.get("crs", "N/A"),
            "extent":              metriche.get("extent", "N/A"),
            "load_rx_MB":          metriche.get("load_rx_MB", 0),
            "load_rx_media_MBs":   metriche.get("load_rx_media_MBs", 0),
            "load_rx_picco_MBs":   metriche.get("load_rx_picco_MBs", 0),
            "render_rx_MB":        metriche.get("render_rx_MB", 0),
            "render_rx_media_MBs": metriche.get("render_rx_media_MBs", 0),
            "render_rx_picco_MBs": metriche.get("render_rx_picco_MBs", 0),
            "flush_smb":           flush_smb,
            "timestamp":           datetime.now().isoformat(timespec="seconds")
        }
        risultati.append(riga_csv)

        # Accumula serie temporale per CSV separato (se richiesta)
        if salva_serie and esito == "OK":
            for fase, serie in [("load", metriche.get("serie_load", [])),
                                ("render", metriche.get("serie_render", []))]:
                for camp in serie:
                    serie_completa.append({
                        "progetto": nome,
                        "run":      i,
                        "fase":     fase,
                        "t_s":      camp["t_s"],
                        "rx_MB":    camp["rx_MB"],
                        "rx_MBs":   camp["rx_MBs"],
                    })

        if i < runs:
            print(f"  Pausa {pausa_tra_run} s...", flush=True)
            time.sleep(pausa_tra_run)

    return {"riepilogo": risultati, "serie": serie_completa}


# ---------------------------------------------------------------------------
# STATISTICHE
# ---------------------------------------------------------------------------

def stampa_statistiche(risultati: list, nome: str):
    ok = [r for r in risultati if r["esito"] == "OK"]
    if not ok:
        print(f"  {nome}: nessun run OK.")
        return

    def stats(vals, unita="s", dec=3):
        if len(vals) == 1:
            return f"media: {vals[0]:.{dec}f} {unita}"
        return (f"media: {mean(vals):.{dec}f} {unita}  |  "
                f"min: {min(vals):.{dec}f} {unita}  |  "
                f"max: {max(vals):.{dec}f} {unita}  |  "
                f"stdev: {stdev(vals):.{dec}f} {unita}")

    rt    = [r["t_render_s"]   for r in ok]
    lt    = [r["t_load_s"]     for r in ok]
    rxL   = [r["load_rx_MB"]   for r in ok]
    rxR   = [r["render_rx_MB"] for r in ok]

    print(f"\n  {nome}  ({len(ok)} run OK / {len(risultati)} totali):")
    print(f"    Tempo caricamento : {stats(lt)}")
    print(f"    Tempo rendering   : {stats(rt)}")
    print(f"    Banda caricamento : {stats(rxL, 'MB', 2)}")
    print(f"    Banda rendering   : {stats(rxR, 'MB', 2)}")


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------

def salva_csv_riepilogo(risultati: list, output_dir: str, ts: str) -> str:
    path = os.path.join(output_dir, f"benchmark_render_{ts}.csv")
    os.makedirs(output_dir, exist_ok=True)

    campi = ["progetto", "run", "esito", "t_load_s", "t_render_s",
             "n_layers", "crs", "extent",
             "load_rx_MB", "load_rx_media_MBs", "load_rx_picco_MBs",
             "render_rx_MB", "render_rx_media_MBs", "render_rx_picco_MBs",
             "flush_smb", "timestamp"]

    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=campi, extrasaction="ignore")
        w.writeheader()
        w.writerows(risultati)

    return path


def salva_csv_serie(serie: list, output_dir: str, ts: str) -> str | None:
    if not serie:
        return None
    path = os.path.join(output_dir, f"benchmark_banda_serie_{ts}.csv")
    os.makedirs(output_dir, exist_ok=True)

    campi = ["progetto", "run", "fase", "t_s", "rx_MB", "rx_MBs"]

    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=campi)
        w.writeheader()
        w.writerows(serie)

    return path


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Benchmark rendering QGIS con run isolati + misura banda (netstat)"
    )
    parser.add_argument("--nocog",   required=True,
                        help="Path progetto SENZA COG")
    parser.add_argument("--cog",     required=True,
                        help="Path progetto CON COG multiscala")
    parser.add_argument("--runs",    type=int, default=3,
                        help="Numero run per progetto (default: 3)")
    parser.add_argument("--width",   type=int, default=1920)
    parser.add_argument("--height",  type=int, default=1080)
    parser.add_argument("--output",  default=str(Path.home() / "benchmark_qgis"),
                        help="Cartella output CSV")
    parser.add_argument("--flush-smb", action="store_true",
                        help="Flush cache SMB tra i run (richiede admin)")
    parser.add_argument("--pausa",   type=int, default=5,
                        help="Secondi di pausa tra un run e il successivo (default: 5)")
    parser.add_argument("--intervallo-banda", type=int, default=500,
                        help="Intervallo campionamento banda in ms (default: 500)")
    parser.add_argument("--salva-serie", action="store_true",
                        help="Salva anche la serie temporale dettagliata della banda "
                             "in un CSV separato (utile per grafici banda/tempo)")
    args = parser.parse_args()

    osgeo4w = os.environ.get("OSGEO4W_ROOT",
                             r"C:\Program Files\QGIS 3.40.15")
    python_bat = os.path.join(osgeo4w, "bin", "python-qgis-ltr.bat")
    script_dir = os.path.dirname(os.path.abspath(__file__))
    script_singolo = os.path.join(script_dir, "benchmark_run_singolo.py")

    if not os.path.isfile(python_bat):
        print(f"ERRORE: python-qgis-ltr.bat non trovato in {python_bat}")
        print("Imposta OSGEO4W_ROOT correttamente o modifica il path nello script.")
        sys.exit(1)

    if not os.path.isfile(script_singolo):
        print(f"ERRORE: benchmark_run_singolo.py non trovato in {script_singolo}")
        print("I due script devono essere nella stessa cartella.")
        sys.exit(1)

    print(f"\n{'='*58}")
    print("  BENCHMARK RENDERING QGIS — RUN ISOLATI + BANDA")
    print(f"{'='*58}")
    print(f"  python-qgis-ltr.bat   : {python_bat}")
    print(f"  Script companion      : {script_singolo}")
    print(f"  Runs per progetto     : {args.runs}")
    print(f"  Risoluzione           : {args.width}x{args.height}")
    print(f"  Flush SMB             : {'SI (admin)' if args.flush_smb else 'NO'}")
    print(f"  Pausa tra run         : {args.pausa} s")
    print(f"  Campionamento banda   : {args.intervallo_banda} ms")
    print(f"  Salva serie temporale : {'SI' if args.salva_serie else 'NO'}")
    print("\n  NOTA: chiudere OneDrive/Teams/browser per misure di banda pulite.")

    tutti_riepilogo = []
    tutta_serie     = []

    for nome, path in [("NO-COG", args.nocog), ("COG", args.cog)]:
        res = benchmark_progetto(
            nome, path, args.runs,
            python_bat, script_singolo,
            args.width, args.height,
            args.flush_smb, args.pausa,
            args.intervallo_banda, args.salva_serie
        )
        tutti_riepilogo.extend(res["riepilogo"])
        tutta_serie.extend(res["serie"])

    # Riepilogo console
    print(f"\n{'='*58}")
    print("  RIEPILOGO STATISTICO")
    print(f"{'='*58}")
    for nome in ["NO-COG", "COG"]:
        stampa_statistiche([r for r in tutti_riepilogo if r["progetto"] == nome], nome)

    # Confronti rapidi
    ok_nocog = [r for r in tutti_riepilogo if r["progetto"] == "NO-COG" and r["esito"] == "OK"]
    ok_cog   = [r for r in tutti_riepilogo if r["progetto"] == "COG"    and r["esito"] == "OK"]
    if ok_nocog and ok_cog:
        speedup_t  = round(mean([r["t_render_s"] for r in ok_nocog]) /
                           mean([r["t_render_s"] for r in ok_cog]), 1)
        riduz_banda_load = round(
            (1 - mean([r["load_rx_MB"] for r in ok_cog]) /
                 mean([r["load_rx_MB"] for r in ok_nocog])) * 100, 1
        ) if mean([r["load_rx_MB"] for r in ok_nocog]) > 0 else 0
        print(f"\n  Speedup rendering COG vs NO-COG      : {speedup_t}×")
        print(f"  Riduzione banda caricamento COG vs NO-COG : {riduz_banda_load}%")

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_riepilogo = salva_csv_riepilogo(tutti_riepilogo, args.output, ts)
    print(f"\n  CSV riepilogo : {csv_riepilogo}")

    if args.salva_serie:
        csv_serie = salva_csv_serie(tutta_serie, args.output, ts)
        if csv_serie:
            print(f"  CSV serie banda : {csv_serie}")
        else:
            print("  CSV serie banda : nessun dato (tutti i run falliti?)")


if __name__ == "__main__":
    main()
