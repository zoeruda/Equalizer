//! The application window and the shared state behind it.
use adw::prelude::*;
use gtk::{gdk, gio, glib, pango};
use std::cell::{Cell, RefCell};
use std::collections::{BTreeMap, VecDeque};
use std::path::PathBuf;
use std::rc::Rc;
use std::time::{Duration, Instant};

use crate::dsp::{self, SPECTRUM_FLOOR, Spectrum};
use crate::engine::{self, Cmd, Device, Event};
use crate::eq_graph;
use crate::graphs::{self, HISTORY_FLOOR, fmt_db, fmt_freq, fmt_level};
use crate::model::{
    self, AppState, Band, Direction, EffectSpec, FilterKind, GAIN_LIMIT, MAX_BANDS, ParamSpec, Plot, Sound, effect, plugin_controls,
};
use crate::theme::{self, Rgb};

pub const APP_ID: &str = "io.github.zoeruda.Equalizer";
const HISTORY_LEN: usize = 900; // one minute at the 15 Hz history rate
const SPARK_LEN: usize = 150;
/// How long the after-processing graph waits for its meter after the chain turns on,
/// in frames. Past this, silence is shown as silence.
const AFTER_GRACE: u32 = 30;
/// How far each level graph is blurred. The small ones pack in about two samples per pixel,
/// so they take far less, or their jitter disappears.
const LEVEL_BLUR_PX: f64 = 1.0;
const SPARK_BLUR_PX: f64 = 0.4;
const DIRS: [Direction; 2] = [Direction::Output, Direction::Mic];

/// Live measurements for one chain.
pub struct Rt {
    pub spectrum_pre: Spectrum,
    pub spectrum_post: Spectrum,
    pub history_pre: VecDeque<f64>,
    pub history_post: VecDeque<f64>,
    pub peak_pre: f64,
    pub peak_post: f64,
    pub rate: f64,
    frame: u32,
    smooth_pre: f64,
    smooth_post: f64,
    after: AfterStart,
    /// Nothing the graphs show is moving: see `at_rest`.
    still: bool,
}

/// Decides when the after-processing graph starts again once the chain turns on. Its
/// meter takes a moment to connect, and recording before then would start each stretch
/// with a drop to silence that never happened.
#[derive(Default)]
struct AfterStart {
    started: bool,
    waited: u32,
}

impl AfterStart {
    /// Whether this frame's after-processing level should be recorded.
    fn step(&mut self, processing: bool, has_sound: bool) -> bool {
        if !processing {
            *self = Self::default();
            return false;
        }
        if !self.started {
            self.waited += 1;
            self.started = has_sound || self.waited >= AFTER_GRACE;
        }
        self.started
    }
}

impl Rt {
    fn new() -> Self {
        Self {
            spectrum_pre: Spectrum::new(),
            spectrum_post: Spectrum::new(),
            history_pre: VecDeque::with_capacity(HISTORY_LEN + 1),
            history_post: VecDeque::with_capacity(HISTORY_LEN + 1),
            peak_pre: HISTORY_FLOOR,
            peak_post: HISTORY_FLOOR,
            rate: 48_000.0,
            frame: 0,
            smooth_pre: HISTORY_FLOOR,
            smooth_post: HISTORY_FLOOR,
            after: AfterStart::default(),
            still: false,
        }
    }
}

type Listener = Rc<dyn Fn(Direction)>;

pub struct Ctx {
    pub state: RefCell<AppState>,
    pub engine: engine::Handle,
    pub rt: [RefCell<Rt>; 2],
    selected: [Cell<usize>; 2],
    sent: [RefCell<BTreeMap<String, f32>>; 2],
    plugin: PathBuf,
    dirty: Cell<bool>,
    save_at: Cell<Option<Instant>>,
    listeners: RefCell<Vec<Listener>>,
}

impl Ctx {
    pub fn rate(&self, dir: Direction) -> f64 {
        self.rt[dir as usize].borrow().rate
    }

    pub fn selected(&self, dir: Direction) -> usize {
        self.selected[dir as usize].get()
    }

    pub fn select(&self, dir: Direction, i: usize) {
        if self.selected[dir as usize].replace(i) != i {
            self.refresh(dir);
        }
    }

    fn refresh(&self, dir: Direction) {
        let listeners: Vec<Listener> = self.listeners.borrow().clone();
        for l in listeners {
            l(dir);
        }
    }

    fn listen(&self, l: impl Fn(Direction) + 'static) {
        self.listeners.borrow_mut().push(Rc::new(l));
    }

    fn mark_changed(&self) {
        self.dirty.set(true);
        self.save_at.set(Some(Instant::now() + Duration::from_secs(1)));
    }

    /// Records a change to the sound. The preset name stops applying once edited.
    pub fn edit_sound(&self, dir: Direction, f: impl FnOnce(&mut Sound)) {
        {
            let mut s = self.state.borrow_mut();
            let chain = s.chain_mut(dir);
            f(&mut chain.sound);
            chain.preset = "Custom".into();
        }
        self.mark_changed();
        self.refresh(dir);
    }

    pub fn edit_band(&self, dir: Direction, i: usize, f: impl FnOnce(&mut Band)) {
        self.edit_sound(dir, |s| {
            if let Some(b) = s.bands.get_mut(i) {
                f(b);
            }
        });
    }

    pub fn add_band(&self, dir: Direction, band: Band) -> Option<usize> {
        let count = self.state.borrow().chain(dir).sound.bands.len();
        if count >= MAX_BANDS {
            return None;
        }
        self.edit_sound(dir, |s| s.bands.push(band));
        self.select(dir, count);
        Some(count)
    }

    pub fn remove_band(&self, dir: Direction, i: usize) {
        self.edit_sound(dir, |s| {
            if i < s.bands.len() {
                s.bands.remove(i);
            }
        });
        let count = self.state.borrow().chain(dir).sound.bands.len();
        self.selected[dir as usize].set(i.min(count.saturating_sub(1)));
        self.refresh(dir);
    }

    fn load_sound(&self, dir: Direction, name: &str, sound: Sound) {
        {
            let mut s = self.state.borrow_mut();
            let chain = s.chain_mut(dir);
            chain.sound = sound;
            chain.preset = name.to_string();
        }
        self.selected[dir as usize].set(0);
        self.mark_changed();
        self.refresh(dir);
    }

    /// Sends only the plugin controls that actually changed.
    fn push_params(&self, dir: Direction) {
        let wanted = plugin_controls(&self.state.borrow().chain(dir).sound, dir);
        let mut sent = self.sent[dir as usize].borrow_mut();
        let changed: Vec<(String, f32)> = wanted
            .iter()
            .filter(|(k, v)| sent.get(*k) != Some(*v))
            .map(|(k, v)| (k.clone(), *v))
            .collect();
        if !changed.is_empty() {
            self.engine.send(Cmd::Params(dir, changed));
            *sent = wanted;
        }
    }

    fn start(&self, dir: Direction) {
        let (device, controls) = {
            let s = self.state.borrow();
            let c = s.chain(dir);
            (c.device.clone(), plugin_controls(&c.sound, dir))
        };
        *self.sent[dir as usize].borrow_mut() = controls.clone();
        self.engine.send(Cmd::Enable {
            dir,
            device,
            controls,
            plugin: self.plugin.clone(),
        });
    }

    fn set_enabled(&self, dir: Direction, on: bool) {
        let was = {
            let mut s = self.state.borrow_mut();
            std::mem::replace(&mut s.chain_mut(dir).enabled, on)
        };
        if was == on {
            return;
        }
        if on {
            self.start(dir);
        } else {
            self.engine.send(Cmd::Disable(dir));
        }
        self.mark_changed();
        self.refresh(dir);
    }

    fn save_now(&self) {
        self.save_at.set(None);
        if let Err(e) = model::save_state(&self.state.borrow()) {
            eprintln!("equalizer: couldn't save settings: {e}");
        }
    }
}

fn label(text: &str, class: &str) -> gtk::Label {
    let l = gtk::Label::new(Some(text));
    l.add_css_class(class);
    l.set_xalign(0.0);
    l
}

fn parse_number(text: &str) -> Option<f64> {
    let t = text.trim().to_lowercase().replace('−', "-");
    let kilo = t.contains("khz") || t.ends_with('k');
    let digits: String = t
        .chars()
        .take_while(|c| c.is_ascii_digit() || matches!(c, '.' | '-' | '+'))
        .collect();
    let v: f64 = digits.parse().ok()?;
    v.is_finite().then_some(if kilo { v * 1000.0 } else { v })
}

fn fmt_unit(v: f64, unit: &str) -> String {
    match unit {
        "Hz" => fmt_freq(v),
        "dB" => fmt_db(v),
        "ms" if v < 10.0 => format!("{v:.1} ms"),
        "ms" => format!("{v:.0} ms"),
        ":1" => format!("{v:.1}:1"),
        "Q" => format!("{v:.2}"),
        "%" => format!("{v:.0}%"),
        "LUFS" => format!("{v:.0} LUFS").replace('-', "−"),
        _ => format!("{v:.1} {unit}"),
    }
}

struct Slider<'a> {
    label: &'a str,
    help: &'a str,
    min: f64,
    max: f64,
    log: bool,
    unit: &'a str,
}

