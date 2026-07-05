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

    setupLightbox();
    map.on("load", () => setupPoints(map));

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
            fetch(`/api/point/${props.osm_id}`)
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
            const resp = await fetch(`/api/points?bbox=${bbox}`);
            if (!resp.ok) return;
            const source = map.getSource("points");
            if (source) source.setData(await resp.json());
        } catch (err) {
            console.error("point load failed", err);
        }
    }

    // Curated card built instantly from the point's own properties. Photos come from
    // the local /photos route; onerror drops the <img> for nodes with no cached image.
    function cardHtml(props) {
        const id = props.osm_id;
        const photo = id != null
            ? `<img class="pp-photo" src="/thumbs/${id}" data-full="/photos/${id}" alt="" onerror="this.remove()">`
            : "";
        const rows = [];
        const operator = operatorOf(props);
        if (operator) rows.push(field("Operated by", operator));
        rows.push(field("Made by", manufacturerOf(props)));
        const osm = id != null
            ? `<div class="pp-osm">www.openstreetmap.org/node/${id}</div>`
            : "";
        return `<div class="pp">${photo}<div class="pp-rows">${rows.join("")}</div>`
            + `<div class="pp-details"></div><div class="pp-foot">${osm}</div></div>`;
    }

    // Enriched section (full OSM tags + edit provenance) fetched from /api/point/<id>.
    function detailsHtml(meta) {
        const when = meta.osm_timestamp ? meta.osm_timestamp.slice(0, 10) : "?";
        const by = meta.user ? " by " + escapeHtml(meta.user) : "";
        const prov = `<div class="pp-prov">Last edited ${escapeHtml(when)}${by} · v${meta.version}</div>`;
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
        return s.replace(/[&<>"']/g, (c) => ({
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
