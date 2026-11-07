//! The graphical equalizer: drag points to shape the sound, over a live spectrum.
use gtk::glib;
use gtk::pango::Alignment;
use gtk::prelude::*;
use std::cell::{Cell, RefCell};
use std::f64::consts::PI;
use std::rc::Rc;

use crate::app::Ctx;
use crate::dsp::{self, FREQ_MAX, FREQ_MIN, SPECTRUM_COLUMNS, SPECTRUM_FLOOR};
use crate::graphs::{self, fmt_db, fmt_freq};
use crate::model::{Band, Direction, GAIN_LIMIT};
use crate::theme;

const HANDLE_RADIUS: f64 = 7.0;
const HIT_RADIUS: f64 = 15.0;
const AXIS_H: f64 = 22.0;
const SNAP_DB: f64 = 0.35;
/// How far the live spectrum is blurred for display, in columns.
const SPECTRUM_BLUR: f64 = 0.8;
/// A fixed scale, so a point never jumps out from under the pointer mid-drag.
const RANGE: f64 = GAIN_LIMIT;

fn x_of(f: f64, w: f64) -> f64 {
    (f / FREQ_MIN).ln() / (FREQ_MAX / FREQ_MIN).ln() * w
}

fn f_of(x: f64, w: f64) -> f64 {
    FREQ_MIN * (FREQ_MAX / FREQ_MIN).powf((x / w).clamp(0.0, 1.0))
}

fn y_of(db: f64, h: f64, range: f64) -> f64 {
    (0.5 - db / (2.0 * range)) * h
}

fn db_of(y: f64, h: f64, range: f64) -> f64 {
    (0.5 - y / h) * 2.0 * range
}

/// Where a band's handle sits: pass and notch filters have no gain, so they rest on 0 dB.
/// Imported files can go past the graph's frequencies, and those bands rest on its edge so
/// they can still be grabbed.
fn handle_pos(b: &Band, w: f64, h: f64, range: f64) -> (f64, f64) {
    let db = if b.kind.uses_gain() { b.gain } else { 0.0 };
    (x_of(b.freq, w).clamp(0.0, w), y_of(db, h, range))
}

/// A band's frequency during a drag. One past the graph's edge keeps its exact frequency,
/// so changing only its gain doesn't change it, until it's dragged inward.
fn dragged_freq(start_freq: f64, start_x: f64, dx: f64, w: f64) -> f64 {
    let x = start_x + dx;
    if (start_freq < FREQ_MIN && x <= 0.0) || (start_freq > FREQ_MAX && x >= w) {
        return start_freq;
    }
    f_of(x, w).clamp(FREQ_MIN, FREQ_MAX)
}

/// A band's frequency after an arrow key. One past the graph's edge stays put when stepped
/// further out, and comes to the edge when stepped inward.
fn stepped_freq(freq: f64, factor: f64) -> f64 {
    if (freq < FREQ_MIN && factor < 1.0) || (freq > FREQ_MAX && factor > 1.0) {
        return freq;
    }
    (freq * factor).clamp(FREQ_MIN, FREQ_MAX)
}

fn hit(bands: &[Band], x: f64, y: f64, w: f64, h: f64, range: f64) -> Option<usize> {
    let mut best = None;
    let mut best_d = HIT_RADIUS;
    for (i, b) in bands.iter().enumerate() {
        let (hx, hy) = handle_pos(b, w, h, range);
        let d = (hx - x).hypot(hy - y);
        if d <= best_d {
            best_d = d;
            best = Some(i);
        }
    }
    best
}

#[derive(Clone, Copy)]
struct Drag {
    band: usize,
    start: (f64, f64),
    start_freq: f64,
}