/// A labelled slider whose value can also be clicked and typed in.
fn slider(spec: Slider, get: Rc<dyn Fn() -> f64>, set: Rc<dyn Fn(f64)>) -> (gtk::Box, Rc<dyn Fn()>) {
    let (min, max, log) = (spec.min, spec.max, spec.log);
    let to_pos = move |v: f64| {
        if log {
            (v / min).ln() / (max / min).ln()
        } else {
            (v - min) / (max - min)
        }
    };
    let from_pos = move |p: f64| if log { min * (max / min).powf(p) } else { min + (max - min) * p };

    let root = gtk::Box::new(gtk::Orientation::Vertical, 0);
    root.set_tooltip_text(Some(spec.help));
    let row = gtk::Box::new(gtk::Orientation::Horizontal, 6);
    let name = label(spec.label, "param-label");
    name.set_hexpand(true);
    let value = gtk::EditableLabel::new("");
    value.add_css_class("param-value");
    value.set_halign(gtk::Align::End);
    value.set_alignment(1.0);
    value.set_max_width_chars(11);
    value.set_width_chars(9);
    row.append(&name);
    row.append(&value);
    let scale = gtk::Scale::with_range(gtk::Orientation::Horizontal, 0.0, 1.0, 0.001);
    scale.set_draw_value(false);
    scale.set_hexpand(true);
    scale.update_property(&[gtk::accessible::Property::Label(spec.label)]);
    root.append(&row);
    root.append(&scale);

    let busy = Rc::new(Cell::new(false));
    let unit = spec.unit.to_string();
    let sync: Rc<dyn Fn()> = {
        let (scale, value, get, busy, unit) = (scale.clone(), value.clone(), get.clone(), busy.clone(), unit.clone());
        Rc::new(move || {
            busy.set(true);
            let v = get();
            scale.set_value(to_pos(v.clamp(min, max)));
            if !value.is_editing() {
                value.set_text(&fmt_unit(v, &unit));
            }
            busy.set(false);
        })
    };
    {
        let (set, busy, value, unit) = (set.clone(), busy.clone(), value.clone(), unit.clone());
        scale.connect_value_changed(move |s| {
            if busy.get() {
                return;
            }
            let v = from_pos(s.value());
            value.set_text(&fmt_unit(v, &unit));
            set(v);
        });
    }
    {
        let (set, sync, get, unit) = (set.clone(), sync.clone(), get.clone(), unit.clone());
        value.connect_editing_notify(move |l| {
            if l.is_editing() {
                return;
            }
            // Only a value that was really retyped counts; just clicking away changes nothing.
            if l.text() != fmt_unit(get(), &unit)
                && let Some(v) = parse_number(&l.text())
            {
                set(v.clamp(min, max));
            }
            sync();
        });
    }
    sync();
    (root, sync)
}

fn readout(title: &str) -> (gtk::Box, gtk::Label) {
    let b = gtk::Box::new(gtk::Orientation::Vertical, 2);
    b.append(&label(title, "readout-label"));
    let v = label("—", "readout-value");
    b.append(&v);
    (b, v)
}

fn spec_row(grid: &gtk::Grid, row: i32, key: &str, value: &str) -> gtk::Label {
    grid.attach(&label(key, "spec-key"), 0, row, 1, 1);
    let v = label(value, "spec-value");
    v.set_ellipsize(pango::EllipsizeMode::End);
    v.set_hexpand(true);
    grid.attach(&v, 1, row, 1, 1);
    v
}

/// Everything redrawn on each frame.
#[derive(Default)]
struct Live {
    /// Each with whether it currently shows anything that moves.
    areas: Vec<(gtk::Widget, Box<dyn Fn() -> bool>)>,
    updaters: Vec<Box<dyn Fn()>>,
}

impl Live {
    fn add(&mut self, area: &impl IsA<gtk::Widget>) {
        self.areas.push((area.clone().upcast(), Box::new(|| true)));
    }
}

type LiveRef = Rc<RefCell<Live>>;

fn level_graph(ctx: &Rc<Ctx>, dir: Direction, color: Rgb, live: &LiveRef) -> gtk::DrawingArea {
    let area = gtk::DrawingArea::builder().hexpand(true).vexpand(true).build();
    area.set_size_request(200, 90);
    let ctx = ctx.clone();
    area.set_draw_func(move |_, cr, w, h| {
        let (w, h) = (w as f64, h as f64);
        graphs::panel_begin(cr, w, h);
        graphs::grid(cr, w, h, 20, 4, color);
        let rt = ctx.rt[dir as usize].borrow();
        graphs::trace(cr, w, h, &rt.history_post, HISTORY_LEN, color, true, 1.0, LEVEL_BLUR_PX);
        graphs::trace(cr, w, h, &rt.history_pre, HISTORY_LEN, theme::TEXT, false, 0.35, LEVEL_BLUR_PX);
        drop(rt);
        graphs::panel_end(cr, w, h, color);
    });
    live.borrow_mut().add(&area);
    area
}

/// The input/output plot on compressor, limiter and gate pages.
fn plot_graph(ctx: &Rc<Ctx>, dir: Direction, spec: &'static EffectSpec, live: &LiveRef) -> gtk::DrawingArea {
    let area = gtk::DrawingArea::builder().hexpand(true).vexpand(true).build();
    area.set_size_request(200, 200);
    let ctx = ctx.clone();
    area.set_draw_func(move |_, cr, w, h| {
        let (w, h) = (w as f64, h as f64);
        graphs::panel_begin(cr, w, h);
        let s = ctx.state.borrow();
        let sound = &s.chain(dir).sound;
        let on = sound.is_on(spec.id);
        let color = if on { spec.color } else { theme::DIM };
        // With the chain off, the effect isn't acting on the sound at all.
        let input = s.chain(dir).enabled.then(|| ctx.rt[dir as usize].borrow().smooth_pre);
        let curve: Box<dyn Fn(f64) -> f64> = if !on {
            Box::new(|d| d)
        } else if spec.id == "compressor" {
            let (t, r, k, m) = (
                sound.param("compressor", "threshold"),
                sound.param("compressor", "ratio"),
                sound.param("compressor", "knee"),
                sound.param("compressor", "makeup"),
            );
            Box::new(move |d| graphs::compress(d, t, r, k, m))
        } else if spec.id == "limiter" {
            let t = sound.param("limiter", "threshold");
            Box::new(move |d| d.min(t))
        } else {
            let (t, r) = (sound.param("gate", "threshold"), sound.param("gate", "reduction"));
            Box::new(move |d| if d < t { d + r } else { d })
        };
        let range = if spec.id == "gate" { 90.0 } else { 60.0 };
        graphs::transfer(cr, w, h, color, &*curve, range, input);
        drop(s);
        graphs::text(
            cr,
            "Input level →",
            w - 8.0,
            h - 18.0,
            11.0,
            theme::DIM,
            0.9,
            pango::Alignment::Right,
        );
        graphs::text(cr, "↑ Output level", 8.0, 6.0, 11.0, theme::DIM, 0.9, pango::Alignment::Left);
        graphs::panel_end(cr, w, h, color);
    });
    live.borrow_mut().add(&area);
    area
}

/// The title row and the two-column layout shared by every page.
fn page_shell(title: &str, right_title: &str, left: &gtk::Box, right: &gtk::Box, extra: Option<&gtk::Widget>) -> gtk::Box {
    let root = gtk::Box::new(gtk::Orientation::Vertical, 8);
    root.set_margin_start(20);
    root.set_margin_end(20);
    root.set_margin_top(12);
    root.set_margin_bottom(18);
    let head = gtk::Box::new(gtk::Orientation::Horizontal, 12);
    let t = label(title, "big-title");
    t.set_hexpand(true);
    let r = label(right_title, "subtitle-right");
    r.set_ellipsize(pango::EllipsizeMode::End);
    head.append(&t);
    head.append(&r);
    if let Some(w) = extra {
        head.append(w);
    }
    root.append(&head);
    let cols = gtk::Box::new(gtk::Orientation::Horizontal, 20);
    cols.set_vexpand(true);
    left.set_hexpand(true);
    // A fixed width, whatever the text inside, with all spare room going to the graphs.
    // Otherwise every change in a live readout would resize the graphs beside it.
    let scroller = gtk::ScrolledWindow::builder()
        .hscrollbar_policy(gtk::PolicyType::External)
        .min_content_width(290)
        .hexpand(false)
        .child(right)
        .build();
    scroller.set_propagate_natural_height(false);
    cols.append(left);
    cols.append(&scroller);
    root.append(&cols);
    root
}

fn caption_row(left: &str, right: &str) -> (gtk::Box, gtk::Label) {
    let b = gtk::Box::new(gtk::Orientation::Horizontal, 8);
    let l = label(left, "caption-dim");
    l.set_hexpand(true);
    l.set_ellipsize(pango::EllipsizeMode::End);
    let r = label(right, "caption-dim");
    r.set_ellipsize(pango::EllipsizeMode::End);
    b.append(&l);
    b.append(&r);
    (b, r)
}

fn level_readouts(ctx: &Rc<Ctx>, dir: Direction, right: &gtk::Box, live: &LiveRef) {
    let row = gtk::Box::new(gtk::Orientation::Horizontal, 22);
    row.set_homogeneous(true);
    let (a, input) = readout("Input");
    let (b, output) = readout("Output");
    row.append(&a);
    row.append(&b);
    right.append(&row);
    let ctx = ctx.clone();
    live.borrow_mut().updaters.push(Box::new(move || {
        let rt = ctx.rt[dir as usize].borrow();
        let processing = ctx.state.borrow().chain(dir).enabled;
        input.set_text(&fmt_level(rt.smooth_pre));
        output.set_text(&if processing { fmt_level(rt.smooth_post) } else { "—".into() });
    }));
}

