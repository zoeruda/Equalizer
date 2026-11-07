//! Everything the app remembers: bands, effect settings, presets, and how those
//! settings translate into LSP plugin controls.
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
use std::path::PathBuf;

pub const MAX_BANDS: usize = 32;
pub const GAIN_LIMIT: f64 = 24.0;
pub const SINK_NAME: &str = "equalizer_sink";
pub const SOURCE_NAME: &str = "equalizer_source";

#[derive(Copy, Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub enum FilterKind {
    Bell,
    LowShelf,
    HighShelf,
    LowPass,
    HighPass,
    Notch,
}

impl FilterKind {
    pub const ALL: [FilterKind; 6] = [
        Self::Bell,
        Self::LowShelf,
        Self::HighShelf,
        Self::LowPass,
        Self::HighPass,
        Self::Notch,
    ];

    pub fn label(self) -> &'static str {
        match self {
            Self::Bell => "Bell",
            Self::LowShelf => "Low shelf",
            Self::HighShelf => "High shelf",
            Self::LowPass => "Low pass",
            Self::HighPass => "High pass",
            Self::Notch => "Notch",
        }
    }

    /// The value of LSP's "Filter type" control.
    pub fn lsp_type(self) -> f32 {
        match self {
            Self::Bell => 1.0,
            Self::HighPass => 2.0,
            Self::HighShelf => 3.0,
            Self::LowPass => 4.0,
            Self::LowShelf => 5.0,
            Self::Notch => 6.0,
        }
    }

    pub fn uses_gain(self) -> bool {
        matches!(self, Self::Bell | Self::LowShelf | Self::HighShelf)
    }
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Band {
    pub kind: FilterKind,
    pub freq: f64,
    pub gain: f64,
    pub q: f64,
    pub enabled: bool,
}

impl Band {
    pub fn bell(freq: f64, gain: f64, q: f64) -> Self {
        Self {
            kind: FilterKind::Bell,
            freq,
            gain,
            q,
            enabled: true,
        }
    }
}

/// How a slider's value reaches the plugin.
#[derive(Copy, Clone)]
pub enum Map {
    /// dB in the UI, linear gain in the plugin.
    Gain(&'static str),
    Raw(&'static str),
    /// Compensated loudness: boosts the input by as much as the contour turns
    /// the volume down, so the midrange stays at about the same level.
    Loudness,
}

pub struct ParamSpec {
    pub id: &'static str,
    pub label: &'static str,
    pub help: &'static str,
    pub min: f64,
    pub max: f64,
    pub log: bool,
    pub unit: &'static str,
    pub default: f64,
    pub map: Map,
}

#[derive(Copy, Clone, PartialEq, Eq)]
pub enum Plot {
    Transfer,
    Gate,
    Level,
}

pub struct EffectSpec {
    pub id: &'static str,
    pub title: &'static str,
    pub engine: &'static str,
    pub blurb: &'static str,
    pub plugin: &'static str,
    pub color: (f64, f64, f64),
    pub plot: Plot,
    pub params: &'static [ParamSpec],
    pub fixed: &'static [(&'static str, f32)],
}

const fn p(
    id: &'static str,
    label: &'static str,
    help: &'static str,
    min: f64,
    max: f64,
    log: bool,
    unit: &'static str,
    default: f64,
    map: Map,
) -> ParamSpec {
    ParamSpec {
        id,
        label,
        help,
        min,
        max,
        log,
        unit,
        default,
        map,
    }
}

