// OP-1817 (Track B / B-1) — UVC host IPC commands.
//
// Bridges the enumeration (`device.rs`) and capture (`capture.rs`) layers
// to the Tauri 2.x IPC boundary. Every handler carries `#[tauri::command]`
// so the capability system grants access by name, takes typed args, and
// returns the skeleton's stable `CommandError` envelope (reused from
// `crate::commands`) so the frontend handles failures uniformly via
// `{ kind, message }` — the desktop-tauri role rule.
//
// Session state lives in [`UvcHost`], a `tauri::State`-managed value. Each
// command is a thin shell over a pure `UvcHost` method that takes `&self`,
// so the capture-session bookkeeping is unit-tested with no Tauri runtime.

use std::collections::HashMap;
use std::sync::Mutex;

use serde::{Deserialize, Serialize};

use crate::commands::CommandError;

use super::capture::{CaptureConfig, CaptureLifecycle, CaptureState, PixelFormat};
use super::device::{NullEnumerator, UvcDevice, UvcEnumerator};

// ── DTOs (frontend wire shapes) ────────────────────────────────────────

/// Capture request from the frontend. `pixel_format` is a FourCC string so
/// the TS side never has to mirror the Rust enum's discriminants.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CaptureConfigDto {
    pub device_id: String,
    pub width: u32,
    pub height: u32,
    pub fps: u32,
    pub pixel_format: String,
}

impl CaptureConfigDto {
    fn into_config(self) -> Result<CaptureConfig, CommandError> {
        let pixel_format = PixelFormat::from_fourcc(&self.pixel_format).ok_or_else(|| {
            CommandError::new(
                "invalid_argument",
                format!("unsupported pixel format {:?}", self.pixel_format),
            )
        })?;
        Ok(CaptureConfig {
            device_id: self.device_id,
            width: self.width,
            height: self.height,
            fps: self.fps,
            pixel_format,
        })
    }
}

/// Current state of a device's capture session, returned to the frontend.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CaptureStatusDto {
    pub device_id: String,
    pub state: String,
    pub frames_emitted: u64,
}

fn state_label(state: CaptureState) -> &'static str {
    match state {
        CaptureState::Idle => "idle",
        CaptureState::Running => "running",
        CaptureState::Stopped => "stopped",
    }
}

// ── Managed host state ─────────────────────────────────────────────────

/// Capture sessions keyed by device id, behind a `Mutex` so the IPC
/// handlers (which Tauri may dispatch from multiple threads) serialise
/// access. Register on the builder with `.manage(UvcHost::default())`.
#[derive(Default)]
pub struct UvcHost {
    sessions: Mutex<HashMap<String, CaptureLifecycle>>,
}

impl UvcHost {
    fn lock(&self) -> Result<std::sync::MutexGuard<'_, HashMap<String, CaptureLifecycle>>, CommandError> {
        self.sessions
            .lock()
            .map_err(|_| CommandError::new("poisoned", "uvc session lock poisoned"))
    }

    /// Start (or restart) capture for `config.device_id`.
    pub fn start(&self, config: CaptureConfig) -> Result<CaptureStatusDto, CommandError> {
        let mut sessions = self.lock()?;
        let device_id = config.device_id.clone();
        let session = sessions.entry(device_id.clone()).or_default();
        session
            .start(config)
            .map_err(|e| CommandError::new("capture_transition", e.to_string()))?;
        Ok(CaptureStatusDto {
            device_id,
            state: state_label(session.state()).to_string(),
            frames_emitted: session.frames_emitted(),
        })
    }

    /// Stop capture for `device_id`.
    pub fn stop(&self, device_id: &str) -> Result<CaptureStatusDto, CommandError> {
        let mut sessions = self.lock()?;
        let session = sessions.get_mut(device_id).ok_or_else(|| {
            CommandError::new("not_found", format!("no capture session for {device_id:?}"))
        })?;
        session
            .stop()
            .map_err(|e| CommandError::new("capture_transition", e.to_string()))?;
        Ok(CaptureStatusDto {
            device_id: device_id.to_string(),
            state: state_label(session.state()).to_string(),
            frames_emitted: session.frames_emitted(),
        })
    }

    /// Report a device's session state — `idle` for an unknown device.
    pub fn status(&self, device_id: &str) -> Result<CaptureStatusDto, CommandError> {
        let sessions = self.lock()?;
        let (state, frames) = sessions
            .get(device_id)
            .map(|s| (s.state(), s.frames_emitted()))
            .unwrap_or((CaptureState::Idle, 0));
        Ok(CaptureStatusDto {
            device_id: device_id.to_string(),
            state: state_label(state).to_string(),
            frames_emitted: frames,
        })
    }
}

