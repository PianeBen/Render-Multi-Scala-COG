# Generazione di un COG Multi-Scala con PyQGIS e GDAL

*Analisi tecnica dettagliata dello script Render_Multiscale_Cog_Proc.py*

Sistema di riferimento EPSG:6708 

---

## 1. Introduzione

### 1.1 Premessa

Ciò che segue è il risultato di numerosi tentativi falliti e ripetuti fino a ottenere un risultato soddisfacente, realizzato grazie alla collaborazione determinante di Claude.ai (Sonnet 4.6). Trattandosi di codice Python non revisionato da "cuochi esperti", si consiglia di adottare ogni cautela prima di utilizzarlo in produzione.

### 1.2 Da dove nasce l'esigenza

Lo script Render_Multiscale_Cog_Proc.py risponde a un problema pratico tipico dei sistemi GIS su rete locale: un progetto QGIS composto da decine di layer vettoriali tematizzati e da raster di sfondo risulta lento da caricare quando i dati risiedono su un server di rete, perché ogni layer viene trasferito per intero prima di poter essere visualizzato.

La soluzione adottata è la pre-renderizzazione del progetto in un file raster unico strutturato come Cloud Optimized GeoTIFF (COG) con piramidi interne generate da render reali a scale diverse. A differenza delle piramidi standard — che sono semplici ricampionamenti (media dei pixel) del livello base — le overview di questo COG contengono immagini renderizzate direttamente da QGIS alla scala corretta, con i layer appropriati attivi e le data-defined override già valutate.

Il COG viene utilizzato come sfondo cartografico nell'intervallo 1:150.000–1:600.000. Al di sotto di 1:150.000 il progetto carica i layer originali ad alta risoluzione.

Il COG può essere considerato un tile server embedded nel file, senza server: GDAL lato client legge solo i blocchi 512×512 px necessari alla vista corrente attraverso la condivisione di rete esistente, senza scaricare l'intero file. Non richiede infrastruttura aggiuntiva rispetto a una normale condivisione SMB.

---

## 2. Architettura della pipeline

La pipeline si divide in due fasi distinte eseguite in sequenza:

**Fase A — algoritmo Processing QGIS (rendering):** eseguito dal pannello Processing di QGIS. Per ciascuno dei tre livelli di scala renderizza l'intera estensione geografica in un'unica immagine, poi la ritaglia in tile tramite QImage.copy(). Genera build_cog.bat e inject_ovr.py.

**Fase B — GDAL (assemblaggio):** eseguita in OSGeo4W Shell. Georiferisce i tile PNG, assembla VRT, costruisce i GeoTIFF intermedi e produce il COG finale con overview reali.

```
Algoritmo Processing QGIS
  ├─ Scala 1:150.000 → render intera estensione → 36 tile PNG
  ├─ Scala 1:300.000 → render intera estensione → 36 tile PNG
  └─ Scala 1:600.000 → render intera estensione → 36 tile PNG
        └─ genera: build_cog.bat  +  inject_ovr.py

OSGeo4W Shell: build_cog.bat
  ├─ [1] gdal_translate  → 108 GeoTIFF georeferenziati
  ├─ [2] gdalbuildvrt    → 3 VRT (uno per scala)
  ├─ [3] gdal_translate  → 3 GeoTIFF compressi per scala
  └─ [4] inject_ovr.py  → COG finale con overview reali
```

La scelta di renderizzare l'intera estensione in un'unica immagine (anziché tile per tile) è architetturalmente fondamentale: il motore di etichettatura PAL lavora una sola volta sull'intera area, eliminando alla radice qualsiasi problema di etichette troncate, duplicate o disallineate ai confini tra tile — indipendentemente dalla forma delle etichette (dritte, curve, lunghe).

---

## 3. Parametri di configurazione

I parametri sono esposti nel dialogo del Processing e non richiedono alcuna modifica al codice. I valori di default coprono il caso tipico del progetto FVG.

| Parametro (dialogo) | Tipo | Default | Descrizione |
|---|---|---|---|
| Layer di riferimento estensione | Layer (opzionale) | — | La extent() di questo layer diventa l'area di rendering; se vuoto usa il campo manuale |
| Estensione (manuale) | Extent (opzionale) | — | Usata solo se nessun layer di riferimento è selezionato |
| DPI di rendering | Intero | 96 | Risoluzione del render; la risoluzione m/px è calcolata automaticamente da scala e DPI |
| Colonne / Righe griglia tile | Interi | 6 / 6 | Griglia di ritaglio: 6×6 = 36 tile per scala |
| Includi scala 1:150.000 | Checkbox | ✓ | Attiva il livello base COG |
| Includi scala 1:300.000 | Checkbox | ✓ | Attiva la overview 2× |
| Includi scala 1:600.000 | Checkbox | ✓ | Attiva la overview 4× |
| Margine scala-overview (%) | Double | 5.0 | Riduce la risoluzione effettiva per anticipare la soglia di passaggio GDAL tra overview |
| Algoritmo compressione COG | Enum | DEFLATE | DEFLATE (universale) o ZSTD (decompressione più veloce su LAN) |
| Livello di compressione | Intero | 9 | 1–9 per DEFLATE, 1–22 per ZSTD |
| Cartella di output | Cartella | C:\temp\cog_fvg | Dove vengono salvati tile PNG, .bat e .py intermedi |

La risoluzione in m/px non è più un parametro esplicito: viene calcolata automaticamente a runtime dalla scala nominale e dal DPI, secondo la relazione res_m = scala × 0,0254 / dpi. Questo garantisce che la scala effettivamente renderizzata coincida sempre con la scala nominale del livello, indipendentemente dal DPI scelto.

---

## 4. Lettura corretta del layer tree

### 4.1 Il problema di mapLayers()

Il metodo QgsProject.instance().mapLayers() restituisce tutti i layer registrati nel progetto indipendentemente dalla loro visibilità. Usarlo direttamente include layer non voluti, ignora i layer disabilitati e non rispetta la struttura gerarchica dei gruppi.

### 4.2 La soluzione: percorrere il QgsLayerTree

La funzione get_layers_at_scale() percorre ricorsivamente il layer tree partendo dalla radice. Per ogni nodo viene verificata itemVisibilityChecked() che corrisponde esattamente alla spunta nel pannello Layer.

```
def get_layers_at_scale(scala):
    layers = []
    def _collect(node):
        if not node.itemVisibilityChecked():
            return
        if isinstance(node, QgsLayerTreeLayer):
            layer = node.layer()
            if layer and layer.isSpatial():
                if layer.hasScaleBasedVisibility():
                    if layer.isInScaleRange(scala):
                        layers.append(layer)
                else:
                    layers.append(layer)
        elif isinstance(node, QgsLayerTreeGroup):
            for child in node.children():
                _collect(child)
    for child in root.children():
        _collect(child)
    return layers
```