pub static EFFECTS: &[EffectSpec] = &[
    EffectSpec {
        id: "gate",
        title: "Noise Gate",
        engine: "LSP Gate",
        blurb: "Quiets everything below the threshold, such as room noise between words.",
        plugin: "gate_stereo",
        color: (0.20, 0.78, 0.45),
        plot: Plot::Gate,
        params: &[
            p(
                "threshold",
                "Threshold",
                "Sound below this level is turned down",
                -80.0,
                0.0,
                false,
                "dB",
                -50.0,
                Map::Gain("Curve threshold (G)"),
            ),
            p(
                "reduction",
                "Reduction",
                "How far quiet sound is turned down",
                -80.0,
                0.0,
                false,
                "dB",
                -24.0,
                Map::Gain("Reduction (G)"),
            ),
            p(
                "attack",
                "Attack",
                "How fast the gate opens",
                0.1,
                200.0,
                true,
                "ms",
                5.0,
                Map::Raw("Attack (ms)"),
            ),
            p(
                "release",
                "Release",
                "How fast the gate closes",
                5.0,
                2000.0,
                true,
                "ms",
                150.0,
                Map::Raw("Release (ms)"),
            ),
        ],
        fixed: &[("Sidechain mode", 1.0)],
    },
    EffectSpec {
        id: "autogain",
        title: "Auto Gain",
        engine: "LSP Autogain",
        blurb: "Keeps everything near the same loudness, so quiet and loud sources even out.",
        plugin: "autogain_stereo",
        color: (0.30, 0.78, 0.82),
        plot: Plot::Level,
        params: &[
            p(
                "target",
                "Target",
                "The loudness to aim for",
                -48.0,
                -6.0,
                false,
                "LUFS",
                -20.0,
                Map::Raw("Desired loudness level (LUFS)"),
            ),
            p(
                "maxgain",
                "Max boost",
                "The most it may turn up quiet sound",
                0.0,
                40.0,
                false,
                "dB",
                12.0,
                Map::Raw("The maximum amplification gain (dB)"),
            ),
        ],
        fixed: &[("Enable maximum amplification gain limitation", 1.0)],
    },
    EffectSpec {
        id: "compressor",
        title: "Compressor",
        engine: "LSP Compressor",
        blurb: "Brings the loudest moments down, for a smoother and more even sound.",
        plugin: "compressor_stereo",
        color: (0.96, 0.62, 0.20),
        plot: Plot::Transfer,
        params: &[
            p(
                "threshold",
                "Threshold",
                "Compression starts above this level",
                -60.0,
                0.0,
                false,
                "dB",
                -18.0,
                Map::Gain("Attack threshold (G)"),
            ),
            p(
                "ratio",
                "Ratio",
                "How strongly loud sound is reduced",
                1.0,
                20.0,
                true,
                ":1",
                3.0,
                Map::Raw("Ratio"),
            ),
            p(
                "knee",
                "Knee",
                "Lower values ease into compression more gently",
                -24.0,
                0.0,
                false,
                "dB",
                -6.0,
                Map::Gain("Knee (G)"),
            ),
            p(
                "attack",
                "Attack",
                "How fast it reacts to loud sound",
                0.1,
                200.0,
                true,
                "ms",
                15.0,
                Map::Raw("Attack time (ms)"),
            ),
            p(
                "release",
                "Release",
                "How fast it lets go",
                5.0,
                2000.0,
                true,
                "ms",
                150.0,
                Map::Raw("Release time (ms)"),
            ),
            p(
                "makeup",
                "Makeup",
                "Adds back level after compression",
                0.0,
                24.0,
                false,
                "dB",
                0.0,
                Map::Gain("Makeup gain (G)"),
            ),
        ],
        fixed: &[("Compression mode", 0.0), ("Sidechain mode", 1.0)],
    },
    EffectSpec {
        id: "loudness",
        title: "Loudness",
        engine: "LSP Loudness Compensator",
        blurb: "Restores the bass and treble your ears lose at low volume.",
        plugin: "loud_comp_stereo",
        color: (0.68, 0.48, 0.95),
        plot: Plot::Level,
        params: &[p(
            "amount",
            "Amount",
            "Higher suits quieter listening",
            0.0,
            40.0,
            false,
            "dB",
            15.0,
            Map::Loudness,
        )],
        fixed: &[],
    },
    EffectSpec {
        id: "limiter",
        title: "Limiter",
        engine: "LSP Limiter",
        blurb: "Catches peaks so that boosts never turn into harsh digital clipping.",
        plugin: "limiter_stereo",
        color: (0.94, 0.33, 0.38),
        plot: Plot::Transfer,
        params: &[
            p(
                "threshold",
                "Ceiling",
                "Output never goes above this level",
                -24.0,
                0.0,
                false,
                "dB",
                -1.0,
                Map::Gain("Threshold (G)"),
            ),
            p(
                "lookahead",
                "Lookahead",
                "Adds this much delay to catch peaks cleanly",
                0.25,
                20.0,
                true,
                "ms",
                3.0,
                Map::Raw("Lookahead (ms)"),
            ),
            p(
                "release",
                "Release",
                "How fast it lets go",
                0.25,
                20.0,
                true,
                "ms",
                8.0,
                Map::Raw("Release time (ms)"),
            ),
        ],
        fixed: &[
            ("Operating mode", 1.0),
            ("Gain boost", 0.0),
            ("Attack time (ms)", 1.0),
            ("Automatic level regulation", 0.0),
        ],
    },
];

