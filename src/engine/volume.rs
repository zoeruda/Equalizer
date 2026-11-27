//! The rules that keep Equalizer's output volume and the real device's volume the same,
//! with no PipeWire in them: events go in, and what to do comes out. `Inner` makes the
//! PipeWire calls and hands the events over.

use super::*;

/// What `VolumeSync` asks for.
#[derive(Debug, PartialEq)]
pub(super) enum Action {
    /// Set the real device to this volume.
    SetDevice(Volume),
    /// Show this volume on Equalizer's output (with its soft volumes).
    ShowOnSink(Volume),
}

/// What's known of the real device when Equalizer's output reports a volume.
#[derive(Clone, Copy)]
pub(super) enum RealDevice<'a> {
    /// The Output chain has no real device.
    Absent,
    /// It has one, whose volume isn't known.
    NoVolume,
    Volume(&'a Volume),
}

#[derive(Default)]
pub(super) struct VolumeSync {
    /// Equalizer's own output volume, as last reported.
    pub(super) sink_volume: Option<Volume>,
    /// Whether Equalizer's output has shown the real device's volume yet. Until then,
    /// its own volume is only the one it started with.
    pub(super) synced: bool,
    /// Volumes recently sent to the real device, to recognize them coming back.
    pub(super) sent: VecDeque<Sent>,
    /// The volumes set on Equalizer's output that it hasn't reported back yet, oldest
    /// first (it reports in order). The last is what it will show: setting the same one
    /// again would do nothing useful, and a report of an earlier one is the app's own
    /// change coming back, not a change made on the desktop.
    pub(super) shown: VecDeque<(Volume, Instant)>,
}

impl VolumeSync {
    /// The real device is a different one, or none: Equalizer's output shows its volume
    /// before passing changes on.
    pub(super) fn device_changed(&mut self) {
        self.sent.clear();
        self.shown.clear();
        self.synced = false;
    }

    /// Equalizer's output is gone.
    pub(super) fn sink_gone(&mut self) {
        self.sink_volume = None;
        self.synced = false;
        self.shown.clear();
    }

    /// The real device reports its volume, which may be a change made anywhere. Shows it
    /// on Equalizer's output, unless it only comes from what was sent to the device.
    /// `sink` is whether Equalizer's output can be set yet.
    pub(super) fn device_reported(&mut self, volume: Option<&Volume>, sink: bool, now: Instant) -> Option<Action> {
        self.sent.retain(|s| now.duration_since(s.at) < ECHO_WAIT);
        let v = volume?;
        for s in self.sent.iter_mut().filter(|s| s.volume.close(v, ECHO_DB)) {
            s.settled = true;
        }
        // The device reports changes in the order it got them, so once a later one has
        // come back, an earlier one that came back before it won't again: its volume
        // reported from now on was set on the device, by a button that steps as the
        // desktop's keys do.
        // (A report near several is taken for the oldest, so that none is given up early.)
        if let Some(i) = self.sent.iter().position(|s| s.volume.close(v, ECHO_DB)) {
            self.sent[i].back = true;
            let mut n = 0;
            self.sent.retain(|s| {
                n += 1;
                n > i || !s.back
            });
        }
        if let Some(kind) = echo_kind(&self.sent, v, now) {
            match kind {
                Echo::Sent => trace!("volume: device reports {}, ignored as an echo of what was sent", db_text(v)),
                Echo::Before => trace!(
                    "volume: device reports {}, ignored as the volume it had before, reported once more",
                    db_text(v)
                ),
            }
            return None;
        }
        // A change made on the device: what was sent before is no longer what it has, and
        // what has come back can be chosen again, by the next press.
        self.sent.retain(|s| !s.back);
        for s in &mut self.sent {
            s.settled = true;
        }
        let shown = v.for_channels(2);
        // What Equalizer's output has, or is about to have: a volume just set on it that it
        // hasn't reported yet counts, or two reports close together would undo each other.
        self.shown.retain(|(_, at)| now.duration_since(*at) < ECHO_WAIT);
        if let Some((pending, _)) = self.shown.back() {
            if pending.close(&shown, SAME_DB) {
                // Not "synced": the output hasn't reported it, so it has shown nothing yet.
                trace!(
                    "volume: device reports {}, what's already on its way to Equalizer's device",
                    db_text(v)
                );
                return None;
            }
        } else if self.sink_volume.as_ref().is_some_and(|s| s.close(&shown, SAME_DB)) {
            trace!("volume: device reports {}, already what Equalizer's device has", db_text(v));
            self.synced = true;
            return None;
        }
        trace!("volume: device reports {}, a change from outside: showing it", db_text(v));
        self.show(shown, sink, now)
    }

