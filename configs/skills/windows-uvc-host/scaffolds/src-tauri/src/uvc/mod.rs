// OP-1817 (Track B / B-1) — UVC host capture domain root.
//
// The host-side USB Video Class capability for the Windows desktop app:
// enumerate attached cameras (`device`), run a capture session
// (`capture`), and bridge both to the Tauri IPC boundary (`commands`).
// This is the consumer side — capturing FROM a UVC camera — not the
// device-side `uvc` gadget pack.
//
// WIRING (manual — this overlay is additive and does not edit the base
// skeleton, matching the B-1 reuse-pack convention). To activate the
// domain in a rendered project:
//
//   1. In `src-tauri/src/lib.rs`, add `mod uvc;` and, on the Builder,
//      `.manage(uvc::commands::UvcHost::default())`.
//   2. Append the handlers to `tauri::generate_handler![ … ]`:
//        uvc::commands::uvc_enumerate,
//        uvc::commands::uvc_start_capture,
//        uvc::commands::uvc_stop_capture,
//        uvc::commands::uvc_capture_status,
//   3. Grant the four command IDs in `capabilities/default.json` (the
//      capability system rejects ungranted commands at runtime).
//
// The frontend talks to these handlers through `src/uvc.ts`.

pub mod capture;
pub mod commands;
pub mod device;

pub use capture::{
    CaptureConfig, CaptureError, CaptureLifecycle, CaptureState, Frame, PixelFormat,
};
pub use commands::{CaptureConfigDto, CaptureStatusDto, UvcHost};
pub use device::{
    fourcc, FrameSize, NullEnumerator, UvcDevice, UvcEnumerator, UvcError, UvcFormat,
};