pub fn build(ctx: Rc<Ctx>, dir: Direction) -> gtk::DrawingArea {
    let area = gtk::DrawingArea::builder()
        .hexpand(true)
        .vexpand(true)
        .focusable(true)
        .can_focus(true)
        .build();
    area.set_accessible_role(gtk::AccessibleRole::Application);
    area.update_property(&[gtk::accessible::Property::Label(
        "Equalizer graph. Arrow keys change the selected band, Tab and Shift+Tab cycle through the bands, Delete removes one, and Escape leaves the graph.",
    )]);
    area.set_size_request(320, 220);

    let hover: Rc<Cell<Option<usize>>> = Rc::new(Cell::new(None));
    let dragging: Rc<RefCell<Option<Drag>>> = Rc::new(RefCell::new(None));

    {
        let (ctx, hover, dragging) = (ctx.clone(), hover.clone(), dragging.clone());
        area.set_draw_func(move |a, cr, w, h| {
            draw(
                &ctx,
                dir,
                cr,
                w as f64,
                h as f64,
                hover.get(),
                dragging.borrow().is_some(),
                a.has_focus(),
            );
        });
    }

    // Hover: highlight the point under the pointer.
    let motion = gtk::EventControllerMotion::new();
    {
        let (ctx, hover, area2, dragging) = (ctx.clone(), hover.clone(), area.clone(), dragging.clone());
        motion.connect_motion(move |_, x, y| {
            if dragging.borrow().is_some() {
                return;
            }
            let (w, h) = (area2.width() as f64, area2.height() as f64 - AXIS_H);
            let found = hit(&ctx.state.borrow().chain(dir).sound.bands, x, y, w, h, RANGE);
            if found != hover.get() {
                hover.set(found);
                area2.set_cursor_from_name(Some(if found.is_some() { "grab" } else { "crosshair" }));
                area2.queue_draw();
            }
        });
    }
    {
        let (hover, area2) = (hover.clone(), area.clone());
        motion.connect_leave(move |_| {
            hover.set(None);
            area2.queue_draw();
        });
    }
    area.add_controller(motion);

    // Drag a point. Dragging also selects it.
    let drag = gtk::GestureDrag::new();
    {
        let (ctx, dragging, area2) = (ctx.clone(), dragging.clone(), area.clone());
        drag.connect_drag_begin(move |g, x, y| {
            let (w, h) = (area2.width() as f64, area2.height() as f64 - AXIS_H);
            let s = ctx.state.borrow();
            let bands = &s.chain(dir).sound.bands;
            let found = hit(bands, x, y, w, h, RANGE);
            let start = found.map(|i| (handle_pos(&bands[i], w, h, RANGE), bands[i].freq));
            drop(s);
            area2.grab_focus();
            match (found, start) {
                (Some(band), Some((start, start_freq))) => {
                    *dragging.borrow_mut() = Some(Drag { band, start, start_freq });
                    ctx.select(dir, band);
                    area2.set_cursor_from_name(Some("grabbing"));
                    g.set_state(gtk::EventSequenceState::Claimed);
                }
                _ => {
                    g.set_state(gtk::EventSequenceState::Denied);
                }
            }
        });
    }
    {
        let (ctx, dragging, area2) = (ctx.clone(), dragging.clone(), area.clone());
        drag.connect_drag_update(move |_, dx, dy| {
            let Some(d) = *dragging.borrow() else { return };
            let (w, h) = (area2.width() as f64, area2.height() as f64 - AXIS_H);
            ctx.edit_band(dir, d.band, |b| {
                b.freq = dragged_freq(d.start_freq, d.start.0, dx, w);
                if b.kind.uses_gain() {
                    let db = db_of(d.start.1 + dy, h, RANGE).clamp(-GAIN_LIMIT, GAIN_LIMIT);
                    b.gain = if db.abs() < SNAP_DB { 0.0 } else { (db * 10.0).round() / 10.0 };
                }
            });
            area2.queue_draw();
        });
    }
    {
        let (dragging, area2) = (dragging.clone(), area.clone());
        drag.connect_drag_end(move |_, _, _| {
            *dragging.borrow_mut() = None;
            area2.set_cursor_from_name(Some("grab"));
            area2.queue_draw();
        });
    }
    area.add_controller(drag);

    // Double-click: empty space adds a point, an existing point returns to 0 dB.
    let click = gtk::GestureClick::new();
    {
        let (ctx, area2, hover) = (ctx.clone(), area.clone(), hover.clone());
        click.connect_pressed(move |_, n, x, y| {
            let (w, h) = (area2.width() as f64, area2.height() as f64 - AXIS_H);
            let found = hit(&ctx.state.borrow().chain(dir).sound.bands, x, y, w, h, RANGE);
            area2.grab_focus();
            if let Some(i) = found {
                ctx.select(dir, i);
                if n == 2 {
                    ctx.edit_band(dir, i, |b| b.gain = 0.0);
                }
            } else if n == 2 && y <= h {
                let db = db_of(y, h, RANGE);
                let db = if db.abs() < SNAP_DB { 0.0 } else { (db * 10.0).round() / 10.0 };
                if let Some(i) = ctx.add_band(dir, Band::bell(f_of(x, w), db, 1.0)) {
                    hover.set(Some(i));
                }
            }
            area2.queue_draw();
        });
    }
    area.add_controller(click);

    // Scroll over a point to make it wider or narrower.
    let scroll = gtk::EventControllerScroll::new(gtk::EventControllerScrollFlags::VERTICAL);
    {
        let (ctx, hover, area2) = (ctx.clone(), hover.clone(), area.clone());
        scroll.connect_scroll(move |_, _, dy| {
            let Some(i) = hover.get() else { return glib::Propagation::Proceed };
            ctx.select(dir, i);
            ctx.edit_band(dir, i, |b| b.q = (b.q * (1.0 - dy * 0.08)).clamp(0.1, 20.0));
            area2.queue_draw();
            glib::Propagation::Stop
        });
    }
    area.add_controller(scroll);

    // Every mouse action has a keyboard equivalent.
    let keys = gtk::EventControllerKey::new();
    {
        let (ctx, area2) = (ctx.clone(), area.clone());
        keys.connect_key_pressed(move |_, key, _, mods| {
            use gtk::gdk::{Key, ModifierType};
            let fine = mods.contains(ModifierType::SHIFT_MASK);
            let i = ctx.selected(dir);
            let count = ctx.state.borrow().chain(dir).sound.bands.len();
            if count == 0 {
                return glib::Propagation::Proceed;
            }
            let db_step = if fine { 0.1 } else { 0.5 };
            let f_step: f64 = if fine { 1.005 } else { 1.03 };
            match key {
                Key::Up => ctx.edit_band(dir, i, |b| b.gain = (b.gain + db_step).clamp(-GAIN_LIMIT, GAIN_LIMIT)),
                Key::Down => ctx.edit_band(dir, i, |b| b.gain = (b.gain - db_step).clamp(-GAIN_LIMIT, GAIN_LIMIT)),
                Key::Right => ctx.edit_band(dir, i, |b| b.freq = stepped_freq(b.freq, f_step)),
                Key::Left => ctx.edit_band(dir, i, |b| b.freq = stepped_freq(b.freq, 1.0 / f_step)),
                // Holding Shift turns Tab into a different key, ISO_Left_Tab.
                Key::Tab | Key::KP_Tab => ctx.select(dir, next_band(i, count, fine)),
                Key::ISO_Left_Tab => ctx.select(dir, next_band(i, count, true)),
                // Tab stays on the graph, so this is how the keyboard gets back out.
                Key::Escape => {
                    area2.emit_move_focus(gtk::DirectionType::TabForward);
                    return glib::Propagation::Stop;
                }
                Key::Delete | Key::BackSpace => ctx.remove_band(dir, i),
                Key::plus | Key::equal | Key::KP_Add => {
                    ctx.add_band(dir, Band::bell(1000.0, 0.0, 1.0));
                }
                _ => return glib::Propagation::Proceed,
            }
            area2.queue_draw();
            glib::Propagation::Stop
        });
    }
    area.add_controller(keys);
    area
}