    /// Equalizer's output reports a volume: passes a change on to the real device.
    pub(super) fn sink_reported(&mut self, v: Volume, device: RealDevice, sink: bool, now: Instant) -> Option<Action> {
        trace!("volume: Equalizer's device reports {}", db_text(&v));
        self.shown.retain(|(_, at)| now.duration_since(*at) < ECHO_WAIT);
        if let Some(i) = self.shown.iter().position(|(s, _)| s.close(&v, SAME_DB)) {
            self.shown.drain(..=i);
            self.sink_volume = Some(v.clone());
            if !self.shown.is_empty() {
                // The app's own change, and a newer one is still on its way: not one made on
                // the desktop, and not the volume to compare with the device's yet.
                trace!("volume: that's what was shown earlier, a newer volume is still on its way");
                return None;
            }
            return self.shown_arrived(&v, device, sink, now);
        }
        self.sink_volume = Some(v.clone());
        let current = match device {
            RealDevice::Absent => {
                trace!("volume: no real device to pass it to");
                return None;
            }
            RealDevice::NoVolume => {
                trace!("volume: the real device has no volume to compare with");
                return None;
            }
            RealDevice::Volume(c) => c,
        };
        // What the device is taken to have: the last volume sent to it while that's still
        // on its way (it reports a change late, and the volume it last reported is then
        // the one from before), otherwise the one it last reported.
        let on_its_way = self
            .sent
            .iter()
            .rev()
            .find(|s| !s.settled && now.duration_since(s.at) < ECHO_WAIT)
            .map(|s| &s.volume);
        let expected = on_its_way.unwrap_or(current);
        if v.close(&expected.for_channels(v.channels.len()), SAME_DB) {
            trace!(
                "volume: same as the device's {}{}, nothing to pass on",
                db_text(expected),
                if on_its_way.is_some() { " (as sent, not reported yet)" } else { "" }
            );
            self.synced = true;
            return None;
        }
        if !self.synced {
            trace!(
                "volume: not synced yet, showing the device's {} instead of passing this on",
                db_text(current)
            );
            // Equalizer's output has just appeared, at the volume it starts with. It
            // shows the real device's volume first, and only then passes changes on.
            return self.show(current.for_channels(2), sink, now);
        }
        let out = v.for_channels(current.channels.len());
        trace!("volume: passing {} on to the device (it had {})", db_text(&out), db_text(current));
        self.sent.push_back(Sent {
            volume: out.clone(),
            before: current.clone(),
            at: now,
            settled: false,
            back: false,
        });
        Some(Action::SetDevice(out))
    }

    /// Equalizer's output now has the volume the app showed there. That is never passed
    /// on to the device: if the device has moved on meanwhile, the output follows it.
    fn shown_arrived(&mut self, v: &Volume, device: RealDevice, sink: bool, now: Instant) -> Option<Action> {
        let RealDevice::Volume(current) = device else { return None };
        // With a change from the desktop on its way, the device's volume is an old one.
        let on_its_way = self.sent.iter().any(|s| !s.settled && now.duration_since(s.at) < ECHO_WAIT);
        if on_its_way || v.close(&current.for_channels(v.channels.len()), ECHO_DB) {
            self.synced = true;
            return None;
        }
        trace!("volume: the device has moved on to {}", db_text(current));
        self.show(current.for_channels(2), sink, now)
    }