fn eq_page(ctx: &Rc<Ctx>, win: &adw::ApplicationWindow, toasts: &adw::ToastOverlay, dir: Direction, live: &LiveRef) -> gtk::Box {
    let left = gtk::Box::new(gtk::Orientation::Vertical, 4);
    let (cap, _) = caption_row(
        "Frequency response and live spectrum",
        "Double-click to add a band · Scroll to change width",
    );
    left.append(&cap);
    let graph = eq_graph::build(ctx.clone(), dir);
    live.borrow_mut().add(&graph);
    left.append(&graph);
    let (cap2, _) = caption_row("Output level over 1 minute", "Filled: after processing · Line: before");
    cap2.set_margin_top(8);
    left.append(&cap2);
    let lg = level_graph(ctx, dir, theme::EQ_BLUE, live);
    lg.set_vexpand(false);
    lg.set_size_request(200, 110);
    left.append(&lg);

    let right = gtk::Box::new(gtk::Orientation::Vertical, 10);
    right.set_margin_end(10);
    let row1 = gtk::Box::new(gtk::Orientation::Horizontal, 22);
    row1.set_homogeneous(true);
    let (a, peak) = readout("Output peak");
    let (b, headroom) = readout("Headroom");
    row1.append(&a);
    row1.append(&b);
    right.append(&row1);
    let row2 = gtk::Box::new(gtk::Orientation::Horizontal, 22);
    row2.set_homogeneous(true);
    let (a, bands_v) = readout("Bands");
    let (b, rate_v) = readout("Sample rate");
    row2.append(&a);
    row2.append(&b);
    right.append(&row2);
    {
        let ctx2 = ctx.clone();
        live.borrow_mut().updaters.push(Box::new(move || {
            let rt = ctx2.rt[dir as usize].borrow();
            // What reaches the device: with the chain off, that's the unprocessed sound.
            let p = if ctx2.state.borrow().chain(dir).enabled {
                rt.smooth_post
            } else {
                rt.smooth_pre
            };
            peak.set_text(&fmt_level(p));
            headroom.set_text(&if p <= HISTORY_FLOOR {
                "—".into()
            } else {
                format!("{:.1} dB", (-p).max(0.0))
            });
            rate_v.set_text(&format!("{:.1} kHz", rt.rate / 1000.0));
            bands_v.set_text(&ctx2.state.borrow().chain(dir).sound.bands.len().to_string());
        }));
    }

    // The selected band.
    let section = label("Selected band", "section-title");
    right.append(&section);
    let kinds = gtk::StringList::new(&FilterKind::ALL.map(|k| k.label()));
    let kind_row = gtk::DropDown::builder().model(&kinds).build();
    kind_row.update_property(&[gtk::accessible::Property::Label("Filter shape")]);
    right.append(&kind_row);

    let band_get = |ctx: &Rc<Ctx>, f: fn(&Band) -> f64, fallback: f64| -> Rc<dyn Fn() -> f64> {
        let ctx = ctx.clone();
        Rc::new(move || ctx.state.borrow().chain(dir).sound.bands.get(ctx.selected(dir)).map_or(fallback, f))
    };
    let band_set = |ctx: &Rc<Ctx>, f: fn(&mut Band, f64)| -> Rc<dyn Fn(f64)> {
        let ctx = ctx.clone();
        Rc::new(move |v| ctx.edit_band(dir, ctx.selected(dir), |b| f(b, v)))
    };
    let (freq_row, freq_sync) = slider(
        Slider {
            label: "Frequency",
            help: "The pitch this band affects",
            min: 20.0,
            max: 20000.0,
            log: true,
            unit: "Hz",
        },
        band_get(ctx, |b| b.freq, 1000.0),
        band_set(ctx, |b, v| b.freq = v),
    );
    let (gain_row, gain_sync) = slider(
        Slider {
            label: "Gain",
            help: "How much to boost or cut",
            min: -GAIN_LIMIT,
            max: GAIN_LIMIT,
            log: false,
            unit: "dB",
        },
        band_get(ctx, |b| b.gain, 0.0),
        band_set(ctx, |b, v| b.gain = v),
    );
    let (q_row, q_sync) = slider(
        Slider {
            label: "Width (Q)",
            help: "Higher values affect a narrower range",
            min: 0.1,
            max: 20.0,
            log: true,
            unit: "Q",
        },
        band_get(ctx, |b| b.q, 1.0),
        band_set(ctx, |b, v| b.q = v),
    );
    right.append(&freq_row);
    right.append(&gain_row);
    right.append(&q_row);

    let actions = gtk::Box::new(gtk::Orientation::Horizontal, 6);
    actions.add_css_class("linked");
    let bypass_band = gtk::ToggleButton::with_label("Mute band");
    bypass_band.set_hexpand(true);
    bypass_band.set_tooltip_text(Some("Temporarily turn this band off to hear the difference"));
    let add = gtk::Button::from_icon_name("list-add-symbolic");
    add.set_tooltip_text(Some("Add a band (or double-click the graph)"));
    let remove = gtk::Button::from_icon_name("user-trash-symbolic");
    remove.set_tooltip_text(Some("Remove this band"));
    actions.append(&bypass_band);
    actions.append(&add);
    actions.append(&remove);
    right.append(&actions);

    let busy = Rc::new(Cell::new(false));
    {
        let (ctx2, busy) = (ctx.clone(), busy.clone());
        kind_row.connect_selected_notify(move |d| {
            if !busy.get() {
                let kind = FilterKind::ALL[(d.selected() as usize).min(5)];
                ctx2.edit_band(dir, ctx2.selected(dir), |b| b.kind = kind);
            }
        });
    }
    {
        let (ctx2, busy) = (ctx.clone(), busy.clone());
        bypass_band.connect_toggled(move |t| {
            if !busy.get() {
                let on = !t.is_active();
                ctx2.edit_band(dir, ctx2.selected(dir), |b| b.enabled = on);
            }
        });
    }
    {
        let ctx2 = ctx.clone();
        add.connect_clicked(move |_| {
            // Place new bands in the largest gap, so they don't land on top of another.
            let f = {
                let s = ctx2.state.borrow();
                let mut fs: Vec<f64> = s.chain(dir).sound.bands.iter().map(|b| b.freq).collect();
                fs.extend([20.0, 20000.0]);
                fs.sort_by(f64::total_cmp);
                fs.windows(2)
                    .max_by(|a, b| (a[1] / a[0]).total_cmp(&(b[1] / b[0])))
                    .map_or(1000.0, |g| (g[0] * g[1]).sqrt())
            };
            ctx2.add_band(dir, Band::bell(f, 0.0, 1.0));
        });
    }
    {
        let ctx2 = ctx.clone();
        remove.connect_clicked(move |_| ctx2.remove_band(dir, ctx2.selected(dir)));
    }
    {
        let ctx2 = ctx.clone();
        let (kind_row, freq_row, gain_row, q_row, add, remove, bypass_band, busy) = (
            kind_row.clone(),
            freq_row.clone(),
            gain_row.clone(),
            q_row.clone(),
            add.clone(),
            remove.clone(),
            bypass_band.clone(),
            busy.clone(),
        );
        ctx.listen(move |d| {
            if d != dir {
                return;
            }
            freq_sync();
            gain_sync();
            q_sync();
            let s = ctx2.state.borrow();
            let bands = &s.chain(dir).sound.bands;
            let band = bands.get(ctx2.selected(dir));
            busy.set(true);
            if let Some(b) = band {
                kind_row.set_selected(FilterKind::ALL.iter().position(|k| *k == b.kind).unwrap_or(0) as u32);
                bypass_band.set_active(!b.enabled);
            }
            busy.set(false);
            let has = band.is_some();
            kind_row.set_sensitive(has);
            freq_row.set_sensitive(has);
            q_row.set_sensitive(has);
            bypass_band.set_sensitive(has);
            remove.set_sensitive(has);
            gain_row.set_sensitive(band.is_some_and(|b| b.kind.uses_gain()));
            add.set_sensitive(bands.len() < MAX_BANDS);
        });
    }

    // Whole-equalizer controls.
    right.append(&label("Overall", "section-title"));
    let (pre_row, pre_sync) = {
        let (c1, c2) = (ctx.clone(), ctx.clone());
        slider(
            Slider {
                label: "Preamp",
                help: "Turn everything down to leave room for boosts",
                min: -GAIN_LIMIT,
                max: GAIN_LIMIT,
                log: false,
                unit: "dB",
            },
            Rc::new(move || c1.state.borrow().chain(dir).sound.preamp),
            Rc::new(move |v| c2.edit_sound(dir, |s| s.preamp = v)),
        )
    };
    let (bal_row, bal_sync) = {
        let (c1, c2) = (ctx.clone(), ctx.clone());
        slider(
            Slider {
                label: "Balance",
                help: "Left (−) or right (+)",
                min: -100.0,
                max: 100.0,
                log: false,
                unit: "%",
            },
            Rc::new(move || c1.state.borrow().chain(dir).sound.balance),
            Rc::new(move |v| c2.edit_sound(dir, |s| s.balance = v)),
        )
    };
    right.append(&pre_row);
    right.append(&bal_row);
    // Presets replace every effect, so this is the only way to reset just the equalizer.
    let reset = gtk::Button::with_label("Reset to Defaults");
    reset.add_css_class("flat");
    reset.set_margin_top(4);
    reset.set_tooltip_text(Some(
        "Bands, preamp and balance as in the Flat preset. Other effects stay as they are.",
    ));
    {
        let ctx2 = ctx.clone();
        reset.connect_clicked(move |_| {
            ctx2.selected[dir as usize].set(0);
            ctx2.edit_sound(dir, Sound::reset_eq);
        });
    }
    right.append(&reset);
    ctx.listen(move |d| {
        if d == dir {
            pre_sync();
            bal_sync();
        }
    });
    let eq_switch = gtk::Switch::new();
    eq_switch.set_valign(gtk::Align::Center);
    eq_switch.set_tooltip_text(Some("Turn the curve on or off to compare. Other effects stay as they are."));
    eq_switch.update_property(&[gtk::accessible::Property::Label("Equalizer curve")]);
    let busy2 = Rc::new(Cell::new(false));
    {
        let (ctx2, busy2) = (ctx.clone(), busy2.clone());
        eq_switch.connect_active_notify(move |sw| {
            if !busy2.get() {
                let on = sw.is_active();
                ctx2.edit_sound_keep_preset(dir, move |s| s.eq_enabled = on);
            }
        });
    }
    {
        let (ctx2, sw) = (ctx.clone(), eq_switch.clone());
        ctx.listen(move |d| {
            if d == dir {
                busy2.set(true);
                sw.set_active(ctx2.state.borrow().chain(dir).sound.eq_enabled);
                busy2.set(false);
            }
        });
    }
    let head_extra = gtk::Box::new(gtk::Orientation::Horizontal, 10);
    head_extra.append(&preset_button(ctx, win, toasts, dir));
    head_extra.append(&eq_switch);
    page_shell(
        "Equalizer",
        "LSP Parametric EQ · 32 bands",
        &left,
        &right,
        Some(head_extra.upcast_ref()),
    )
}

