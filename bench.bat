@echo off
REM run_benchmark.bat
REM Lancia il benchmark QGIS (tempo + banda) da cmd normale
REM Nessun privilegio amministratore richiesto per la misura di banda.
REM Metti questo file nella stessa cartella dei due script Python.

set QGISROOT=C:\Program Files\QGIS 3.40.15

REM --- ADATTA QUESTI PATH ---
set NOCOG=C:\temp\GISDIS_FVG_ReteNOCOG.qgs
set COG=C:\temp\GISDIS_FVG_Rete.qgs
set OUTPUT=%~dp0risultati
set RUNS=5

REM --- LANCIO ---
echo.
echo  Avvio benchmark QGIS (tempo + banda)...
echo  NO-COG : %NOCOG%
echo  COG    : %COG%
echo  Runs   : %RUNS%
echo.
echo  IMPORTANTE: chiudere OneDrive/Teams/browser per misure di banda pulite.
echo.

call "%QGISROOT%\bin\python-qgis-ltr.bat" "%~dp0benchmark_render_qgis.py" ^
    --nocog  "%NOCOG%" ^
    --cog    "%COG%"   ^
    --runs   %RUNS%    ^
    --width  1920      ^
    --height 1080      ^
    --output "%OUTPUT%" ^
    --pausa  5 ^
    --intervallo-banda 500 ^
    --salva-serie

REM Aggiungi --flush-smb sopra (eseguendo come Amministratore) per misure a freddo

echo.
pause
