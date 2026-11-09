//! Owns every PipeWire object, on its own thread. The audio itself is processed
//! by PipeWire's filter-chain module running the LSP plugins; the UI only sends
//! settings and reads the meters.
use pipewire as pw;
use pw::metadata::{Metadata, MetadataListener};
use pw::node::Node;
use pw::properties::properties;
use pw::registry::GlobalObject;
use pw::spa;
use pw::spa::pod::{Object, Pod, Property, PropertyFlags, Value, ValueArray, deserialize::PodDeserializer, serialize::PodSerializer};
use pw::spa::utils::dict::DictRef;
use std::cell::{Cell, RefCell};
use std::collections::{BTreeMap, HashMap};
use std::ffi::CString;
use std::fmt::Write;
use std::path::{Path, PathBuf};
use std::rc::Rc;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use crate::dsp::Analyzer;
use crate::model::{Direction, SINK_NAME, SOURCE_NAME, effect};

const OUT_STREAM: &str = "equalizer_out";
const MIC_STREAM: &str = "equalizer_mic_in";
/// The graph nodes that apply the virtual output's volume, one per channel.
const VOLUME_L: &str = "volume_l";
const VOLUME_R: &str = "volume_r";
/// How long streams get to leave the virtual output before it's removed anyway.
const DRAIN_TIMEOUT: Duration = Duration::from_secs(3);
/// How long the output stays after the last stream has left. WirePlumber updates its
/// own record of links a moment after they go, and pauses any media player that record
/// still shows on an output when it's removed.
const DRAIN_SETTLE: Duration = Duration::from_millis(500);
/// How often a draining output is checked, to notice that one of those times has passed.
const DRAIN_TICK: Duration = Duration::from_millis(100);

pub enum Cmd {
    Enable {
        dir: Direction,
        device: Option<String>,
        controls: BTreeMap<String, f32>,
        plugin: PathBuf,
    },
    Disable(Direction),
    Params(Direction, Vec<(String, f32)>),
    Device(Direction, Option<String>),
    Meters(Option<Direction>),
    Quit,
}

#[derive(Clone, Debug, PartialEq)]
pub struct Device {
    pub name: String,
    pub description: String,
}

pub enum Event {
    Devices {
        sinks: Vec<Device>,
        sources: Vec<Device>,
    },
    /// The person chose a different default device in system settings.
    TurnedOffExternally(Direction),
    Error(String),
}

pub struct Handle {
    tx: pw::channel::Sender<Cmd>,
    pub pre: Arc<Mutex<Analyzer>>,
    pub post: Arc<Mutex<Analyzer>>,
    thread: RefCell<Option<std::thread::JoinHandle<()>>>,
}

impl Handle {
    pub fn send(&self, cmd: Cmd) {
        let _ = self.tx.send(cmd);
    }

    pub fn shutdown(&self) {
        self.send(Cmd::Quit);
        if let Some(t) = self.thread.borrow_mut().take() {
            let _ = t.join();
        }
    }
}

pub fn spawn(events: async_channel::Sender<Event>) -> Handle {
    let (tx, rx) = pw::channel::channel();
    let pre = Arc::new(Mutex::new(Analyzer::new()));
    let post = Arc::new(Mutex::new(Analyzer::new()));
    let (p1, p2) = (pre.clone(), post.clone());
    let errors = events.clone();
    let thread = std::thread::Builder::new()
        .name("pipewire".into())
        .spawn(move || {
            if let Err(e) = run(rx, events, p1, p2) {
                let _ = errors.send_blocking(Event::Error(format!("Can't connect to PipeWire: {e}")));
            }
        })
        .expect("thread starts");
    Handle {
        tx,
        pre,
        post,
        thread: RefCell::new(Some(thread)),
    }
}

/// Where the LSP plugin file is, if it's installed.
pub fn find_plugin() -> Option<PathBuf> {
    let home = gtk::glib::home_dir();
    let mut dirs: Vec<PathBuf> = std::env::var("LADSPA_PATH")
        .unwrap_or_default()
        .split(':')
        .filter(|s| !s.is_empty())
        .map(PathBuf::from)
        .collect();
    dirs.extend([
        home.join(".local/lib/ladspa"),
        home.join(".ladspa"),
        "/usr/lib64/ladspa".into(),
        "/usr/lib/ladspa".into(),
        "/usr/local/lib/ladspa".into(),
        "/app/lib/ladspa".into(),
        "/usr/lib/x86_64-linux-gnu/ladspa".into(),
    ]);
    dirs.into_iter().map(|d| d.join("lsp-plugins-ladspa.so")).find(|p| p.exists())
}

/// Quotes a string for PipeWire's config format, which reads the same escapes as JSON.
/// Other programs choose device names, so without this a name could add its own settings.
fn quoted(s: &str) -> String {
    serde_json::to_string(s).expect("strings serialize")
}