| Metodo | Comportamento |
|---|---|
| isVisible() | Considera la visibilità ereditata dai nodi padre — non corrisponde alla spunta |
| itemVisibilityChecked() | Legge esattamente la spunta del singolo nodo nel pannello Layer — corretto |
| hasScaleBasedVisibility() | True se il layer ha almeno un limite di scala configurato |
| isInScaleRange(scala) | True se il valore è nell'intervallo min-max configurato nel layer |

---

## 5. Contesto espressioni e @map_scale

### 5.1 Il problema

Le data-defined override in QGIS usano la variabile @map_scale per modificare simbologia o etichettatura. Senza un contesto espressioni esplicito, @map_scale riceve NULL e le espressioni CASE eseguono sempre il ramo ELSE — producendo ad esempio etichette a dimensione fissa indipendentemente dalla scala.

### 5.2 La soluzione: make_expr_context()

```
def make_expr_context(settings):
    ctx = QgsExpressionContext()
    ctx.appendScope(QgsExpressionContextUtils.globalScope())
    ctx.appendScope(QgsExpressionContextUtils.projectScope(
                        QgsProject.instance()))
    ctx.appendScope(
        QgsExpressionContextUtils.mapSettingsScope(settings))
    return ctx
```

| Scope | Variabili fornite |
|---|---|
| globalScope() | Variabili globali QGIS (es. @qgis_version) |
| projectScope(project) | Variabili del progetto (es. @project_title) |
| mapSettingsScope(settings) | @map_scale, @map_extent, @map_crs, @map_rotation |

Il punto critico è che mapSettingsScope(settings) deve ricevere l'oggetto QgsMapSettings già completamente configurato — con extent, output size e DPI impostati — perché il calcolo di @map_scale dipende da questi tre valori.

---

## 6. Rendering a estensione unica per livello di scala

### 6.1 Un solo render per livello

Per ciascun livello di scala, l'intera estensione geografica viene renderizzata in un'unica QImage, poi ritagliata in 36 tile tramite QImage.copy() — una pura operazione di slicing dei pixel, senza alcun rendering aggiuntivo. Questa scelta architetturale è la conseguenza diretta dei problemi incontrati con il rendering per-tile (descritti nel capitolo 18): è la soluzione più semplice possibile ed elimina strutturalmente l'intera classe di problemi legati ai confini tra tile.

```
# Calcolo dimensioni del render completo
full_px_w = tile_px_w * tile_cols
full_px_h = tile_px_h * tile_rows

# Render unico sull'intera estensione
settings = QgsMapSettings()
settings.setLayers(layers)
settings.setExtent(QgsRectangle(EX_MIN, EY_MIN, EX_MAX, EY_MAX))
settings.setOutputSize(QSize(full_px_w, full_px_h))
settings.setOutputDpi(dpi)

job = QgsMapRendererSequentialJob(settings)
job.start()
job.waitForFinished()
full_img = job.renderedImage().copy()
del job ; gc.collect()

# Ritaglio in tile: nessun rendering, solo slicing dei pixel
for row in range(tile_rows):
    for col in range(tile_cols):
        tile_img = full_img.copy(col*tile_px_w, row*tile_px_h,
                                  tile_px_w, tile_px_h)
        tile_img.save(path)
```

### 6.2 QgsMapRendererSequentialJob

Il rendering usa QgsMapRendererSequentialJob (anziché il CustomPainterJob usato nelle versioni iniziali) perché renderizza i layer uno alla volta nello stesso contesto, senza thread interni in parallelo. Questo previene conflitti sulle risorse condivise del motore Qt (cache glyph/font del labeling engine) che causavano blocchi della canvas e etichette assenti in alcuni livelli — problemi documentati nel capitolo 18. L'immagine risultante arriva direttamente da renderedImage() senza gestione manuale di QPainter.

### 6.3 Flag di rendering QgsMapSettings

| Flag | Effetto |
|---|---|
| Antialiasing = True | Smoothing dei bordi di poligoni e linee |
| DrawLabeling = True | Include le etichette nel rendering |
| UseAdvancedEffects = True | Abilita trasparenze, blend mode ed effetti di strato |
| UseRenderingOptimization = True | Ottimizza il rendering delle geometrie |

### 6.4 Smaltimento esplicito delle risorse

Per garantire che ogni livello di scala parta da uno stato pulito, job e full_img vengono esplicitamente distrutti al termine di ciascun livello tramite del + gc.collect(). Questo forza la distruzione immediata degli oggetti C++ sottostanti invece di affidarsi al garbage collector Python, che potrebbe ritardarla fino al livello successivo propagando stato residuo tra render.

---

## 7. Generazione degli script GDAL

### 7.1 build_cog.bat — georiferimento e assemblaggio

Per ogni tile PNG viene generata una chiamata a gdal_translate con -a_srs per assegnare il SR senza riproiettare e -a_ullr per georeferenziare tramite le coordinate dell'angolo superiore sinistro e inferiore destro. I tile georeferenziati vengono assemblati in un VRT tramite gdalbuildvrt e convertiti in GeoTIFF compresso.

### 7.2 inject_ovr.py — iniezione delle overview pre-renderizzate

Invece di costruire le piramidi per ricampionamento, questo script inietta come overview i GeoTIFF renderizzati realmente alle scale 1:300.000 e 1:600.000. Il flusso è:

- Apertura del file base (render_150k.tif) in modalità GA_Update

- Chiamata a ds.BuildOverviews('AVERAGE', OVR_FACTORS) per creare le strutture overview vuote

- Per ciascun livello, lettura dei dati con ReadAsArray(buf_xsize, buf_ysize) e scrittura con WriteArray()

- Flush con ds.FlushCache() e conversione finale in COG con OVERVIEWS=FORCE_USE_EXISTING

---

## 8. Parametri di ottimizzazione del COG

| Parametro | Valore | Motivazione |
|---|---|---|
| COMPRESS | DEFLATE / ZSTD | DEFLATE: compatibilità universale. ZSTD: decompressione più veloce su rete LAN |
| PREDICTOR | 2 | Differenziazione orizzontale: riduce l'entropia dei dati Byte RGB |
| ZLEVEL / ZSTD_LEVEL | 9 | Massima compressione; la lettura è identica al livello 1 |
| BLOCKSIZE | 512 | Tile interni 512×512 px; GDAL legge solo i blocchi nella finestra corrente |
| NUM_THREADS | ALL_CPUS | Encoding parallelo su tutti i core disponibili |
| OVERVIEWS | FORCE_USE_EXISTING | Non ricalcolare le piramidi — usa quelle iniettate da inject_ovr.py |
| OVERVIEW_COMPRESS | uguale a COMPRESS | Senza questo parametro le overview restano non compresse |
| BIGTIFF | IF_NEEDED | Abilita automaticamente il formato BigTIFF se il file supera 4 GB |