impl Ctx {
    /// For on/off comparisons, which shouldn't make the preset read as "Custom".
    fn edit_sound_keep_preset(&self, dir: Direction, f: impl FnOnce(&mut Sound)) {
        f(&mut self.state.borrow_mut().chain_mut(dir).sound);
        self.mark_changed();
        self.refresh(dir);
    }
}

fn effect_page(ctx: &Rc<Ctx>, dir: Direction, spec: &'static EffectSpec, live: &LiveRef) -> gtk::Box {
    let left = gtk::Box::new(gtk::Orientation::Vertical, 4);
    if spec.plot != Plot::Level {
        let (cap, _) = caption_row("What goes in and what comes out", "White dot: current input");
        left.append(&cap);
        left.append(&plot_graph(ctx, dir, spec, live));
    }
    let (cap, _) = caption_row("Level over 1 minute", "Filled: after processing · Line: before");
    if spec.plot != Plot::Level {
        cap.set_margin_top(8);
    }
    left.append(&cap);
    let lg = level_graph(ctx, dir, spec.color, live);
    if spec.plot != Plot::Level {
        lg.set_vexpand(false);
        lg.set_size_request(200, 110);
    }
    left.append(&lg);

    let right = gtk::Box::new(gtk::Orientation::Vertical, 10);
    right.set_margin_end(10);
    level_readouts(ctx, dir, &right, live);
    let blurb = label(spec.blurb, "blurb");
    blurb.set_wrap(true);
    blurb.set_max_width_chars(34);
    right.append(&blurb);
    right.append(&label("Settings", "section-title"));

    let mut syncs: Vec<Rc<dyn Fn()>> = vec![];
    for p in spec.params {
        let param: &'static ParamSpec = p;
        let (c1, c2) = (ctx.clone(), ctx.clone());
        let (row, sync) = slider(
            Slider {
                label: param.label,
                help: param.help,
                min: param.min,
                max: param.max,
                log: param.log,
                unit: param.unit,
            },
            Rc::new(move || c1.state.borrow().chain(dir).sound.param(spec.id, param.id)),
            Rc::new(move |v| {
                c2.edit_sound(dir, |s| {
                    if let Some(e) = s.effects.get_mut(spec.id) {
                        e.params.insert(param.id.to_string(), v);
                    }
                })
            }),
        );
        right.append(&row);
        syncs.push(sync);
    }
    let reset = gtk::Button::with_label("Reset to Defaults");
    reset.add_css_class("flat");
    reset.set_margin_top(4);
    {
        let ctx2 = ctx.clone();
        reset.connect_clicked(move |_| {
            ctx2.edit_sound(dir, |s| {
                if let Some(e) = s.effects.get_mut(spec.id) {
                    e.params = spec.params.iter().map(|p| (p.id.to_string(), p.default)).collect();
                }
            })
        });
    }
    right.append(&reset);

    let grid = gtk::Grid::builder().column_spacing(12).row_spacing(6).margin_top(6).build();
    spec_row(&grid, 0, "Engine:", spec.engine);
    let pos = dir.chain().iter().position(|i| *i == spec.id).unwrap_or(0) + 1;
    spec_row(&grid, 1, "Position:", &format!("{pos} of {}", dir.chain().len()));
    right.append(&grid);

    let sw = gtk::Switch::new();
    sw.set_valign(gtk::Align::Center);
    sw.set_tooltip_text(Some("Turn this effect on or off"));
    sw.update_property(&[gtk::accessible::Property::Label(spec.title)]);
    let busy = Rc::new(Cell::new(false));
    {
        let (ctx2, busy) = (ctx.clone(), busy.clone());
        sw.connect_active_notify(move |s| {
            if !busy.get() {
                let on = s.is_active();
                ctx2.edit_sound_keep_preset(dir, move |snd| {
                    if let Some(e) = snd.effects.get_mut(spec.id) {
                        e.enabled = on;
                    }
                });
            }
        });
    }
    {
        let (ctx2, sw2, right2) = (ctx.clone(), sw.clone(), right.clone());
        let set_state = move || {
            let on = ctx2.state.borrow().chain(dir).sound.is_on(spec.id);
            busy.set(true);
            sw2.set_active(on);
            busy.set(false);
            right2.set_opacity(if on { 1.0 } else { 0.72 });
            for s in &syncs {
                s();
            }
        };
        set_state();
        ctx.listen(move |d| {
            if d == dir {
                set_state();
            }
        });
    }
    page_shell(spec.title, spec.engine, &left, &right, Some(sw.upcast_ref()))
}

/// A toast showing its text as written. Toasts read markup by default, and a file name
/// with an `&` in it isn't valid markup, which would leave the toast blank.
fn plain_toast(text: &str) -> adw::Toast {
    adw::Toast::builder().title(text).use_markup(false).build()
}

