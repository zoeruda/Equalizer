//! A widget that draws itself into a snapshot, for the EQ graph.
//!
//! A `gtk::DrawingArea` is one cairo drawing, which GTK rasterizes on the CPU and uploads
//! again whenever anything in it moves: at 30 fps for the EQ graph, even though only the
//! spectrum moves. Drawing into the snapshot lets the graph hand GTK its still parts as
//! textures, which stay on the GPU until they change, and a cairo drawing only the size
//! of the spectrum.
use gtk::gdk;
use gtk::glib;
use gtk::graphene;
use gtk::prelude::*;
use gtk::subclass::prelude::*;
use std::cell::RefCell;

type SnapshotFn = Box<dyn Fn(&EqWidget, &gtk::Snapshot)>;

mod imp {
    use super::*;

    #[derive(Default)]
    pub struct EqWidget {
        pub snapshot: RefCell<Option<SnapshotFn>>,
    }

    #[glib::object_subclass]
    impl ObjectSubclass for EqWidget {
        const NAME: &'static str = "EqualizerEqWidget";
        type Type = super::EqWidget;
        type ParentType = gtk::Widget;
    }

    impl ObjectImpl for EqWidget {}

    impl WidgetImpl for EqWidget {
        fn snapshot(&self, snapshot: &gtk::Snapshot) {
            if let Some(f) = &*self.snapshot.borrow() {
                f(&self.obj(), snapshot);
            }
        }
    }
}

glib::wrapper! {
    pub struct EqWidget(ObjectSubclass<imp::EqWidget>)
        @extends gtk::Widget,
        @implements gtk::Accessible, gtk::Buildable, gtk::ConstraintTarget;
}

impl Default for EqWidget {
    fn default() -> Self {
        glib::Object::new()
    }
}

impl EqWidget {
    pub fn set_snapshot_func(&self, f: impl Fn(&EqWidget, &gtk::Snapshot) + 'static) {
        *self.imp().snapshot.borrow_mut() = Some(Box::new(f));
        self.queue_draw();
    }
}

/// A still part of a graph, drawn once with cairo and kept as a texture.
pub struct Layer {
    texture: gdk::MemoryTexture,
    bounds: graphene::Rect,
}

impl Layer {
    /// Draws a `w`×`h` area at `scale`. It's drawn shifted by `offset`, the fraction of a
    /// device pixel the widget's corner falls at, so the texture's pixels land on the
    /// screen's pixels and GTK never has to resample it.
    pub fn render(w: f64, h: f64, scale: f64, offset: (f64, f64), draw: impl FnOnce(&gtk::cairo::Context)) -> Option<Layer> {
        let (ox, oy) = offset;
        let (pw, ph) = ((w * scale + ox).ceil().max(1.0) as i32, (h * scale + oy).ceil().max(1.0) as i32);
        let mut image = gtk::cairo::ImageSurface::create(gtk::cairo::Format::ARgb32, pw, ph).ok()?;
        {
            let cr = gtk::cairo::Context::new(&image).ok()?;
            cr.translate(ox, oy);
            cr.scale(scale, scale);
            draw(&cr);
        }
        image.flush();
        let stride = image.stride() as usize;
        let bytes = glib::Bytes::from(&*image.data().ok()?);
        // cairo's ARGB32 is a native-endian 32-bit word.
        #[cfg(target_endian = "little")]
        let format = gdk::MemoryFormat::B8g8r8a8Premultiplied;
        #[cfg(target_endian = "big")]
        let format = gdk::MemoryFormat::A8r8g8b8Premultiplied;
        Some(Layer {
            texture: gdk::MemoryTexture::new(pw, ph, format, &bytes, stride),
            bounds: graphene::Rect::new(
                (-ox / scale) as f32,
                (-oy / scale) as f32,
                (pw as f64 / scale) as f32,
                (ph as f64 / scale) as f32,
            ),
        })
    }

    pub fn append(&self, snapshot: &gtk::Snapshot) {
        snapshot.append_texture(&self.texture, &self.bounds);
    }
}
