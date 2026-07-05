// Regenerate the vendored basemap style from the official Protomaps theme
// (protomaps-themes-base). Output is a complete, self-contained MapLibre style
// pointing at the locally vendored tiles, glyphs, and sprite — no runtime deps.
//
//   npm i protomaps-themes-base@4.5.0        # one-time
//   node scripts/build-style.mjs [light|dark|white|grayscale|black]
//
// If you switch flavor, also vendor that flavor's sprite + fonts:
//   fonts:  https://protomaps.github.io/basemaps-assets/fonts/<stack>/<range>.pbf
//   sprite: https://protomaps.github.io/basemaps-assets/sprites/v4/<flavor>.{json,png} (+ @2x)
import { writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import theme from "protomaps-themes-base";

const flavor = process.argv[2] || "light";
const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const out = join(root, "src/offline_maps/web/static/styles/basemap.json");

const style = {
  version: 8,
  name: `protomaps-${flavor}`,
  glyphs: "/static/glyphs/{fontstack}/{range}.pbf",
  sprite: `/static/sprites/${flavor}`,
  sources: {
    protomaps: {
      type: "vector",
      url: "pmtiles:///tiles/basemap.pmtiles",
      attribution: "© OpenStreetMap",
    },
  },
  layers: theme("protomaps", flavor),
};
writeFileSync(out, JSON.stringify(style, null, 2) + "\n");
console.log(`wrote ${out} — ${style.layers.length} layers (${flavor})`);
