//! Filter maths and signal analysis. Nothing here touches PipeWire or GTK.
use rustfft::{Fft, FftPlanner, num_complex::Complex};
use std::f64::consts::PI;
use std::sync::Arc;

use crate::model::{Band, FilterKind};

pub const FFT_SIZE: usize = 4096;

/// Magnitude response in dB of one band. These are the Audio EQ Cookbook
/// formulas, which is what LSP's "APO (DR)" filter mode implements. The audio
/// test in `tests/measure.py` checks them against the plugin's real output.
pub fn band_response_db(band: &Band, freq: f64, rate: f64) -> f64 {
    if !band.enabled {
        return 0.0;
    }
    let w0 = 2.0 * PI * band.freq.min(rate * 0.49) / rate;
    let w = 2.0 * PI * freq.min(rate * 0.5) / rate;
    let alpha = w0.sin() / (2.0 * band.q.max(0.05));
    let c = w0.cos();
    let a = 10f64.powf(band.gain / 40.0);
    let s = 2.0 * a.sqrt() * alpha;
    let (b0, b1, b2, a0, a1, a2) = match band.kind {
        FilterKind::Bell => (
            1.0 + alpha * a,
            -2.0 * c,
            1.0 - alpha * a,
            1.0 + alpha / a,
            -2.0 * c,
            1.0 - alpha / a,
        ),
        FilterKind::LowShelf => (
            a * ((a + 1.0) - (a - 1.0) * c + s),
            2.0 * a * ((a - 1.0) - (a + 1.0) * c),
            a * ((a + 1.0) - (a - 1.0) * c - s),
            (a + 1.0) + (a - 1.0) * c + s,
            -2.0 * ((a - 1.0) + (a + 1.0) * c),
            (a + 1.0) + (a - 1.0) * c - s,
        ),
        FilterKind::HighShelf => (
            a * ((a + 1.0) + (a - 1.0) * c + s),
            -2.0 * a * ((a - 1.0) + (a + 1.0) * c),
            a * ((a + 1.0) + (a - 1.0) * c - s),
            (a + 1.0) - (a - 1.0) * c + s,
            2.0 * ((a - 1.0) - (a + 1.0) * c),
            (a + 1.0) - (a - 1.0) * c - s,
        ),
        FilterKind::LowPass => ((1.0 - c) / 2.0, 1.0 - c, (1.0 - c) / 2.0, 1.0 + alpha, -2.0 * c, 1.0 - alpha),
        FilterKind::HighPass => ((1.0 + c) / 2.0, -(1.0 + c), (1.0 + c) / 2.0, 1.0 + alpha, -2.0 * c, 1.0 - alpha),
        FilterKind::Notch => (1.0, -2.0 * c, 1.0, 1.0 + alpha, -2.0 * c, 1.0 - alpha),
    };
    let (cw, sw, c2w, s2w) = (w.cos(), w.sin(), (2.0 * w).cos(), (2.0 * w).sin());
    let nr = b0 + b1 * cw + b2 * c2w;
    let ni = -b1 * sw - b2 * s2w;
    let dr = a0 + a1 * cw + a2 * c2w;
    let di = -a1 * sw - a2 * s2w;
    let mag = (nr * nr + ni * ni) / (dr * dr + di * di).max(1e-30);
    10.0 * mag.max(1e-12).log10()
}

pub fn total_response_db(bands: &[Band], freq: f64, rate: f64) -> f64 {
    bands.iter().map(|b| band_response_db(b, freq, rate)).sum()
}

pub fn to_db(v: f64) -> f64 {
    20.0 * v.max(1e-7).log10()
}

/// Collects captured samples. Shared between the PipeWire thread and the UI.
pub struct Analyzer {
    ring: Vec<f32>,
    pos: usize,
    pub rate: u32,
    peak: f32,
    /// Applied to each channel as it's fed. It undoes a volume that was applied to the
    /// sound being measured, so that only the effects show.
    pub gain: [f32; 2],
}

impl Analyzer {
    pub fn new() -> Self {
        Self {
            ring: vec![0.0; FFT_SIZE],
            pos: 0,
            rate: 48_000,
            peak: 0.0,
            gain: [1.0; 2],
        }
    }

