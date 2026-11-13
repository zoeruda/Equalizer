//! Colors and styling, in the spirit of Mission Center's monitoring graphs.
pub type Rgb = (f64, f64, f64);

pub const GRAPH_BG: Rgb = (0.098, 0.114, 0.133);
pub const EQ_BLUE: Rgb = (0.17, 0.52, 0.86);
pub const TEXT: Rgb = (0.93, 0.94, 0.96);
pub const DIM: Rgb = (0.60, 0.63, 0.68);

/// One distinct color per band, so each is easy to pick out on the graph.
pub const BANDS: [Rgb; 8] = [
    (0.36, 0.72, 0.98),
    (0.27, 0.85, 0.65),
    (0.70, 0.86, 0.30),
    (0.98, 0.78, 0.25),
    (0.98, 0.55, 0.30),
    (0.97, 0.42, 0.55),
    (0.80, 0.50, 0.97),
    (0.50, 0.58, 0.99),
];

pub fn band_color(i: usize) -> Rgb {
    BANDS[i % BANDS.len()]
}

pub const CSS: &str = r#"
.big-title { font-size: 26px; font-weight: 800; }
.subtitle-right { font-size: 18px; font-weight: 700; }
.caption-dim { font-size: 12.5px; opacity: 0.85; }
.readout-label { font-size: 12.5px; opacity: 0.72; }
.readout-value { font-size: 19px; font-weight: 700; font-feature-settings: "tnum"; }
.spec-key { font-size: 12.5px; opacity: 0.72; }
.spec-value { font-size: 12.5px; }
.section-title { font-size: 12.5px; font-weight: 700; opacity: 0.85; margin-top: 6px; }
.param-label { font-size: 12.5px; opacity: 0.8; }
.param-value { font-size: 12.5px; font-weight: 700; font-feature-settings: "tnum"; }
/* While a value is typed, it keeps its place and its look: the cursor and the selection
   show that it's open, without the box of a text field around it. */
.param-value > stack > text { background: none; border: none; border-radius: 0; box-shadow: none; outline: none; padding: 0; min-height: 0; }
.blurb { font-size: 12.5px; opacity: 0.72; }
.tile-name { font-weight: 700; }
.tile-value { font-size: 12px; opacity: 0.72; font-feature-settings: "tnum"; }
.tile-off { opacity: 0.45; }
.tiles { background: transparent; }
.preset-list { background: transparent; }
.preset-list row { padding: 0; min-height: 0; }
.tiles row { border-radius: 8px; margin: 2px 6px; padding: 6px; }
"#;