// ── IPC commands ───────────────────────────────────────────────────────

/// List attached UVC capture devices. The scaffold binds the
/// [`NullEnumerator`] (empty list); a follow-on swaps in the Media
/// Foundation backend behind the same [`UvcEnumerator`] trait.
#[tauri::command]
pub fn uvc_enumerate() -> Result<Vec<UvcDevice>, CommandError> {
    let enumerator = NullEnumerator;
    enumerator
        .enumerate()
        .map_err(|e| CommandError::new("enumerate_failed", e.to_string()))
}

#[tauri::command]
pub fn uvc_start_capture(
    host: tauri::State<'_, UvcHost>,
    config: CaptureConfigDto,
) -> Result<CaptureStatusDto, CommandError> {
    host.start(config.into_config()?)
}

#[tauri::command]
pub fn uvc_stop_capture(
    host: tauri::State<'_, UvcHost>,
    device_id: String,
) -> Result<CaptureStatusDto, CommandError> {
    host.stop(&device_id)
}

#[tauri::command]
pub fn uvc_capture_status(
    host: tauri::State<'_, UvcHost>,
    device_id: String,
) -> Result<CaptureStatusDto, CommandError> {
    host.status(&device_id)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn dto(device_id: &str) -> CaptureConfigDto {
        CaptureConfigDto {
            device_id: device_id.to_string(),
            width: 640,
            height: 480,
            fps: 30,
            pixel_format: "yuy2".to_string(),
        }
    }

    #[test]
    fn dto_parses_fourcc_into_enum() {
        let cfg = dto("dev0").into_config().unwrap();
        assert_eq!(cfg.pixel_format, PixelFormat::Yuy2);
    }

    #[test]
    fn dto_rejects_unknown_fourcc() {
        let mut bad = dto("dev0");
        bad.pixel_format = "ZZZZ".to_string();
        let err = bad.into_config().unwrap_err();
        assert_eq!(err.kind, "invalid_argument");
    }

    #[test]
    fn host_start_then_stop_tracks_state() {
        let host = UvcHost::default();
        let started = host.start(dto("dev0").into_config().unwrap()).unwrap();
        assert_eq!(started.state, "running");

        let stopped = host.stop("dev0").unwrap();
        assert_eq!(stopped.state, "stopped");
    }

    #[test]
    fn host_status_of_unknown_device_is_idle() {
        let host = UvcHost::default();
        let status = host.status("ghost").unwrap();
        assert_eq!(status.state, "idle");
        assert_eq!(status.frames_emitted, 0);
    }

    #[test]
    fn host_stop_unknown_device_is_not_found() {
        let host = UvcHost::default();
        let err = host.stop("ghost").unwrap_err();
        assert_eq!(err.kind, "not_found");
    }

    #[test]
    fn host_double_start_surfaces_transition_error() {
        let host = UvcHost::default();
        host.start(dto("dev0").into_config().unwrap()).unwrap();
        let err = host.start(dto("dev0").into_config().unwrap()).unwrap_err();
        assert_eq!(err.kind, "capture_transition");
    }
}