    /// `samples` is interleaved stereo.
    pub fn feed(&mut self, samples: &[f32]) {
        let [gl, gr] = self.gain;
        for frame in samples.as_chunks::<2>().0 {
            let (l, r) = (frame[0] * gl, frame[1] * gr);
            self.peak = self.peak.max(l.abs()).max(r.abs());
            self.ring[self.pos] = (l + r) * 0.5;
            self.pos = (self.pos + 1) % FFT_SIZE;
        }
    }

    /// Returns the peak since the last call and resets it.
    pub fn take_peak(&mut self) -> f32 {
        std::mem::take(&mut self.peak)
    }

    pub fn snapshot(&self, out: &mut [f32]) {
        for (i, v) in out.iter_mut().enumerate() {
            *v = self.ring[(self.pos + i) % FFT_SIZE];
        }
    }

    pub fn clear(&mut self) {
        self.ring.fill(0.0);
        self.peak = 0.0;
    }
}

/// How steep a smooth curve should be at a point, given the change on either side of it.
/// Carrying the slope through each point makes the line flow like a wave, rather than
/// leveling off into terraces, at the cost of sometimes going slightly past a sharp peak.
pub fn curve_slope(before: f64, after: f64) -> f64 {
    (before + after) / 2.0
}

/// Reads between `y1` and `y2`, with `t` from 0 to 1, along a curve that also takes in their neighbors.
fn curve_between(y0: f64, y1: f64, y2: f64, y3: f64, t: f64) -> f64 {
    let (m1, m2) = (curve_slope(y1 - y0, y2 - y1), curve_slope(y2 - y1, y3 - y2));
    let (t2, t3) = (t * t, t * t * t);
    (2.0 * t3 - 3.0 * t2 + 1.0) * y1 + (t3 - 2.0 * t2 + t) * m1 + (3.0 * t2 - 2.0 * t3) * y2 + (t3 - t2) * m2
}

/// Turns the newest samples into log-spaced columns of dB values.
pub struct Spectrum {
    fft: Arc<dyn Fft<f32>>,
    window: Vec<f32>,
    input: Vec<f32>,
    buf: Vec<Complex<f32>>,
    bins: Vec<f64>,
    pub columns: Vec<f64>,
}

pub const SPECTRUM_COLUMNS: usize = 256;
pub const SPECTRUM_FLOOR: f64 = -90.0;
pub const FREQ_MIN: f64 = 20.0;
pub const FREQ_MAX: f64 = 20_000.0;

impl Spectrum {
    pub fn new() -> Self {
        let window = (0..FFT_SIZE)
            .map(|i| (0.5 - 0.5 * (2.0 * PI * i as f64 / FFT_SIZE as f64).cos()) as f32)
            .collect();
        Self {
            fft: FftPlanner::new().plan_fft_forward(FFT_SIZE),
            window,
            input: vec![0.0; FFT_SIZE],
            buf: vec![Complex::default(); FFT_SIZE],
            bins: vec![SPECTRUM_FLOOR; FFT_SIZE / 2 + 1],
            columns: vec![SPECTRUM_FLOOR; SPECTRUM_COLUMNS],
        }
    }

    #[cfg(test)]
    pub fn update(&mut self, analyzer: &Analyzer) {
        self.capture(analyzer);
        self.analyze(analyzer.rate);
    }

    /// Copies the newest samples. This is the only part that needs the analyzer, which
    /// the audio thread shares, so the slower `analyze` can run after letting go of it.
    pub fn capture(&mut self, analyzer: &Analyzer) {
        analyzer.snapshot(&mut self.input);
    }