/// The button that picks, saves and imports presets.
fn preset_button(ctx: &Rc<Ctx>, win: &adw::ApplicationWindow, toasts: &adw::ToastOverlay, dir: Direction) -> gtk::MenuButton {
    let button = gtk::MenuButton::new();
    button.set_valign(gtk::Align::Center);
    button.set_tooltip_text(Some("Presets"));
    let popover = gtk::Popover::new();
    let col = gtk::Box::new(gtk::Orientation::Vertical, 6);
    col.set_size_request(240, -1);
    let list = gtk::ListBox::new();
    list.set_selection_mode(gtk::SelectionMode::None);
    list.add_css_class("preset-list");
    let scroller = gtk::ScrolledWindow::builder()
        .child(&list)
        .hscrollbar_policy(gtk::PolicyType::Never)
        .propagate_natural_height(true)
        .max_content_height(320)
        .build();
    col.append(&scroller);
    let save = gtk::Button::with_label("Save Current Sound…");
    let import = gtk::Button::with_label("Import AutoEq / APO File…");
    col.append(&save);
    col.append(&import);
    popover.set_child(Some(&col));
    button.set_popover(Some(&popover));

    let rebuild: Rc<dyn Fn()> = {
        let (ctx, list, popover, toasts) = (ctx.clone(), list.clone(), popover.clone(), toasts.clone());
        let this: Rc<RefCell<Option<Rc<dyn Fn()>>>> = Rc::new(RefCell::new(None));
        let this2 = this.clone();
        let f: Rc<dyn Fn()> = Rc::new(move || {
            while let Some(c) = list.first_child() {
                list.remove(&c);
            }
            let current = ctx.state.borrow().chain(dir).preset.clone();
            let mut entries: Vec<(String, Sound, bool)> = model::builtin_presets(dir)
                .into_iter()
                .map(|(n, s)| (n.to_string(), s, false))
                .collect();
            entries.extend(model::user_presets(dir).into_iter().map(|(n, s)| (n, s, true)));
            for (name, sound, user) in entries {
                let row = gtk::Box::new(gtk::Orientation::Horizontal, 6);
                let pick = gtk::Button::new();
                pick.add_css_class("flat");
                pick.set_hexpand(true);
                let l = gtk::Label::new(Some(&name));
                l.set_xalign(0.0);
                l.set_ellipsize(pango::EllipsizeMode::End);
                if name == current {
                    l.add_css_class("heading");
                }
                pick.set_child(Some(&l));
                {
                    let (ctx, popover, name) = (ctx.clone(), popover.clone(), name.clone());
                    pick.connect_clicked(move |_| {
                        ctx.load_sound(dir, &name, sound.clone());
                        popover.popdown();
                    });
                }
                row.append(&pick);
                if user {
                    let del = gtk::Button::from_icon_name("user-trash-symbolic");
                    del.add_css_class("flat");
                    del.set_tooltip_text(Some("Delete this preset"));
                    let (toasts, this2, name) = (toasts.clone(), this2.clone(), name.clone());
                    del.connect_clicked(move |_| {
                        if let Some(path) = model::preset_path(dir, &name) {
                            match std::fs::remove_file(path) {
                                Ok(()) => toasts.add_toast(plain_toast(&format!("Deleted “{name}”"))),
                                Err(e) => toasts.add_toast(plain_toast(&format!("Couldn't delete: {e}"))),
                            }
                        }
                        if let Some(f) = this2.borrow().as_ref() {
                            f();
                        }
                    });
                    row.append(&del);
                }
                list.append(&row);
            }
        });
        *this.borrow_mut() = Some(f.clone());
        f
    };
    {
        let rebuild = rebuild.clone();
        popover.connect_show(move |_| rebuild());
    }
    {
        let (ctx2, win2, toasts2, popover2) = (ctx.clone(), win.clone(), toasts.clone(), popover.clone());
        save.connect_clicked(move |_| {
            popover2.popdown();
            let entry = gtk::Entry::builder()
                .placeholder_text("Preset name")
                .activates_default(true)
                .build();
            let body = match dir {
                Direction::Output => "Saves the equalizer and all the effects. It's listed on the Output tab only.",
                Direction::Mic => "Saves the equalizer and all the effects. It's listed on the Microphone tab only.",
            };
            let dialog = adw::AlertDialog::new(Some("Save Preset"), Some(body));
            dialog.set_extra_child(Some(&entry));
            dialog.add_response("cancel", "Cancel");
            dialog.add_response("save", "Save");
            dialog.set_response_appearance("save", adw::ResponseAppearance::Suggested);
            dialog.set_default_response(Some("save"));
            dialog.set_close_response("cancel");
            dialog.set_response_enabled("save", false);
            {
                let dialog2 = dialog.clone();
                entry.connect_changed(move |e| dialog2.set_response_enabled("save", !e.text().trim().is_empty()));
            }
            let (ctx3, toasts3) = (ctx2.clone(), toasts2.clone());
            let name_entry = entry.clone();
            dialog.connect_response(None, move |_, response| {
                if response != "save" {
                    return;
                }
                let name = name_entry.text().trim().to_string();
                let sound = ctx3.state.borrow().chain(dir).sound.clone();
                match model::save_preset(dir, &name, &sound) {
                    Ok(()) => {
                        ctx3.state.borrow_mut().chain_mut(dir).preset = name.clone();
                        ctx3.mark_changed();
                        ctx3.refresh(dir);
                        toasts3.add_toast(plain_toast(&format!("Saved “{name}”")));
                    }
                    Err(e) => toasts3.add_toast(plain_toast(&format!("Couldn't save: {e}"))),
                }
            });
            dialog.present(Some(&win2));
            dialog.set_focus(Some(&entry));
        });
    }
    {
        let (ctx2, win2, toasts2, popover2) = (ctx.clone(), win.clone(), toasts.clone(), popover.clone());
        import.connect_clicked(move |_| {
            popover2.popdown();
            let chooser = gtk::FileDialog::builder()
                .title("Import an AutoEq or Equalizer APO File")
                .modal(true)
                .build();
            let (ctx3, toasts3) = (ctx2.clone(), toasts2.clone());
            chooser.open(Some(&win2), gio::Cancellable::NONE, move |res| {
                let Ok(file) = res else { return };
                let name = file
                    .basename()
                    .and_then(|n| n.file_stem().map(|s| s.to_string_lossy().into_owned()))
                    .unwrap_or_else(|| "Imported".into());
                let result = file
                    .path()
                    .ok_or("This file can't be opened".to_string())
                    .and_then(|p| std::fs::read_to_string(p).map_err(|e| e.to_string()))
                    .and_then(|t| model::parse_apo(&t));
                match result {
                    Ok(sound) => {
                        let n = sound.bands.len();
                        ctx3.load_sound(dir, &name, sound);
                        toasts3.add_toast(plain_toast(&format!("Imported {n} bands from “{name}”")));
                    }
                    Err(e) => toasts3.add_toast(plain_toast(&format!("Couldn't import: {e}"))),
                }
            });
        });
    }
    let ctx2 = ctx.clone();
    let b2 = button.clone();
    ctx.listen(move |d| {
        if d == dir {
            b2.set_label(&ctx2.state.borrow().chain(dir).preset);
        }
    });
    button
}

/// One sidebar tile: a small live graph, a name, and a current value.
fn tile(ctx: &Rc<Ctx>, dir: Direction, id: &'static str, live: &LiveRef) -> gtk::ListBoxRow {
    let (title, color) = if id == "eq" {
        ("Equalizer", theme::EQ_BLUE)
    } else {
        (effect(id).title, effect(id).color)
    };
    let row = gtk::ListBoxRow::new();
    row.set_widget_name(id);
    let hb = gtk::Box::new(gtk::Orientation::Horizontal, 10);
    let spark = gtk::DrawingArea::new();
    spark.set_size_request(72, 46);
    spark.set_valign(gtk::Align::Center);
    {
        let ctx = ctx.clone();
        spark.set_draw_func(move |_, cr, w, h| {
            let (w, h) = (w as f64, h as f64);
            let on = tile_live(&ctx, dir, id);
            let c = if on { color } else { theme::DIM };
            graphs::panel_begin(cr, w, h);
            graphs::grid(cr, w, h, 6, 3, c);
            let rt = ctx.rt[dir as usize].borrow();
            let recent: VecDeque<f64> = rt.history_post.iter().rev().take(SPARK_LEN).rev().copied().collect();
            drop(rt);
            if on {
                graphs::trace(cr, w, h, &recent, SPARK_LEN, c, true, 1.0, SPARK_BLUR_PX);
            }
            graphs::panel_end(cr, w, h, c);
        });
    }
    // A tile whose effect is off shows a still panel, so it's only redrawn when the
    // settings change. Each redraw costs far more than drawing the graph itself.
    {
        let ctx = ctx.clone();
        live.borrow_mut()
            .areas
            .push((spark.clone().upcast(), Box::new(move || tile_live(&ctx, dir, id))));
    }
    let texts = gtk::Box::new(gtk::Orientation::Vertical, 0);
    texts.set_valign(gtk::Align::Center);
    let name = label(title, "tile-name");
    let value = label("", "tile-value");
    value.set_ellipsize(pango::EllipsizeMode::End);
    texts.append(&name);
    texts.append(&value);
    hb.append(&spark);
    hb.append(&texts);
    row.set_child(Some(&hb));

    let ctx2 = ctx.clone();
    let update = move || {
        let s = ctx2.state.borrow();
        let chain = s.chain(dir);
        let snd = &chain.sound;
        let on = snd.is_on(id);
        let text = if !on {
            "Off".to_string()
        } else {
            match id {
                "eq" => format!("{} · {} bands", chain.preset, snd.bands.len()),
                "gate" => fmt_unit(snd.param(id, "threshold"), "dB"),
                "autogain" => fmt_unit(snd.param(id, "target"), "LUFS"),
                "compressor" => format!(
                    "{} · {}",
                    fmt_unit(snd.param(id, "ratio"), ":1"),
                    fmt_unit(snd.param(id, "threshold"), "dB")
                ),
                "loudness" => fmt_unit(snd.param(id, "amount"), "dB"),
                "limiter" => fmt_unit(snd.param(id, "threshold"), "dB"),
                _ => String::new(),
            }
        };
        value.set_text(&text);
        if on {
            name.remove_css_class("tile-off");
        } else {
            name.add_css_class("tile-off");
        }
    };
    update();
    ctx.listen(move |d| {
        if d == dir {
            update();
            spark.queue_draw();
        }
    });
    row
}

/// Whether a sidebar tile shows a live graph: its effect is on, in a chain that's on.
fn tile_live(ctx: &Ctx, dir: Direction, id: &str) -> bool {
    let s = ctx.state.borrow();
    let chain = s.chain(dir);
    chain.enabled && chain.sound.is_on(id)
}

fn chain_page(
    ctx: &Rc<Ctx>,
    win: &adw::ApplicationWindow,
    toasts: &adw::ToastOverlay,
    dir: Direction,
    split_toggle: &gtk::ToggleButton,
    live: &LiveRef,
) -> (adw::OverlaySplitView, gtk::DropDown) {
    let stack = gtk::Stack::builder()
        .transition_type(gtk::StackTransitionType::Crossfade)
        .transition_duration(120)
        .hhomogeneous(false)
        .vhomogeneous(false)
        .build();
    let list = gtk::ListBox::new();
    list.add_css_class("tiles");
    list.set_selection_mode(gtk::SelectionMode::Single);
    list.set_vexpand(true);
    for id in dir.chain() {
        let page: gtk::Box = if *id == "eq" {
            eq_page(ctx, win, toasts, dir, live)
        } else {
            effect_page(ctx, dir, effect(id), live)
        };
        stack.add_named(&page, Some(id));
        list.append(&tile(ctx, dir, id, live));
    }
    stack.set_visible_child_name("eq");
    {
        let stack = stack.clone();
        list.connect_row_selected(move |_, row| {
            if let Some(r) = row {
                stack.set_visible_child_name(&r.widget_name());
            }
        });
    }
    let eq_index = dir.chain().iter().position(|i| *i == "eq").unwrap_or(0) as i32;
    list.select_row(list.row_at_index(eq_index).as_ref());

    let side = gtk::Box::new(gtk::Orientation::Vertical, 6);
    let scroller = gtk::ScrolledWindow::builder()
        .child(&list)
        .hscrollbar_policy(gtk::PolicyType::Never)
        .vexpand(true)
        .build();
    side.append(&scroller);
    let dev_box = gtk::Box::new(gtk::Orientation::Vertical, 4);
    dev_box.set_margin_start(12);
    dev_box.set_margin_end(12);
    dev_box.set_margin_bottom(12);
    dev_box.append(&label(
        if dir == Direction::Output {
            "Plays through"
        } else {
            "Records from"
        },
        "caption-dim",
    ));
    let devices = gtk::DropDown::from_strings(&["System default"]);
    devices.update_property(&[gtk::accessible::Property::Label("Audio device")]);
    dev_box.append(&devices);
    side.append(&dev_box);

    let split = adw::OverlaySplitView::builder()
        .sidebar(&side)
        .content(&stack)
        .min_sidebar_width(220.0)
        .max_sidebar_width(280.0)
        .build();
    split_toggle
        .bind_property("active", &split, "show-sidebar")
        .sync_create()
        .bidirectional()
        .build();
    (split, devices)
}