/// Builds the arguments for libpipewire-module-filter-chain.
pub fn module_args(dir: Direction, hw: &str, controls: &BTreeMap<String, f32>, plugin: &Path) -> String {
    let plugin = quoted(&plugin.to_string_lossy());
    let hw = quoted(hw);
    let mut nodes = String::new();
    let mut links = String::new();
    let chain = dir.chain();
    for (i, id) in chain.iter().enumerate() {
        let label = if *id == "eq" {
            "para_equalizer_x32_stereo"
        } else {
            effect(id).plugin
        };
        let prefix = format!("{id}:");
        let mut ctl = String::new();
        for (k, v) in controls.iter().filter(|(k, _)| k.starts_with(&prefix)) {
            let _ = write!(ctl, "\"{}\" = {v:?} ", &k[prefix.len()..]);
        }
        let _ = writeln!(
            nodes,
            "{{ type = ladspa name = {id} plugin = {plugin} label =\"http://lsp-plug.in/plugins/ladspa/{label}\" control = {{ {ctl} }} }}"
        );
        if let Some(next) = chain.get(i + 1) {
            let _ = writeln!(
                links,
                "{{ output = \"{id}:Output L\" input = \"{next}:Input L\" }} {{ output = \"{id}:Output R\" input = \"{next}:Input R\" }}"
            );
        }
    }
    let first = chain[0];
    let mut outputs = format!("\"{0}:Output L\" \"{0}:Output R\"", chain[chain.len() - 1]);
    let mut volumes = String::new();
    // The virtual output's volume would otherwise be applied as the sound enters the
    // chain, where Auto Gain and the compressor take a lowered slider for quiet sound
    // and undo it. These controls apply it after the chain instead. The microphone
    // needs nothing: a source's volume is already applied after its chain.
    if dir == Direction::Output {
        let last = chain[chain.len() - 1];
        let _ = writeln!(
            nodes,
            "{{ type = builtin name = {VOLUME_L} label = linear }} {{ type = builtin name = {VOLUME_R} label = linear }}"
        );
        let _ = writeln!(
            links,
            "{{ output = \"{last}:Output L\" input = \"{VOLUME_L}:In\" }} {{ output = \"{last}:Output R\" input = \"{VOLUME_R}:In\" }}"
        );
        outputs = format!("\"{VOLUME_L}:Out\" \"{VOLUME_R}:Out\"");
        // One per channel, in audio.position order.
        volumes = format!("capture.volumes = [ {{ control = \"{VOLUME_L}:Mult\" }} {{ control = \"{VOLUME_R}:Mult\" }} ] ");
    }
    let (capture, playback, description) = match dir {
        Direction::Output => (
            format!("node.name = \"{SINK_NAME}\" node.description = \"Equalizer\" media.class = Audio/Sink"),
            format!("node.name = \"{OUT_STREAM}\" node.description = \"Equalizer output\" node.passive = true target.object = {hw}"),
            "Equalizer",
        ),
        Direction::Mic => (
            format!(
                "node.name = \"{MIC_STREAM}\" node.description = \"Equalizer microphone input\" node.passive = true target.object = {hw}"
            ),
            format!("node.name = \"{SOURCE_NAME}\" node.description = \"Equalizer Microphone\" media.class = Audio/Source"),
            "Equalizer Microphone",
        ),
    };
    format!(
        "{{ node.description = \"{description}\" media.name = \"{description}\" \
         filter.graph = {{ nodes = [ {nodes} ] links = [ {links} ] \
         inputs = [ \"{first}:Input L\" \"{first}:Input R\" ] outputs = [ {outputs} ] {volumes}}} \
         audio.channels = 2 audio.position = [ FL FR ] \
         capture.props = {{ {capture} }} playback.props = {{ {playback} }} }}"
    )
}

#[derive(Clone)]
struct NodeInfo {
    name: String,
    description: String,
    class: String,
    serial: String,
    dont_fallback: bool,
}

struct LinkEnds {
    input: u32,
    output: u32,
}

/// The progress of streams leaving an output that's turning off.
struct Drain {
    limit: Instant,
    left_at: Option<Instant>,
}

impl Drain {
    fn new(now: Instant) -> Self {
        Self {
            limit: now + DRAIN_TIMEOUT,
            left_at: None,
        }
    }

    /// Whether the output can be removed yet.
    fn ready(&mut self, now: Instant, in_use: bool) -> bool {
        if now >= self.limit {
            return true;
        }
        if in_use {
            self.left_at = None;
            return false;
        }
        now >= *self.left_at.get_or_insert(now) + DRAIN_SETTLE
    }
}

/// The gain, per channel, that undoes the volume in a virtual device's Props. Silence
/// stays silence, so muted or at zero it's 0. None if the Props don't carry a volume.
fn volume_gain(props: &Value) -> Option<[f32; 2]> {
    let Value::Object(obj) = props else { return None };
    let mut volumes = None;
    let mut mute = false;
    for p in &obj.properties {
        match (p.key, &p.value) {
            (spa_sys::SPA_PROP_channelVolumes, Value::ValueArray(ValueArray::Float(v))) if !v.is_empty() => {
                volumes = Some(v.clone());
            }
            (spa_sys::SPA_PROP_mute, Value::Bool(m)) => mute = *m,
            _ => {}
        }
    }
    let v = volumes?;
    let gain = |i: usize| {
        let v = v[i.min(v.len() - 1)];
        if mute || v.is_nan() || v <= 0.0 { 0.0 } else { 1.0 / v }
    };
    Some([gain(0), gain(1)])
}

/// The default device to hand back, if it still exists. WirePlumber remembers every
/// default it's been given, including our virtual device. So if it were given a device
/// that's gone, it could fall back on ours, and streams would have no reason to leave.
fn hand_back_device(previous: Option<&str>, devices: &[Device]) -> Option<String> {
    previous.filter(|p| devices.iter().any(|d| d.name == *p)).map(str::to_owned)
}

/// The streams on a device that should be moved off it. A stream told to have no
/// fallback is left alone, as it asked not to be sent anywhere else.
fn streams_to_move(links: &HashMap<u32, LinkEnds>, nodes: &HashMap<u32, NodeInfo>, device: u32) -> Vec<u32> {
    let mut streams: Vec<u32> = links
        .values()
        .filter(|link| link.input == device)
        .map(|link| link.output)
        .filter(|id| {
            nodes
                .get(id)
                .is_some_and(|n| n.class.starts_with("Stream/Output") && !n.dont_fallback)
        })
        .collect();
    streams.sort_unstable();
    streams.dedup();
    streams
}

struct Chain {
    module: *mut pw_sys::pw_impl_module,
    wanted: Option<String>,
    hw: Option<String>,
    /// Watches the virtual device's volume. Declared before `control`, so that it's
    /// dropped first: it unhooks itself from the node.
    volume_listener: Option<pw::node::NodeListener>,
    control: Option<Node>,
    /// What the post meter needs, per channel, to undo the virtual device's volume.
    gain: [f32; 2],
    claim_default: bool,
    previous_default: Option<String>,
    /// Set while turning off, until the streams have left.
    draining: Option<Drain>,
    /// Streams sent to a real device while turning off, to let go of afterward.
    pinned: Vec<u32>,
    /// Settings that arrived before the plugin chain finished starting.
    pending: Vec<(String, f32)>,
}

impl Chain {
    fn new() -> Self {
        Self {
            module: std::ptr::null_mut(),
            wanted: None,
            hw: None,
            volume_listener: None,
            control: None,
            gain: [1.0; 2],
            claim_default: false,
            previous_default: None,
            draining: None,
            pinned: vec![],
            pending: vec![],
        }
    }

    fn on(&self) -> bool {
        !self.module.is_null()
    }
}

struct Meter {
    stream: pw::stream::StreamRc,
    _listener: pw::stream::StreamListener<MeterData>,
}

struct MeterData {
    analyzer: Arc<Mutex<Analyzer>>,
    format: spa::param::audio::AudioInfoRaw,
    buf: Vec<f32>,
}

