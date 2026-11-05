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
}

impl Analyzer {
    pub fn new() -> Self {
        Self {
            ring: vec![0.0; FFT_SIZE],
            pos: 0,
            rate: 48_000,
            peak: 0.0,
        }
    }

    /// `samples` is interleaved stereo.
    pub fn feed(&mut self, samples: &[f32]) {
        for frame in samples.as_chunks::<2>().0 {
            self.peak = self.peak.max(frame[0].abs()).max(frame[1].abs());
            self.ring[self.pos] = (frame[0] + frame[1]) * 0.5;
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

/// Turns the newest samples into log-spaced columns of dB values.
pub struct Spectrum {
    fft: Arc<dyn Fft<f32>>,
    window: Vec<f32>,
    input: Vec<f32>,
    buf: Vec<Complex<f32>>,
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
            columns: vec![SPECTRUM_FLOOR; SPECTRUM_COLUMNS],
        }
    }

    pub fn update(&mut self, analyzer: &Analyzer) {
        analyzer.snapshot(&mut self.input);
        for i in 0..FFT_SIZE {
            self.buf[i] = Complex::new(self.input[i] * self.window[i], 0.0);
        }
        self.fft.process(&mut self.buf);
        let rate = analyzer.rate as f64;
        let bin_hz = rate / FFT_SIZE as f64;
        let ratio = (FREQ_MAX / FREQ_MIN).powf(1.0 / SPECTRUM_COLUMNS as f64);
        // A Hann window halves the amplitude, hence 4 rather than 2.
        let norm = 4.0 / FFT_SIZE as f64;
        for c in 0..SPECTRUM_COLUMNS {
            let lo = FREQ_MIN * ratio.powi(c as i32);
            let hi = lo * ratio;
            let a = ((lo / bin_hz) as usize).clamp(1, FFT_SIZE / 2 - 1);
            let b = ((hi / bin_hz).ceil() as usize).clamp(a + 1, FFT_SIZE / 2);
            let mut m = 0f64;
            for bin in &self.buf[a..b] {
                m = m.max(bin.norm() as f64 * norm);
            }
            let db = to_db(m).max(SPECTRUM_FLOOR);
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
    fn peak_resets_after_reading() {
        let mut a = Analyzer::new();
        a.feed(&[0.5, -0.8, 0.1, 0.2]);
        assert!((a.take_peak() - 0.8).abs() < 1e-6);
        assert_eq!(a.take_peak(), 0.0);
    }
}