    fn show(&mut self, v: Volume, sink: bool, now: Instant) -> Option<Action> {
        if self
            .shown
            .back()
            .is_some_and(|(s, at)| s.close(&v, SAME_DB) && now.duration_since(*at) < ECHO_WAIT)
        {
            trace!("volume: not showing {} again, just shown", db_text(&v));
            return None;
        }
        if !sink {
            return None;
        }
        trace!("volume: showing {} on Equalizer's device", db_text(&v));
        self.shown.push_back((v.clone(), now));
        Some(Action::ShowOnSink(v))
    }
}

#[cfg(test)]
mod tests {
    //! Random sequences of what the user does and what a misbehaving device reports,
    //! against rules that must hold for all of them. The device is simulated: it may
    //! report late, report its old volume first, repeat itself, round to its own steps,
    //! have another channel count, and change by itself (its own buttons).

    use super::*;
    use proptest::prelude::*;

    /// How far apart the sink's and the device's volumes may end up: a device's rounding
    /// plus the app's own tolerance for it (`ECHO_DB`).
    const SETTLED_DB: f32 = 1.0;

    fn lin(db: f32) -> f32 {
        10f32.powf(db / 20.0)
    }

    /// A volume for two channels, the same on both most of the time, muted now and then.
    fn arb_volume() -> impl Strategy<Value = Volume> {
        (-50.0f32..0.0, -50.0f32..0.0, 0u8..10, 0u8..10).prop_map(|(a, b, split, mute)| Volume {
            channels: vec![lin(a), lin(if split < 7 { a } else { b })],
            mute: mute == 0,
        })
    }

    #[derive(Clone, Debug)]
    struct Hardware {
        channels: usize,
        /// Its volume steps, in dB; 0 for none.
        step_db: f32,
        /// Reports its old volume once, just after a change from the app.
        stale: bool,
        /// Reports the new volume twice.
        repeat: bool,
    }

    impl Hardware {
        fn round(&self, v: &Volume) -> Volume {
            let mut v = v.for_channels(self.channels);
            if self.step_db > 0.0 {
                for c in &mut v.channels {
                    let db = 20.0 * c.log10();
                    *c = lin((db / self.step_db).round() * self.step_db);
                }
            }
            v
        }
    }

    fn arb_hardware() -> impl Strategy<Value = Hardware> {
        (
            prop_oneof![Just(1usize), Just(2), Just(2), Just(6)],
            prop_oneof![Just(0.0f32), Just(0.5), Just(1.0)],
            any::<bool>(),
            any::<bool>(),
        )
            .prop_map(|(channels, step_db, stale, repeat)| Hardware {
                channels,
                step_db,
                stale,
                repeat,
            })
    }

    #[derive(Clone, Debug)]
    enum Op {
        /// The user sets Equalizer's volume (the desktop's slider or keys).
        User(Volume),
        /// The device's own buttons.
        Button(Volume),
        /// Another device takes over the chain.
        Replace(Volume, Hardware),
    }

    fn arb_op() -> impl Strategy<Value = (u64, Op)> {
        // Mostly well apart, so that the final check isn't skipped for most runs; the
        // rest close together, to find races.
        let gap = prop_oneof![6 => 1600u64..4000, 2 => 0u64..100, 1 => 100u64..1500];
        let op = prop_oneof![
            4 => arb_volume().prop_map(Op::User),
            2 => arb_volume().prop_map(Op::Button),
            1 => (arb_volume(), arb_hardware()).prop_map(|(v, h)| Op::Replace(v, h)),
        ];
        (gap, op)
    }