struct Inner {
    nodes: HashMap<u32, NodeInfo>,
    /// Every link in the graph, by id.
    links: HashMap<u32, LinkEnds>,
    quitting: bool,
    chains: [Chain; 2],
    metadata: Option<Metadata>,
    meta_listener: Option<MetadataListener>,
    configured: [Option<String>; 2],
    effective: [Option<String>; 2],
    metering: Option<Direction>,
    meters: Vec<Meter>,
    last_devices: (Vec<Device>, Vec<Device>),
}

const DEFAULT_KEYS: [&str; 2] = ["default.configured.audio.sink", "default.configured.audio.source"];
const EFFECTIVE_KEYS: [&str; 2] = ["default.audio.sink", "default.audio.source"];

fn own(dir: usize) -> &'static str {
    if dir == 0 { SINK_NAME } else { SOURCE_NAME }
}

/// Virtual devices count too: people often route through them on purpose.
fn device_class(dir: usize, class: &str) -> bool {
    match dir {
        0 => class == "Audio/Sink",
        _ => class == "Audio/Source" || class == "Audio/Source/Virtual",
    }
}

const fn libc_enoent() -> i32 {
    2
}

fn name_json(v: Option<&str>) -> Option<String> {
    let parsed: serde_json::Value = serde_json::from_str(v?).ok()?;
    parsed.get("name")?.as_str().map(str::to_owned)
}

fn default_device_json(name: &str) -> String {
    serde_json::json!({ "name": name }).to_string()
}

impl Inner {
    fn devices(&self, dir: usize) -> Vec<Device> {
        let mut v: Vec<Device> = self
            .nodes
            .values()
            .filter(|n| device_class(dir, &n.class) && n.name != own(dir))
            .map(|n| Device {
                name: n.name.clone(),
                description: n.description.clone(),
            })
            .collect();
        v.sort_by(|a, b| a.description.cmp(&b.description));
        v
    }

    /// The real device a chain should use: the chosen one if it's connected,
    /// otherwise the system default, otherwise anything available.
    fn resolve_hw(&self, dir: usize) -> Option<String> {
        let devices = self.devices(dir);
        let exists = |n: &String| devices.iter().any(|d| &d.name == n);
        self.chains[dir]
            .wanted
            .clone()
            .filter(exists)
            .or(self.chains[dir].previous_default.clone().filter(exists))
            .or(self.configured[dir].clone().filter(exists))
            .or(self.effective[dir].clone().filter(exists))
            .or(devices.first().map(|d| d.name.clone()))
    }

    fn set_default(&self, dir: usize, name: Option<&str>) {
        if let Some(m) = &self.metadata {
            match name {
                Some(n) => m.set_property(0, DEFAULT_KEYS[dir], Some("Spa:String:JSON"), Some(&default_device_json(n))),
                None => m.set_property(0, DEFAULT_KEYS[dir], None, None),
            }
        }
    }

    /// The gain the post meter needs for what it's measuring now. With the chain off or
    /// draining, sound reaches the device without passing through the virtual one.
    fn post_gain(&self) -> [f32; 2] {
        let Some(dir) = self.metering else { return [1.0; 2] };
        let c = &self.chains[dir as usize];
        if c.on() && c.draining.is_none() { c.gain } else { [1.0; 2] }
    }

    fn node_id(&self, name: &str) -> Option<u32> {
        self.nodes.iter().find(|(_, n)| n.name == name).map(|(id, _)| *id)
    }

    /// Moves a running chain to another real device without restarting it.
    fn retarget(&mut self, dir: usize) {
        if !self.chains[dir].on() {
            return;
        }
        let Some(hw) = self.resolve_hw(dir) else { return };
        if self.chains[dir].hw.as_deref() == Some(&hw) {
            return;
        }
        let stream = if dir == 0 { OUT_STREAM } else { MIC_STREAM };
        let serial = self.nodes.values().find(|n| n.name == hw).map(|n| n.serial.clone());
        if let (Some(id), Some(serial), Some(m)) = (self.node_id(stream), serial, &self.metadata) {
            m.set_property(id, "target.object", Some("Spa:Id"), Some(&serial));
            self.chains[dir].hw = Some(hw);
        }
    }

    fn unload(&mut self, dir: usize) {
        let chain = &mut self.chains[dir];
        if !chain.on() {
            return;
        }
        chain.volume_listener = None;
        chain.control = None;
        chain.gain = [1.0; 2];
        chain.pending.clear();
        chain.claim_default = false;
        chain.draining = None;
        // SAFETY: the pointer came from pw_context_load_module and is cleared here.
        unsafe { pw_sys::pw_impl_module_destroy(chain.module) };
        chain.module = std::ptr::null_mut();
        chain.hw = None;
        // Only now, as until the device is gone they could go straight back to it.
        self.unpin_streams(dir);
    }

    /// Whether any streams are still playing into this chain's virtual device.
    fn in_use(&self, dir: usize) -> bool {
        self.node_id(own(dir))
            .is_some_and(|id| self.links.values().any(|link| link.input == id))
    }

    /// Sends the streams on this chain's virtual device to a real one. Handing back the
    /// default isn't enough, because a stream won't follow it if it has its own target,
    /// or if WirePlumber doesn't choose a new default until our device is gone.
    fn pin_streams(&mut self, dir: usize, destination: Option<&str>) {
        let serial = destination
            .and_then(|name| self.nodes.values().find(|n| n.name == name))
            .map(|n| n.serial.as_str())
            .filter(|serial| !serial.is_empty());
        let (Some(serial), Some(device), Some(metadata)) = (serial, self.node_id(own(dir)), &self.metadata) else {
            return;
        };
        let streams = streams_to_move(&self.links, &self.nodes, device);
        for id in &streams {
            metadata.set_property(*id, "target.object", Some("Spa:Id"), Some(serial));
        }
        self.chains[dir].pinned = streams;
    }

    /// Lets pinned streams follow the default device again. This also clears the target
    /// that WirePlumber remembers for them, so it won't be used the next time they play.
    fn unpin_streams(&mut self, dir: usize) {
        let streams = std::mem::take(&mut self.chains[dir].pinned);
        if let Some(metadata) = &self.metadata {
            for id in streams {
                metadata.set_property(id, "target.object", None, None);
            }
        }
    }