/// The band Tab or Shift+Tab selects, wrapping around at either end.
fn next_band(i: usize, count: usize, back: bool) -> usize {
    let i = i.min(count - 1);
    if back { (i + count - 1) % count } else { (i + 1) % count }
}

#[allow(clippy::too_many_arguments)]
fn draw(ctx: &Ctx, dir: Direction, cr: &gtk::cairo::Context, w: f64, full_h: f64, hover: Option<usize>, dragging: bool, focused: bool) {
    let h = (full_h - AXIS_H).max(40.0);
    let state = ctx.state.borrow();
    let chain = state.chain(dir);
    let sound = &chain.sound;
    let bands = &sound.bands;
    let range = RANGE;
    let rate = ctx.rate(dir);
    let selected = ctx.selected(dir).min(bands.len().saturating_sub(1));
    let active = chain.enabled && sound.eq_enabled;
    let color = theme::EQ_BLUE;

    graphs::panel_begin(cr, w, h);

    // Grid: decades and 1-2-5 steps, with dB lines.
    graphs::seta(cr, color, 0.11);
    cr.set_line_width(1.0);
    for f in [
        30., 40., 50., 60., 70., 80., 90., 200., 300., 400., 500., 600., 700., 800., 900., 2000., 3000., 4000., 5000., 6000., 7000., 8000.,
        9000.,
    ] {
        let x = x_of(f, w).round() + 0.5;
        cr.move_to(x, 0.0);
        cr.line_to(x, h);
    }
    let _ = cr.stroke();
    graphs::seta(cr, color, 0.24);
    for f in [100.0, 1000.0, 10_000.0] {
        let x = x_of(f, w).round() + 0.5;
        cr.move_to(x, 0.0);
        cr.line_to(x, h);
    }
    let labels = [12.0, -12.0];
    for db in [-18.0, -12.0, -6.0, 6.0, 12.0, 18.0] {
        let y = y_of(db, h, range).round() + 0.5;
        cr.move_to(0.0, y);
        cr.line_to(w, y);
    }
    let _ = cr.stroke();
    let zero = y_of(0.0, h, range).round() + 0.5;
    graphs::seta(cr, theme::TEXT, 0.32);
    cr.move_to(0.0, zero);
    cr.line_to(w, zero);
    let _ = cr.stroke();

    // Live spectrum behind the curve: filled after the EQ, outline before it.
    {
        let rt = ctx.rt[dir as usize].borrow();
        let y_sp = |db: f64| h - ((db - SPECTRUM_FLOOR) / -SPECTRUM_FLOOR).clamp(0.0, 1.0) * h * 0.74;
        let step = w / SPECTRUM_COLUMNS as f64;
        let curve = |cols: &[f64]| {
            let ys: Vec<f64> = graphs::smooth_levels(cols.iter().copied(), SPECTRUM_BLUR)
                .iter()
                .map(|db| y_sp(*db))
                .collect();
            graphs::curve_through(cr, step / 2.0, step, &ys);
        };
        let cols = &rt.spectrum_post.columns;
        if chain.enabled && cols.iter().any(|c| *c > SPECTRUM_FLOOR + 0.5) {
            cr.move_to(0.0, h);
            curve(cols);
            cr.line_to(w, h);
            cr.close_path();
            graphs::seta(cr, color, 0.30);
            let _ = cr.fill();
        }
        let cols = &rt.spectrum_pre.columns;
        if cols.iter().any(|c| *c > SPECTRUM_FLOOR + 0.5) {
            curve(cols);
            graphs::seta(cr, theme::TEXT, 0.20);
            cr.set_line_width(1.0);
            let _ = cr.stroke();
        }
    }

    let n = (w / 2.0).max(32.0) as usize;
    let f_at = |i: usize| f_of(i as f64 / n as f64 * w, w);

    // Each band's own contribution as a soft colored hill.
    for (i, b) in bands.iter().enumerate() {
        if !b.enabled {
            continue;
        }
        let c = theme::band_color(i);
        let is_sel = i == selected || hover == Some(i);
        cr.move_to(0.0, zero);
        for s in 0..=n {
            let f = f_at(s);
            let y = y_of(dsp::band_response_db(b, f, rate).clamp(-range * 1.1, range * 1.1), h, range);
            cr.line_to(x_of(f, w), y);
        }
        cr.line_to(w, zero);
        cr.close_path();
        graphs::seta(cr, c, if is_sel { 0.26 } else { 0.10 });
        let _ = cr.fill();
    }

    // The combined curve: what you actually hear.
    let curve_color = if active { theme::TEXT } else { theme::DIM };
    for s in 0..=n {
        let f = f_at(s);
        let db = if sound.eq_enabled {
            dsp::total_response_db(bands, f, rate)
        } else {
            0.0
        };
        let y = y_of((db + sound.preamp).clamp(-range * 1.2, range * 1.2), h, range);
        if s == 0 {
            cr.move_to(x_of(f, w), y)
        } else {
            cr.line_to(x_of(f, w), y)
        }
    }
    graphs::set(cr, curve_color);
    cr.set_line_width(2.2);
    cr.set_line_join(gtk::cairo::LineJoin::Round);
    let _ = cr.stroke();

    // dB labels on the left edge.
    for db in labels {
        graphs::text(
            cr,
            &fmt_db(db).replace(" dB", ""),
            6.0,
            y_of(db, h, range) + 2.0,
            11.0,
            theme::DIM,
            0.85,
            Alignment::Left,
        );
    }
    graphs::text(cr, "0 dB", 6.0, zero + 2.0, 11.0, theme::DIM, 0.85, Alignment::Left);

    // Handles on top.
    for (i, b) in bands.iter().enumerate() {
        let (x, y) = handle_pos(b, w, h, range);
        let c = theme::band_color(i);
        let hot = i == selected || hover == Some(i);
        if !b.enabled {
            graphs::seta(cr, c, 0.5);
            cr.set_line_width(1.5);
            cr.arc(x, y, HANDLE_RADIUS - 1.0, 0.0, 2.0 * PI);
            let _ = cr.stroke();
            continue;
        }
        if hot {
            graphs::seta(cr, c, 0.28);
            cr.arc(x, y, HANDLE_RADIUS + 6.0, 0.0, 2.0 * PI);
            let _ = cr.fill();
        }
        graphs::set(cr, c);
        cr.arc(x, y, HANDLE_RADIUS, 0.0, 2.0 * PI);
        let _ = cr.fill_preserve();
        graphs::set(cr, if i == selected { theme::TEXT } else { theme::GRAPH_BG });
        cr.set_line_width(if i == selected { 2.0 } else { 1.5 });
        let _ = cr.stroke();
    }

    // A readout beside the point being worked on.
    if let Some(i) = hover.or(if dragging { Some(selected) } else { None }).filter(|i| *i < bands.len()) {
        let b = &bands[i];
        let (x, y) = handle_pos(b, w, h, range);
        let label = if b.kind.uses_gain() {
            format!("{}   {}   Q {:.2}", fmt_freq(b.freq), fmt_db(b.gain), b.q)
        } else {
            format!("{}   Q {:.2}", fmt_freq(b.freq), b.q)
        };
        let box_w = 8.2 * label.chars().count() as f64 * 0.86 + 16.0;
        let bx = (x - box_w / 2.0).clamp(4.0, (w - box_w - 4.0).max(4.0));
        let by = if y - 44.0 > 4.0 { y - 44.0 } else { y + 22.0 };
        graphs::rounded(cr, bx, by, box_w, 24.0, 5.0);
        cr.set_source_rgba(0.05, 0.06, 0.08, 0.93);
        let _ = cr.fill_preserve();
        graphs::seta(cr, theme::band_color(i), 0.9);
        cr.set_line_width(1.0);
        let _ = cr.stroke();
        graphs::text(cr, &label, bx + box_w / 2.0, by + 4.5, 12.0, theme::TEXT, 1.0, Alignment::Center);
    }

    graphs::panel_end(cr, w, h, if focused { theme::TEXT } else { color });

    // Frequency axis.
    for f in [20.0, 50.0, 100.0, 200.0, 500.0, 1000.0, 2000.0, 5000.0, 10_000.0, 20_000.0] {
        let label = if f >= 1000.0 { format!("{}k", f / 1000.0) } else { format!("{f}") };
        let x = x_of(f, w);
        let align = if f <= 20.0 {
            Alignment::Left
        } else if f >= 20_000.0 {
            Alignment::Right
        } else {
            Alignment::Center
        };
        graphs::text(cr, &label, x, h + 4.0, 11.0, theme::DIM, 0.9, align);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn tab_cycles_through_the_bands_in_both_directions() {
        assert_eq!(next_band(0, 3, false), 1);
        assert_eq!(next_band(2, 3, false), 0, "forward wraps to the first");
        assert_eq!(next_band(1, 3, true), 0);
        assert_eq!(next_band(0, 3, true), 2, "back wraps to the last");
        assert_eq!(next_band(0, 1, false), 0, "a single band stays selected");
        assert_eq!(next_band(0, 1, true), 0);
        assert_eq!(next_band(7, 3, false), 0, "a stale selection is treated as the last");
        assert_eq!(next_band(7, 3, true), 1);
    }

    #[test]
    fn positions_convert_both_ways() {
        let f = f_of(x_of(1234.0, 800.0), 800.0);
        assert!((f - 1234.0).abs() < 0.001);
        let db = db_of(y_of(7.5, 400.0, 24.0), 400.0, 24.0);
        assert!((db - 7.5).abs() < 1e-9);
    }

    #[test]
    fn clicks_find_the_nearest_point() {
        let bands = vec![Band::bell(100.0, 0.0, 1.0), Band::bell(1000.0, 0.0, 1.0)];
        let (x, y) = handle_pos(&bands[1], 800.0, 400.0, 12.0);
        assert_eq!(hit(&bands, x + 3.0, y - 3.0, 800.0, 400.0, 12.0), Some(1));
        assert_eq!(hit(&bands, x, y + 80.0, 800.0, 400.0, 12.0), None);
    }

    #[test]
    fn points_past_the_graph_can_still_be_clicked() {
        let bands = vec![Band::bell(10.0, 3.0, 1.0), Band::bell(24_000.0, -3.0, 1.0)];
        let (low_x, low_y) = handle_pos(&bands[0], 800.0, 400.0, 12.0);
        let (high_x, high_y) = handle_pos(&bands[1], 800.0, 400.0, 12.0);
        assert_eq!(low_x, 0.0);
        assert_eq!(high_x, 800.0);
        assert_eq!(hit(&bands, 2.0, low_y, 800.0, 400.0, 12.0), Some(0));
        assert_eq!(hit(&bands, 798.0, high_y, 800.0, 400.0, 12.0), Some(1));
    }

    #[test]
    fn dragging_a_point_past_the_graph_keeps_its_frequency_until_it_moves_inward() {
        let w = 800.0;
        assert_eq!(dragged_freq(10.0, 0.0, 0.0, w), 10.0, "straight up or down");
        assert_eq!(dragged_freq(10.0, 0.0, -30.0, w), 10.0, "further out");
        assert!((dragged_freq(10.0, 0.0, w / 3.0, w) - 200.0).abs() < 0.001, "inward");
        assert_eq!(dragged_freq(24_000.0, w, 0.0, w), 24_000.0);
        assert_eq!(dragged_freq(24_000.0, w, 30.0, w), 24_000.0);
        assert!((dragged_freq(24_000.0, w, -w / 3.0, w) - 2000.0).abs() < 0.001);
    }

    #[test]
    fn dragging_a_point_on_the_graph_still_stops_at_its_edges() {
        let w = 800.0;
        let x = x_of(1000.0, w);
        assert!((dragged_freq(1000.0, x, 0.0, w) - 1000.0).abs() < 0.001);
        assert_eq!(dragged_freq(1000.0, x, -w, w), FREQ_MIN);
        assert_eq!(dragged_freq(1000.0, x, w, w), FREQ_MAX);
    }

    #[test]
    fn arrow_keys_bring_a_point_past_the_graph_inward_but_never_further_out() {
        assert_eq!(stepped_freq(10.0, 0.97), 10.0);
        assert_eq!(stepped_freq(10.0, 1.03), FREQ_MIN);
        assert_eq!(stepped_freq(24_000.0, 1.03), 24_000.0);
        assert_eq!(stepped_freq(24_000.0, 0.97), FREQ_MAX);
        assert!((stepped_freq(1000.0, 1.03) - 1030.0).abs() < 0.001);
    }
}