    /// Turns the captured samples into columns.
    pub fn analyze(&mut self, rate: u32) {
        for i in 0..FFT_SIZE {
            self.buf[i] = Complex::new(self.input[i] * self.window[i], 0.0);
        }
        self.fft.process(&mut self.buf);
        let bin_hz = rate as f64 / FFT_SIZE as f64;
        let ratio = (FREQ_MAX / FREQ_MIN).powf(1.0 / SPECTRUM_COLUMNS as f64);
        // A Hann window halves the amplitude, hence 4 rather than 2.
        let norm = 4.0 / FFT_SIZE as f64;
        for (level, bin) in self.bins.iter_mut().zip(&self.buf) {
            *level = to_db(bin.norm() as f64 * norm).max(SPECTRUM_FLOOR);
        }
        let last = FFT_SIZE / 2;
        for c in 0..SPECTRUM_COLUMNS {
            let lo = FREQ_MIN * ratio.powi(c as i32);
            let hi = lo * ratio;
            // Low columns are narrower than a bin, and reading the nearest one would turn
            // them into stairs, so each reads the curve between bins at its own center.
            let center = ((lo * hi).sqrt() / bin_hz).clamp(1.0, (last - 1) as f64);
            let a = center as usize;
            let mut db = curve_between(
                self.bins[(a - 1).max(1)],
                self.bins[a],
                self.bins[a + 1],
                self.bins[(a + 2).min(last)],
                center - a as f64,
            );
            // Higher columns span several bins. The loudest one sets the level, so peaks stay true.
            let first = ((lo / bin_hz).ceil() as usize).max(1);
            let end = ((hi / bin_hz).floor() as usize).min(last);
            if first <= end {
                for bin in &self.bins[first..=end] {
                    db = db.max(*bin);
                }
            }
            // Rise instantly and fall gently, like a monitor trace.
            let held = self.columns[c] - 1.8;
            self.columns[c] = db.max(held).max(SPECTRUM_FLOOR);
        }
    }