    /// Hands the default device back and stops the chain. Returns true if the output
    /// is draining, in which case `finish_drains` removes it once the streams have moved.
    ///
    /// WirePlumber pauses media players that are still linked to a sink when it's
    /// removed, which is meant for unplugged headphones. So the sink has to outlive
    /// the move. Nothing like that happens for sources, so they stop right away.
    fn stop(&mut self, dir: usize) -> bool {
        if !self.chains[dir].on() || self.chains[dir].draining.is_some() {
            return false;
        }
        // Only hand the default back if it's still ours.
        let default = if self.configured[dir].as_deref() == Some(own(dir)) {
            let previous = self.chains[dir].previous_default.as_deref();
            let device = hand_back_device(previous, &self.devices(dir));
            self.set_default(dir, device.as_deref());
            device
        } else {
            self.configured[dir].clone()
        };
        self.chains[dir].claim_default = false;
        if dir == 0 && self.chains[dir].control.is_some() && self.in_use(dir) {
            let destination = default.or_else(|| self.chains[dir].hw.clone());
            self.pin_streams(dir, destination.as_deref());
            self.chains[dir].draining = Some(Drain::new(Instant::now()));
            true
        } else {
            self.unload(dir);
            false
        }
    }
}

fn set_params(node: &Node, params: &[(String, f32)]) {
    let mut values = Vec::with_capacity(params.len() * 2);
    for (k, v) in params {
        values.push(Value::String(k.clone()));
        values.push(Value::Float(*v));
    }
    let value = Value::Object(Object {
        type_: spa::utils::SpaTypes::ObjectParamProps.as_raw(),
        id: spa::param::ParamType::Props.as_raw(),
        properties: vec![Property {
            key: spa_sys::SPA_PROP_params,
            flags: PropertyFlags::empty(),
            value: Value::Struct(values),
        }],
    });
    match PodSerializer::serialize(std::io::Cursor::new(Vec::new()), &value) {
        Ok((bytes, _)) => {
            let bytes = bytes.into_inner();
            if let Some(pod) = Pod::from_bytes(&bytes) {
                node.set_param(spa::param::ParamType::Props, 0, pod);
            }
        }
        Err(e) => eprintln!("equalizer: couldn't encode settings: {e:?}"),
    }
}

fn make_meter(
    core: &pw::core::CoreRc,
    target: &str,
    monitor: bool,
    passive: bool,
    label: &str,
    analyzer: Arc<Mutex<Analyzer>>,
) -> Result<Meter, pw::Error> {
    let mut props = properties! {
        *pw::keys::MEDIA_TYPE => "Audio",
        *pw::keys::MEDIA_CATEGORY => "Capture",
        *pw::keys::MEDIA_ROLE => "DSP",
        *pw::keys::NODE_NAME => format!("equalizer_meter_{label}"),
        "target.object" => target,
        "node.dont-reconnect" => "true",
        "node.dont-fallback" => "true",
    };
    if passive {
        props.insert("node.passive", "true");
    }
    if monitor {
        props.insert(*pw::keys::STREAM_CAPTURE_SINK, "true");
        // GNOME and KDE show every PulseAudio source output as an app using the
        // microphone, including one that records a speaker's monitor. pipewire-pulse
        // only lists Stream/Input/Audio nodes as source outputs, and WirePlumber still
        // links this class. A meter on the microphone keeps the usual class, as it
        // really does use it.
        props.insert(*pw::keys::MEDIA_CLASS, "Stream/Input/Audio/Internal");
    }
    let stream = pw::stream::StreamRc::new(core.clone(), "Equalizer meter", props)?;
    let data = MeterData {
        analyzer,
        format: Default::default(),
        buf: Vec::new(),
    };
    let listener = stream
        .add_local_listener_with_user_data(data)
        .param_changed(|_, d, id, param| {
            let Some(param) = param else { return };
            if id == spa::param::ParamType::Format.as_raw()
                && d.format.parse(param).is_ok()
                && d.format.rate() > 0
                && let Ok(mut a) = d.analyzer.lock()
            {
                a.rate = d.format.rate();
            }
        })
        .process(|stream, d| {
            let Some(mut buffer) = stream.dequeue_buffer() else { return };
            let Some(data) = buffer.datas_mut().first_mut() else { return };
            let (offset, size) = (data.chunk().offset() as usize, data.chunk().size() as usize);
            let Some(bytes) = data.data() else { return };
            let Some(bytes) = bytes.get(offset..offset + size) else { return };
            d.buf.clear();
            d.buf.extend(
                bytes
                    .as_chunks::<4>()
                    .0
                    .iter()
                    .map(|c| f32::from_le_bytes([c[0], c[1], c[2], c[3]])),
            );
            if let Ok(mut a) = d.analyzer.lock() {
                a.feed(&d.buf);
            }
        })
        .register()?;

    let mut info = spa::param::audio::AudioInfoRaw::new();
    info.set_format(spa::param::audio::AudioFormat::F32LE);
    info.set_channels(2);
    let mut position = [0u32; spa_sys::SPA_AUDIO_MAX_CHANNELS as usize];
    position[0] = spa_sys::SPA_AUDIO_CHANNEL_FL;
    position[1] = spa_sys::SPA_AUDIO_CHANNEL_FR;
    info.set_position(position);
    let value = Value::Object(Object {
        type_: spa::utils::SpaTypes::ObjectParamFormat.as_raw(),
        id: spa::param::ParamType::EnumFormat.as_raw(),
        properties: info.into(),
    });
    let bytes = PodSerializer::serialize(std::io::Cursor::new(Vec::new()), &value)
        .map_err(|_| pw::Error::CreationFailed)?
        .0
        .into_inner();
    let pod = Pod::from_bytes(&bytes).ok_or(pw::Error::CreationFailed)?;
    stream.connect(
        spa::utils::Direction::Input,
        None,
        pw::stream::StreamFlags::AUTOCONNECT | pw::stream::StreamFlags::MAP_BUFFERS,
        &mut [pod],
    )?;
    Ok(Meter {
        stream,
        _listener: listener,
    })
}