    #[derive(Clone, Debug)]
    struct Script {
        hardware: Hardware,
        /// The device's volume when the chain starts, and Equalizer's own.
        device: Volume,
        sink: Volume,
        /// Which of the two reports its volume first, and how many ms apart.
        sink_first: bool,
        first_gap: u64,
        /// How late the device reports, taken in turn, and how late the sink does (ms).
        lags: Vec<u64>,
        sink_lag: u64,
        ops: Vec<(u64, Op)>,
        /// Whether the two must agree in the end even after one of the known races (see
        /// `Sim::taint`). Only the tests of those races ask for it.
        strict: bool,
    }

    fn arb_script() -> impl Strategy<Value = Script> {
        (
            arb_hardware(),
            arb_volume(),
            arb_volume(),
            any::<bool>(),
            0u64..50,
            proptest::collection::vec(0u64..500, 1..6),
            0u64..30,
            proptest::collection::vec(arb_op(), 0..16),
        )
            .prop_map(|(hardware, device, sink, sink_first, first_gap, lags, sink_lag, ops)| Script {
                strict: false,
                hardware,
                device,
                sink,
                sink_first,
                first_gap,
                lags,
                sink_lag,
                ops,
            })
    }

    enum Ev {
        /// The device reports a volume, and whether it's from its own buttons.
        Device(Volume, bool),
        /// Equalizer's output reports a volume, and where it came from.
        Sink(Volume, Origin),
    }

    #[derive(Clone, Copy, PartialEq)]
    enum Origin {
        /// The volume it started with.
        Initial,
        /// The user set it, at this time.
        User(u64),
        /// The app showed the device's volume there.
        Show,
    }