fn run_in_background_menu() -> gio::Menu {
    let menu = gio::Menu::new();
    let a = gio::Menu::new();
    a.append(Some("_Keep Running When Closed"), Some("app.background"));
    a.append(Some("_Start at Login"), Some("app.autostart"));
    menu.append_section(None, &a);
    let b = gio::Menu::new();
    b.append(Some("_About Equalizer"), Some("app.about"));
    b.append(Some("_Quit"), Some("app.quit"));
    menu.append_section(None, &b);
    menu
}

fn autostart_file() -> PathBuf {
    glib::user_config_dir().join("autostart").join(format!("{APP_ID}.desktop"))
}

fn set_autostart(on: bool) -> std::io::Result<()> {
    let path = autostart_file();
    if !on {
        return match std::fs::remove_file(&path) {
            Err(e) if e.kind() != std::io::ErrorKind::NotFound => Err(e),
            _ => Ok(()),
        };
    }
    std::fs::create_dir_all(path.parent().expect("has a parent"))?;
    let exe = std::env::current_exe()?;
    std::fs::write(
        path,
        format!(
            "[Desktop Entry]\nType=Application\nName=Equalizer\nExec=\"{}\" --background\nIcon={APP_ID}\nX-GNOME-Autostart-enabled=true\n",
            exe.display()
        ),
    )
}

fn in_flatpak() -> bool {
    std::path::Path::new("/.flatpak-info").exists()
}

/// The arguments of the portal's RequestBackground call, as its `(sa{sv})` signature.
fn background_request(on: bool, token: &str) -> glib::Variant {
    let options = glib::VariantDict::new(None);
    options.insert("handle_token", token);
    options.insert("reason", "Start processing sound when you log in");
    options.insert("autostart", on);
    options.insert("commandline", vec!["equalizer", "--background"]);
    // Not a plain tuple, which would wrap the options in a `v`.
    glib::Variant::tuple_from_iter(["".to_variant(), options.end()])
}

/// Whether the app will start at login, from the portal's `(ua{sv})` reply.
/// `None` if the request was declined or cancelled.
fn background_reply(reply: &glib::Variant) -> Option<bool> {
    let (code, results) = reply.get::<(u32, BTreeMap<String, glib::Variant>)>()?;
    (code == 0).then(|| results.get("autostart").and_then(|v| v.get::<bool>()).unwrap_or(false))
}

/// Asks the Background portal to add or remove the login entry, as a sandboxed app
/// can't write it itself. `done` receives whether the app will now start at login.
fn portal_autostart(on: bool, done: impl Fn(Result<bool, String>) + 'static) {
    const DESKTOP: &str = "org.freedesktop.portal.Desktop";
    let bus = match gio::bus_get_sync(gio::BusType::Session, gio::Cancellable::NONE) {
        Ok(bus) => bus,
        Err(e) => return done(Err(e.to_string())),
    };
    let sender = bus.unique_name().unwrap_or_default().trim_start_matches(':').replace('.', "_");
    let token = format!("equalizer{}", glib::random_int());
    let request = format!("/org/freedesktop/portal/desktop/request/{sender}/{token}");

    // Subscribed before the call, so a quick reply can't be missed.
    let done = Rc::new(done);
    let subscription = Rc::new(Cell::new(None));
    let on_reply = {
        let (done, subscription) = (done.clone(), subscription.clone());
        move |signal: gio::DBusSignalRef| {
            subscription.take();
            done(background_reply(signal.parameters).ok_or_else(|| "The request was declined".to_string()));
        }
    };
    subscription.set(Some(bus.subscribe_to_signal(
        Some(DESKTOP),
        Some("org.freedesktop.portal.Request"),
        Some("Response"),
        Some(&request),
        None,
        gio::DBusSignalFlags::NONE,
        on_reply,
    )));
    bus.call(
        Some(DESKTOP),
        "/org/freedesktop/portal/desktop",
        "org.freedesktop.portal.Background",
        "RequestBackground",
        Some(&background_request(on, &token)),
        None,
        gio::DBusCallFlags::NONE,
        -1,
        gio::Cancellable::NONE,
        move |res| {
            if let Err(e) = res {
                subscription.take();
                done(Err(e.to_string()));
            }
        },
    );
}

fn missing_plugin_page(win: &adw::ApplicationWindow) {
    let page = adw::StatusPage::builder()
        .icon_name("dialog-warning-symbolic")
        .title("Audio Plugins Needed")
        .description("Equalizer processes sound with LSP Plugins, the same engine Easy Effects uses, and couldn't find them.\n\nInstall the “lsp-plugins-ladspa” package, or run the install script that came with Equalizer, then open the app again.")
        .build();
    let view = adw::ToolbarView::new();
    view.add_top_bar(&adw::HeaderBar::new());
    view.set_content(Some(&page));
    win.set_content(Some(&view));
}

/// Closing the window while audio is being processed hides it instead of quitting.
fn wire_close(win: &adw::ApplicationWindow, app: &adw::Application, ctx: Option<Rc<Ctx>>) {
    let app = app.clone();
    win.connect_close_request(move |w| {
        let keep = ctx.as_ref().is_some_and(|c| {
            let s = c.state.borrow();
            s.run_in_background && (s.output.enabled || s.mic.enabled)
        });
        if keep {
            w.set_visible(false);
        } else {
            app.quit();
        }
        glib::Propagation::Stop
    });
}