Nota: ZSTD usa il parametro ZSTD_LEVEL (non LEVEL né ZLEVEL). La presenza di ZSTD_LEVEL nella lista opzioni di gdal_translate conferma che libzstd è linkato correttamente nella build OSGeo4W.

---

## 9. Riesecuzione parziale della pipeline

### 9.1 Aggiornare un singolo livello di scala

Se si modifica la simbologia di layer presenti solo a 1:300.000, è sufficiente rieseguire l'algoritmo Processing deselezionando i livelli 1:150.000 e 1:600.000. Lo script produce i nuovi tile PNG e rigenera render_300k.tif.

### 9.2 Riniettare una overview

Riniettare significa sostituire il contenuto di un livello overview esistente con i dati di un nuovo GeoTIFF pre-renderizzato. inject_ovr.py apre il file base in GA_Update, chiama BuildOverviews per preparare le strutture, sovrascrive i dati con WriteArray e rigenera il COG finale con FORCE_USE_EXISTING. Il file base (render_150k.tif) non viene modificato nel contenuto, quindi il COG aggiornato mantiene invariato il livello 1:150.000.

---

## 10. Utilizzo del COG in QGIS

Il file COG finale può essere caricato in QGIS come un normale GeoTIFF tramite percorso UNC di rete. GDAL gestisce automaticamente il fetch dei soli blocchi 512×512 px necessari alla vista corrente, riducendo il traffico di rete rispetto al caricamento dei layer originali.

Per la corretta integrazione nel progetto impostare la visibilità dipendente dalla scala sul layer COG:

- Scala massima (più grande): 1:150.000

- Scala minima (più piccola): 1:600.000

---

## 11. Troncamento delle etichette ai bordi del tile — nota storica

Nella versione originale dell'algoritmo, basata sul rendering di 36 tile separati per livello di scala, le etichette posizionate in prossimità del bordo di un tile venivano troncate di netto nell'immagine assemblata. Il motore di etichettatura ancora l'etichetta al punto della feature, ma il testo si estende fisicamente oltre il bounding box del tile.

Il problema era stato affrontato con un buffer perimetrale: ogni tile veniva renderizzato su un'area estesa di LABEL_BUFFER_M metri per lato, e l'immagine risultante veniva poi ritagliata alla dimensione esatta tramite QImage.copy(). La calibrazione del buffer dipendeva da corpo carattere, DPI e risoluzione in m/px — un parametro da aggiustare manualmente al variare del DPI.

Nell'architettura corrente (capitolo 6) il problema non esiste più per costruzione: non ci sono confini interni tra tile durante il rendering, perché il motore PAL lavora una sola volta sull'intera estensione. Il parametro LABEL_BUFFER_M è stato rimosso dal dialogo. La trattazione storica completa del meccanismo a buffer è documentata nel capitolo 18.

---

## 12. Risultati ottenuti

| Parametro | Valore |
|---|---|
| Griglia tile | 6×6 = 36 tile per scala |
| Livelli di scala | 3 (1:150.000, 1:300.000, 1:600.000) |
| Tile totali prodotti | 108 PNG |
| Risoluzione base | 37.5 m/px (1:150.000) |
| Sistema di riferimento | EPSG:6708 nativo, nessuna riproiezione |
| Dimensione file COG | 94 MB |
| Compressione | DEFLATE + PREDICTOR=2 + ZLEVEL=9 |
| Layer inclusi | 18 layer vettoriali e raster tematizzati |

---

## 13. COG in QGIS 4.0 — novità e miglioramenti

QGIS 4.0, rilasciato il 6 marzo 2026, rappresenta la più importante migrazione tecnica dalla versione 3.0: il framework grafico passa da Qt 5 a Qt 6. Sul piano funzionale, la versione 4.0 consolida e amplia il supporto nativo al formato COG introducendo tre miglioramenti specifici.

### 13.1 Nuovo algoritmo Processing nativo

QGIS 4.0 introduce un algoritmo dedicato nel pannello Processing — Crea Cloud Optimized GeoTIFF — che permette la creazione diretta di COG a partire da una cartella di raster di input, senza dover ricorrere a GDAL da riga di comando. L'algoritmo supporta la configurazione delle piramidi, del tipo di compressione e del blocksize direttamente dalla finestra di dialogo Processing.

Rispetto allo script illustrato in questo articolo, l'algoritmo nativo di QGIS 4.0 non gestisce il rendering multi-scala con overview pre-renderizzate: è adatto alla conversione di raster già esistenti, non alla produzione di COG da layer QGIS tematizzati. Le due soluzioni sono quindi complementari.

### 13.2 Dialogo di esportazione raster con supporto COG esplicito

I dialoghi Esporta raster e Salva con nome ora includono un'opzione esplicita per il formato COG. In QGIS 3.x l'export COG era disponibile solo tramite il driver GDAL selezionando manualmente l'estensione .tif e specificando i parametri di creazione — una procedura non intuitiva. In QGIS 4.0 l'utente può selezionare COG come formato di destinazione con opzioni dedicate per piramidi e compressione.

### 13.3 Correzione del problema -of COG nella riga di comando

In QGIS 3.x, quando si specificava il formato di output tramite nome file, il driver GTiff e il driver COG erano indistinguibili poiché entrambi usano l'estensione .tif/.tiff. Questo causava export in formato GTiff standard quando si voleva COG. QGIS 4.0 permette ora di specificare esplicitamente -of COG nelle operazioni Processing che accettano flag GDAL, eliminando questa ambiguità.

### 13.4 Supporto COG in QGIS 3.x — stato attuale

Per completezza: il supporto in lettura dei COG era già presente in QGIS dalla versione 3.2 tramite GDAL. Il driver COG di GDAL gestisce automaticamente la lettura a blocchi e il caching delle overview. Lo script presentato in questo articolo è quindi pienamente operativo su QGIS 3.x (testato su 3.40 LTR) e non richiede QGIS 4.0 per il rendering o per la generazione degli script GDAL.

---

## 14. Compatibilità con Qt6 — modifiche per QGIS 4.0

La migrazione da Qt5 a Qt6 comporta alcune modifiche al codice Python. Il team QGIS fornisce un compatibility shim (qgis.PyQt) che permette di scrivere codice compatibile con entrambe le versioni tramite modifiche minime.

