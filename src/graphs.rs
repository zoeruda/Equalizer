//! Drawing shared by every graph: the outlined panel, grid, traces and text.
use gtk::cairo::Context;
use gtk::pango;
use std::collections::VecDeque;
use std::f64::consts::PI;

use crate::theme::{self, Rgb};

pub const HISTORY_FLOOR: f64 = -60.0;

pub fn rounded(cr: &Context, x: f64, y: f64, w: f64, h: f64, r: f64) {
    cr.new_sub_path();
    cr.arc(x + w - r, y + r, r, -PI / 2.0, 0.0);
    cr.arc(x + w - r, y + h - r, r, 0.0, PI / 2.0);
    cr.arc(x + r, y + h - r, r, PI / 2.0, PI);
    cr.arc(x + r, y + r, r, PI, 3.0 * PI / 2.0);
    cr.close_path();
}

pub fn set(cr: &Context, c: Rgb) {
    cr.set_source_rgb(c.0, c.1, c.2);
}

pub fn seta(cr: &Context, c: Rgb, a: f64) {
    cr.set_source_rgba(c.0, c.1, c.2, a);
}

/// Fills the panel and clips to it. Pair with `panel_end`.
pub fn panel_begin(cr: &Context, w: f64, h: f64) {
    let _ = cr.save();
    rounded(cr, 0.5, 0.5, w - 1.0, h - 1.0, 6.0);
    set(cr, theme::GRAPH_BG);
    let _ = cr.fill_preserve();
    cr.clip();
}

pub fn panel_end(cr: &Context, w: f64, h: f64, color: Rgb) {
    let _ = cr.restore();
    rounded(cr, 0.5, 0.5, w - 1.0, h - 1.0, 6.0);
    set(cr, color);
    cr.set_line_width(1.2);
    let _ = cr.stroke();
}

pub fn grid(cr: &Context, w: f64, h: f64, cols: usize, rows: usize, color: Rgb) {
    seta(cr, color, 0.16);
    cr.set_line_width(1.0);
    for c in 1..cols {
        let x = (c as f64 / cols as f64 * w).round() + 0.5;
        cr.move_to(x, 0.0);
        cr.line_to(x, h);
    }
    for r in 1..rows {
        let y = (r as f64 / rows as f64 * h).round() + 0.5;
        cr.move_to(0.0, y);
        cr.line_to(w, y);
    }
    let _ = cr.stroke();
}

pub fn text(cr: &Context, s: &str, x: f64, y: f64, size: f64, color: Rgb, alpha: f64, align: pango::Alignment) -> (f64, f64) {
    let layout = pangocairo::functions::create_layout(cr);
    let mut desc = pango::FontDescription::new();
    desc.set_family("sans");
    desc.set_absolute_size(size * pango::SCALE as f64);
    layout.set_font_description(Some(&desc));
    layout.set_text(s);
    let (tw, th) = layout.pixel_size();
    let tx = match align {
        pango::Alignment::Center => x - tw as f64 / 2.0,
        pango::Alignment::Right => x - tw as f64,
        _ => x,
    };
    seta(cr, color, alpha);
    cr.move_to(tx, y);
    pangocairo::functions::show_layout(cr, &layout);
    // Showing text keeps the current point, and the next arc would draw a line from it.
    cr.new_path();
    (tw as f64, th as f64)
}

/// A scrolling level trace, newest on the right, in dBFS.
pub fn trace(cr: &Context, w: f64, h: f64, data: &VecDeque<f64>, capacity: usize, color: Rgb, fill: bool, line_alpha: f64) {
    if data.len() < 2 {
        return;
    }
    let step = w / (capacity.max(2) - 1) as f64;
    let start = w - (data.len() - 1) as f64 * step;
    let y_of = |db: f64| h - ((db - HISTORY_FLOOR) / -HISTORY_FLOOR).clamp(0.0, 1.0) * (h - 3.0);
    cr.move_to(start, y_of(data[0]));
    for (i, v) in data.iter().enumerate().skip(1) {
        cr.line_to(start + i as f64 * step, y_of(*v));
    }
    if fill {
        let path = cr.copy_path();
        cr.line_to(w, h);
        cr.line_to(start, h);
        cr.close_path();
        seta(cr, color, 0.34);
        let _ = cr.fill();
        if let Ok(path) = path {
            cr.append_path(&path);
        }
    }
    seta(cr, color, line_alpha);
    cr.set_line_width(1.4);
    let _ = cr.stroke();
}