pub fn build(app: &adw::Application, start_hidden: bool) {
    let style = adw::StyleManager::default();
    style.set_color_scheme(adw::ColorScheme::ForceDark);
    let provider = gtk::CssProvider::new();
    provider.load_from_string(theme::CSS);
    if let Some(display) = gdk::Display::default() {
        gtk::style_context_add_provider_for_display(&display, &provider, gtk::STYLE_PROVIDER_PRIORITY_APPLICATION);
    }

    let win = adw::ApplicationWindow::builder()
        .application(app)
        .title("Equalizer")
        .default_width(1240)
        .default_height(800)
        .width_request(820)
        .height_request(560)
        .icon_name(APP_ID)
        .build();

    let about = gio::SimpleAction::new("about", None);
    {
        let win = win.clone();
        about.connect_activate(move |_, _| {
            let dialog = adw::AboutDialog::builder()
                .application_name("Equalizer")
                .application_icon(APP_ID)
                .developer_name("Zoe Ronen (zoeruda)")
                .version(env!("CARGO_PKG_VERSION"))
                .comments("A clean, simple equalizer for PipeWire, built on the LSP audio plugins.")
                .website(env!("CARGO_PKG_REPOSITORY"))
                .issue_url(concat!(env!("CARGO_PKG_REPOSITORY"), "/issues"))
                .license_type(gtk::License::Gpl30)
                .build();
            dialog.add_acknowledgement_section(
                Some("Special Thanks"),
                &[
                    "Easy Effects https://github.com/wwmm/easyeffects",
                    "Mission Center https://gitlab.com/mission-center-devs/mission-center",
                ],
            );
            dialog.present(Some(&win));
        });
    }
    app.add_action(&about);
    let quit = gio::SimpleAction::new("quit", None);
    {
        let app = app.clone();
        quit.connect_activate(move |_, _| app.quit());
    }
    app.add_action(&quit);
    app.set_accels_for_action("app.quit", &["<Control>q"]);
    app.set_accels_for_action("window.close", &["<Control>w"]);

    let Some(plugin) = engine::find_plugin() else {
        missing_plugin_page(&win);
        wire_close(&win, app, None);
        win.present();
        return;
    };

    if let Err(e) = model::split_presets() {
        eprintln!("equalizer: couldn't make the Microphone's preset folder: {e}");
    }
    let (tx, rx) = async_channel::unbounded();
    let ctx = Rc::new(Ctx {
        state: RefCell::new(model::load_state()),
        engine: engine::spawn(tx),
        rt: [RefCell::new(Rt::new()), RefCell::new(Rt::new())],
        selected: [Cell::new(0), Cell::new(0)],
        sent: [RefCell::new(BTreeMap::new()), RefCell::new(BTreeMap::new())],
        plugin,
        dirty: Cell::new(false),
        save_at: Cell::new(None),
        listeners: RefCell::new(vec![]),
    });

    let live: LiveRef = Rc::new(RefCell::new(Live::default()));
    let toasts = adw::ToastOverlay::new();
    let stack = adw::ViewStack::new();
    let toggle = gtk::ToggleButton::builder()
        .icon_name("sidebar-show-symbolic")
        .active(true)
        .tooltip_text("Toggle Sidebar")
        .build();

    let (out_page, out_dev) = chain_page(&ctx, &win, &toasts, Direction::Output, &toggle, &live);
    let (mic_page, mic_dev) = chain_page(&ctx, &win, &toasts, Direction::Mic, &toggle, &live);
    stack.add_titled_with_icon(&out_page, Some("output"), "Output", "audio-speakers-symbolic");
    stack.add_titled_with_icon(&mic_page, Some("mic"), "Microphone", "audio-input-microphone-symbolic");

    let header = adw::HeaderBar::new();
    header.pack_start(&toggle);
    let switcher = adw::ViewSwitcher::builder()
        .stack(&stack)
        .policy(adw::ViewSwitcherPolicy::Wide)
        .build();
    header.set_title_widget(Some(&switcher));
    let menu_btn = gtk::MenuButton::builder()
        .icon_name("open-menu-symbolic")
        .menu_model(&run_in_background_menu())
        .primary(true)
        .tooltip_text("Main Menu")
        .build();
    header.pack_end(&menu_btn);
    let power = gtk::Switch::new();
    power.set_valign(gtk::Align::Center);
    power.set_tooltip_text(Some("When off, this sound goes straight to the device with no processing"));
    power.update_property(&[gtk::accessible::Property::Label("Process audio")]);
    header.pack_end(&power);
    let power_label = label("Active", "caption-dim");
    power_label.set_valign(gtk::Align::Center);
    header.pack_end(&power_label);

    let view = adw::ToolbarView::new();
    view.add_top_bar(&header);
    toasts.set_child(Some(&stack));
    view.set_content(Some(&toasts));
    win.set_content(Some(&view));

    let breakpoint = adw::Breakpoint::new(adw::BreakpointCondition::new_length(
        adw::BreakpointConditionLengthType::MaxWidth,
        1000.0,
        adw::LengthUnit::Sp,
    ));
    breakpoint.add_setter(&out_page, "collapsed", Some(&true.to_value()));
    breakpoint.add_setter(&mic_page, "collapsed", Some(&true.to_value()));
    win.add_breakpoint(breakpoint);

    let current_dir = {
        let stack = stack.clone();
        move || {
            if stack.visible_child_name().as_deref() == Some("mic") {
                Direction::Mic
            } else {
                Direction::Output
            }
        }
    };

    // The on/off switch follows whichever page is showing.
    let power_busy = Rc::new(Cell::new(false));
    let sync_power: Rc<dyn Fn()> = {
        let (ctx, power, busy, dir_of) = (ctx.clone(), power.clone(), power_busy.clone(), current_dir.clone());
        Rc::new(move || {
            busy.set(true);
            power.set_active(ctx.state.borrow().chain(dir_of()).enabled);
            busy.set(false);
        })
    };
    {
        let (ctx2, busy, dir_of) = (ctx.clone(), power_busy.clone(), current_dir.clone());
        power.connect_active_notify(move |p| {
            if !busy.get() {
                ctx2.set_enabled(dir_of(), p.is_active());
            }
        });
    }
    {
        let sync = sync_power.clone();
        ctx.listen(move |_| sync());
    }

    // Device pickers.
    let device_lists: Rc<RefCell<[Vec<Device>; 2]>> = Rc::new(RefCell::new([vec![], vec![]]));
    let dev_busy = Rc::new(Cell::new(false));
    let dropdowns = [out_dev.clone(), mic_dev.clone()];
    let fill_devices: Rc<dyn Fn()> = {
        let (ctx, lists, busy, dropdowns) = (ctx.clone(), device_lists.clone(), dev_busy.clone(), dropdowns.clone());
        Rc::new(move || {
            busy.set(true);
            for (i, dir) in DIRS.iter().enumerate() {
                let devs = &lists.borrow()[i];
                let mut names = vec!["System default".to_string()];
                names.extend(devs.iter().map(|d| d.description.clone()));
                let refs: Vec<&str> = names.iter().map(String::as_str).collect();
                dropdowns[i].set_model(Some(&gtk::StringList::new(&refs)));
                let wanted = ctx.state.borrow().chain(*dir).device.clone();
                let pos = wanted.and_then(|w| devs.iter().position(|d| d.name == w)).map_or(0, |p| p + 1);
                dropdowns[i].set_selected(pos as u32);
            }
            busy.set(false);
        })
    };
    for (i, dir) in DIRS.iter().enumerate() {
        let dir = *dir;
        let (ctx2, lists, busy) = (ctx.clone(), device_lists.clone(), dev_busy.clone());
        dropdowns[i].connect_selected_notify(move |d| {
            if busy.get() {
                return;
            }
            let pos = d.selected() as usize;
            let device = if pos == 0 {
                None
            } else {
                lists.borrow()[dir as usize].get(pos - 1).map(|x| x.name.clone())
            };
            ctx2.state.borrow_mut().chain_mut(dir).device = device.clone();
            ctx2.mark_changed();
            ctx2.engine.send(Cmd::Device(dir, device));
        });
    }

    // Menu toggles.
    let bg = gio::SimpleAction::new_stateful("background", None, &ctx.state.borrow().run_in_background.to_variant());
    {
        let ctx2 = ctx.clone();
        bg.connect_activate(move |a, _| {
            let on = !a.state().and_then(|v| v.get::<bool>()).unwrap_or(true);
            a.set_state(&on.to_variant());
            ctx2.state.borrow_mut().run_in_background = on;
            ctx2.mark_changed();
        });
    }
    app.add_action(&bg);
    let autostart_on = if in_flatpak() {
        ctx.state.borrow().autostart
    } else {
        autostart_file().exists()
    };
    let auto = gio::SimpleAction::new_stateful("autostart", None, &autostart_on.to_variant());
    {
        let (toasts2, ctx2) = (toasts.clone(), ctx.clone());
        auto.connect_activate(move |a, _| {
            let on = !a.state().and_then(|v| v.get::<bool>()).unwrap_or(false);
            if in_flatpak() {
                let (a, toasts3, ctx3) = (a.clone(), toasts2.clone(), ctx2.clone());
                portal_autostart(on, move |result| match result {
                    Ok(now_on) => {
                        if on && !now_on {
                            toasts3.add_toast(plain_toast(
                                "Starting at login isn't allowed. Check this app's background permission in Settings.",
                            ));
                        }
                        a.set_state(&now_on.to_variant());
                        ctx3.state.borrow_mut().autostart = now_on;
                        ctx3.mark_changed();
                    }
                    Err(e) => toasts3.add_toast(plain_toast(&format!("Couldn't change this: {e}"))),
                });
                return;
            }
            match set_autostart(on) {
                Ok(()) => a.set_state(&on.to_variant()),
                Err(e) => toasts2.add_toast(plain_toast(&format!("Couldn't change this: {e}"))),
            }
        });
    }
    app.add_action(&auto);

    // Messages from the audio thread.
    {
        let (ctx2, lists, fill, toasts2) = (ctx.clone(), device_lists.clone(), fill_devices.clone(), toasts.clone());
        glib::spawn_future_local(async move {
            while let Ok(event) = rx.recv().await {
                match event {
                    Event::Devices { sinks, sources } => {
                        *lists.borrow_mut() = [sinks, sources];
                        fill();
                    }
                    Event::TurnedOffExternally(dir) => ctx2.set_enabled(dir, false),
                    Event::Error(message) => toasts2.add_toast(plain_toast(&message)),
                }
            }
        });
    }

    // Start what was running last time.
    for dir in DIRS {
        if ctx.state.borrow().chain(dir).enabled {
            ctx.start(dir);
        }
    }

    // The steady heartbeat: meters, redraws, settings, saving.
    {
        let (ctx2, win2, live2, dir_of) = (ctx.clone(), win.clone(), live.clone(), current_dir.clone());
        let metering: Cell<Option<Direction>> = Cell::new(None);
        let sent_meter_cmd = Cell::new(false);
        let was_still = Cell::new(false);
        let beat: Rc<dyn Fn() -> bool> = Rc::new(move || {
            if ctx2.dirty.replace(false) {
                for dir in DIRS {
                    if ctx2.state.borrow().chain(dir).enabled {
                        ctx2.push_params(dir);
                    }
                }
            }
            if ctx2.save_at.get().is_some_and(|t| Instant::now() >= t) {
                ctx2.save_now();
            }
            let showing = win2.is_visible()
                && !win2
                    .surface()
                    .and_downcast::<gdk::Toplevel>()
                    .is_some_and(|t| t.state().intersects(gdk::ToplevelState::MINIMIZED | gdk::ToplevelState::SUSPENDED));
            let want = showing.then(&dir_of);
            if want != metering.get() || !sent_meter_cmd.get() {
                metering.set(want);
                sent_meter_cmd.set(true);
                ctx2.engine.send(Cmd::Meters(want));
            }
            if let Some(dir) = want {
                tick_meters(&ctx2, dir);
                let l = live2.borrow();
                for u in &l.updaters {
                    u();
                }
                // Once a frame has shown everything at rest, the next would look the same.
                // Settings changes redraw by themselves.
                let still = ctx2.rt[dir as usize].borrow().still;
                let shown_at_rest = was_still.replace(still);
                if !(still && shown_at_rest) {
                    for (a, moving) in &l.areas {
                        if a.is_mapped() && moving() {
                            a.queue_draw();
                        }
                    }
                }
            }
            showing
        });
        heartbeat(beat, true);
    }

    {
        let ctx2 = ctx.clone();
        app.connect_shutdown(move |_| {
            ctx2.save_now();
            ctx2.engine.shutdown();
        });
    }
    wire_close(&win, app, Some(ctx.clone()));
    {
        let app = app.clone();
        glib_unix::unix_signal_add_local(15, move || {
            app.quit();
            glib::ControlFlow::Break
        });
    }
    {
        let app = app.clone();
        glib_unix::unix_signal_add_local(2, move || {
            app.quit();
            glib::ControlFlow::Break
        });
    }

    // The graphs also show settings, which the heartbeat stops redrawing while the
    // sound is at rest.
    {
        let live = live.clone();
        ctx.listen(move |_| {
            if let Ok(l) = live.try_borrow() {
                for (a, _) in &l.areas {
                    a.queue_draw();
                }
            }
        });
    }

    // Fill in every control from the saved state.
    for dir in DIRS {
        ctx.refresh(dir);
    }
    fill_devices();
    {
        let sync = sync_power.clone();
        stack.connect_visible_child_notify(move |_| sync());
    }
    sync_power();
    if !start_hidden {
        win.present();
    }
}