### 14.1 Import tramite shim qgis.PyQt

La modifica principale è sostituire gli import diretti da PyQt5 con il proxy qgis.PyQt, che reindirizza automaticamente verso PyQt5 o PyQt6 a seconda dell'ambiente QGIS:

```
# Compatibile Qt5 (QGIS 3.x) e Qt6 (QGIS 4.x)
from qgis.PyQt.QtCore import QSize, QCoreApplication, QT_VERSION_STR

# Rilevamento versione per la logica condizionale degli enum
IS_QT6 = int(QT_VERSION_STR.split('.')[0]) >= 6
```

Nell'algoritmo corrente QImage e QPainter non sono più importati direttamente: il rendering usa QgsMapRendererSequentialJob che restituisce l'immagine tramite renderedImage() senza richiedere la gestione manuale di un QPainter.

### 14.2 Enum completamente qualificati in Qt6

Qt6 richiede che gli enum siano completamente qualificati. Il pattern IS_QT6 risolve il problema in un'unica riga per tutti i flag di QgsMapSettings:

```
_FLAGS = QgsMapSettings.Flag if IS_QT6 else QgsMapSettings

settings.setFlag(_FLAGS.Antialiasing,            True)
settings.setFlag(_FLAGS.DrawLabeling,             True)
settings.setFlag(_FLAGS.UseAdvancedEffects,       True)
settings.setFlag(_FLAGS.UseRenderingOptimization, True)
```

### 14.3 Riepilogo compatibilità

| Componente | QGIS 3.40 LTR | QGIS 4.0 | Azione richiesta |
|---|---|---|---|
| Import QtCore | from PyQt5.QtCore | from qgis.PyQt.QtCore | Usare qgis.PyQt ovunque |
| QgsMapSettings flags | non qualificato | QgsMapSettings.Flag.* | Usare _FLAGS = ... if IS_QT6 |
| qgis.core API | invariata | invariata (deprecate 2.x) | Nessuna modifica |
| osgeo / GDAL | invariata | invariata | Nessuna modifica |
| Algoritmo Processing | compatibile | compatibile | Nessuna modifica |

---

## 15. Risultati e considerazioni finali

| Parametro | Valore |
|---|---|
| Render per livello di scala | 1 (intera estensione, poi ritagliata in tile) |
| Griglia tile | 6×6 = 36 tile per scala (ritaglio da render unico) |
| Livelli di scala | 3 (1:150.000, 1:300.000, 1:600.000) |
| Tile totali prodotti | 108 PNG |
| Sistema di riferimento | EPSG:6708 nativo — nessuna distorsione di etichette |
| Dimensione file COG | 94 MB |
| Compressione | DEFLATE/ZSTD + PREDICTOR=2, LEVEL=9 (driver COG) |
| Layer inclusi | 18 layer vettoriali e raster tematizzati |
| Motore di rendering | QgsMapRendererSequentialJob + FlagNoThreading |
| Compatibilità | QGIS 3.40 LTR e QGIS 4.0 senza modifiche al codice |

---

## 16. Algoritmo di Processing — versione corrente

L'algoritmo compare nel pannello Processing di QGIS sotto Scripts → GISDIS FVG → Render Multi-Scala → COG. Può essere richiamato come qualsiasi strumento nativo, incluso l'utilizzo in modelli grafici (Graphical Modeler) e in batch tramite qgis_process.

### 16.1 Struttura della classe QgsProcessingAlgorithm

Una QgsProcessingAlgorithm richiede l'implementazione di un set minimo di metodi obbligatori, oltre alla logica applicativa:

| Metodo | Ruolo |
|---|---|
| name() / displayName() | Identificatore interno e nome visualizzato nel pannello Processing |
| group() / groupId() | Categoria di raggruppamento (es. "GISDIS FVG") |
| flags() | FlagNoThreading: forza esecuzione sul thread GUI — necessario per rendering Qt |
| shortHelpString() | Testo HTML mostrato nel pannello di aiuto del dialogo |
| initAlgorithm() | Definisce i parametri che generano i controlli del dialogo |
| processAlgorithm() | Contiene la logica di rendering, ritaglio e generazione script GDAL |

### 16.2 Parametri del dialogo (versione corrente)

| Classe parametro | Widget generato | Uso corrente |
|---|---|---|
| QgsProcessingParameterFolderDestination | Selettore cartella | Cartella di output per tile PNG e script GDAL |
| QgsProcessingParameterMapLayer (opz.) | Selettore layer | Layer di riferimento per l'estensione — la sua extent() diventa l'area di rendering |
| QgsProcessingParameterExtent (opz.) | Selettore su mappa | Estensione manuale, usata solo se nessun layer di riferimento è selezionato |
| QgsProcessingParameterNumber (DPI) | Spinbox intero | DPI di rendering; res_m calcolata automaticamente da scala e DPI |
| QgsProcessingParameterNumber (griglia) | Spinbox intero | Colonne e righe della griglia di ritaglio (default 6×6) |
| QgsProcessingParameterBoolean (×3) | Checkbox | Inclusione dei livelli 1:150k, 1:300k, 1:600k |
| QgsProcessingParameterNumber (margine) | Spinbox double | Margine scala-overview in % per anticipare soglia di passaggio GDAL |
| QgsProcessingParameterEnum | Menu a tendina | Algoritmo di compressione COG: DEFLATE o ZSTD |
| QgsProcessingParameterNumber (livello) | Spinbox intero | Livello di compressione (1–9 per DEFLATE, 1–22 per ZSTD) |

L'estensione da layer di riferimento (QgsProcessingParameterMapLayer) sostituisce i meccanismi precedenti basati sull'impostazione della scala della canvas — che interferivano con il job di rendering interno di QGIS bloccandone la risposta ai comandi. layer.extent() non tocca in alcun modo la canvas, eliminando questa classe di problemi.

### 16.3 FlagNoThreading — perché è necessario

Per default QGIS esegue gli algoritmi Processing su un thread di lavoro separato. Questo script usa QgsMapRendererSequentialJob che interagisce con il motore di rendering Qt (cache glyph/font, paint engine): eseguirlo in background può corrompere risorse condivise con il thread GUI. FlagNoThreading forza l'esecuzione sincrona sul thread principale:

```
def flags(self):
    return super().flags() | QgsProcessingAlgorithm.FlagNoThreading
```

### 16.4 Log diagnostico

L'algoritmo produce un log dettagliato per ciascun livello di scala, utile per diagnosticare problemi di configurazione senza dover aprire le proprietà di ogni layer:

- Layer visibili a questa scala (nome e presenza di limiti scala-etichette)

