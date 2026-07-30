// Offline MapLibre GL map: vendored PMTiles basemap + a viewport-queried
// GeoParquet point layer (served as GeoJSON from /api/points).
(function () {
    const cfg = window.MAP_CONFIG;

    // Teach MapLibre to read the vendored single-file .pmtiles archive.
    const protocol = new pmtiles.Protocol();
    maplibregl.addProtocol("pmtiles", protocol.tile);

    const map = new maplibregl.Map({
        container: "map",
        style: cfg.styleUrl,
        center: cfg.center,
        zoom: cfg.zoom,
    });

    map.addControl(
        new maplibregl.NavigationControl(),
        "top-right",
    );
    map.addControl(new maplibregl.ScaleControl());

    let currentDeck = null;

    setupLightbox();
    map.on("load", () => setupDecks(map));

    // List the decks, restore the last selection, and build the picker.
    // An empty decks directory leaves a plain basemap.
    async function setupDecks(map) {
        let decks;
        try {
            decks = (await (await fetch("/api/decks")).json()).decks;
        } catch (err) {
            console.error("deck list failed", err);
            return;
        }
        if (!decks.length) return;
        const saved = localStorage.getItem("offline-maps.deck");
        currentDeck = decks.some((d) => d.id === saved) ? saved : decks[0].id;
        document.body.appendChild(deckPicker(map, decks));
        setupPoints(map);
    }

    // One deck shows at a time, like swapping game cartridges: switching decks
    // repoints the single GeoJSON source instead of stacking per-deck layers.
    // Deck names are untrusted data — built as text nodes, never markup.
    function deckPicker(map, decks) {
        const picker = document.createElement("div");
        picker.className = "deck-picker";
        for (const deck of decks) {
            const input = document.createElement("input");
            input.type = "radio";
            input.name = "deck";
            input.value = deck.id;
            input.checked = deck.id === currentDeck;
            input.addEventListener("change", () => switchDeck(map, deck.id));
            const label = document.createElement("label");
            label.appendChild(input);
            label.appendChild(document.createTextNode(deck.name));
            if (deck.count != null) {
                const count = document.createElement("span");
                count.className = "deck-count";
                count.textContent = deck.count.toLocaleString();
                label.appendChild(count);
            }
            picker.appendChild(label);
        }
        return picker;
    }

    function switchDeck(map, deckId) {
        currentDeck = deckId;
        localStorage.setItem("offline-maps.deck", deckId);
        map.getSource("points").setData(emptyCollection());
        loadPointsInView(map);
    }

    function setupPoints(map) {
        map.addSource("points", {
            type: "geojson",
            data: emptyCollection(),
            cluster: true,
            clusterRadius: 50,
            clusterMaxZoom: 14,
        });

        map.addLayer({
            id: "clusters",
            type: "circle",
            source: "points",
            filter: ["has", "point_count"],
            paint: {
                "circle-color": "#4a7fb5",
                "circle-opacity": 0.85,
                "circle-radius": [
                    "step",
                    ["get", "point_count"],
                    14,
                    50, 18,
                    200, 24,
                ],
            },
        });

        map.addLayer({
            id: "cluster-count",
            type: "symbol",
            source: "points",
            filter: ["has", "point_count"],
            layout: {
                "text-field": ["get", "point_count_abbreviated"],
                "text-font": ["Noto Sans Regular"],
                "text-size": 12,
            },
            paint: {
                "text-color": "#ffffff",
            },
        });

        map.addLayer({
            id: "unclustered-point",
            type: "circle",
            source: "points",
            filter: ["!", ["has", "point_count"]],
            paint: {
                "circle-color": "#d1495b",
                "circle-radius": 5,
                "circle-stroke-width": 1,
                "circle-stroke-color": "#ffffff",
            },
        });

        // Cluster click → zoom in to expand it.
        map.on("click", "clusters", (e) => {
            const feature = map.queryRenderedFeatures(e.point, {
                layers: ["clusters"],
            })[0];
            map.getSource("points")
                .getClusterExpansionZoom(feature.properties.cluster_id)
                .then((zoom) => {
                    map.easeTo({
                        center: feature.geometry.coordinates,
                        zoom: zoom,
                    });
                });
        });

        // Point click → deflock-style card: photo + operator/manufacturer, then the
        // full OSM tags + edit provenance filled in lazily from /api/point/<id>.
        map.on("click", "unclustered-point", (e) => {
            const props = e.features[0].properties;
            const popup = new maplibregl.Popup({ maxWidth: "280px" })
                .setLngLat(e.features[0].geometry.coordinates.slice())
                .setHTML(cardHtml(props))
                .addTo(map);
            if (props.osm_id == null) return;
            fetch(`/api/point/${encodeURIComponent(props.osm_id)}`)
                .then((r) => (r.ok ? r.json() : null))
                .then((meta) => {
                    if (!meta) return;
                    const slot = popup.getElement()?.querySelector(".pp-details");
                    if (slot) slot.innerHTML = detailsHtml(meta);
                })
                .catch(() => {});
        });

        for (const layer of ["clusters", "unclustered-point"]) {
            map.on("mouseenter", layer, () => {
                map.getCanvas().style.cursor = "pointer";
            });
            map.on("mouseleave", layer, () => {
                map.getCanvas().style.cursor = "";
            });
        }

        // Reload points for the current viewport whenever the map settles.
        let debounce;
        map.on("moveend", () => {
            clearTimeout(debounce);
            debounce = setTimeout(() => loadPointsInView(map), 150);
        });
        loadPointsInView(map);
    }

    async function loadPointsInView(map) {
        const b = map.getBounds();
        const bbox = [b.getWest(), b.getSouth(), b.getEast(), b.getNorth()].join(",");
        try {
            const resp = await fetch(`/api/decks/${encodeURIComponent(currentDeck)}/points?bbox=${bbox}`);
            if (!resp.ok) return;
            const source = map.getSource("points");
            if (source) source.setData(await resp.json());
        } catch (err) {
            console.error("point load failed", err);
        }
    }

    // Keys whose presence marks a DeFlock-style surveillance point, which gets the
    // curated card; every other deck gets a generic card of its own properties.
    const SURVEILLANCE_KEYS = [
        "brand",
        "manufacturer",
        "operator",
        "surveillance:brand",
        "surveillance:manufacturer",
        "surveillance:operator",
    ];

    // Card built instantly from the point's own properties. Photos come from
    // the local /thumbs route; onerror drops the <img> for points with no cached image.
    // Point properties are untrusted data: escape for HTML, URL-encode for URLs.
    function cardHtml(props) {
        const id = props.osm_id;
        const idUrl = id != null ? escapeHtml(encodeURIComponent(id)) : null;
        const photo = id != null
            ? `<img class="pp-photo" src="/thumbs/${idUrl}" data-full="/photos/${idUrl}" alt="" onerror="this.remove()">`
            : "";
        const title = props.name
            ? `<div class="pp-title">${escapeHtml(props.name)}</div>`
            : "";
        const rows = [];
        if (SURVEILLANCE_KEYS.some((key) => props[key] != null)) {
            const operator = operatorOf(props);
            if (operator) rows.push(field("Operated by", operator));
            rows.push(field("Made by", manufacturerOf(props)));
        } else {
            for (const [key, value] of Object.entries(props)) {
                if (key === "name" || key === "id" || key === "osm_id") continue;
                if (value == null || value === "") continue;
                rows.push(field(key, value));
            }
        }
        const osm = id != null
            ? `<div class="pp-osm">www.openstreetmap.org/node/${escapeHtml(id)}</div>`
            : "";
        return `<div class="pp">${photo}${title}<div class="pp-rows">${rows.join("")}</div>`
            + `<div class="pp-details"></div><div class="pp-foot">${osm}</div></div>`;
    }

    // Enriched section (full OSM tags + edit provenance) fetched from /api/point/<id>.
    function detailsHtml(meta) {
        const when = meta.osm_timestamp ? meta.osm_timestamp.slice(0, 10) : "?";
        const by = meta.user ? " by " + escapeHtml(meta.user) : "";
        const prov = `<div class="pp-prov">Last edited ${escapeHtml(when)}${by} · v${escapeHtml(meta.version)}</div>`;
        const tags = meta.tags || {};
        const n = Object.keys(tags).length;
        const tagList = n
            ? `<details class="pp-tags"><summary>All ${n} OSM tags</summary>${propsTable(tags)}</details>`
            : "";
        return prov + tagList;
    }

    function field(label, value) {
        return `<div class="pp-field"><div class="pp-label">${escapeHtml(label)}</div>`
            + `<div class="pp-value">${escapeHtml(value)}</div></div>`;
    }

    // Manufacturer/operator with DeFlock's fallback + abbreviation behavior.
    function manufacturerOf(p) {
        return p.manufacturer || p["surveillance:manufacturer"] || p.brand
            || p["surveillance:brand"] || "Unknown";
    }

    function operatorOf(p) {
        const operator = p.operator || p["surveillance:operator"];
        if (!operator) return null;
        return operator.replace("Police Department", "PD").replace(/Sheriff'?s Office/, "SO");
    }

    function propsTable(props) {
        const rows = Object.entries(props)
            .map(
                ([k, v]) =>
                    `<tr><td class="k">${escapeHtml(k)}</td><td>${escapeHtml(String(v))}</td></tr>`,
            )
            .join("");
        return `<table class="props">${rows}</table>`;
    }

    function escapeHtml(s) {
        return String(s).replace(/[&<>"']/g, (c) => ({
            "&": "&amp;",
            "<": "&lt;",
            ">": "&gt;",
            '"': "&quot;",
            "'": "&#39;",
        })[c]);
    }

    function emptyCollection() {
        return { type: "FeatureCollection", features: [] };
    }

    // Fullscreen lightbox: click a popup thumbnail to view the full-size photo;
    // click the backdrop or press Escape to dismiss it.
    function setupLightbox() {
        const lightbox = document.createElement("div");
        lightbox.className = "lightbox";
        lightbox.innerHTML = '<button class="lightbox-close" type="button" aria-label="Close">×</button><img class="lightbox-img" alt="">';
        document.body.appendChild(lightbox);
        const img = lightbox.querySelector(".lightbox-img");

        const open = (src) => {
            img.src = src;
            lightbox.classList.add("open");
        };
        const close = () => {
            lightbox.classList.remove("open");
            img.removeAttribute("src");
        };

        document.addEventListener("click", (e) => {
            const thumb = e.target.closest(".pp-photo");
            if (thumb && thumb.dataset.full) {
                open(thumb.dataset.full);
            } else if (e.target === lightbox || e.target.closest(".lightbox-close")) {
                close();
            }
        });
        document.addEventListener("keydown", (e) => {
            if (e.key === "Escape") close();
        });
    }
})();