    struct Sim<'a> {
        script: &'a Script,
        base: Instant,
        now: u64,
        sync: VolumeSync,
        hardware: Hardware,
        /// Equalizer's output, and the device, as they really are.
        sink: Volume,
        actual: Volume,
        /// The device's volume as last reported, which is all the app knows of it.
        seen: Option<Volume>,
        /// Reports on their way: when, in which order, to which device, and what.
        queue: Vec<(u64, u64, u32, Ev)>,
        seq: u64,
        epoch: u32,
        last_report: u64,
        /// The same for Equalizer's output, which reports in the order things happened.
        last_sink_report: u64,
        /// When the reports of the last button press are all in.
        button_until: Option<u64>,
        /// When the app last sent the device a volume.
        last_send: Option<u64>,
        /// When the user or the device last changed the volume.
        last_change: Option<u64>,
        lag_i: usize,
        /// Every volume the user or the device chose, and the last of them.
        chosen: Vec<Volume>,
        last_chosen: Volume,
        actions: usize,
        externals: usize,
        /// What happened, for the failure message.
        log: Vec<String>,
        tainted: Option<&'static str>,
    }

    impl<'a> Sim<'a> {
        fn new(script: &'a Script) -> Self {
            let hardware = script.hardware.clone();
            let actual = hardware.round(&script.device);
            let mut sim = Sim {
                script,
                base: Instant::now(),
                now: 0,
                sync: VolumeSync::default(),
                hardware,
                sink: script.sink.clone(),
                seen: None,
                queue: vec![],
                seq: 0,
                epoch: 0,
                last_report: 0,
                last_sink_report: 0,
                button_until: None,
                last_send: None,
                last_change: None,
                lag_i: 0,
                chosen: vec![actual.clone()],
                last_chosen: actual.clone(),
                actual,
                actions: 0,
                externals: 1,
                log: vec![],
                tainted: None,
            };
            let (a, b) = if script.sink_first {
                (0, script.first_gap)
            } else {
                (script.first_gap, 0)
            };
            sim.sink_report(script.sink.clone(), a, Origin::Initial);
            sim.device_report(sim.actual.clone(), b, false);
            sim
        }

        fn push(&mut self, at: u64, ev: Ev) {
            self.seq += 1;
            self.queue.push((at, self.seq, self.epoch, ev));
        }

        fn lag(&mut self) -> u64 {
            let lag = self.script.lags[self.lag_i % self.script.lags.len()];
            self.lag_i += 1;
            lag
        }

        /// A report from Equalizer's output, in order with the ones before it.
        fn sink_report(&mut self, v: Volume, delay: u64, origin: Origin) {
            let at = if self.last_sink_report == 0 && self.queue.is_empty() && self.seq == 0 {
                delay
            } else {
                (self.now + delay).max(self.last_sink_report + 1)
            };
            self.last_sink_report = at;
            self.push(at, Ev::Sink(v, origin));
        }

        /// A report from the device, in order with the ones before it.
        fn device_report(&mut self, v: Volume, delay: u64, button: bool) {
            let at = (self.now + delay).max(self.last_report + 1);
            self.last_report = at;
            self.push(at, Ev::Device(v, button));
        }

        /// The device's volume changes, from the app or by itself.
        fn device_changes(&mut self, to: &Volume, from_app: bool) {
            let before = self.actual.clone();
            self.actual = self.hardware.round(to);
            let lag = self.lag();
            if from_app && self.hardware.stale && before != self.actual {
                self.device_report(before, 5.min(lag), false);
            }
            self.device_report(self.actual.clone(), lag, !from_app);
            if self.hardware.repeat {
                self.device_report(self.actual.clone(), lag + 7, !from_app);
            }
        }

        fn instant(&self) -> Instant {
            self.base + Duration::from_millis(self.now)
        }

        fn act(&mut self, action: Option<Action>) -> Result<(), String> {
            let Some(action) = action else { return Ok(()) };
            self.actions += 1;
            self.log.push(format!("{:>6} ms  app: {action:?}", self.now));
            match action {
                Action::SetDevice(out) => {
                    // The app never sends the device a volume nobody chose.
                    let n = out.channels.len();
                    if !self.chosen.iter().any(|c| c.for_channels(n) == out) {
                        return Err(format!("sent the device {out:?}, which nobody chose"));
                    }
                    self.last_send = Some(self.now);
                    self.device_changes(&out, true);
                }
                Action::ShowOnSink(v) => {
                    // Never the volume the output has reported: PipeWire would apply it
                    // twice. (Only what the app has been told counts: before the output
                    // reports a change in flight, the app can't know. The soft volumes
                    // that go with every volume make that harmless.)
                    // (A volume set on it earlier and not reported yet means it doesn't
                    // have the reported one any more: the app's own pending change is the
                    // last in `shown`, so the one before it counts.)
                    if self.sync.sink_volume.as_ref() == Some(&v) && self.sync.shown.len() <= 1 {
                        return Err(format!("showed {v:?} on Equalizer's output, which has it already"));
                    }
                    self.sink = v.clone();
                    self.sink_report(v, self.script.sink_lag, Origin::Show);
                }
            }
            Ok(())
        }

        /// Notes that one of the known races happened, after which the two volumes may
        /// legitimately end up apart. Each is a find of Part 4 of the 0.0.8 plan, with a
        /// test of its own below; the random runs still check every other rule after them.
        fn taint(&mut self, why: &'static str) {
            self.tainted.get_or_insert(why);
        }

        fn handle(&mut self, ev: Ev) -> Result<(), String> {
            let now = self.instant();
            let action = match ev {
                Ev::Device(v, button) => {
                    self.log.push(format!("{:>6} ms  device reports {v:?}", self.now));
                    let stale = |s: &Sent| now.duration_since(s.at) < STALE_WAIT && s.before.close(&v, STALE_DB);
                    if button && self.sync.sent.iter().any(stale) {
                        // Back to the old volume in the very moment the app's change goes
                        // out: taken for the old one reported once more (find 2).
                        self.taint("a button press back to the volume before, as the app's change went out");
                    }
                    self.seen = Some(v);
                    self.sync.device_reported(self.seen.as_ref(), true, now)
                }
                Ev::Sink(v, origin) => {
                    self.log.push(format!("{:>6} ms  Equalizer's output reports {v:?}", self.now));
                    if let Origin::User(at) = origin {
                        if !self.sync.synced {
                            self.taint("a change before the device's volume was shown on Equalizer's output");
                        }
                        if self.button_until.is_some_and(|until| at <= until) {
                            self.taint("a change from the desktop while a button press was still being reported");
                        }
                    }
                    let device = match &self.seen {
                        None => RealDevice::NoVolume,
                        Some(s) => RealDevice::Volume(s),
                    };
                    self.sync.sink_reported(v, device, true, now)
                }
            };
            self.act(action)
        }

        /// Lets the reports due by `t` arrive.
        fn run_until(&mut self, t: u64) -> Result<(), String> {
            loop {
                let next = self
                    .queue
                    .iter()
                    .enumerate()
                    .filter(|(_, (at, ..))| *at <= t)
                    .min_by_key(|(_, (at, seq, ..))| (*at, *seq))
                    .map(|(i, _)| i);
                let Some(i) = next else { break };
                let (at, _, epoch, ev) = self.queue.remove(i);
                if epoch != self.epoch {
                    continue; // from a device that's gone
                }
                self.now = at;
                self.handle(ev)?;
            }
            self.now = t;
            Ok(())
        }

        fn op(&mut self, op: &Op) -> Result<(), String> {
            if let Op::Replace(..) = op
                && (!self.queue.is_empty() || self.last_change.is_some_and(|t| self.now < t + ECHO_WAIT.as_millis() as u64))
            {
                self.taint("another device took over while changes were still on their way");
            }
            self.externals += 1;
            self.log.push(format!("{:>6} ms  {op:?}", self.now));
            if !matches!(op, Op::Replace(..)) {
                self.last_change = Some(self.now);
            }
            match op {
                Op::User(v) => {
                    self.sink = v.clone();
                    self.chosen.push(v.clone());
                    self.last_chosen = v.clone();
                    self.sink_report(v.clone(), self.script.sink_lag, Origin::User(self.now));
                }
                Op::Button(v) => {
                    if self.queue.iter().any(|(.., ev)| matches!(ev, Ev::Sink(_, Origin::User(_)))) {
                        // The app passes the desktop's change on afterwards, over the button's.
                        self.taint("a button press before the app had passed on a desktop change");
                    }
                    self.device_changes(v, false);
                    self.button_until = Some(self.last_report);
                    self.chosen.push(self.actual.clone());
                    self.last_chosen = self.actual.clone();
                }
                Op::Replace(v, hardware) => {
                    self.epoch += 1;
                    self.queue.retain(|(_, _, e, _)| *e == self.epoch);
                    self.hardware = hardware.clone();
                    self.actual = hardware.round(v);
                    self.seen = None;
                    self.last_report = self.now;
                    self.sync.device_changed();
                    let lag = self.lag();
                    self.device_report(self.actual.clone(), lag, false);
                    self.chosen.push(self.actual.clone());
                    self.last_chosen = self.actual.clone();
                }
            }
            Ok(())
        }

        fn run(&mut self) -> Result<(), String> {
            self.run_all()
                .map_err(|e| format!("{e}\nafter: {:?}\n{}", self.tainted, self.log.join("\n")))
        }

        fn run_all(&mut self) -> Result<(), String> {
            for (gap, op) in &self.script.ops {
                let t = self.now + gap;
                self.run_until(t)?;
                self.op(op)?;
            }
            // Nothing changes for a while: everything arrives, and it settles.
            let t = self.now + 2000;
            self.run_until(t)?;
            self.settled()
        }

        fn settled(&self) -> Result<(), String> {
            // A device with one channel can't show left and right apart, and one with
            // more has only the loudest of them to show.
            let shown = self.sink.for_channels(self.hardware.channels);
            if self.tainted.is_some() && !self.script.strict {
                return self.bounded();
            }
            if !self.actual.close(&shown, SETTLED_DB) {
                return Err(format!(
                    "settled with Equalizer showing {:?} and the device at {:?}",
                    self.sink, self.actual
                ));
            }
            let tolerance = self.hardware.step_db / 2.0 + 0.1;
            let wanted = self.last_chosen.for_channels(self.hardware.channels);
            if !self.actual.close(&wanted, tolerance) {
                return Err(format!(
                    "settled with the device at {:?}, not the last volume chosen, {:?}",
                    self.actual, wanted
                ));
            }
            self.bounded()
        }

        /// One change causes a bounded number of messages: no bouncing.
        fn bounded(&self) -> Result<(), String> {
            if self.actions > 3 * self.externals + 4 {
                return Err(format!("{} messages for {} changes", self.actions, self.externals));
            }
            Ok(())
        }
    }

    #[test]
    #[ignore = "find 3 of Part 4, left on purpose (the user, 2026-11-18): a button press and a desktop change at the same instant have no right answer; seen only in the model"]
    fn a_button_press_during_a_change_from_the_desktop_can_leave_them_apart() {
        let v = |db: f32, mute| Volume {
            channels: vec![lin(db); 2],
            mute,
        };
        let script = Script {
            hardware: Hardware {
                channels: 2,
                step_db: 0.0,
                stale: false,
                repeat: false,
            },
            device: v(0.0, true),
            sink: v(0.0, true),
            sink_first: false,
            first_gap: 0,
            lags: vec![52],
            sink_lag: 0,
            // The headphones' button unmutes; in the same moment the desktop's volume
            // is set to something else.
            ops: vec![(0, Op::Button(v(0.0, false))), (0, Op::User(v(-14.0, true)))],
            strict: true,
        };
        Sim::new(&script).run().unwrap();
    }

    #[test]
    #[ignore = "find 5 of Part 4, left on purpose (the user, 2026-11-18): by design, the output shows the device's volume before passing changes on; about 0.3 s on Bluetooth; seen only in the model"]
    fn a_change_before_the_device_has_reported_is_lost() {
        let v = |db: f32, mute| Volume {
            channels: vec![lin(db); 2],
            mute,
        };
        let script = Script {
            hardware: Hardware {
                channels: 2,
                step_db: 0.0,
                stale: false,
                repeat: false,
            },
            device: v(0.0, true),
            sink: v(0.0, true),
            sink_first: false,
            first_gap: 0,
            lags: vec![119],
            sink_lag: 0,
            // Another device takes over, and the volume is changed before it has
            // reported its own: the change goes nowhere, and the device's volume is shown.
            ops: vec![
                (
                    0,
                    Op::Replace(
                        v(0.0, true),
                        Hardware {
                            channels: 2,
                            step_db: 0.0,
                            stale: false,
                            repeat: false,
                        },
                    ),
                ),
                (0, Op::User(v(0.0, false))),
            ],
            strict: true,
        };
        Sim::new(&script).run().unwrap();
    }

    #[test]
    /// Part 4's find 1, fixed in Part 8: the volume the device is expected to have is the last one sent.
    fn going_back_before_the_device_reports_is_forgotten() {
        let v = |db: f32| Volume {
            channels: vec![lin(db); 2],
            mute: false,
        };
        let hardware = Hardware {
            channels: 2,
            step_db: 0.0,
            stale: false,
            repeat: false,
        };
        let script = Script {
            hardware,
            device: v(0.0),
            sink: v(0.0),
            sink_first: false,
            first_gap: 0,
            lags: vec![250],
            sink_lag: 0,
            // Down to -12 dB and back to 0 dB within the quarter second the device takes
            // to report: the second is "already the device's volume", which it isn't yet.
            ops: vec![(0, Op::User(v(-12.0))), (100, Op::User(v(0.0)))],
            strict: true,
        };
        Sim::new(&script).run().unwrap();
    }

    #[test]
    /// Part 4's find 4. The app still sets the device to the first volume for a moment, but
    /// the output's next report sets it to the second again, so they end up together.
    fn two_reports_closer_together_than_the_output_takes_to_report_one_undo_the_second() {
        let v = |db: f32| Volume {
            channels: vec![lin(db); 2],
            mute: false,
        };
        let hardware = Hardware {
            channels: 2,
            step_db: 0.0,
            stale: false,
            repeat: false,
        };
        let script = Script {
            hardware,
            device: v(-20.0),
            sink: v(-20.0),
            sink_first: false,
            first_gap: 0,
            lags: vec![100],
            sink_lag: 5,
            // The headphones' buttons: -10 dB, then -5 dB a millisecond later. Equalizer's
            // output reports the first after the app has shown the second, and the app
            // takes it for a change made on the desktop, and sets the device back to it.
            ops: vec![(1000, Op::Button(v(-10.0))), (0, Op::Button(v(-5.0)))],
            strict: true,
        };
        Sim::new(&script).run().unwrap();
    }

    #[test]
    fn a_button_press_back_to_a_volume_just_passed_through_is_followed() {
        let v = |db: f32| Volume {
            channels: vec![lin(db); 2],
            mute: false,
        };
        // The desktop's keys step from -18.1 dB up to -9.9 dB, and then the headphones'
        // button, which steps the same way, goes back one step, two, or nearly one.
        for pressed in [-12.4, -15.1, -12.0] {
            for gap in [110, 150, 450, 1000] {
                let script = Script {
                    hardware: Hardware {
                        channels: 2,
                        step_db: 0.0,
                        stale: true,
                        repeat: true,
                    },
                    device: v(-18.1),
                    sink: v(-18.1),
                    sink_first: false,
                    first_gap: 0,
                    lags: vec![8],
                    sink_lag: 0,
                    ops: vec![
                        (1000, Op::User(v(-15.1))),
                        (200, Op::User(v(-12.4))),
                        (200, Op::User(v(-9.9))),
                        (gap, Op::Button(v(pressed))),
                    ],
                    strict: true,
                };
                if let Err(e) = Sim::new(&script).run() {
                    panic!("{pressed} dB, {gap} ms after the last key: {e}");
                }
            }
        }
    }

    #[test]
    fn a_button_press_just_after_a_change_is_taken_for_its_echo() {
        let v = |db: f32| Volume {
            channels: vec![lin(db); 2],
            mute: false,
        };
        let hardware = Hardware {
            channels: 2,
            step_db: 0.0,
            stale: false,
            repeat: false,
        };
        let script = Script {
            hardware,
            device: v(-20.0),
            sink: v(-20.0),
            sink_first: false,
            first_gap: 0,
            lags: vec![0],
            sink_lag: 0,
            // The desktop sets 0 dB; 100 ms later the headphones' button goes to -19.5 dB,
            // which is near what the device had before (-20), reported once more as
            // far as the app can tell. Equalizer keeps showing 0 dB.
            ops: vec![(0, Op::User(v(0.0))), (100, Op::Button(v(-19.5)))],
            strict: true,
        };
        Sim::new(&script).run().unwrap();
    }

    proptest! {
        #![proptest_config(ProptestConfig::with_cases(
            std::env::var("PROPTEST_CASES").ok().and_then(|v| v.parse().ok()).unwrap_or(2000)
        ))]

        #[test]
        fn the_two_volumes_agree_whatever_the_device_does(script in arb_script()) {
            let mut sim = Sim::new(&script);
            if let Err(e) = sim.run() {
                return Err(TestCaseError::fail(e));
            }
        }
    }
}