- [label-scale] — range di visibilità delle etichette per ciascun layer vettoriale; segnala se il range esclude la scala corrente

- [label-size] — dimensione font calcolata per le espressioni data-defined basate su @map_scale; segnala size=0 che renderebbe l'etichetta invisibile

- Scala QGIS effettiva e @map_scale — conferma che la scala nominale e quella renderizzata coincidono

### 16.5 Installazione

```
Processing → Opzioni → Impostazioni generali
  → Cartelle script aggiuntivi → [aggiungi cartella]

oppure copiare in:
%APPDATA%\QGIS\QGIS3\profiles\default\processing\scripts\

Poi: Processing → Strumenti di Processing → Scripts
  → tasto destro → "Carica script dal file"
```

---

## 17. Parametro LEVEL per il driver COG

Durante l'esecuzione di build_cog.bat compariva l'avviso 'ZLEVEL creation option not supported'. La causa è un'incoerenza tra driver GDAL distinti: il driver GTiff utilizza ZLEVEL per DEFLATE e ZSTD_LEVEL per ZSTD, mentre il driver COG utilizza un parametro unificato — LEVEL — valido per tutti gli algoritmi di compressione.

| Driver target | Parametro DEFLATE | Parametro ZSTD |
|---|---|---|
| GTiff (file intermedi render_XXXk.tif) | ZLEVEL | ZSTD_LEVEL |
| COG (file finale — inject_ovr.py) | LEVEL | LEVEL |

La correzione sostituisce in inject_ovr.py il parametro condizionale con il valore unificato:

```
# PRIMA — genera warning sul driver COG
creationOptions = [f"{comp_level_param}={comp_level}", ...]

# DOPO — corretto per il driver COG
creationOptions = ["LEVEL={comp_level}", ...]
```

---

## 18. Evoluzione architetturale: dal rendering per-tile al rendering a estensione unica

I capitoli precedenti documentano l'architettura originaria, basata su 36 render per livello di scala (uno per tile) con buffer perimetrale per le etichette dritte e un doppio passaggio per le etichette curve. Questa architettura, messa alla prova nell'uso quotidiano, ha rivelato due criticità strutturali che ne hanno richiesto una revisione radicale, descritta in questo capitolo.

### 18.1 Limiti dell'approccio per-tile con buffer e doppio passaggio

Il primo problema riguardava QgsMapRendererCustomPainterJob, utilizzato per ciascuno dei 108 render (36 tile × 3 scale). Questa classe avvia internamente thread di lavoro per il rendering dei singoli layer — un comportamento ereditato dal motore di rendering della canvas stessa. Eseguire 108 job in sequenza senza una distruzione esplicita degli oggetti C++ sottostanti lasciava risorse del motore di rendering Qt (cache glyph/font, paint engine) in uno stato non completamente smaltito tra un job e il successivo. Il sintomo osservato era un blocco della canvas di QGIS al termine dello script — zoom e F5 smettevano di rispondere — che sopravviveva persino alla chiusura del progetto, segno che la corruzione avveniva a livello di processo e non di singolo progetto QGIS.

Il secondo problema riguardava il meccanismo a doppio passaggio per le etichette curve, basato su QgsNullSymbolRenderer per isolare temporaneamente le sole etichette in un render globale separato. In alcuni casi questo produceva tile completamente vuote a eccezione delle etichette — sintomo di un'interazione imprevista tra la sostituzione del renderer e il ciclo di vita degli oggetti layer durante un rendering asincrono.

### 18.2 Nuova architettura: un render per livello di scala

La soluzione adottata elimina alla radice entrambi i problemi: invece di 36 render per livello con buffer e doppio passaggio, l'intera estensione geografica viene renderizzata in un'unica immagine per ciascuno dei tre livelli di scala, e solo successivamente questa immagine viene ritagliata in tile tramite QImage.copy() — una pura operazione di slicing dei pixel, senza alcun rendering aggiuntivo.

```
# Render dell'intera estensione (una sola volta per livello)
settings = QgsMapSettings()
settings.setLayers(layers)
settings.setExtent(QgsRectangle(EX_MIN, EY_MIN, EX_MAX, EY_MAX))
settings.setOutputSize(QSize(full_px_w, full_px_h))
# ... rendering (vedi 18.3) ...

# Ritaglio in tile: nessun rendering aggiuntivo
for row in range(tile_rows):
    for col in range(tile_cols):
        px_x, px_y = col * tile_px_w, row * tile_px_h
        tile_img = full_img.copy(px_x, px_y, tile_px_w, tile_px_h)
        tile_img.save(path)
```

Questa architettura rende strutturalmente impossibile sia il troncamento sia la duplicazione delle etichette ai bordi tile, qualunque sia la loro forma (dritte, curve, lunghe): il motore PAL (Placement Algorithm) lavora una sola volta sull'intera estensione, e il ritaglio successivo non altera in alcun modo il testo già posizionato — taglia semplicemente i pixel, non rielabora il layout.

### 18.3 QgsMapRendererSequentialJob al posto di CustomPainterJob

Il render dell'estensione unica utilizza QgsMapRendererSequentialJob anziché QgsMapRendererCustomPainterJob. La differenza sostanziale è che i layer vengono renderizzati uno alla volta nello stesso contesto, anziché in parallelo su thread separati per layer — eliminando la contesa tra thread sulle risorse condivise del motore di rendering che causava il blocco della canvas. L'API è anche più semplice: l'immagine arriva direttamente da renderedImage(), senza gestione manuale di QPainter.

```
job = QgsMapRendererSequentialJob(settings)
job.start()
job.waitForFinished()

errors = job.errors()
if errors:
    for err in errors:
        feedback.pushWarning(f'Errore layer "{err.layerId}": {err.message}')

full_img = job.renderedImage().copy()  # copia indipendente dal job
```

Il controllo job.errors() è una diagnostica aggiuntiva introdotta in questa fase: espone nel log eventuali errori di rendering per singolo layer, utile per individuare problemi di dati o di stile senza dover ispezionare manualmente ogni layer.

### 18.4 Esecuzione forzata sul thread GUI principale

Per default QGIS esegue gli algoritmi Processing su un thread di lavoro separato dal thread GUI principale. Il metodo flags() viene sovrascritto per dichiarare esplicitamente FlagNoThreading, forzando l'esecuzione sincrona sul thread GUI — comportamento corretto per algoritmi che eseguono rendering diretto:

```
def flags(self):
    return super().flags() | QgsProcessingAlgorithm.FlagNoThreading
```