    pub fn decay(&mut self) {
        for c in &mut self.columns {
            *c = (*c - 1.8).max(SPECTRUM_FLOOR);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_analyzer_applies_its_gain_to_each_channel() {
        let mut a = Analyzer::new();
        a.gain = [8.0, 2.0];
        a.feed(&[0.1, 0.0, 0.0, 0.25]);
        assert_eq!(a.take_peak(), 0.8);
        let mut out = vec![0.0; FFT_SIZE];
        a.snapshot(&mut out);
        assert_eq!(&out[FFT_SIZE - 2..], &[0.4, 0.25]);
        a.clear();
        assert_eq!(a.gain, [8.0, 2.0], "clearing the samples keeps the gain");
    }

    fn band(kind: FilterKind, freq: f64, gain: f64, q: f64) -> Band {
        Band {
            kind,
            freq,
            gain,
            q,
            enabled: true,
        }
    }

    #[test]
    fn bell_hits_its_gain_at_center() {
        let b = band(FilterKind::Bell, 1000.0, 12.0, 1.0);
        assert!((band_response_db(&b, 1000.0, 48_000.0) - 12.0).abs() < 0.001);
    }

    #[test]
    fn bell_matches_the_measured_plugin_output() {
        // These numbers came from the LSP plugin at +12 dB, Q 1, 1 kHz.
        let b = band(FilterKind::Bell, 1000.0, 12.0, 1.0);
        assert!((band_response_db(&b, 500.0, 48_000.0) - 3.95).abs() < 0.25);
        assert!((band_response_db(&b, 2000.0, 48_000.0) - 3.93).abs() < 0.25);
    }

    #[test]
    fn shelves_reach_their_gain_on_the_shelf() {
        let lo = band(FilterKind::LowShelf, 200.0, 6.0, 0.7);
        assert!((band_response_db(&lo, 20.0, 48_000.0) - 6.0).abs() < 0.1);
        assert!(band_response_db(&lo, 5000.0, 48_000.0).abs() < 0.1);
        let hi = band(FilterKind::HighShelf, 5000.0, -6.0, 0.7);
        assert!((band_response_db(&hi, 20_000.0, 48_000.0) + 6.0).abs() < 0.3);
        assert!(band_response_db(&hi, 100.0, 48_000.0).abs() < 0.1);
    }

    #[test]
    fn pass_filters_and_notch_cut_where_they_should() {
        let hp = band(FilterKind::HighPass, 1000.0, 0.0, 0.7);
        assert!(band_response_db(&hp, 100.0, 48_000.0) < -30.0);
        assert!(band_response_db(&hp, 10_000.0, 48_000.0).abs() < 0.5);
        let lp = band(FilterKind::LowPass, 1000.0, 0.0, 0.7);
        assert!(band_response_db(&lp, 10_000.0, 48_000.0) < -30.0);
        let n = band(FilterKind::Notch, 1000.0, 0.0, 2.0);
        assert!(band_response_db(&n, 1000.0, 48_000.0) < -60.0);
    }

    #[test]
    fn disabled_band_is_flat() {
        let mut b = band(FilterKind::Bell, 1000.0, 12.0, 1.0);
        b.enabled = false;
        assert_eq!(band_response_db(&b, 1000.0, 48_000.0), 0.0);
    }

    #[test]
    fn spectrum_finds_a_full_scale_tone_in_the_right_place() {
        let mut a = Analyzer::new();
        let samples: Vec<f32> = (0..FFT_SIZE)
            .flat_map(|i| {
                let v = (2.0 * PI * 1000.0 * i as f64 / 48_000.0).sin() as f32;
                [v, v]
            })
            .collect();
        a.feed(&samples);
        let mut s = Spectrum::new();
        s.update(&a);
        let (col, db) = s
            .columns
            .iter()
            .enumerate()
            .fold((0, f64::MIN), |m, (i, v)| if *v > m.1 { (i, *v) } else { m });
        let ratio = (FREQ_MAX / FREQ_MIN).powf(1.0 / SPECTRUM_COLUMNS as f64);
        let f = FREQ_MIN * ratio.powi(col as i32);
        assert!(f > 900.0 && f < 1100.0, "peak at {f} Hz");
        assert!(db.abs() < 1.0, "peak level {db} dB");
    }

    #[test]
    fn low_frequencies_change_gradually_rather_than_in_steps() {
        let mut a = Analyzer::new();
        let samples: Vec<f32> = (0..FFT_SIZE)
            .flat_map(|i| {
                let v = (2.0 * PI * 80.0 * i as f64 / 48_000.0).sin() as f32;
                [v, v]
            })
            .collect();
        a.feed(&samples);
        let mut s = Spectrum::new();
        s.update(&a);
        let ratio = (FREQ_MAX / FREQ_MIN).powf(1.0 / SPECTRUM_COLUMNS as f64);
        let col_at = |f: f64| ((f / FREQ_MIN).ln() / ratio.ln()) as usize;
        // Around the tone, where several columns share each bin, every column moves.
        for pair in s.columns[col_at(50.0)..col_at(110.0)].windows(2) {
            assert!((pair[1] - pair[0]).abs() > 1e-6, "flat step at {pair:?}");
        }
    }

    #[test]
    fn the_curve_between_points_touches_them_and_stays_close() {
        // Touches the points it starts and ends on.
        assert!((curve_between(0.0, 2.0, 5.0, 6.0, 0.0) - 2.0).abs() < 1e-9);
        assert!((curve_between(0.0, 2.0, 5.0, 6.0, 1.0) - 5.0).abs() < 1e-9);
        // Even beside a sharp peak it only strays a little past its two points.
        for i in 0..=20 {
            let v = curve_between(0.0, 10.0, 0.0, 0.0, i as f64 / 20.0);
            assert!((-1.5..=11.5).contains(&v), "{v} strays too far");
        }
    }

    #[test]
    fn a_curve_keeps_its_slope_through_a_point() {
        assert_eq!(curve_slope(2.0, 4.0), 3.0);
        // Level only at a symmetric peak, so uneven peaks stay pointed rather than flat.
        assert_eq!(curve_slope(5.0, -5.0), 0.0);
        assert_eq!(curve_slope(6.0, -2.0), 2.0);
    }

    #[test]
    fn peak_resets_after_reading() {
        let mut a = Analyzer::new();
        a.feed(&[0.5, -0.8, 0.1, 0.2]);
        assert!((a.take_peak() - 0.8).abs() < 1e-6);
        assert_eq!(a.take_peak(), 0.0);
    }
}
