# Vendored assets

| file | source | licence |
|---|---|---|
| `maplibre-gl-4.7.1.css` | https://cdnjs.cloudflare.com/ajax/libs/maplibre-gl/4.7.1/maplibre-gl.css (the script loads from cdnjs; stylesheets cannot, on the artifact host) | BSD-3-Clause, `MAPLIBRE-LICENSE.txt` |
| `glyphs/*.pbf` | Noto Sans Regular / Italic glyph ranges from https://github.com/protomaps/basemaps-assets (fonts/) | SIL OFL 1.1, `glyphs/OFL.txt` |

Glyph ranges: 0-255 (Latin-1), 256-511 (Latin Extended-A: macrons ā ē ī ō ū), 8192-8447
(punctuation). `webapp.write_static` writes them as base64 text next to the page.