pub fn effect(id: &str) -> &'static EffectSpec {
    EFFECTS.iter().find(|e| e.id == id).expect("unknown effect")
}

#[derive(Copy, Clone, PartialEq, Eq, Debug)]
pub enum Direction {
    Output = 0,
    Mic = 1,
}

impl Direction {
    /// Signal order through the chain. "eq" is the equalizer.
    pub fn chain(self) -> &'static [&'static str] {
        match self {
            Self::Output => &["autogain", "compressor", "eq", "loudness", "limiter"],
            Self::Mic => &["gate", "autogain", "eq", "compressor", "limiter"],
        }
    }
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct EffectState {
    pub enabled: bool,
    pub params: BTreeMap<String, f64>,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Sound {
    pub bands: Vec<Band>,
    pub preamp: f64,
    pub balance: f64,
    pub eq_enabled: bool,
    pub effects: BTreeMap<String, EffectState>,
}

impl Sound {
    pub fn flat() -> Self {
        let mut effects = BTreeMap::new();
        for e in EFFECTS {
            let params = e.params.iter().map(|p| (p.id.to_string(), p.default)).collect();
            effects.insert(
                e.id.to_string(),
                EffectState {
                    enabled: e.id == "limiter",
                    params,
                },
            );
        }
        Self {
            bands: [60.0, 250.0, 1000.0, 4000.0, 12000.0]
                .iter()
                .map(|f| Band::bell(*f, 0.0, 1.0))
                .collect(),
            preamp: 0.0,
            balance: 0.0,
            eq_enabled: true,
            effects,
        }
    }

    /// Puts the equalizer back to how the Flat preset has it: its bands, preamp and
    /// balance. The other effects, and whether the equalizer is on, stay as they are.
    pub fn reset_eq(&mut self) {
        let flat = Self::flat();
        self.bands = flat.bands;
        self.preamp = flat.preamp;
        self.balance = flat.balance;
    }

    pub fn param(&self, effect: &str, id: &str) -> f64 {
        let spec = self::effect(effect).params.iter().find(|p| p.id == id).expect("unknown param");
        self.effects
            .get(effect)
            .and_then(|e| e.params.get(id))
            .copied()
            .unwrap_or(spec.default)
    }

    pub fn is_on(&self, effect: &str) -> bool {
        if effect == "eq" {
            return self.eq_enabled;
        }
        self.effects.get(effect).is_some_and(|e| e.enabled)
    }

    /// Brings every value into the range the app allows. Imported and saved files can
    /// contain anything, and the sliders and graph only limit what's changed through them.
    pub fn sanitize(&mut self) {
        self.bands.truncate(MAX_BANDS);
        for band in &mut self.bands {
            band.freq = limited(band.freq, 10.0, 24_000.0, 1000.0);
            band.gain = limited(band.gain, -GAIN_LIMIT, GAIN_LIMIT, 0.0);
            band.q = limited(band.q, 0.1, 20.0, 1.0);
        }
        self.preamp = limited(self.preamp, -GAIN_LIMIT, GAIN_LIMIT, 0.0);
        self.balance = limited(self.balance, -100.0, 100.0, 0.0);
        for spec in EFFECTS {
            let Some(state) = self.effects.get_mut(spec.id) else { continue };
            for param in spec.params {
                if let Some(v) = state.params.get_mut(param.id) {
                    *v = limited(*v, param.min, param.max, param.default);
                }
            }
        }
    }
}

/// A value within its range. Infinity and NaN become the default, as `clamp` leaves NaN as it is.
fn limited(v: f64, min: f64, max: f64, default: f64) -> f64 {
    if v.is_finite() { v.clamp(min, max) } else { default }
}

fn db_to_g(db: f64) -> f32 {
    10f64.powf(db / 20.0) as f32
}

/// Flattens a sound into `(node:control, value)` pairs for one chain.
pub fn plugin_controls(sound: &Sound, dir: Direction) -> BTreeMap<String, f32> {
    let mut m = BTreeMap::new();
    for id in dir.chain() {
        if *id == "eq" {
            m.insert("eq:Bypass".into(), if sound.eq_enabled { 0.0 } else { 1.0 });
            m.insert("eq:Input gain (G)".into(), db_to_g(sound.preamp));
            m.insert("eq:Output balance (%)".into(), sound.balance as f32);
            for i in 0..MAX_BANDS {
                let band = sound.bands.get(i).filter(|b| b.enabled);
                let kind = band.map_or(0.0, |b| b.kind.lsp_type());
                m.insert(format!("eq:Filter type {i}"), kind);
                m.insert(format!("eq:Filter mode {i}"), 6.0); // APO (DR)
                if let Some(b) = band {
                    m.insert(format!("eq:Frequency {i} (Hz)"), b.freq as f32);
                    m.insert(format!("eq:Gain {i} (G)"), db_to_g(if b.kind.uses_gain() { b.gain } else { 0.0 }));
                    m.insert(format!("eq:Quality factor {i}"), b.q as f32);
                }
            }
            continue;
        }
        let spec = effect(id);
        m.insert(format!("{id}:Bypass"), if sound.is_on(id) { 0.0 } else { 1.0 });
        for (name, v) in spec.fixed {
            m.insert(format!("{id}:{name}"), *v);
        }
        for param in spec.params {
            let v = sound.param(id, param.id);
            match param.map {
                Map::Gain(port) => {
                    m.insert(format!("{id}:{port}"), db_to_g(v));
                }
                Map::Raw(port) => {
                    m.insert(format!("{id}:{port}"), v as f32);
                }
                Map::Loudness => {
                    m.insert(format!("{id}:Output volume (dB)"), -v as f32);
                    m.insert(format!("{id}:Input gain (G)"), db_to_g(v));
                }
            }
        }
    }
    m
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct ChainState {
    pub enabled: bool,
    /// The real device to use. `None` follows the system default.
    pub device: Option<String>,
    pub preset: String,
    pub sound: Sound,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct AppState {
    pub output: ChainState,
    pub mic: ChainState,
    #[serde(default = "yes")]
    pub run_in_background: bool,
    /// Only used inside Flatpak, where the sandbox can't see the real autostart file.
    #[serde(default)]
    pub autostart: bool,
}

fn yes() -> bool {
    true
}

impl Default for AppState {
    fn default() -> Self {
        let flat = |enabled| ChainState {
            enabled,
            device: None,
            preset: "Flat".into(),
            sound: Sound::flat(),
        };
        Self {
            output: flat(true),
            mic: flat(false),
            run_in_background: true,
            autostart: false,
        }
    }
}

impl AppState {
    pub fn chain(&self, d: Direction) -> &ChainState {
        match d {
            Direction::Output => &self.output,
            Direction::Mic => &self.mic,
        }
    }

    pub fn chain_mut(&mut self, d: Direction) -> &mut ChainState {
        match d {
            Direction::Output => &mut self.output,
            Direction::Mic => &mut self.mic,
        }
    }
}

pub fn config_dir() -> PathBuf {
    gtk::glib::user_config_dir().join("equalizer")
}

pub fn load_state() -> AppState {
    let mut state: AppState = std::fs::read(config_dir().join("state.json"))
        .ok()
        .and_then(|d| serde_json::from_slice(&d).ok())
        .unwrap_or_default();
    state.output.sound.sanitize();
    state.mic.sound.sanitize();
    state
}

pub fn save_state(s: &AppState) -> std::io::Result<()> {
    let dir = config_dir();
    std::fs::create_dir_all(&dir)?;
    let tmp = dir.join("state.json.tmp");
    std::fs::write(&tmp, serde_json::to_vec_pretty(s).expect("state serializes"))?;
    std::fs::rename(tmp, dir.join("state.json"))
}

pub fn presets_dir() -> PathBuf {
    config_dir().join("presets")
}

fn with_bands(bands: &[(FilterKind, f64, f64, f64)]) -> Sound {
    let mut s = Sound::flat();
    s.bands = bands
        .iter()
        .map(|&(kind, freq, gain, q)| Band {
            kind,
            freq,
            gain,
            q,
            enabled: true,
        })
        .collect();
    s
}

pub fn builtin_presets() -> Vec<(&'static str, Sound)> {
    use FilterKind::*;
    let mut late = with_bands(&[(LowShelf, 90.0, 2.0, 0.7), (Bell, 3000.0, 1.5, 0.8)]);
    late.effects.get_mut("compressor").unwrap().enabled = true;
    late.effects.get_mut("loudness").unwrap().enabled = true;
    vec![
        ("Flat", Sound::flat()),
        ("Bass Boost", with_bands(&[(LowShelf, 110.0, 6.0, 0.7), (Bell, 350.0, -1.5, 1.0)])),
        ("Treble Boost", with_bands(&[(HighShelf, 6000.0, 5.0, 0.7)])),
        (
            "Smile",
            with_bands(&[(LowShelf, 90.0, 5.0, 0.7), (Bell, 800.0, -3.0, 0.7), (HighShelf, 8000.0, 4.5, 0.7)]),
        ),
        (
            "Vocal Clarity",
            with_bands(&[
                (HighPass, 80.0, 0.0, 0.7),
                (Bell, 250.0, -2.0, 1.0),
                (Bell, 3000.0, 3.5, 0.9),
                (Bell, 7500.0, -1.0, 2.0),
            ]),
        ),
        ("Late Night", late),
    ]
}

pub fn user_presets() -> Vec<(String, Sound)> {
    let mut out = vec![];
    if let Ok(dir) = std::fs::read_dir(presets_dir()) {
        for e in dir.flatten() {
            let path = e.path();
            if path.extension().is_some_and(|x| x == "json")
                && let (Some(stem), Ok(data)) = (path.file_stem(), std::fs::read(&path))
                && let Ok(mut sound) = serde_json::from_slice::<Sound>(&data)
            {
                sound.sanitize();
                out.push((stem.to_string_lossy().into_owned(), sound));
            }
        }
    }
    out.sort_by_key(|(n, _)| n.to_lowercase());
    out
}

/// Keeps a preset name safe to use as a file name.
pub fn preset_path(name: &str) -> Option<PathBuf> {
    let name = name.trim();
    if name.is_empty() || name.contains(['/', '\\', '\0']) || name.starts_with('.') {
        return None;
    }
    Some(presets_dir().join(format!("{name}.json")))
}

pub fn save_preset(name: &str, sound: &Sound) -> std::io::Result<()> {
    if builtin_presets().iter().any(|(n, _)| n.eq_ignore_ascii_case(name.trim())) {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidInput,
            "A built-in preset already has that name",
        ));
    }
    let path = preset_path(name).ok_or_else(|| std::io::Error::new(std::io::ErrorKind::InvalidInput, "That name can't be used"))?;
    std::fs::create_dir_all(presets_dir())?;
    std::fs::write(path, serde_json::to_vec_pretty(sound).expect("sound serializes"))
}

/// A number from an APO file. Rust also reads "NaN" and "inf" as numbers, but they
/// aren't usable settings, so they count as unreadable.
fn apo_number(word: &str) -> Option<f64> {
    word.parse::<f64>().ok().filter(|v| v.is_finite())
}

/// Reads an Equalizer APO / AutoEq file, the format headphone correction
/// presets are usually shared in.
pub fn parse_apo(text: &str) -> Result<Sound, String> {
    let mut sound = Sound::flat();
    sound.bands.clear();
    let mut skipped = 0;
    for line in text.lines() {
        let line = line.trim();
        if let Some(rest) = line.strip_prefix("Preamp:") {
            let v = rest.split_whitespace().next().and_then(apo_number);
            sound.preamp = v.ok_or("The Preamp line isn't readable")?;
        } else if line.starts_with("Filter") {
            let words: Vec<&str> = line.split_whitespace().collect();
            let Some(on) = words.iter().position(|w| *w == "ON" || *w == "OFF") else {
                continue;
            };
            let kind = match words.get(on + 1).copied() {
                Some("PK") | Some("Modal") | Some("PEQ") => FilterKind::Bell,
                Some("LSC") | Some("LS") => FilterKind::LowShelf,
                Some("HSC") | Some("HS") => FilterKind::HighShelf,
                Some("LP") | Some("LPQ") => FilterKind::LowPass,
                Some("HP") | Some("HPQ") => FilterKind::HighPass,
                Some("NO") => FilterKind::Notch,
                _ => {
                    skipped += 1;
                    continue;
                }
            };
            let value = |key: &str| {
                words
                    .iter()
                    .position(|w| *w == key)
                    .and_then(|i| words.get(i + 1))
                    .and_then(|v| apo_number(v))
            };
            let Some(freq) = value("Fc") else { continue };
            if sound.bands.len() == MAX_BANDS {
                skipped += 1;
                continue;
            }
            sound.bands.push(Band {
                kind,
                freq,
                gain: value("Gain").unwrap_or(0.0),
                q: value("Q").unwrap_or(if kind == FilterKind::Bell { 1.0 } else { 0.71 }),
                enabled: words[on] == "ON",
            });
        }
    }
    if sound.bands.is_empty() {
        return Err(if skipped > 0 {
            "None of the filters in this file are supported".into()
        } else {
            "This doesn't look like an APO file".into()
        });
    }
    sound.sanitize();
    Ok(sound)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn apo_import_reads_filters_and_preamp() {
        let s = parse_apo("Preamp: -6.1 dB\nFilter 1: ON PK Fc 105 Hz Gain -3.5 dB Q 1.41\nFilter 2: ON LSC Fc 80 Hz Gain 5 dB Q 0.7\nFilter 3: OFF HSC Fc 9000 Hz Gain 2 dB Q 0.7\n").unwrap();
        assert_eq!(s.preamp, -6.1);
        assert_eq!(s.bands.len(), 3);
        assert_eq!(s.bands[0], Band::bell(105.0, -3.5, 1.41));
        assert_eq!(s.bands[1].kind, FilterKind::LowShelf);
        assert!(!s.bands[2].enabled);
    }

    #[test]
    fn apo_import_rejects_other_files() {
        assert!(parse_apo("hello\nworld").is_err());
    }

    #[test]
    fn apo_import_limits_extreme_values() {
        let s = parse_apo("Preamp: 500 dB\nFilter 1: ON PK Fc 99999 Hz Gain -500 dB Q 900\n").unwrap();
        assert_eq!(s.preamp, GAIN_LIMIT);
        assert_eq!(s.bands[0], Band::bell(24_000.0, -GAIN_LIMIT, 20.0));
    }

    #[test]
    fn apo_import_refuses_numbers_that_are_not_finite() {
        for word in ["NaN", "nan", "inf", "-inf", "infinity"] {
            assert!(parse_apo(&format!("Preamp: {word} dB\nFilter 1: ON PK Fc 100 Hz Gain 1 dB Q 1\n")).is_err());
            // A filter with no usable frequency is skipped, like one without any.
            assert!(parse_apo(&format!("Filter 1: ON PK Fc {word} Hz Gain 1 dB Q 1\n")).is_err());
            // A missing gain or width falls back as usual.
            let s = parse_apo(&format!("Filter 1: ON PK Fc 100 Hz Gain {word} dB Q {word}\n")).unwrap();
            assert_eq!(s.bands[0], Band::bell(100.0, 0.0, 1.0));
        }
    }

    #[test]
    fn sanitize_brings_every_value_into_range() {
        let mut s = Sound::flat();
        s.preamp = 1000.0;
        s.balance = f64::NAN;
        s.bands = vec![Band::bell(f64::NAN, f64::INFINITY, -5.0); MAX_BANDS + 5];
        let limiter = effect("limiter");
        let param = &limiter.params[0];
        s.effects.get_mut("limiter").unwrap().params.insert(param.id.into(), f64::NAN);
        let loudness = effect("loudness").params[0].id;
        s.effects.get_mut("loudness").unwrap().params.insert(loudness.into(), 1e300);

        s.sanitize();

        assert_eq!(s.preamp, GAIN_LIMIT);
        assert_eq!(s.balance, 0.0);
        assert_eq!(s.bands.len(), MAX_BANDS);
        assert_eq!(s.bands[0], Band::bell(1000.0, 0.0, 0.1));
        assert_eq!(s.effects["limiter"].params[param.id], param.default);
        assert_eq!(s.effects["loudness"].params[loudness], effect("loudness").params[0].max);
    }

    #[test]
    fn resetting_the_equalizer_leaves_the_other_effects_alone() {
        let mut s = builtin_presets().into_iter().find(|(n, _)| *n == "Late Night").unwrap().1;
        s.preamp = -4.0;
        s.balance = 30.0;
        s.eq_enabled = false;
        s.effects.get_mut("autogain").unwrap().params.insert("target".into(), -30.0);
        let effects = s.effects.clone();
        s.reset_eq();
        let flat = Sound::flat();
        assert_eq!(s.bands, flat.bands);
        assert_eq!((s.preamp, s.balance), (0.0, 0.0));
        assert_eq!(s.effects, effects);
        assert!(!s.eq_enabled, "the on/off switch is for comparing, not a setting to reset");
    }

    #[test]
    fn sanitize_leaves_valid_sounds_alone() {
        for (name, sound) in builtin_presets() {
            let mut clean = sound.clone();
            clean.sanitize();
            assert_eq!(clean, sound, "{name}");
        }
    }

    #[test]
    fn an_imported_sound_can_be_saved_and_read_back() {
        let s = parse_apo("Preamp: -3 dB\nFilter 1: ON PK Fc 100 Hz Gain NaN dB Q inf\n").unwrap();
        let mut state = AppState::default();
        state.output.sound = s.clone();
        let back: AppState = serde_json::from_slice(&serde_json::to_vec(&state).unwrap()).unwrap();
        assert_eq!(back.output.sound, s);
    }

    #[test]
    fn controls_turn_unused_bands_off_and_convert_gain() {
        let mut s = Sound::flat();
        s.bands = vec![Band::bell(1000.0, 20.0, 1.0)];
        let m = plugin_controls(&s, Direction::Output);
        assert_eq!(m["eq:Filter type 0"], 1.0);
        assert_eq!(m["eq:Filter type 1"], 0.0);
        assert!((m["eq:Gain 0 (G)"] - 10.0).abs() < 1e-4);
        assert_eq!(m["eq:Filter mode 0"], 6.0);
        assert_eq!(m["compressor:Bypass"], 1.0);
        assert_eq!(m["limiter:Bypass"], 0.0);
        assert!(!m.contains_key("gate:Bypass"), "the output chain has no gate");
    }

    #[test]
    fn loudness_keeps_midrange_level_by_balancing_two_controls() {
        let mut s = Sound::flat();
        s.effects.get_mut("loudness").unwrap().params.insert("amount".into(), 20.0);
        let m = plugin_controls(&s, Direction::Output);
        assert_eq!(m["loudness:Output volume (dB)"], -20.0);
        assert!((m["loudness:Input gain (G)"] - 10.0).abs() < 1e-4);
    }

    #[test]
    fn preset_names_cannot_escape_the_presets_folder() {
        assert!(preset_path("../evil").is_none());
        assert!(preset_path(".hidden").is_none());
        assert!(preset_path("  ").is_none());
        assert!(preset_path("My Sound").is_some());
    }

    #[test]
    fn state_survives_a_round_trip() {
        let s = AppState::default();
        let back: AppState = serde_json::from_slice(&serde_json::to_vec(&s).unwrap()).unwrap();
        assert_eq!(back.output.sound, s.output.sound);
    }

    #[test]
    fn every_chain_entry_exists() {
        for d in [Direction::Output, Direction::Mic] {
            for id in d.chain() {
                if *id != "eq" {
                    effect(id);
                }
            }
        }
    }
}
