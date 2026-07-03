# -*- coding: utf-8 -*-
# ============================================================
# render_multiscale_cog_processing.py
#
# QGIS Processing Algorithm — Render Multi-Scala → COG
# Compatibile QGIS 3.40 LTR (Qt5) e QGIS 4.0 (Qt6)
#
# Architettura: render dell'intera estensione in un'unica
# immagine per livello di scala, poi ritaglio in tile via
# QImage.copy(). Elimina definitivamente qualsiasi problema
# di etichette ai bordi tile (curve, lunghe, ecc.) perché
# il motore PAL lavora una sola volta sull'intera estensione.
# Nessuna manipolazione di renderer o opacity dei layer.
# ============================================================

import os, math, gc
from qgis.core import (
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingParameterFolderDestination,
    QgsProcessingParameterExtent,
    QgsProcessingParameterMapLayer,
    QgsProcessingParameterNumber,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterEnum,
    QgsProcessingException,
    QgsRectangle,
    QgsProject,
    QgsLayerTreeLayer,
    QgsLayerTreeGroup,
    QgsExpressionContext,
    QgsExpressionContextUtils,
    QgsMapRendererSequentialJob,
    QgsMapSettings,
    QgsCoordinateTransform,
    QgsRuleBasedLabeling,
    QgsVectorLayer,
    QgsPalLayerSettings,
)
from qgis.PyQt.QtCore import QSize, QCoreApplication, QT_VERSION_STR

# ── Compatibilità Qt5 / Qt6 ──────────────────────────────────
IS_QT6 = int(QT_VERSION_STR.split('.')[0]) >= 6
_FLAGS = QgsMapSettings.Flag if IS_QT6 else QgsMapSettings
_PAL_PROPS = QgsPalLayerSettings.Property if IS_QT6 else QgsPalLayerSettings