Va precisato che questo intervento, da solo, non era sufficiente a risolvere il blocco della canvas: il problema reale riguardava i thread interni spawnati da CustomPainterJob per il rendering dei singoli layer, non il thread su cui gira processAlgorithm(). La combinazione di FlagNoThreading con QgsMapRendererSequentialJob (18.3) è la soluzione completa: il primo garantisce un contesto di esecuzione prevedibile, il secondo elimina la causa di fondo del conflitto tra thread.

### 18.5 Smaltimento esplicito delle risorse tra livelli

Per garantire che ogni livello di scala parta da uno stato pulito, gli oggetti di rendering vengono esplicitamente distrutti al termine di ciascun livello, invece di affidarsi al solo garbage collector automatico di Python — che potrebbe ritardare la distruzione degli oggetti C++ sottostanti fino a dopo l'avvio del livello successivo:

```
del job
gc.collect()
# ... rendering tile ...
del full_img, settings
gc.collect()
```

### 18.6 Parametri divenuti strutturalmente superflui

Con l'architettura a estensione unica, due parametri della versione precedente sono stati rimossi perché il problema che cercavano di risolvere non può più presentarsi: LABEL_BUFFER_PX (il buffer perimetrale per evitare il troncamento di etichette ai bordi tile) e GLOBAL_LABEL_LAYERS (la selezione dei layer da trattare con il render globale separato per le etichette curve). Non esistono più confini interni tra render: il motore PAL lavora sempre sull'intera estensione, quindi non c'è nulla da bufferizzare né da isolare in un passaggio separato.

---

## 19. Estensione da layer di riferimento

Una revisione indipendente ha riguardato il modo in cui lo script determina l'area geografica da renderizzare, sostituendo l'interazione diretta con la canvas di QGIS con un meccanismo che non dipende in alcun modo dall'interfaccia grafica.

### 19.1 Il problema dell'impostazione di scala sulla canvas

Una versione intermedia dello script impostava automaticamente la scala della canvas a 1:600.000 tramite canvas.zoomScale() e leggeva l'estensione risultante, per evitare all'utente la selezione manuale delle coordinate. Sebbene la causa principale del blocco della canvas si sia poi rivelata essere CustomPainterJob (capitolo 18.1), l'approccio basato su zoomScale() restava comunque fragile per altri motivi: richiede l'oggetto iface, non disponibile in esecuzione headless (ad esempio tramite qgis_process da riga di comando), e introduce una dipendenza dall'interfaccia grafica non necessaria per un'operazione concettualmente semplice come la determinazione di un'estensione geografica.

### 19.2 Soluzione: selezione di un layer di riferimento

Il meccanismo adottato sostituisce interamente l'interazione con la canvas con un parametro QgsProcessingParameterMapLayer: l'utente seleziona un layer del progetto (tipicamente un confine amministrativo o una maschera dedicata), e la sua extent() diventa l'area di rendering:

```
self.addParameter(QgsProcessingParameterMapLayer(
    self.EXTENT_LAYER,
    'Layer di riferimento per l\'estensione (opzionale)',
    optional=True
))

extent_layer = self.parameterAsLayer(
    parameters, self.EXTENT_LAYER, context)
if extent_layer is not None:
    ext     = extent_layer.extent()
    ext_crs = extent_layer.crs()
else:
    # ricade sul parametro Estensione manuale
    ext     = self.parameterAsExtent(parameters, self.EXTENT, context)
    ext_crs = self.parameterAsExtentCrs(parameters, self.EXTENT, context)
```

Questo elimina ogni dipendenza da iface e da canvas, rendendo lo script eseguibile identicamente da dialogo interattivo, da modello del Graphical Modeler o da riga di comando headless.

---

## 20. Diagnostica e correzione della scala-dipendenza delle etichette

Durante la messa a punto è emerso che alcune etichette risultavano assenti a scale specifiche pur essendo i layer correttamente inclusi nel rendering. La diagnosi ha richiesto di distinguere due meccanismi di QGIS concettualmente separati, entrambi basati sulla scala ma indipendenti l'uno dall'altro.

### 20.1 Due controlli di scala distinti in QGIS

Il primo meccanismo è la visibilità del layer per scala (hasScaleBasedVisibility / isInScaleRange), già gestito da get_layers_at_scale fin dalle prime versioni dello script: se il layer è fuori range, l'intero layer — simbologia ed etichette insieme — viene escluso dal rendering.

Il secondo meccanismo, distinto e indipendente, è la scala-dipendenza configurata dentro le proprietà di etichettatura del layer (scheda Rendering del pannello Etichette in QGIS): un intervallo di scala che limita la sola visibilità delle etichette, lasciando la simbologia del layer sempre presente. Se questo intervallo esclude la scala corrente, il layer appare regolarmente sulla mappa ma privo di testo — esattamente il sintomo osservato, e indistinguibile a prima vista da altre possibili cause.

### 20.2 relax_label_scale_visibility()

Questa funzione individua, per ciascun layer e per ciascuna regola di un'eventuale etichettatura basata su regole (QgsRuleBasedLabeling, attraversata ricorsivamente), se la scala-dipendenza delle etichette esclude la scala corrente; in tal caso la disattiva temporaneamente per la durata del render, ripristinandola subito dopo in un blocco finally:

```
def relax_label_scale_visibility(layers_list, feedback, scala):
    saved = []
    for layer in layers_list:
        if not isinstance(layer, QgsVectorLayer):
            continue  # i layer raster non hanno etichettatura
        labeling = layer.labeling()
        if labeling is None:
            continue
        for rule, settings in _iter_label_settings(labeling):
            if not settings.scaleVisibility:
                continue
            smin, smax = settings.minimumScale, settings.maximumScale
            in_range = not ((smin and scala > smin) or
                            (smax and scala < smax))
            feedback.pushInfo(f'[label-scale] {layer.name()}: 
                range 1:{smax:.0f}-1:{smin:.0f}  
                {"dentro" if in_range else "FUORI"} a 1:{scala:,}')
            if not in_range:
                settings.scaleVisibility = False
                # ... applica e registra per il ripristino ...
```

Un bug iniziale in questa funzione tentava di chiamare layer.labeling() indiscriminatamente su tutti i layer, inclusi i raster (ad esempio il DEM del progetto), che non possiedono questo metodo — causando un AttributeError che interrompeva l'esecuzione. La correzione, riportata nel codice sopra, antepone un controllo isinstance(layer, QgsVectorLayer).

### 20.3 diagnose_label_size_expression() e il caveat sui campi feature

Oltre alla scala-dipendenza on/off, le etichette possono avere una dimensione del font controllata da un'espressione data-defined basata su @map_scale, capace di restituire 0 — etichetta invisibile — senza che alcun flag lo segnali. Questa funzione valuta l'espressione usando il contesto del render corrente, riportando il valore calcolato:

```
dd = settings_obj.dataDefinedProperties()
prop = dd.property(_PAL_PROPS.Size)
if prop is not None and prop.isActive():
    val, ok = prop.value(expr_context, None)
```

Una prima versione di questa diagnostica ha prodotto un risultato fuorviante per il layer places: l'espressione di quel layer dipende dal campo population, non da @map_scale, e valutandola senza una feature concreta nel contesto il campo risultava NULL, facendo collassare la CASE al valore di default impostato nella chiamata — letto erroneamente come "size calcolata 0". La correzione individua preventivamente, tramite QgsExpression(expr_str).referencedColumns(), se l'espressione dipende da campi della feature: in tal caso segnala esplicitamente che il valore non è valutabile in modo affidabile senza una feature concreta, invece di presentare un numero potenzialmente errato.

### 20.4 Caso di studio: analisi dello stile del layer places

L'ispezione diretta del file QML di stile del layer places ha permesso di esaminare la configurazione XML sottostante e confermare che non era la causa del problema osservato. Le proprietà rilevanti—"scaleVisibility=1" con range 1:50.000–1:1.000.100 per le etichette, e un'espressione di dimensione font basata sul campo population con valore minimo 8 (mai zero)—mostravano che il layer avrebbe dovuto mostrare le etichette correttamente a tutte e tre le scale target. Questa analisi ha permesso di escludere places e indirizzare la diagnosi verso altri layer e, infine, verso la causa radice descritta nel capitolo seguente.

---

## 21. Causa radice: scala effettiva diversa dalla scala nominale

Le correzioni dei capitoli 18-20, pur necessarie e corrette, non erano sufficienti a spiegare un sintomo specifico: le etichette di alcuni layer risultavano assenti unicamente al livello 1:600.000, pur essendo visibili sia a 1:150.000 sia a 1:300.000, e pur essendo visibili anche nella canvas interattiva di QGIS quando impostata manualmente a quella stessa scala.

### 21.1 Il sintomo nel log diagnostico

L'indagine ha esaminato il log di rendering del livello "600k", individuando una riga rivelatrice:

```
Scala QGIS: 1:883896 | @map_scale=883895.9854014597
```

Il livello nominalmente "1:600.000" stava in realtà renderizzando a scala 1:883.896 — uno scarto del 47%. Questo spiega l'intera classe di sintomi osservati: i limiti di scala-etichette e le espressioni @map_scale-based nel progetto sono tarati su soglie nominali rotonde, ma la scala realmente utilizzata durante il render era ben oltre quelle soglie, mentre nella canvas interattiva la scala mostrata è sempre quella effettiva e corretta.

### 21.2 Causa: risoluzione fissa calibrata per un solo DPI

La causa risiedeva nei parametri RES_150K, RES_300K e RES_600K (risoluzione in m/px), valori fissi calibrati per 96 DPI nella progettazione iniziale dello script. La relazione corretta fra scala, risoluzione e DPI è:

```
scala = res_m × dpi / 0,0254
```

Se il DPI impostato nel dialogo differisce da 96 — nel caso diagnosticato, 150 — la stessa risoluzione fissa in m/px corrisponde a una scala diversa. Con res_m=150 e dpi=150: scala = 150 × 150 / 0,0254 ≈ 885.827, coerente con l'884k osservato a meno di arrotondamenti sui pixel del tile.

### 21.3 Fix: calcolo dinamico della risoluzione

La correzione elimina la possibilità stessa dell'errore: invece di un valore di risoluzione fisso e indipendente dal DPI, res_m viene calcolato a runtime dalla scala nominale e dal DPI corrente, garantendo che la scala effettivamente renderizzata coincida sempre esattamente con quella nominale:

```
M_PER_INCH = 0.0254

for include_key, scala, tag, ovr in [
    (self.INCLUDE_150K, 150_000, '150k', 1),
    (self.INCLUDE_300K, 300_000, '300k', 2),
    (self.INCLUDE_600K, 600_000, '600k', 4),
]:
    if self.parameterAsBool(parameters, include_key, context):
        res_m = scala * self.M_PER_INCH / dpi
        scale_levels.append({'scala': scala, 'res_m': res_m, ...})
```

### 21.4 Semplificazione del dialogo

Conseguenza diretta del fix è la rimozione dei tre parametri numerici di risoluzione (RES_150K, RES_300K, RES_600K) dal dialogo Processing: non essendoci più alcun valore da calibrare manualmente, restano solo le tre caselle di spunta per includere o escludere ciascun livello di scala. Questo elimina strutturalmente un'intera classe di errori di configurazione, oltre a semplificare l'interfaccia.

---

## 22. Margine di tolleranza per la selezione dell'overview

Una volta corretta la scala effettiva di ciascun livello, è emerso un ultimo dettaglio legato non al contenuto dei render ma a come GDAL e QGIS decidono quale overview del COG mostrare durante la navigazione interattiva.

### 22.1 Comportamento di selezione degli overview in GDAL

Quando QGIS visualizza un raster COG, per ogni livello di zoom calcola la risoluzione richiesta dalla vista corrente e seleziona l'overview la cui risoluzione è la più vicina senza essere più grossolana del necessario. Questo meccanismo di selezione non scatta esattamente alla scala nominale di un overview, ma solo dopo che la vista la supera di un margine di tolleranza intrinseco all'algoritmo — nel caso osservato, un passaggio dall'overview 1:300.000 a quello 1:600.000 avveniva navigando fino a circa 1:630.000, anziché esattamente a 1:600.000.

È importante sottolineare che questo non è un difetto della pipeline di rendering: il contenuto del livello "600k" è correttamente renderizzato alla scala 1:600.000 esatta (capitolo 21); il margine riguarda esclusivamente la soglia con cui il visualizzatore decide quando mostrare quel contenuto rispetto al precedente.

### 22.2 Parametro SCALE_MARGIN_PCT

Per compensare questo margine intrinseco, è stato introdotto un parametro che riduce leggermente la sola risoluzione effettiva di ciascun livello — non la scala nominale usata per etichettatura e diagnostica — spostando la soglia di passaggio del visualizzatore più vicino alla scala nominale desiderata:

```
margin_factor = 1.0 - (margin_pct / 100.0)   # default: 5%
res_m = scala * margin_factor * self.M_PER_INCH / dpi
```