fn run(
    rx: pw::channel::Receiver<Cmd>,
    events: async_channel::Sender<Event>,
    pre: Arc<Mutex<Analyzer>>,
    post: Arc<Mutex<Analyzer>>,
) -> Result<(), pw::Error> {
    pw::init();
    let mainloop = pw::main_loop::MainLoopRc::new(None)?;
    // Inside Flatpak, only a "Manager" can route audio and change the default device.
    // On the context so that it also covers the filter-chain module's own connection.
    let context = pw::context::ContextRc::new(&mainloop, Some(properties! { "media.category" => "Manager" }))?;
    let core = context.connect_rc(None)?;
    let registry = core.get_registry_rc()?;

    let inner = Rc::new(RefCell::new(Inner {
        nodes: HashMap::new(),
        links: HashMap::new(),
        quitting: false,
        chains: [Chain::new(), Chain::new()],
        metadata: None,
        meta_listener: None,
        configured: [None, None],
        effective: [None, None],
        metering: None,
        meters: vec![],
        last_devices: (vec![], vec![]),
    }));

    // Rebuilds the meter streams to match the current state.
    let rebuild_meters: Rc<dyn Fn()> = {
        let (inner, core, pre, post) = (inner.clone(), core.clone(), pre.clone(), post.clone());
        Rc::new(move || {
            let mut s = inner.borrow_mut();
            for m in s.meters.drain(..) {
                let _ = m.stream.disconnect();
            }
            if let Ok(mut a) = pre.lock() {
                a.clear();
            }
            if let Ok(mut a) = post.lock() {
                a.clear();
                a.gain = s.post_gain();
            }
            let Some(dir) = s.metering else { return };
            let d = dir as usize;
            let on = s.chains[d].on() && s.chains[d].draining.is_none();
            let hw = s.chains[d].hw.clone().or_else(|| s.resolve_hw(d));
            let mut wanted: Vec<(String, bool, Arc<Mutex<Analyzer>>, &str)> = vec![];
            match dir {
                Direction::Output => {
                    let processing = on && s.node_id(SINK_NAME).is_some();
                    if processing {
                        wanted.push((SINK_NAME.to_string(), true, pre.clone(), "pre"));
                    }
                    // Without the chain, what reaches the device is the sound before
                    // processing, and there's nothing after it to show.
                    if let Some(hw) = hw {
                        if processing {
                            wanted.push((hw, true, post.clone(), "post"));
                        } else {
                            wanted.push((hw, true, pre.clone(), "pre"));
                        }
                    }
                }
                // Looking at this page must never open the microphone by itself.
                Direction::Mic if on => {
                    if let Some(hw) = hw {
                        wanted.push((hw, false, pre.clone(), "pre"));
                    }
                    if s.node_id(SOURCE_NAME).is_some() {
                        wanted.push((SOURCE_NAME.to_string(), false, post.clone(), "post"));
                    }
                }
                Direction::Mic => {}
            }
            let passive = dir == Direction::Output;
            for (target, monitor, analyzer, label) in wanted {
                match make_meter(&core, &target, monitor, passive, label, analyzer) {
                    Ok(m) => s.meters.push(m),
                    Err(e) => eprintln!("equalizer: meter for {target} failed: {e}"),
                }
            }
        })
    };

    // Removes any draining output that's ready, and ends the loop once quitting is done.
    let quit_seq: Rc<Cell<Option<i32>>> = Rc::new(Cell::new(None));
    let finish_drains: Rc<dyn Fn()> = {
        let (inner, core, ml, rebuild, quit_seq) = (
            inner.clone(),
            core.clone(),
            mainloop.clone(),
            rebuild_meters.clone(),
            quit_seq.clone(),
        );
        Rc::new(move || {
            let mut s = inner.borrow_mut();
            let mut removed = false;
            for d in 0..2 {
                let in_use = s.in_use(d);
                let Some(drain) = s.chains[d].draining.as_mut() else { continue };
                if drain.ready(Instant::now(), in_use) {
                    if in_use {
                        eprintln!("equalizer: streams didn't leave the output in time, so it's being removed with them attached");
                    }
                    s.unload(d);
                    removed = true;
                }
            }
            let quit = s.quitting && !s.chains.iter().any(Chain::on) && quit_seq.get().is_none();
            drop(s);
            if quit {
                // Let the server see these changes before the connection closes.
                match core.sync(0) {
                    Ok(seq) => quit_seq.set(Some(seq.seq())),
                    Err(_) => ml.quit(),
                }
            } else if removed {
                rebuild();
            }
        })
    };

    let send_devices: Rc<dyn Fn()> = {
        let (inner, events) = (inner.clone(), events.clone());
        Rc::new(move || {
            let mut s = inner.borrow_mut();
            let current = (s.devices(0), s.devices(1));
            if current != s.last_devices {
                s.last_devices = current.clone();
                let _ = events.try_send(Event::Devices {
                    sinks: current.0,
                    sources: current.1,
                });
            }
        })
    };

    let reg2 = registry.clone();
    let _reg_listener = {
        let (inner, events, rebuild, send_devices, reg) = (
            inner.clone(),
            events.clone(),
            rebuild_meters.clone(),
            send_devices.clone(),
            registry.clone(),
        );
        let post = post.clone();
        let inner_rm = inner.clone();
        let (rebuild_rm, send_rm, finish_rm) = (rebuild_meters.clone(), send_devices.clone(), finish_drains.clone());
        reg2.add_listener_local()
            .global(move |g: &GlobalObject<&DictRef>| {
                let Some(props) = g.props else { return };
                match g.type_ {
                    pw::types::ObjectType::Link => {
                        let input = props.get("link.input.node").and_then(|v| v.parse().ok());
                        let output = props.get("link.output.node").and_then(|v| v.parse().ok());
                        if let (Some(input), Some(output)) = (input, output) {
                            inner.borrow_mut().links.insert(g.id, LinkEnds { input, output });
                        }
                    }
                    pw::types::ObjectType::Node => {
                        let name = props.get("node.name").unwrap_or_default().to_string();
                        let info = NodeInfo {
                            description: props
                                .get("node.description")
                                .or(props.get("node.nick"))
                                .unwrap_or(&name)
                                .to_string(),
                            class: props.get("media.class").unwrap_or_default().to_string(),
                            serial: props.get("object.serial").unwrap_or_default().to_string(),
                            dont_fallback: matches!(props.get("node.dont-fallback"), Some("true" | "1")),
                            name: name.clone(),
                        };
                        let dir = if name == SINK_NAME {
                            Some(0)
                        } else if name == SOURCE_NAME {
                            Some(1)
                        } else {
                            None
                        };
                        inner.borrow_mut().nodes.insert(g.id, info);
                        if let Some(d) = dir {
                            let mut s = inner.borrow_mut();
                            match reg.bind::<Node, _>(g) {
                                Ok(node) => {
                                    let pending = std::mem::take(&mut s.chains[d].pending);
                                    if !pending.is_empty() {
                                        set_params(&node, &pending);
                                    }
                                    // The post meter measures after the virtual device's
                                    // volume. Following it lets the graphs show only what
                                    // the effects do, wherever the slider is.
                                    // Weak, as the listener is kept inside `inner` itself.
                                    let (weak, meter) = (Rc::downgrade(&inner), post.clone());
                                    let listener = node
                                        .add_listener_local()
                                        .param(move |_, id, _, _, param| {
                                            if id != spa::param::ParamType::Props {
                                                return;
                                            }
                                            let Some(param) = param else { return };
                                            let Ok((_, value)) = PodDeserializer::deserialize_any_from(param.as_bytes()) else {
                                                return;
                                            };
                                            let Some(gain) = volume_gain(&value) else { return };
                                            let Some(inner) = weak.upgrade() else { return };
                                            let Ok(mut s) = inner.try_borrow_mut() else { return };
                                            s.chains[d].gain = gain;
                                            if let Ok(mut a) = meter.lock() {
                                                a.gain = s.post_gain();
                                            }
                                        })
                                        .register();
                                    node.subscribe_params(&[spa::param::ParamType::Props]);
                                    s.chains[d].volume_listener = Some(listener);
                                    s.chains[d].control = Some(node);
                                }
                                Err(e) => {
                                    let _ = events.try_send(Event::Error(format!("Can't control the equalizer: {e}")));
                                }
                            }
                            if s.chains[d].claim_default {
                                s.chains[d].claim_default = false;
                                s.set_default(d, Some(own(d)));
                            }
                            drop(s);
                            rebuild();
                        }
                        send_devices();
                    }
                    pw::types::ObjectType::Metadata if props.get("metadata.name") == Some("default") => {
                        let meta: Metadata = match reg.bind(g) {
                            Ok(m) => m,
                            Err(e) => {
                                let _ = events.try_send(Event::Error(format!("Can't read default devices: {e}")));
                                return;
                            }
                        };
                        let (inner2, events2) = (inner.clone(), events.clone());
                        let listener = meta
                            .add_listener_local()
                            .property(move |subject, key, _type, value| {
                                if subject != 0 {
                                    return 0;
                                }
                                let Some(key) = key else { return 0 };
                                let mut s = inner2.borrow_mut();
                                for d in 0..2 {
                                    if key == EFFECTIVE_KEYS[d] {
                                        s.effective[d] = name_json(value);
                                    } else if key == DEFAULT_KEYS[d] {
                                        let name = name_json(value);
                                        if name.as_deref() == Some(own(d)) {
                                            s.chains[d].claim_default = false;
                                        }
                                        // Someone picked another device while we were active: step aside.
                                        if s.chains[d].on()
                                            && s.chains[d].draining.is_none()
                                            && !s.chains[d].claim_default
                                            && name.is_some()
                                            && name.as_deref() != Some(own(d))
                                        {
                                            s.chains[d].previous_default = None;
                                            let dir = if d == 0 { Direction::Output } else { Direction::Mic };
                                            let _ = events2.try_send(Event::TurnedOffExternally(dir));
                                        }
                                        s.configured[d] = name;
                                    }
                                }
                                0
                            })
                            .register();
                        let mut s = inner.borrow_mut();
                        s.metadata = Some(meta);
                        s.meta_listener = Some(listener);
                    }
                    _ => {}
                }
            })
            .global_remove(move |id| {
                if inner_rm.borrow_mut().links.remove(&id).is_some() {
                    finish_rm();
                    return;
                }
                let removed = inner_rm.borrow_mut().nodes.remove(&id);
                let Some(removed) = removed else { return };
                let mut needs_rebuild = false;
                {
                    let mut s = inner_rm.borrow_mut();
                    // A device in use was unplugged: move to whatever is next best.
                    for d in 0..2 {
                        if s.chains[d].hw.as_deref() == Some(&removed.name) {
                            s.retarget(d);
                            needs_rebuild = true;
                        } else if !s.chains[d].on() && device_class(d, &removed.class) {
                            needs_rebuild = true;
                        }
                    }
                }
                if needs_rebuild {
                    rebuild_rm();
                }
                send_rm();
            })
            .register()
    };

    // Wait for the current objects to arrive, twice over: the first round finds
    // the metadata object and the second receives its values.
    let phase = Rc::new(Cell::new(0));
    let first = core.sync(0)?;
    let _core_listener = {
        let (ml, core2, phase, events, quit_seq) = (mainloop.clone(), core.clone(), phase.clone(), events.clone(), quit_seq.clone());
        let second: Rc<Cell<Option<spa::utils::result::AsyncSeq>>> = Rc::new(Cell::new(None));
        core.add_listener_local()
            .done(move |id, seq| {
                if id != pw::core::PW_ID_CORE {
                    return;
                }
                if phase.get() == 0 && seq == first {
                    phase.set(1);
                    second.set(core2.sync(0).ok());
                } else if phase.get() == 1 && Some(seq) == second.get() {
                    phase.set(2);
                    ml.quit();
                } else if quit_seq.get() == Some(seq.seq()) {
                    ml.quit();
                }
            })
            .error(move |id, _seq, res, message| {
                if id != pw::core::PW_ID_CORE {
                    return;
                }
                // -ENOENT only means an object we referred to has already gone, such
                // as a meter on a device that was just unplugged. The meters get
                // rebuilt anyway, so there's nothing to tell the person about.
                if res == -libc_enoent() {
                    eprintln!("equalizer: {message}");
                } else {
                    let _ = events.try_send(Event::Error(format!("PipeWire reported a problem: {message}")));
                }
            })
            .register()
    };
    mainloop.run();
    send_devices();

    // Wakes the loop while an output drains, as waiting for it to settle or for its time
    // limit to pass doesn't produce any other event.
    let (wake_tx, wake_rx) = pw::channel::channel::<()>();
    let _wake = {
        let finish = finish_drains.clone();
        wake_rx.attach(mainloop.loop_(), move |()| finish())
    };
    let wake_after_limit = move || {
        let tx = wake_tx.clone();
        std::thread::spawn(move || {
            let end = Instant::now() + DRAIN_TIMEOUT + DRAIN_TICK;
            while Instant::now() < end {
                std::thread::sleep(DRAIN_TICK);
                if tx.send(()).is_err() {
                    break;
                }
            }
        });
    };

    let _receiver = {
        let (inner, context, rebuild, events, finish) = (
            inner.clone(),
            context.clone(),
            rebuild_meters.clone(),
            events.clone(),
            finish_drains.clone(),
        );
        rx.attach(mainloop.loop_(), move |cmd| match cmd {
            Cmd::Enable {
                dir,
                device,
                controls,
                plugin,
            } => {
                let d = dir as usize;
                let mut s = inner.borrow_mut();
                let configured = s.configured[d].clone().filter(|n| n != own(d));
                if s.chains[d].on() {
                    // Turned back on before the old device was removed: just keep it.
                    if s.chains[d].draining.take().is_some() {
                        s.chains[d].wanted = device;
                        if configured.is_some() {
                            s.chains[d].previous_default = configured;
                        }
                        // Ignore the hand-back still on its way, until our claim comes back.
                        s.chains[d].claim_default = true;
                        s.set_default(d, Some(own(d)));
                        // Now that the default is ours again, they can follow it back.
                        s.unpin_streams(d);
                        if let Some(node) = &s.chains[d].control {
                            set_params(node, &controls.into_iter().collect::<Vec<_>>());
                        }
                        s.retarget(d);
                        drop(s);
                        rebuild();
                    }
                    return;
                }
                s.chains[d].wanted = device;
                s.chains[d].previous_default = configured;
                let Some(hw) = s.resolve_hw(d) else {
                    let what = if d == 0 { "output" } else { "microphone" };
                    let _ = events.try_send(Event::Error(format!("No {what} device is connected")));
                    let _ = events.try_send(Event::TurnedOffExternally(dir));
                    return;
                };
                let args = CString::new(module_args(dir, &hw, &controls, &plugin)).expect("no nul bytes");
                // SAFETY: the context outlives the module, and the strings live through the call.
                let module = unsafe {
                    pw_sys::pw_context_load_module(
                        context.as_raw_ptr(),
                        c"libpipewire-module-filter-chain".as_ptr(),
                        args.as_ptr(),
                        std::ptr::null_mut(),
                    )
                };
                if module.is_null() {
                    let why = std::io::Error::last_os_error();
                    let _ = events.try_send(Event::Error(format!("The audio engine couldn't start: {why}")));
                    let _ = events.try_send(Event::TurnedOffExternally(dir));
                    return;
                }
                s.chains[d].module = module;
                s.chains[d].hw = Some(hw);
                s.chains[d].claim_default = true;
            }
            Cmd::Disable(dir) => {
                let d = dir as usize;
                let mut s = inner.borrow_mut();
                // Meters first: the session manager removes a stream whose device
                // disappears, and we mustn't be holding one when that happens.
                for m in s.meters.drain(..) {
                    let _ = m.stream.disconnect();
                }
                if s.stop(d) {
                    wake_after_limit();
                }
                drop(s);
                finish();
                rebuild();
            }
            Cmd::Params(dir, params) => {
                let mut s = inner.borrow_mut();
                let chain = &mut s.chains[dir as usize];
                match &chain.control {
                    Some(node) => set_params(node, &params),
                    None if chain.on() => chain.pending.extend(params),
                    None => {}
                }
            }
            Cmd::Device(dir, device) => {
                let d = dir as usize;
                let mut s = inner.borrow_mut();
                s.chains[d].wanted = device;
                s.retarget(d);
                drop(s);
                rebuild();
            }
            Cmd::Meters(dir) => {
                inner.borrow_mut().metering = dir;
                rebuild();
            }
            Cmd::Quit => {
                let mut s = inner.borrow_mut();
                s.metering = None;
                for m in s.meters.drain(..) {
                    let _ = m.stream.disconnect();
                }
                s.quitting = true;
                for d in 0..2 {
                    if s.stop(d) {
                        wake_after_limit();
                    }
                }
                drop(s);
                finish();
            }
        })
    };
    mainloop.run();

    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::{Sound, plugin_controls};

    #[test]
    fn args_route_the_chain_in_order() {
        let c = plugin_controls(&Sound::flat(), Direction::Output);
        let a = module_args(Direction::Output, "hw_sink", &c, Path::new("/x/lsp.so"));
        assert!(a.contains("inputs = [ \"autogain:Input L\""));
        assert!(a.contains("{ output = \"eq:Output R\" input = \"loudness:Input R\" }"));
        assert!(a.contains("outputs = [ \"volume_l:Out\" \"volume_r:Out\" ]"));
        assert!(a.contains("target.object = \"hw_sink\""));
        assert!(a.contains(SINK_NAME));
        assert!(a.contains("\"Gain 0 (G)\" = 1.0"));
    }

    #[test]
    fn the_output_volume_comes_after_the_whole_chain() {
        let c = plugin_controls(&Sound::flat(), Direction::Output);
        let a = module_args(Direction::Output, "hw_sink", &c, Path::new("/x/lsp.so"));
        assert!(a.contains("{ type = builtin name = volume_l label = linear }"));
        assert!(a.contains("{ type = builtin name = volume_r label = linear }"));
        assert!(a.contains("{ output = \"limiter:Output L\" input = \"volume_l:In\" }"));
        assert!(a.contains("{ output = \"limiter:Output R\" input = \"volume_r:In\" }"));
        // Left then right, matching audio.position, and inside filter.graph.
        let vols = "capture.volumes = [ { control = \"volume_l:Mult\" } { control = \"volume_r:Mult\" } ] }";
        assert!(a.contains(vols), "{a}");
        assert!(a.find(vols) < a.find("audio.channels"));
        assert_eq!(a.matches("volume_l:In").count(), 1);
        assert!(!a.contains("playback.volumes"));
    }

    fn props(volumes: Option<Vec<f32>>, mute: Option<bool>) -> Value {
        let mut properties = vec![];
        let mut add = |key, value| {
            properties.push(Property {
                key,
                flags: PropertyFlags::empty(),
                value,
            })
        };
        add(spa_sys::SPA_PROP_volume, Value::Float(1.0));
        if let Some(v) = volumes {
            add(spa_sys::SPA_PROP_channelVolumes, Value::ValueArray(ValueArray::Float(v)));
        }
        if let Some(m) = mute {
            add(spa_sys::SPA_PROP_mute, Value::Bool(m));
        }
        Value::Object(Object {
            type_: spa::utils::SpaTypes::ObjectParamProps.as_raw(),
            id: spa::param::ParamType::Props.as_raw(),
            properties,
        })
    }

    #[test]
    fn the_post_meter_undoes_each_channels_volume() {
        assert_eq!(volume_gain(&props(Some(vec![0.125, 0.5]), Some(false))), Some([8.0, 2.0]));
        assert_eq!(volume_gain(&props(Some(vec![1.0, 1.0]), None)), Some([1.0, 1.0]));
        assert_eq!(volume_gain(&props(Some(vec![0.25]), None)), Some([4.0, 4.0]));
    }

    #[test]
    fn muted_or_silent_stays_silent_on_the_post_meter() {
        assert_eq!(volume_gain(&props(Some(vec![0.5, 0.5]), Some(true))), Some([0.0, 0.0]));
        assert_eq!(volume_gain(&props(Some(vec![0.0, 0.5]), None)), Some([0.0, 2.0]));
    }

    #[test]
    fn props_without_a_volume_leave_the_post_meter_alone() {
        assert_eq!(volume_gain(&props(None, Some(true))), None);
        assert_eq!(volume_gain(&props(Some(vec![]), None)), None);
        assert_eq!(volume_gain(&Value::Bool(true)), None);
    }

    #[test]
    fn the_mic_chain_makes_a_source_not_a_sink() {
        let c = plugin_controls(&Sound::flat(), Direction::Mic);
        let a = module_args(Direction::Mic, "hw_mic", &c, Path::new("/x/lsp.so"));
        assert!(a.contains("media.class = Audio/Source"));
        assert!(!a.contains("Audio/Sink"));
        assert!(a.contains("inputs = [ \"gate:Input L\""));
        // The source's own volume is already applied after the chain.
        assert!(a.contains("outputs = [ \"limiter:Output L\" \"limiter:Output R\" ]"));
        assert!(!a.contains("volumes") && !a.contains("builtin"), "{a}");
    }

    #[test]
    fn a_device_name_cannot_add_its_own_settings() {
        let evil = r#"x" } filter.graph = { nodes = [ ] } playback.props = { a = "b"#;
        let escaped = r#"target.object = "x\" } filter.graph = { nodes = [ ] } playback.props = { a = \"b""#;
        for dir in [Direction::Output, Direction::Mic] {
            let c = plugin_controls(&Sound::flat(), dir);
            let a = module_args(dir, evil, &c, Path::new("/x/lsp.so"));
            assert!(a.contains(escaped), "{a}");
            assert_eq!(a.matches("filter.graph = {").count(), 1 + a.matches(escaped).count());
        }
    }

    #[test]
    fn the_plugin_path_is_escaped_too() {
        let c = plugin_controls(&Sound::flat(), Direction::Output);
        let a = module_args(Direction::Output, "hw", &c, Path::new(r#"/x"y\z/lsp.so"#));
        assert!(a.contains(r#"plugin = "/x\"y\\z/lsp.so""#), "{a}");
    }

    #[test]
    fn a_default_device_name_is_escaped() {
        let name = r#"x","other":"y"#;
        assert_eq!(name_json(Some(&default_device_json(name))).as_deref(), Some(name));
    }

    #[test]
    fn default_device_json_parses() {
        assert_eq!(name_json(Some("{\"name\":\"abc\"}")).as_deref(), Some("abc"));
        assert_eq!(name_json(Some("nonsense")), None);
        assert_eq!(name_json(None), None);
    }

    #[test]
    fn an_output_waits_for_streams_to_leave_and_then_to_settle() {
        let start = Instant::now();
        let mut drain = Drain::new(start);
        assert!(!drain.ready(start, true), "a stream is still attached");

        let left = start + Duration::from_millis(100);
        assert!(!drain.ready(left, false), "the stream has only just left");
        assert!(!drain.ready(left + DRAIN_SETTLE - Duration::from_millis(1), false));
        assert!(drain.ready(left + DRAIN_SETTLE, false));
    }

    #[test]
    fn a_stream_coming_back_restarts_the_settling() {
        let start = Instant::now();
        let mut drain = Drain::new(start);
        assert!(!drain.ready(start, false));
        let back = start + Duration::from_millis(400);
        assert!(!drain.ready(back, true));
        let left = back + Duration::from_millis(50);
        assert!(!drain.ready(left, false));
        assert!(!drain.ready(start + DRAIN_SETTLE, false), "the first departure no longer counts");
        assert!(drain.ready(left + DRAIN_SETTLE, false));
    }

    #[test]
    fn an_output_is_removed_at_the_limit_even_with_streams_attached() {
        let start = Instant::now();
        let mut drain = Drain::new(start);
        assert!(!drain.ready(start + DRAIN_TIMEOUT - Duration::from_millis(1), true));
        assert!(drain.ready(start + DRAIN_TIMEOUT, true));
    }

    #[test]
    fn a_device_is_only_handed_back_if_it_still_exists() {
        let device = |name: &str| Device {
            name: name.to_string(),
            description: String::new(),
        };
        let devices = [device("speakers"), device("headphones")];
        assert_eq!(hand_back_device(Some("headphones"), &devices).as_deref(), Some("headphones"));
        assert_eq!(hand_back_device(Some("unplugged"), &devices), None);
        assert_eq!(hand_back_device(None, &devices), None);
    }

    #[test]
    fn only_ordinary_streams_on_the_device_are_moved() {
        let node = |class: &str, dont_fallback| NodeInfo {
            name: String::new(),
            description: String::new(),
            class: class.to_string(),
            serial: String::new(),
            dont_fallback,
        };
        let nodes = HashMap::from([
            (1, node("Audio/Sink", false)),
            (2, node("Stream/Output/Audio", false)),
            (3, node("Stream/Output/Audio", false)),
            (4, node("Stream/Output/Audio", true)),
            (5, node("Audio/Source", false)),
            (6, node("Stream/Output/Audio", false)),
        ]);
        let link = |input, output| LinkEnds { input, output };
        let links = HashMap::from([
            (10, link(1, 3)),
            (11, link(1, 2)),
            (12, link(1, 2)), // A stereo stream has one link for each channel.
            (13, link(1, 4)),
            (14, link(1, 5)),
            (15, link(9, 6)),  // Plays to another device.
            (16, link(1, 99)), // A node that's already gone.
        ]);
        assert_eq!(streams_to_move(&links, &nodes, 1), vec![2, 3]);
    }
}