class RenderMultiscaleCOG(QgsProcessingAlgorithm):

    # ── Nomi parametri ───────────────────────────────────────
    OUTPUT_DIR     = 'OUTPUT_DIR'
    EXTENT_LAYER   = 'EXTENT_LAYER'
    EXTENT         = 'EXTENT'
    DPI            = 'DPI'
    TILE_COLS      = 'TILE_COLS'
    TILE_ROWS      = 'TILE_ROWS'
    INCLUDE_150K   = 'INCLUDE_150K'
    INCLUDE_300K   = 'INCLUDE_300K'
    INCLUDE_600K   = 'INCLUDE_600K'
    SCALE_MARGIN_PCT = 'SCALE_MARGIN_PCT'
    COMPRESSION    = 'COMPRESSION'
    COMP_LEVEL     = 'COMP_LEVEL'

    # Conversione scala ↔ risoluzione: 1 pollice = 0.0254 m.
    # res_m = scala × MM_PER_INCH / dpi mantiene la scala EFFETTIVA
    # renderizzata sempre identica alla scala nominale (150k/300k/
    # 600k), qualunque sia il DPI scelto — invece di un res_m fisso
    # calibrato per un solo DPI, che produce una scala reale diversa
    # da quella nominale se il DPI viene cambiato (causa di etichette
    # mancanti: i limiti di scala nel progetto sono tarati su valori
    # nominali rotondi, non sulla scala effettiva derivata).
    M_PER_INCH = 0.0254

    DPI_REFERENCE = 96.0
    # Limite sicurezza sul lato maggiore del render completo (px).
    # A 96 dpi, 1:150k FVG → 11130×6018 px (~268 MB): ampiamente
    # sotto il limite. A 150 dpi → 17391×9403 px (~654 MB): ok.
    MAX_FULL_PX = 25_000

    # ── Metadati algoritmo ────────────────────────────────────
    def name(self):
        return 'render_multiscale_cog'

    def displayName(self):
        return 'Render Multi-Scala → COG'

    def group(self):
        return 'GISDIS FVG'

    def groupId(self):
        return 'gisdis_fvg'

    def flags(self):
        # ── FIX 1: esecuzione forzata sul thread GUI ───────────
        # Per default QGIS esegue gli algoritmi Processing su un
        # thread di lavoro separato dal thread GUI principale.
        # FlagNoThreading forza l'esecuzione sincrona sul thread GUI,
        # comportamento corretto/raccomandato per algoritmi che fanno
        # rendering custom diretto. Da solo non è sufficiente: il vero
        # problema (vedi processAlgorithm — uso di
        # QgsMapRendererSequentialJob al posto di CustomPainterJob)
        # riguarda i thread interni spawnati dal job di rendering
        # stesso per processare i singoli layer, non il thread su cui
        # gira processAlgorithm().
        return (super().flags()
                | QgsProcessingAlgorithm.FlagNoThreading)

    def shortHelpString(self):
        return (
            '<b>Render Multi-Scala → Cloud Optimized GeoTIFF</b><br><br>'
            'Renderizza il progetto QGIS a tre scale rispettando '
            'visibilità del pannello Layer, scala-dipendenza e '
            'data-defined override (inclusa @map_scale).<br><br>'
            'Produce:<ul>'
            '<li>Tile PNG per ogni livello di scala</li>'
            '<li><b>build_cog.bat</b> — georiferimento + VRT + GeoTIFF</li>'
            '<li><b>inject_ovr.py</b> — iniezione overview + COG finale</li>'
            '</ul>'
            'Eseguire <b>build_cog.bat</b> in OSGeo4W Shell al termine.'
            '<br><br>'
            '<i>Architettura:</i> ogni livello di scala viene renderizzato '
            'in un\'<b>unica immagine</b> sull\'intera estensione, poi '
            'ritagliata in tile. Il motore di etichettatura PAL lavora '
            'una sola volta: nessun troncamento né duplicazione di '
            'etichette ai bordi tile, indipendentemente dalla loro forma '
            '(dritte, curve, lunghe).<br><br>'
            '<i>Estensione:</i> selezionare un layer di riferimento '
            'tramite il parametro dedicato — la sua extent() diventa '
            'l\'area di rendering. In alternativa, lasciare vuoto il '
            'layer di riferimento e specificare l\'estensione '
            'manualmente nel campo Estensione.<br><br>'
            '<i>Scala e DPI:</i> la risoluzione (m/px) di ciascun '
            'livello viene calcolata automaticamente dalla scala '
            'nominale (150k/300k/600k) e dal DPI scelto, secondo '
            'scala = res_m × dpi / 0,0254. Questo garantisce che la '
            'scala EFFETTIVA renderizzata (quella usata per valutare '
            '@map_scale e i limiti di scala-etichette del progetto) '
            'coincida sempre esattamente con quella nominale, '
            'qualunque DPI venga scelto — evitando lo scarto che si '
            'avrebbe con una risoluzione fissa calibrata per un solo '
            'DPI di riferimento.<br><br>'
            '<i>Margine scala-overview:</i> GDAL/QGIS non passano a '
            'mostrare un overview più grossolano esattamente alla sua '
            'scala nominale, ma solo dopo averla superata di un '
            'margine di tolleranza (tipicamente qualche punto '
            'percentuale). Il parametro dedicato riduce la sola '
            'risoluzione effettiva (non la scala nominale usata per '
            'etichettatura e diagnostica) di questa percentuale, '
            'spostando la soglia di passaggio più vicino alla scala '
            'nominale desiderata. Impostare a 0 per disattivare.'
            '<br><br>'
            '<i>Nota tecnica:</i> il rendering usa '
            'QgsMapRendererSequentialJob (layer renderizzati uno alla '
            'volta nello stesso contesto, anziché in parallelo su '
            'thread separati) con distruzione esplicita degli oggetti '
            'di rendering tra un livello di scala e il successivo. '
            'Questo previene sia etichette mancanti in alcuni livelli '
            'sia blocchi della canvas dopo l\'esecuzione, entrambi '
            'causati da contesa tra thread di rendering su risorse '
            'condivise (cache glyph/font del labeling engine).'
        )

    def createInstance(self):
        return RenderMultiscaleCOG()

    def tr(self, string):
        return QCoreApplication.translate('RenderMultiscaleCOG', string)

    # ── Definizione parametri ─────────────────────────────────
    def initAlgorithm(self, config=None):

        self.addParameter(QgsProcessingParameterFolderDestination(
            self.OUTPUT_DIR,
            'Cartella di output',
            defaultValue=r'C:\temp\cog_fvg'
        ))

        self.addParameter(QgsProcessingParameterMapLayer(
            self.EXTENT_LAYER,
            'Layer di riferimento per l\'estensione '
            '(opzionale — usa la sua extent() come area di rendering)',
            optional=True
        ))

        self.addParameter(QgsProcessingParameterExtent(
            self.EXTENT,
            'Estensione area di rendering '
            '(usata solo se nessun layer di riferimento è selezionato)',
            optional=True
        ))

        self.addParameter(QgsProcessingParameterNumber(
            self.DPI, 'DPI di rendering',
            type=QgsProcessingParameterNumber.Integer,
            defaultValue=96, minValue=72, maxValue=300
        ))

        self.addParameter(QgsProcessingParameterNumber(
            self.TILE_COLS, 'Colonne griglia tile',
            type=QgsProcessingParameterNumber.Integer,
            defaultValue=6, minValue=1, maxValue=20
        ))
        self.addParameter(QgsProcessingParameterNumber(
            self.TILE_ROWS, 'Righe griglia tile',
            type=QgsProcessingParameterNumber.Integer,
            defaultValue=6, minValue=1, maxValue=20
        ))

        # ── Livelli di scala ──────────────────────────────────
        # La risoluzione (m/px) per ciascun livello viene calcolata
        # automaticamente da scala nominale e DPI — vedi M_PER_INCH.
        # Questo garantisce che la scala EFFETTIVA renderizzata
        # coincida sempre esattamente con quella nominale indicata,
        # qualunque sia il DPI scelto sopra.
        self.addParameter(QgsProcessingParameterBoolean(
            self.INCLUDE_150K,
            'Includi scala 1:150.000  [livello base COG]',
            defaultValue=True
        ))
        self.addParameter(QgsProcessingParameterBoolean(
            self.INCLUDE_300K,
            'Includi scala 1:300.000  [overview 2×]',
            defaultValue=True
        ))
        self.addParameter(QgsProcessingParameterBoolean(
            self.INCLUDE_600K,
            'Includi scala 1:600.000  [overview 4×]',
            defaultValue=True
        ))

        self.addParameter(QgsProcessingParameterNumber(
            self.SCALE_MARGIN_PCT,
            'Margine di tolleranza scala-overview (%, in difetto)',
            type=QgsProcessingParameterNumber.Double,
            defaultValue=5.0, minValue=0.0, maxValue=20.0
        ))

        self.addParameter(QgsProcessingParameterEnum(
            self.COMPRESSION,
            'Algoritmo di compressione COG',
            options=['DEFLATE  (compatibilità universale)',
                     'ZSTD     (decompressione più veloce)'],
            defaultValue=0
        ))
        self.addParameter(QgsProcessingParameterNumber(
            self.COMP_LEVEL,
            'Livello di compressione  (DEFLATE 1–9 / ZSTD 1–22)',
            type=QgsProcessingParameterNumber.Integer,
            defaultValue=9, minValue=1, maxValue=22
        ))

    # ── Logica principale ─────────────────────────────────────
    def processAlgorithm(self, parameters, context, feedback):

        # ── Lettura parametri ──────────────────────────────────
        output_dir = self.parameterAsString(parameters, self.OUTPUT_DIR, context)
        dpi        = self.parameterAsInt(parameters, self.DPI, context)
        tile_cols  = self.parameterAsInt(parameters, self.TILE_COLS, context)
        tile_rows  = self.parameterAsInt(parameters, self.TILE_ROWS, context)
        comp_idx   = self.parameterAsEnum(parameters, self.COMPRESSION, context)
        comp_level = self.parameterAsInt(parameters, self.COMP_LEVEL, context)
        margin_pct = self.parameterAsDouble(
            parameters, self.SCALE_MARGIN_PCT, context)

        compression = ['DEFLATE', 'ZSTD'][comp_idx]

        # ── SR del progetto ────────────────────────────────────
        proj_crs = QgsProject.instance().crs()
        epsg     = proj_crs.authid()

        # ── Estensione: da layer di riferimento o manuale ─────
        # Nessuna interazione con la canvas in nessun caso: evita
        # qualsiasi rischio di interferenza con il job di rendering
        # interno della canvas (causa nota di blocco di zoom/F5 con
        # l'approccio precedente basato su zoomScale()).
        extent_layer = self.parameterAsLayer(
            parameters, self.EXTENT_LAYER, context)

        if extent_layer is not None:
            ext     = extent_layer.extent()
            ext_crs = extent_layer.crs()
            feedback.pushInfo(
                f'Estensione dal layer di riferimento: '
                f'{extent_layer.name()}')
            feedback.pushInfo(
                f'  [{ext.xMinimum():.0f}, {ext.yMinimum():.0f}, '
                f'{ext.xMaximum():.0f}, {ext.yMaximum():.0f}]  '
                f'({ext_crs.authid()})')
        else:
            ext     = self.parameterAsExtent(parameters, self.EXTENT, context)
            ext_crs = self.parameterAsExtentCrs(parameters, self.EXTENT, context)
            if ext is None or ext.isEmpty():
                raise QgsProcessingException(
                    'Estensione non specificata. Selezionare un layer di '
                    'riferimento oppure compilare manualmente il campo '
                    'Estensione.')
            feedback.pushInfo('Estensione: letta dal parametro manuale')

        if ext_crs != proj_crs:
            xform = QgsCoordinateTransform(ext_crs, proj_crs,
                                           QgsProject.instance())
            ext = xform.transformBoundingBox(ext)

        EX_MIN, EY_MIN = ext.xMinimum(), ext.yMinimum()
        EX_MAX, EY_MAX = ext.xMaximum(), ext.yMaximum()

        # ── Lista livelli di scala attivi ─────────────────────
        # res_m calcolato da scala/dpi (vedi M_PER_INCH sopra): la
        # scala effettiva renderizzata coinciderà sempre esattamente
        # con la scala nominale, indipendentemente dal DPI scelto.
        #
        # MARGIN_PCT applica un margine "in difetto" alla sola
        # risoluzione (res_m), NON alla scala nominale usata per
        # etichettatura/diagnostica/nomi file. Una risoluzione
        # leggermente più fine di quella nominale sposta la soglia
        # con cui GDAL seleziona questo overview in visualizzazione
        # più vicino alla scala nominale desiderata, compensando il
        # margine di tolleranza con cui GDAL stesso decide quando
        # passare a un overview più grossolano (tipicamente qualche
        # punto percentuale oltre la risoluzione nominale).
        margin_factor = 1.0 - (margin_pct / 100.0)
        feedback.pushInfo(
            f'Margine scala-overview: {margin_pct:.1f}% '
            f'(fattore risoluzione {margin_factor:.4f})')

        scale_levels = []
        for include_key, scala, tag, ovr in [
            (self.INCLUDE_150K, 150_000, '150k', 1),
            (self.INCLUDE_300K, 300_000, '300k', 2),
            (self.INCLUDE_600K, 600_000, '600k', 4),
        ]:
            if self.parameterAsBool(parameters, include_key, context):
                res_m = scala * margin_factor * self.M_PER_INCH / dpi
                scale_levels.append({
                    'scala': scala,
                    'res_m': res_m,
                    'tag'  : tag,
                    'ovr'  : ovr,
                })

        if not scale_levels:
            raise QgsProcessingException(
                'Selezionare almeno un livello di scala.')

        os.makedirs(output_dir, exist_ok=True)
        root = QgsProject.instance().layerTreeRoot()

        feedback.pushInfo(
            f'SR progetto    : {epsg}')
        feedback.pushInfo(
            f'Compatibilità  : '
            f'{"Qt6 / QGIS 4.x" if IS_QT6 else "Qt5 / QGIS 3.x"}')

        # ── Utility: layer tree con visibilità e scala ────────
        def get_layers_at_scale(scala):
            """
            Percorre il layer tree rispettando:
              - itemVisibilityChecked() → spunta pannello Layer
              - isInScaleRange(scala)   → visibilità per scala
            """
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

        # ── Utility: contesto espressioni con @map_scale ──────
        def make_expr_context(settings):
            ctx = QgsExpressionContext()
            ctx.appendScope(QgsExpressionContextUtils.globalScope())
            ctx.appendScope(QgsExpressionContextUtils.projectScope(
                QgsProject.instance()))
            ctx.appendScope(
                QgsExpressionContextUtils.mapSettingsScope(settings))
            return ctx

        # ── Utility: scala-dipendenza delle SOLE etichette ─────
        # QGIS distingue due controlli di scala separati:
        #   1) Visibilità del layer (hasScaleBasedVisibility /
        #      isInScaleRange) — già gestito da get_layers_at_scale.
        #   2) Visibilità delle ETICHETTE, configurata dentro le
        #      proprietà di etichettatura (scheda Rendering →
        #      "Scale dependent visibility"), indipendente dal
        #      controllo 1). Se attiva e limitata a un intervallo
        #      che non copre 300k/600k, il layer resta visibile ma
        #      le sue etichette no — esattamente il sintomo osservato.
        # Le funzioni seguenti individuano questo controllo (anche
        # dentro regole di un QgsRuleBasedLabeling, ricorsivamente)
        # e lo disattivano temporaneamente per la durata del render
        # di ciascun livello, ripristinandolo subito dopo.

        def _iter_label_settings(labeling):
            """
            Restituisce coppie (rule_or_None, settings) per ogni
            QgsPalLayerSettings raggiungibile dall'oggetto labeling
            del layer — un solo elemento per etichettatura "semplice",
            uno per ciascuna regola (ricorsivamente) per etichettatura
            "basata su regole".
            """
            if labeling is None:
                return
            if isinstance(labeling, QgsRuleBasedLabeling):
                def _walk(rule):
                    s = rule.settings()
                    if s is not None:
                        yield (rule, s)
                    for child in rule.children():
                        yield from _walk(child)
                yield from _walk(labeling.rootRule())
            else:
                try:
                    s = labeling.settings()
                    if s is not None:
                        yield (None, s)
                except Exception:
                    pass

        def relax_label_scale_visibility(layers_list, feedback, scala):
            """
            Per ciascun layer, individua se la scala-dipendenza delle
            etichette (separata da quella del layer) esclude la scala
            corrente, e se sì la disattiva temporaneamente. Registra
            lo stato originale per il ripristino. Logga sempre cosa
            trova, anche quando non interviene, per diagnostica.
            """
            saved = []  # lista di (layer, rule_or_None, was_scale_vis_on)
            for layer in layers_list:
                # Solo i layer vettoriali hanno etichettatura — i raster
                # (es. DEM) non possiedono il metodo .labeling().
                if not isinstance(layer, QgsVectorLayer):
                    continue
                labeling = layer.labeling()
                if labeling is None:
                    continue
                for rule, settings in _iter_label_settings(labeling):
                    try:
                        is_on = bool(settings.scaleVisibility)
                    except AttributeError:
                        continue  # API non disponibile in questa versione
                    if not is_on:
                        continue
                    try:
                        smin = settings.minimumScale
                        smax = settings.maximumScale
                    except AttributeError:
                        smin = smax = None
                    in_range = True
                    if smin and scala > smin:
                        in_range = False
                    if smax and scala < smax:
                        in_range = False
                    label_target = (f'{layer.name()} / regola "{rule.label()}"'
                                     if rule is not None else layer.name())
                    feedback.pushInfo(
                        f'     [label-scale] {label_target}: '
                        f'range etichette 1:{smax:.0f}–1:{smin:.0f}  '
                        f'{"FUORI" if not in_range else "dentro"} range '
                        f'a 1:{scala:,}')
                    if not in_range:
                        settings.scaleVisibility = False
                        if rule is not None:
                            rule.setSettings(settings)
                        else:
                            labeling.setSettings(settings)
                        saved.append((layer, rule, True))
            return saved

        def restore_label_scale_visibility(saved, feedback):
            """Ripristina lo stato originale salvato da relax_label_scale_visibility."""
            for layer, rule, _ in saved:
                labeling = layer.labeling()
                if labeling is None:
                    continue
                try:
                    if rule is not None:
                        s = rule.settings()
                        s.scaleVisibility = True
                        rule.setSettings(s)
                    else:
                        s = labeling.settings()
                        s.scaleVisibility = True
                        labeling.setSettings(s)
                except Exception as e:
                    feedback.pushWarning(
                        f'   ⚠ Ripristino scala-etichette fallito per '
                        f'{layer.name()}: {e}')

        def diagnose_label_size_expression(layers_list, expr_context,
                                            feedback, scala):
            """
            Valuta l'eventuale override data-defined sulla dimensione
            del font delle etichette, usando il CONTESTO ESPRESSIONI
            del render corrente (quindi con @map_scale già al valore
            target). A differenza della scala-visibilità (un semplice
            flag on/off), qui l'etichetta può esistere ed essere
            "visibile" per QGIS ma avere dimensione calcolata 0 per
            via di una CASE basata su @map_scale — invisibile di
            fatto, senza che alcun flag lo segnali. Solo diagnostica:
            non modifica nulla, riporta il valore calcolato per
            ciascun layer/regola.

            ATTENZIONE: se l'espressione referenzia campi della
            feature (es. "population") e non solo variabili di
            contesto come @map_scale, il valore qui calcolato NON è
            rappresentativo — senza una feature concreta i campi
            risultano NULL, e la CASE può collassare al ramo
            ELSE/default in modo fuorviante. Questi casi vengono
            segnalati esplicitamente invece di essere presentati
            come un valore affidabile.
            """
            for layer in layers_list:
                if not isinstance(layer, QgsVectorLayer):
                    continue
                labeling = layer.labeling()
                if labeling is None:
                    continue
                for rule, settings_obj in _iter_label_settings(labeling):
                    try:
                        dd = settings_obj.dataDefinedProperties()
                        prop = dd.property(_PAL_PROPS.Size)
                    except Exception:
                        continue
                    if prop is None or not prop.isActive():
                        continue

                    label_target = (
                        f'{layer.name()} / regola "{rule.label()}"'
                        if rule is not None else layer.name())

                    # Verifica se l'espressione dipende da campi della
                    # feature (non solo da variabili di contesto) —
                    # in tal caso il valore valutato qui non è affidabile.
                    depends_on_fields = False
                    try:
                        expr_str = prop.expressionString()
                        if expr_str:
                            from qgis.core import QgsExpression
                            cols = QgsExpression(expr_str).referencedColumns()
                            depends_on_fields = len(cols) > 0
                    except Exception:
                        pass

                    if depends_on_fields:
                        feedback.pushInfo(
                            f'     [label-size] {label_target}: '
                            f'espressione dipende da campi feature '
                            f'({", ".join(sorted(cols))}) — valore non '
                            f'valutabile in modo affidabile senza una '
                            f'feature concreta, ignorato')
                        continue

                    try:
                        val, ok = prop.value(expr_context, None)
                    except Exception as e:
                        feedback.pushInfo(
                            f'     [label-size] {label_target}: '
                            f'errore valutazione espressione — {e}')
                        continue
                    if not ok or val is None:
                        feedback.pushInfo(
                            f'     [label-size] {label_target}: '
                            f'valutazione non riuscita (ok={ok})')
                        continue
                    flag = (' ⚠ SIZE=0 → ETICHETTA INVISIBILE'
                            if val == 0 else '')
                    feedback.pushInfo(
                        f'     [label-size] {label_target}: '
                        f'size calcolata={val} a 1:{scala:,}{flag}')

        # ── Rendering per livello di scala ────────────────────
        total_levels = len(scale_levels)
        all_records  = {}

        for lvl_idx, lvl in enumerate(scale_levels):
            if feedback.isCanceled():
                break

            scala = lvl['scala']
            res_m = lvl['res_m']
            tag   = lvl['tag']

            layers = get_layers_at_scale(scala)

            feedback.pushInfo(f'\n══ Scala 1:{scala:,}  ({res_m} m/px) ══')
            feedback.pushInfo(f'   Layer visibili ({len(layers)}):')
            for l in layers:
                feedback.pushInfo(f'     · {l.name()}')

            if not layers:
                feedback.pushWarning(
                    f'Nessun layer visibile a 1:{scala:,} — livello saltato.')
                continue

            # ── Diagnostica + override scala-dipendenza etichette ──
            # Individua e disattiva temporaneamente eventuali limiti di
            # scala configurati SOLO per le etichette (separati dalla
            # visibilità del layer) che escluderebbero la scala corrente.
            # Sempre ripristinato nel blocco finally più sotto.
            label_scale_saved = relax_label_scale_visibility(
                layers, feedback, scala)
            if label_scale_saved:
                feedback.pushInfo(
                    f'   → {len(label_scale_saved)} impostazioni di '
                    f'scala-etichette disattivate temporaneamente')

            # ── Dimensioni del render completo ─────────────────
            tile_w_m  = (EX_MAX - EX_MIN) / tile_cols
            tile_h_m  = (EY_MAX - EY_MIN) / tile_rows
            tile_px_w = math.ceil(tile_w_m / res_m)
            tile_px_h = math.ceil(tile_h_m / res_m)
            full_px_w = tile_px_w * tile_cols
            full_px_h = tile_px_h * tile_rows
            est_mb    = full_px_w * full_px_h * 4 / 1024**2

            # Guardia dimensionale: estensione troppo grande per la scala
            if full_px_w > self.MAX_FULL_PX or full_px_h > self.MAX_FULL_PX:
                raise QgsProcessingException(
                    f'Livello 1:{scala:,} — render completo '
                    f'{full_px_w}×{full_px_h} px supera il limite '
                    f'({self.MAX_FULL_PX} px/lato).\n'
                    f'Causa probabile: estensione troppo grande per '
                    f'{res_m} m/px rispetto al layer/area selezionata.\n'
                    f'Soluzioni: ridurre l\'estensione, aumentare res_m, '
                    f'o deselezionare questo livello.')

            feedback.pushInfo(
                f'   Render completo: {full_px_w}×{full_px_h} px  '
                f'(~{est_mb:.0f} MB)')
            feedback.pushInfo(
                f'   Tile: {tile_px_w}×{tile_px_h} px  '
                f'({tile_cols}×{tile_rows} = {tile_cols*tile_rows} tile)')

            if est_mb > 800:
                feedback.pushWarning(
                    f'   Immagine grande (~{est_mb:.0f} MB) — '
                    f'verifica RAM disponibile')

            # ── Render dell'intera estensione in un'unica immagine ──
            # Il motore PAL lavora una sola volta: nessun troncamento
            # né duplicazione di etichette ai bordi tile.
            #
            # QgsMapRendererSequentialJob (anziché CustomPainterJob)
            # renderizza i layer UNO ALLA VOLTA nello stesso contesto:
            # questo ha risolto il blocco della canvas dopo l'esecuzione
            # (causato dai thread interni di CustomPainterJob). Le
            # etichette mancanti a 300k/600k avevano invece una causa
            # diversa e indipendente — vedi relax_label_scale_visibility
            # sopra: un limite di scala specifico delle etichette
            # (separato dalla visibilità del layer) configurato nel
            # progetto.
            settings = QgsMapSettings()
            settings.setLayers(layers)
            settings.setDestinationCrs(proj_crs)
            settings.setExtent(QgsRectangle(EX_MIN, EY_MIN, EX_MAX, EY_MAX))
            settings.setOutputSize(QSize(full_px_w, full_px_h))
            settings.setOutputDpi(dpi)
            settings.setFlag(_FLAGS.Antialiasing,            True)
            settings.setFlag(_FLAGS.DrawLabeling,             True)
            settings.setFlag(_FLAGS.UseAdvancedEffects,       True)
            settings.setFlag(_FLAGS.UseRenderingOptimization, True)
            settings.setExpressionContext(make_expr_context(settings))

            ms = settings.expressionContext().variable('map_scale')
            feedback.pushInfo(
                f'   Scala QGIS: 1:{settings.scale():.0f}  '
                f'| @map_scale={ms}')

            # ── Diagnostica: dimensione font etichette ──────────
            # Valuta l'eventuale CASE basata su @map_scale che
            # controlla la dimensione del font — può restituire 0
            # (etichetta invisibile) a scale specifiche, indipendente
            # dal flag di scala-visibilità già gestito sopra.
            diagnose_label_size_expression(
                layers, settings.expressionContext(), feedback, scala)

            try:
                job = QgsMapRendererSequentialJob(settings)
                job.start()
                job.waitForFinished()

                errors = job.errors()
                if errors:
                    for err in errors:
                        feedback.pushWarning(
                            f'   ⚠ Errore layer "{err.layerId}": '
                            f'{err.message}')

                full_img = job.renderedImage()
                # Copia esplicita: renderedImage() può restituire un
                # riferimento legato al ciclo di vita del job. Una copia
                # indipendente garantisce che full_img resti valida
                # anche dopo la distruzione di job.
                full_img = full_img.copy()

                # Smaltimento esplicito del job PRIMA di procedere: forza
                # la distruzione immediata degli oggetti C++ sottostanti
                # invece di affidarsi al garbage collector Python, che
                # potrebbe ritardarla fino al livello di scala successivo.
                del job
                gc.collect()

                # Mantiene la UI di QGIS reattiva durante l'esecuzione
                QCoreApplication.processEvents()
                feedback.pushInfo('   ✓ Render completo eseguito')

                # ── Ritaglio in tile via QImage.copy() ────────────
                # Nessun rendering aggiuntivo: puro crop dell'immagine.
                tile_dir = os.path.join(output_dir, tag)
                os.makedirs(tile_dir, exist_ok=True)

                records = []
                for row in range(tile_rows):
                    for col in range(tile_cols):
                        if feedback.isCanceled():
                            break

                        # Coordinate geografiche del tile netto
                        xmin = EX_MIN + col     * tile_w_m
                        xmax = EX_MIN + (col+1) * tile_w_m
                        ymin = EY_MAX - (row+1) * tile_h_m
                        ymax = EY_MAX - row     * tile_h_m

                        # Coordinate pixel nell'immagine completa
                        px_x = col * tile_px_w
                        px_y = row * tile_px_h

                        tile_img = full_img.copy(px_x, px_y,
                                                 tile_px_w, tile_px_h)

                        name = f'tile_{row:02d}_{col:02d}.png'
                        path = os.path.join(tile_dir, name)
                        tile_img.save(path)
                        records.append((path, xmin, ymax, xmax, ymin))

                        feedback.pushInfo(
                            f'   ✓ [{row},{col}]  '
                            f'{xmin:.0f},{ymin:.0f}'
                            f'→{xmax:.0f},{ymax:.0f}')
                        QCoreApplication.processEvents()

                all_records[tag] = records
                feedback.pushInfo(
                    f'   Livello {tag}: {len(records)} tile salvati.')

                # ── Smaltimento esplicito a fine livello ──────────
                # Rilascia full_img e settings prima di passare al
                # livello successivo. Garantisce che ogni livello di
                # scala parta da uno stato pulito, senza risorse
                # residue dal livello precedente.
                del full_img, settings
                gc.collect()

            finally:
                # ── Ripristino scala-etichette ─────────────────────
                # Eseguito sempre (successo, annullamento o errore):
                # le impostazioni di scala-dipendenza delle etichette
                # disattivate temporaneamente sopra vengono sempre
                # ripristinate, lasciando il progetto invariato.
                if label_scale_saved:
                    restore_label_scale_visibility(
                        label_scale_saved, feedback)

            # Aggiorna avanzamento per livello completato
            feedback.setProgress(
                int((lvl_idx + 1) / total_levels * 100))

        # ── Generazione build_cog.bat ─────────────────────────
        bat_path    = os.path.join(output_dir, 'build_cog.bat')
        inject_path = os.path.join(output_dir, 'inject_ovr.py')
        base_tag    = scale_levels[0]['tag']
        assembled   = {}

        with open(bat_path, 'w', encoding='utf-8') as f:
            f.write('@echo off\n')
            f.write('echo ================================================\n')
            f.write(f'echo  Pipeline COG multi-scala  [{compression}  L{comp_level}]\n')
            f.write('echo ================================================\n\n')

            step = 1
            for lvl in scale_levels:
                tag = lvl['tag']
                if tag not in all_records:
                    continue
                vrt_path = os.path.join(output_dir, f'merged_{tag}.vrt')
                tif_path = os.path.join(output_dir, f'render_{tag}.tif')
                assembled[tag] = tif_path

                f.write(f'echo [{step}] Georiferimento tile {tag}...\n')
                georef_list = []
                for tp, x0, y1, x1, y0 in all_records[tag]:
                    gp = tp.replace('.png', '_georef.tif')
                    georef_list.append(gp)
                    f.write(
                        f'gdal_translate -of GTiff -a_srs {epsg} '
                        f'-a_ullr {x0:.6f} {y1:.6f} {x1:.6f} {y0:.6f} '
                        f'"{tp}" "{gp}"\n')
                step += 1

                f.write(f'\necho [{step}] VRT + GeoTIFF {tag}...\n')
                f.write(f'gdalbuildvrt "{vrt_path}" '
                        + ' '.join(f'"{p}"' for p in georef_list) + '\n')
                f.write(f'gdal_translate -of GTiff '
                        f'-co COMPRESS={compression} -co PREDICTOR=2 '
                        f'"{vrt_path}" "{tif_path}"\n\n')
                step += 1

            f.write(f'echo [{step}] Iniezione overview + COG finale...\n')
            f.write(f'python "{inject_path}"\n\n')
            f.write('echo.\necho === COMPLETATO ===\npause\n')

        # ── Generazione inject_ovr.py ─────────────────────────
        ovr_lvls    = [l for l in scale_levels
                       if l['ovr'] > 1 and l['tag'] in assembled]
        ovr_factors = [l['ovr'] for l in ovr_lvls]
        ovr_tifs    = [assembled[l['tag']] for l in ovr_lvls]
        base_tif    = assembled.get(base_tag, '')
        cog_out     = os.path.join(
            output_dir,
            f'sfondo_multiscala_{compression.lower()}_cog.tif')

        inject_code = f'''# inject_ovr.py — eseguire da OSGeo4W Shell
from osgeo import gdal
import sys

BASE_TIF    = r"{base_tif}"
COG_OUT     = r"{cog_out}"
OVR_FILES   = {ovr_tifs}
OVR_FACTORS = {ovr_factors}
COMPRESSION = "{compression}"
COMP_LEVEL  = {comp_level}

print("Apertura base:", BASE_TIF)
ds = gdal.Open(BASE_TIF, gdal.GA_Update)
if ds is None:
    sys.exit("ERRORE: impossibile aprire " + BASE_TIF)

print(f"Overview da creare: {{OVR_FACTORS}}")
ds.BuildOverviews("AVERAGE", OVR_FACTORS)

for i, (ovr_file, factor) in enumerate(zip(OVR_FILES, OVR_FACTORS)):
    print(f"  Iniezione overview {{factor}}x da: {{ovr_file}}")
    ovr_ds = gdal.Open(ovr_file)
    if ovr_ds is None:
        print(f"  \\u26a0 skip: {{ovr_file}}")
        continue
    for b in range(1, ds.RasterCount + 1):
        bb  = ds.GetRasterBand(b)
        ovb = bb.GetOverview(i)
        src = ovr_ds.GetRasterBand(b)
        ovb.WriteArray(src.ReadAsArray(
            buf_xsize=ovb.XSize, buf_ysize=ovb.YSize))
    ovr_ds = None
    print(f"    OK {{factor}}x iniettata")

ds.FlushCache()
ds = None

print("\\nCreazione COG:", COG_OUT)
# Il driver COG usa LEVEL (non ZLEVEL/ZSTD_LEVEL del driver GTiff)
gdal.Translate(COG_OUT, BASE_TIF, format="COG",
    creationOptions=[
        f"COMPRESS={{COMPRESSION}}",
        "PREDICTOR=2",
        f"LEVEL={{COMP_LEVEL}}",
        "BLOCKSIZE=512",
        "NUM_THREADS=ALL_CPUS",
        "OVERVIEWS=FORCE_USE_EXISTING",
        f"OVERVIEW_COMPRESS={{COMPRESSION}}",
        "BIGTIFF=IF_NEEDED",
    ])
print("=== COG completato:", COG_OUT, "===")
'''
        with open(inject_path, 'w', encoding='utf-8') as f:
            f.write(inject_code)

        n_tiles = sum(len(v) for v in all_records.values())
        feedback.pushInfo(f'\n✓ Tile prodotti  : {n_tiles}')
        feedback.pushInfo(f'✓ build_cog.bat  : {bat_path}')
        feedback.pushInfo(f'✓ inject_ovr.py  : {inject_path}')
        feedback.pushInfo(
            '→ Apri build_cog.bat in OSGeo4W Shell per completare.')

        return {
            'OUTPUT_DIR'    : output_dir,
            'BAT_SCRIPT'    : bat_path,
            'INJECT_SCRIPT' : inject_path,
        }