/// Input vs output plot for compressors and limiters. Values are in dB.
pub fn transfer(cr: &Context, w: f64, h: f64, color: Rgb, curve: &dyn Fn(f64) -> f64, range: f64, input: Option<f64>) {
    grid(cr, w, h, 6, 6, color);
    let px = |db: f64| (db + range) / range * w;
    let py = |db: f64| h - (db + range) / range * h;
    // The "no change" diagonal.
    seta(cr, theme::DIM, 0.35);
    cr.set_dash(&[4.0, 4.0], 0.0);
    cr.set_line_width(1.0);
    cr.move_to(0.0, h);
    cr.line_to(w, 0.0);
    let _ = cr.stroke();
    cr.set_dash(&[], 0.0);

    let n = w.max(2.0) as usize;
    cr.move_to(0.0, py(curve(-range)));
    for i in 1..=n {
        let db = -range + range * i as f64 / n as f64;
        cr.line_to(px(db), py(curve(db)));
    }
    let path = cr.copy_path();
    cr.line_to(w, h);
    cr.line_to(0.0, h);
    cr.close_path();
    seta(cr, color, 0.20);
    let _ = cr.fill();
    if let Ok(path) = path {
        cr.append_path(&path);
    }
    set(cr, color);
    cr.set_line_width(2.0);
    let _ = cr.stroke();

    for frac in [0.25, 0.5, 0.75] {
        let db = -range * (1.0 - frac);
        text(
            cr,
            &format!("{db:.0}"),
            frac * w,
            h - 18.0,
            11.0,
            theme::DIM,
            0.8,
            pango::Alignment::Center,
        );
        text(
            cr,
            &format!("{db:.0}"),
            6.0,
            (1.0 - frac) * h - 7.0,
            11.0,
            theme::DIM,
            0.8,
            pango::Alignment::Left,
        );
    }

    if let Some(db) = input.filter(|d| *d > -range) {
        let (x, y) = (px(db), py(curve(db)));
        seta(cr, color, 0.30);
        cr.arc(x, y, 9.0, 0.0, 2.0 * PI);
        let _ = cr.fill();
        set(cr, theme::TEXT);
        cr.arc(x, y, 4.0, 0.0, 2.0 * PI);
        let _ = cr.fill();
    }
}

/// Soft-knee downward compression, in dB.
pub fn compress(db: f64, threshold: f64, ratio: f64, knee: f64, makeup: f64) -> f64 {
    let width = (-knee).max(0.001) * 2.0;
    let over = db - threshold;
    let out = if over * 2.0 < -width {
        db
    } else if over * 2.0 > width {
        threshold + over / ratio
    } else {
        db + (1.0 / ratio - 1.0) * (over + width / 2.0).powi(2) / (2.0 * width)
    };
    out + makeup
}

pub fn fmt_freq(f: f64) -> String {
    if f >= 10_000.0 {
        format!("{:.1} kHz", f / 1000.0)
    } else if f >= 1000.0 {
        format!("{:.2} kHz", f / 1000.0)
    } else {
        format!("{f:.0} Hz")
    }
}

pub fn fmt_db(d: f64) -> String {
    let d = if d.abs() < 0.05 { 0.0 } else { d };
    if d > 0.0 {
        format!("+{d:.1} dB")
    } else {
        format!("{d:.1} dB").replace('-', "−")
    }
}

pub fn fmt_level(d: f64) -> String {
    if d <= HISTORY_FLOOR { "—".to_string() } else { fmt_db(d) }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn compression_leaves_quiet_sound_alone_and_reduces_loud_sound() {
        assert!((compress(-40.0, -18.0, 3.0, -6.0, 0.0) + 40.0).abs() < 1e-9);
        assert!((compress(0.0, -18.0, 3.0, -6.0, 0.0) + 12.0).abs() < 1e-9);
        assert!((compress(-40.0, -18.0, 3.0, -6.0, 5.0) + 35.0).abs() < 1e-9);
    }

    #[test]
    fn the_knee_joins_up_without_a_jump() {
        let a = compress(-24.0, -18.0, 4.0, -6.0, 0.0);
        let b = compress(-23.999, -18.0, 4.0, -6.0, 0.0);
        assert!((a - b).abs() < 0.01);
    }

    #[test]
    fn text_leaves_nothing_for_the_next_shape_to_connect_to() {
        let surface = gtk::cairo::ImageSurface::create(gtk::cairo::Format::ARgb32, 100, 100).expect("surface");
        let cr = Context::new(&surface).expect("context");
        text(&cr, "0 dB", 6.0, 50.0, 11.0, theme::DIM, 0.85, pango::Alignment::Left);
        // An arc would otherwise draw a line from the label to wherever the arc starts.
        assert!(!cr.has_current_point().expect("cairo state"));
    }

    #[test]
    fn numbers_read_cleanly() {
        assert_eq!(fmt_freq(1000.0), "1.00 kHz");
        assert_eq!(fmt_freq(62.4), "62 Hz");
        assert_eq!(fmt_db(0.01), "0.0 dB");
        assert_eq!(fmt_db(3.0), "+3.0 dB");
        assert_eq!(fmt_db(-3.0), "−3.0 dB");
    }
}