/// Runs `beat` for each frame while the window shows. Otherwise there's nothing to draw,
/// so it only runs a few times a second, to keep up with saving and to notice the window
/// coming back. `beat` says whether the window is showing.
fn heartbeat(beat: Rc<dyn Fn() -> bool>, showing: bool) {
    let interval = Duration::from_millis(if showing { 33 } else { 250 });
    glib::timeout_add_local(interval, move || {
        let now = beat();
        if now == showing {
            return glib::ControlFlow::Continue;
        }
        heartbeat(beat.clone(), now);
        glib::ControlFlow::Break
    });
}

fn tick_meters(ctx: &Ctx, dir: Direction) {
    let processing = ctx.state.borrow().chain(dir).enabled;
    let mut rt = ctx.rt[dir as usize].borrow_mut();
    let rt = &mut *rt;
    let read = |a: &std::sync::Mutex<dsp::Analyzer>, s: &mut Spectrum| -> (f64, f64) {
        let (peak, rate) = match a.lock() {
            Ok(mut g) => {
                let peak = g.take_peak() as f64;
                if peak > 0.0 {
                    s.capture(&g);
                }
                (peak, g.rate)
            }
            Err(_) => return (HISTORY_FLOOR, 48_000.0),
        };
        // After letting go, so the audio thread never waits for the FFT.
        if peak > 0.0 {
            s.analyze(rate);
        } else {
            s.decay();
        }
        (dsp::to_db(peak), rate as f64)
    };
    let (pre, _) = read(&ctx.engine.pre, &mut rt.spectrum_pre);
    let (post, rate) = read(&ctx.engine.post, &mut rt.spectrum_post);
    rt.rate = rate;
    rt.peak_pre = pre.max(HISTORY_FLOOR);
    rt.peak_post = post.max(HISTORY_FLOOR);
    let recording_after = rt.after.step(processing, rt.peak_post > HISTORY_FLOOR);
    if !recording_after {
        // So a new stretch doesn't open with what the last one ended on.
        rt.peak_post = HISTORY_FLOOR;
        rt.smooth_post = HISTORY_FLOOR;
    }
    // Numbers jump up to a new peak and ease back down, so they stay readable.
    rt.smooth_pre = rt.peak_pre.max(rt.smooth_pre - 0.5);
    rt.smooth_post = rt.peak_post.max(rt.smooth_post - 0.5);
    rt.frame = rt.frame.wrapping_add(1);
    if rt.frame.is_multiple_of(2) {
        rt.history_pre.push_back(rt.smooth_pre);
        // With nothing being processed, there's no "after" to show: a gap, not silence.
        rt.history_post.push_back(if recording_after { rt.smooth_post } else { f64::NAN });
        if rt.history_pre.len() > HISTORY_LEN {
            rt.history_pre.pop_front();
        }
        if rt.history_post.len() > HISTORY_LEN {
            rt.history_post.pop_front();
        }
    }
    rt.still = at_rest(rt);
}

/// Whether the graphs would look the same on the next frame: no spectrum is showing, and
/// the last minute of levels is all one value (silence, or a gap), so scrolling it
/// changes nothing.
fn at_rest(rt: &Rt) -> bool {
    let quiet = |s: &Spectrum| s.columns.iter().all(|c| *c <= SPECTRUM_FLOOR + 0.5);
    let flat = |h: &VecDeque<f64>| h.len() >= HISTORY_LEN && h.iter().all(|v| v.to_bits() == h[0].to_bits());
    quiet(&rt.spectrum_pre) && quiet(&rt.spectrum_post) && flat(&rt.history_pre) && flat(&rt.history_post)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::EFFECTS;

    #[test]
    fn the_graphs_rest_only_once_a_whole_minute_shows_nothing_moving() {
        let mut rt = Rt::new();
        assert!(!at_rest(&rt), "the history is still filling in");
        rt.history_pre = std::iter::repeat_n(HISTORY_FLOOR, HISTORY_LEN).collect();
        rt.history_post = std::iter::repeat_n(f64::NAN, HISTORY_LEN).collect();
        assert!(at_rest(&rt), "silence before, a gap after");
        rt.history_post[3] = -20.0;
        assert!(!at_rest(&rt), "sound from earlier in the minute is still scrolling by");
        rt.history_post[3] = f64::NAN;
        rt.spectrum_pre.columns[10] = -40.0;
        assert!(!at_rest(&rt), "the spectrum is fading out");
    }

    #[test]
    fn the_after_graph_waits_for_its_meter_once_the_chain_turns_on() {
        let mut a = AfterStart::default();
        assert!(!a.step(false, true), "nothing is recorded with the chain off");
        assert!(!a.step(true, false), "the meter hasn't delivered anything yet");
        assert!(a.step(true, true));
        assert!(a.step(true, false), "once started, silence is recorded as silence");
        assert!(!a.step(false, false));
        assert!(!a.step(true, false), "turning back on waits again");
    }

    #[test]
    fn silence_is_shown_after_a_while_even_without_sound() {
        let mut a = AfterStart::default();
        let recorded = (0..AFTER_GRACE).filter(|_| a.step(true, false)).count();
        assert_eq!(recorded, 1);
        assert!(a.step(true, false));
    }

    #[test]
    fn small_graphs_keep_their_jitter() {
        // A 6 dB wobble lasting six samples, on a small graph at its usual width.
        let jitter: Vec<f64> = (0..SPARK_LEN).map(|i| if i % 6 < 3 { -20.0 } else { -26.0 }).collect();
        let step = 72.0 / (SPARK_LEN - 1) as f64;
        let soft = graphs::smooth_levels(jitter, SPARK_BLUR_PX / step);
        let middle = &soft[30..120];
        let highest = middle.iter().copied().fold(f64::MIN, f64::max);
        let lowest = middle.iter().copied().fold(f64::MAX, f64::min);
        assert!(highest - lowest > 3.0, "only {} dB of wobble left", highest - lowest);
    }

    #[test]
    fn typed_values_are_understood() {
        assert_eq!(parse_number("1.5 kHz"), Some(1500.0));
        assert_eq!(parse_number("2k"), Some(2000.0));
        assert_eq!(parse_number("−3.0 dB"), Some(-3.0));
        assert_eq!(parse_number("+6"), Some(6.0));
        assert_eq!(parse_number("loud"), None);
    }

    #[test]
    fn values_format_with_their_units() {
        assert_eq!(fmt_unit(3.0, ":1"), "3.0:1");
        assert_eq!(fmt_unit(1000.0, "Hz"), "1.00 kHz");
        assert_eq!(fmt_unit(-20.0, "LUFS"), "−20 LUFS");
        assert_eq!(fmt_unit(150.0, "ms"), "150 ms");
    }

    #[test]
    fn the_background_request_has_the_portals_signature() {
        let request = background_request(true, "token");
        assert_eq!(request.type_().as_str(), "(sa{sv})");
        let options = request.child_value(1);
        let dict = glib::VariantDict::new(Some(&options));
        assert_eq!(dict.lookup::<bool>("autostart").ok().flatten(), Some(true));
        assert_eq!(dict.lookup::<String>("handle_token").ok().flatten().as_deref(), Some("token"));
        assert_eq!(
            dict.lookup::<Vec<String>>("commandline").ok().flatten(),
            Some(vec!["equalizer".to_string(), "--background".to_string()])
        );
    }

    #[test]
    fn background_replies_are_understood() {
        let reply = |code: u32, on: Option<bool>| {
            let results = glib::VariantDict::new(None);
            if let Some(on) = on {
                results.insert("autostart", on);
            }
            glib::Variant::tuple_from_iter([code.to_variant(), results.end()])
        };
        assert_eq!(background_reply(&reply(0, Some(true))), Some(true));
        assert_eq!(background_reply(&reply(0, Some(false))), Some(false));
        assert_eq!(background_reply(&reply(0, None)), Some(false));
        assert_eq!(background_reply(&reply(1, Some(true))), None);
        assert_eq!(background_reply(&reply(2, None)), None);
    }

    #[test]
    fn every_effect_table_entry_has_sane_ranges() {
        for e in EFFECTS {
            for p in e.params {
                assert!(p.min < p.max, "{}", p.id);
                assert!(p.default >= p.min && p.default <= p.max, "{}", p.id);
                assert!(!p.log || p.min > 0.0, "{}", p.id);
            }
        }
    }
}
