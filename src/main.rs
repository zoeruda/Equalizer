mod app;
mod dsp;
mod engine;
mod eq_graph;
mod graphs;
mod model;
mod theme;

use adw::prelude::*;
use gtk::glib;
use std::cell::Cell;
use std::rc::Rc;

fn main() -> glib::ExitCode {
    let start_hidden = std::env::args().any(|a| a == "--background");
    glib::set_application_name("Equalizer");
    glib::set_prgname(Some(app::APP_ID));
    let application = adw::Application::builder().application_id(app::APP_ID).build();
    let built = Rc::new(Cell::new(false));
    application.connect_activate(move |a| {
        if built.replace(true) {
            // Opening the app again brings the existing window back.
            if let Some(w) = a.windows().first() {
                w.present();
            }
        } else {
            app::build(a, start_hidden);
        }
    });
    // Our own flag is read above, so GTK doesn't need to see the arguments.
    application.run_with_args::<String>(&[])
}