La distinzione fra scala nominale (usata da get_layers_at_scale, relax_label_scale_visibility, diagnose_label_size_expression e dai nomi dei file) e risoluzione effettiva (su cui agisce il margine) è intenzionale: tutte le decisioni di visibilità ed etichettatura restano ancorate al valore nominale rotondo (150.000/300.000/600.000), mentre solo il dettaglio fisico del render viene leggermente affinato. La conseguenza secondaria — la scala vera riportata nel log per il render sarà lievemente inferiore al nominale, ad esempio 1:570.000 anziché 1:600.000 con un margine del 5% — rientra ampiamente nei margini di tolleranza già osservati nelle soglie di scala-etichette del progetto (dell'ordine di centinaia di migliaia), e non ne pregiudica la corretta valutazione. Impostando il parametro a 0 il comportamento torna identico a un calcolo senza margine.

---

## 23. Stato architetturale e risultati finali

Il percorso di messa a punto descritto nei capitoli 18-22 ha portato l'algoritmo a un'architettura sostanzialmente più semplice e robusta di quella originaria: un render per livello di scala anziché trentasei, nessun buffer perimetrale, nessun meccanismo a doppio passaggio, nessuna dipendenza dalla canvas di QGIS, e una relazione matematicamente esatta fra scala nominale, risoluzione e DPI.

| Parametro | Valore attuale |
|---|---|
| Render per livello di scala | 1 (intera estensione, poi ritagliata in tile) |
| Livelli di scala | 3, selezionabili indipendentemente (1:150k, 1:300k, 1:600k) |
| Motore di rendering | QgsMapRendererSequentialJob + FlagNoThreading |
| Estensione | da layer di riferimento (QgsProcessingParameterMapLayer) o manuale |
| Risoluzione per livello | calcolata da scala nominale e DPI (mai un valore fisso) |
| Margine overview | percentuale configurabile, default 5%, disattivabile a 0 |
| Buffer etichette ai bordi | non più necessario — nessun confine interno tra render |
| Gestione etichette curve | non più necessaria — un solo render per l'intera estensione |
| Diagnostica scala-etichette | relax_label_scale_visibility + diagnose_label_size_expression |
| Compatibilità | QGIS 3.40 LTR e QGIS 4.0 (Qt5/Qt6) senza modifiche al codice |

Il file COG risultante, generato con questa architettura, mantiene tutte le caratteristiche di ottimizzazione descritte nei capitoli iniziali — compressione DEFLATE/ZSTD, piramidi reali costruite da render effettivi anziché da semplice ricampionamento, dimensione contenuta sotto i 100 MB per l'intera area del Friuli-Venezia Giulia — risolvendo al contempo, in modo strutturale anziché correttivo, tutte le classi di problemi emerse nell'uso quotidiano: blocco della canvas, etichette troncate o duplicate ai confini, etichette assenti per scarto fra scala nominale ed effettiva.

---

## Appendice: dipendenze e versioni

- QGIS 3.40 LTR o QGIS 4.0, Python 3.x integrato

- qgis.core: QgsMapRendererSequentialJob, QgsMapSettings, QgsProject, QgsRectangle, QgsLayerTreeLayer, QgsLayerTreeGroup, QgsExpressionContext, QgsExpressionContextUtils, QgsVectorLayer, QgsRuleBasedLabeling, QgsPalLayerSettings, QgsExpression, QgsCoordinateTransform

- qgis.core (Processing): QgsProcessing, QgsProcessingAlgorithm, QgsProcessingParameterMapLayer, QgsProcessingParameterExtent, QgsProcessingParameterFolderDestination, QgsProcessingParameterNumber, QgsProcessingParameterBoolean, QgsProcessingParameterEnum, QgsProcessingException

- qgis.PyQt.QtCore: QSize, QCoreApplication, QT_VERSION_STR (compatibile Qt5/Qt6 in QGIS 4.0)

- GDAL 3.x in OSGeo4W Shell: gdal_translate, gdalbuildvrt, gdal.Open, gdal.Translate

- Python standard: os, math, gc, statistics (benchmark), time (benchmark)

---

## Bibliografia e riferimenti

### Standard e specifiche

**[1]**  Open Geospatial Consortium (2023). OGC Cloud Optimized GeoTIFF Standard, Version 1.0. OGC Document 21-026. Disponibile: https://docs.ogc.org/is/21-026/21-026.html

**[2]**  cogeotiff/cog-spec (2023). Cloud Optimized GeoTIFF Specification. GitHub Repository. Disponibile: https://github.com/cogeotiff/cog-spec

### Documentazione tecnica GDAL e QGIS

**[3]**  GDAL Development Team (2024). COG — Cloud Optimized GeoTIFF Generator. GDAL Documentation. Disponibile: https://gdal.org/drivers/raster/cog.html

**[4]**  QGIS Development Team (2026). Changelog for QGIS 4.0 — Norrköping. Rilascio: 6 marzo 2026. Disponibile: https://changelog.qgis.org/en/version/4.0/

**[5]**  QGIS Development Team (2025). Plugin migration to be compatible with Qt5 and Qt6. QGIS Wiki. Disponibile: https://github.com/qgis/QGIS/wiki/Plugin-migration-to-be-compatible-with-Qt5-and-Qt6

**[6]**  QGIS Plugin Repository (2026). Migrate Your Plugin to QGIS 4. Disponibile: https://plugins.qgis.org/docs/migrate-qgis4

### Guide e articoli tecnici

**[7]**  Alberti, K. (2021). GeoTIFF Compression Optimization Guide. Kokoalberti.com. Disponibile: https://kokoalberti.com/articles/geotiff-compression-optimization-guide/

**[8]**  Cloud-Native Geo Community (2024). Cloud-Optimized GeoTIFFs — Cloud-Optimized Geospatial Formats Guide. Disponibile: https://guide.cloudnativegeo.org/cloud-optimized-geotiffs/intro.html

**[9]**  cogeo.org (2024). Cloud Optimized GeoTIFF — Ecosystem and Tools Overview. Disponibile: https://cogeo.org/

### Pubblicazioni scientifiche

**[10]** Friess, M. et al. (2023). COMTiles: A Case Study of a Cloud Optimized Tile Archive Format. ISPRS Archives of the Photogrammetry, Remote Sensing and Spatial Information Sciences, Volume XLVIII-4/W7-2023. FOSS4G 2023, Prizren. DOI: 10.5194/isprs-archives-XLVIII-4-W7-2023

**[11]** Milani, E. et al. (2024). A computational framework for processing time-series of earth observation data based on discrete convolution: global-scale historical Landsat cloud-free aggregates at 30 m spatial resolution. PLOS ONE / PMC. Disponibile: https://www.ncbi.nlm.nih.gov/pmc/articles/PMC11624844/

**[12]** Kowalski, D. et al. (2025). Optimizing Cloud-to-GPU Throughput for Deep Learning With Earth Observation Data. arXiv. Disponibile: https://arxiv.org/abs/2506.06235
