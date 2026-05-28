// OP-1817 (Track B / B-1) — host-side UVC capture session.
//
// Step 2 of the host capability: start/stop frame capture from a device
// the enumeration layer (`device.rs`) found, and model the frames it
// emits. This is the HOST/consumer side — capturing from an attached UVC
// camera — not the device-side `uvc` gadget pack.
//
// Platform split mirrors `device.rs`: the actual frame pump is backend-
// specific (Media Foundation `IMFSourceReader` on Windows, V4L2
// `mmap`/`DQBUF` on Linux, `AVCaptureSession` on macOS). What lives here
// is the platform-agnostic, hardware-free core: the pixel-format model,
// per-frame geometry, and the capture-state machine — all unit-tested so
// a regression in the lifecycle or frame sizing fails fast with no camera.

use std::fmt;

use serde::{Deserialize, Serialize};

/// Pixel formats the scaffold understands. UVC devices expose many more;
/// these are the common host-decodable set. The FourCC mapping is the
/// canonical 4-byte code each format is advertised under.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum PixelFormat {
    /// Packed YUV 4:2:2, 2 bytes/pixel.
    Yuy2,
    /// Planar YUV 4:2:0, 12 bits/pixel (Y plane + interleaved CbCr).
    Nv12,
    /// Packed 24-bit RGB, 3 bytes/pixel.
    Rgb24,
    /// Motion-JPEG — compressed; per-frame length is variable.
    Mjpeg,
}

impl PixelFormat {
    /// Canonical FourCC for this format.
    pub fn fourcc(self) -> &'static str {
        match self {
            PixelFormat::Yuy2 => "YUY2",
            PixelFormat::Nv12 => "NV12",
            PixelFormat::Rgb24 => "RGB3",
            PixelFormat::Mjpeg => "MJPG",
        }
    }

    /// Parse a (possibly lower-case / space-padded) FourCC.
    pub fn from_fourcc(raw: &str) -> Option<Self> {
        let code = super::device::fourcc::normalize(raw)?;
        match code.as_str() {
            "YUY2" => Some(PixelFormat::Yuy2),
            "NV12" => Some(PixelFormat::Nv12),
            "RGB3" => Some(PixelFormat::Rgb24),
            "MJPG" => Some(PixelFormat::Mjpeg),
            _ => None,
        }
    }

    /// Bytes in one decoded frame of `width`×`height`, or `None` for a
    /// compressed format (MJPEG), whose frame length is data-dependent.
    /// Returns `None` on arithmetic overflow rather than panicking.
    pub fn bytes_per_frame(self, width: u32, height: u32) -> Option<usize> {
        let pixels = (width as usize).checked_mul(height as usize)?;
        match self {
            PixelFormat::Yuy2 => pixels.checked_mul(2),
            PixelFormat::Rgb24 => pixels.checked_mul(3),
            // 4:2:0 → 1.5 bytes/pixel; callers must use even dimensions.
            PixelFormat::Nv12 => pixels.checked_mul(3).map(|v| v / 2),
            PixelFormat::Mjpeg => None,
        }
    }
}

/// Requested capture parameters for a session.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CaptureConfig {
    /// Device id from the enumeration layer (`UvcDevice::id`).
    pub device_id: String,
    pub width: u32,
    pub height: u32,
    pub fps: u32,
    pub pixel_format: PixelFormat,
}

/// A single captured frame handed to the consumer.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Frame {
    pub width: u32,
    pub height: u32,
    pub pixel_format: PixelFormat,
    /// Monotonic per-session frame counter (starts at 0).
    pub sequence: u64,
    /// Capture timestamp, microseconds on the host monotonic clock.
    pub timestamp_micros: u64,
    pub bytes: Vec<u8>,
}

/// Where a session is in its lifecycle.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum CaptureState {
    /// Never started, or constructed fresh.
    Idle,
    /// Actively pumping frames.
    Running,
    /// Started then stopped — terminal until a new session is created.
    Stopped,
}

/// Capture lifecycle errors (illegal transition / sizing).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CaptureError {
    /// A start/stop that the current state does not allow.
    InvalidTransition { from: CaptureState, action: &'static str },
    /// Requested geometry the format cannot satisfy.
    InvalidConfig(String),
}

impl fmt::Display for CaptureError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            CaptureError::InvalidTransition { from, action } => {
                write!(f, "cannot {action} while capture is {from:?}")
            }
            CaptureError::InvalidConfig(msg) => write!(f, "invalid capture config: {msg}"),
        }
    }
}

impl std::error::Error for CaptureError {}

/// Pure capture-state machine. Holds no OS handle — the platform backend
/// drives the real pump and calls [`CaptureLifecycle::on_frame`] as frames
/// arrive — so the legal-transition rules are enforced and tested here
/// with no hardware.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CaptureLifecycle {
    state: CaptureState,
    config: Option<CaptureConfig>,
    frames_emitted: u64,
}

impl Default for CaptureLifecycle {
    fn default() -> Self {
        Self {
            state: CaptureState::Idle,
            config: None,
            frames_emitted: 0,
        }
    }
}

impl CaptureLifecycle {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn state(&self) -> CaptureState {
        self.state
    }

    pub fn frames_emitted(&self) -> u64 {
        self.frames_emitted
    }

    pub fn config(&self) -> Option<&CaptureConfig> {
        self.config.as_ref()
    }

    /// Begin capture. Legal only from `Idle` or `Stopped`; a session that
    /// is already `Running` is rejected so a double-start can't leak the
    /// previous backend handle. Validates the geometry for non-compressed
    /// formats up front.
    pub fn start(&mut self, config: CaptureConfig) -> Result<(), CaptureError> {
        if self.state == CaptureState::Running {
            return Err(CaptureError::InvalidTransition {
                from: self.state,
                action: "start",
            });
        }
        if config.width == 0 || config.height == 0 {
            return Err(CaptureError::InvalidConfig(
                "width and height must be non-zero".to_string(),
            ));
        }
        if config.pixel_format != PixelFormat::Mjpeg
            && config
                .pixel_format
                .bytes_per_frame(config.width, config.height)
                .is_none()
        {
            return Err(CaptureError::InvalidConfig(
                "frame size overflows usize".to_string(),
            ));
        }
        self.state = CaptureState::Running;
        self.config = Some(config);
        self.frames_emitted = 0;
        Ok(())
    }

    /// Stop capture. Legal only while `Running`.
    pub fn stop(&mut self) -> Result<(), CaptureError> {
        if self.state != CaptureState::Running {
            return Err(CaptureError::InvalidTransition {
                from: self.state,
                action: "stop",
            });
        }
        self.state = CaptureState::Stopped;
        Ok(())
    }

    /// Account for a frame the backend produced; returns its sequence
    /// number. Frames arriving while not `Running` are ignored (the
    /// backend can race the stop), so the counter never advances off-state.
    pub fn on_frame(&mut self) -> Option<u64> {
        if self.state != CaptureState::Running {
            return None;
        }
        let seq = self.frames_emitted;
        self.frames_emitted += 1;
        Some(seq)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn cfg(fmt: PixelFormat) -> CaptureConfig {
        CaptureConfig {
            device_id: "dev0".to_string(),
            width: 1280,
            height: 720,
            fps: 30,
            pixel_format: fmt,
        }
    }

    #[test]
    fn bytes_per_frame_matches_format_layout() {
        assert_eq!(PixelFormat::Yuy2.bytes_per_frame(1280, 720), Some(1280 * 720 * 2));
        assert_eq!(PixelFormat::Rgb24.bytes_per_frame(1280, 720), Some(1280 * 720 * 3));
        assert_eq!(PixelFormat::Nv12.bytes_per_frame(1280, 720), Some(1280 * 720 * 3 / 2));
        assert_eq!(PixelFormat::Mjpeg.bytes_per_frame(1280, 720), None);
    }

    #[test]
    fn pixel_format_fourcc_round_trips() {
        for fmt in [PixelFormat::Yuy2, PixelFormat::Nv12, PixelFormat::Rgb24, PixelFormat::Mjpeg] {
            assert_eq!(PixelFormat::from_fourcc(fmt.fourcc()), Some(fmt));
        }
        // Case-insensitive + space-padded inputs resolve too.
        assert_eq!(PixelFormat::from_fourcc("yuy2"), Some(PixelFormat::Yuy2));
        assert_eq!(PixelFormat::from_fourcc("zzzz"), None);
    }

    #[test]
    fn lifecycle_idle_to_running_to_stopped() {
        let mut s = CaptureLifecycle::new();
        assert_eq!(s.state(), CaptureState::Idle);
        s.start(cfg(PixelFormat::Yuy2)).unwrap();
        assert_eq!(s.state(), CaptureState::Running);
        assert_eq!(s.on_frame(), Some(0));
        assert_eq!(s.on_frame(), Some(1));
        s.stop().unwrap();
        assert_eq!(s.state(), CaptureState::Stopped);
        assert_eq!(s.frames_emitted(), 2);
    }

    #[test]
    fn double_start_is_rejected() {
        let mut s = CaptureLifecycle::new();
        s.start(cfg(PixelFormat::Nv12)).unwrap();
        let err = s.start(cfg(PixelFormat::Nv12)).unwrap_err();
        assert!(matches!(err, CaptureError::InvalidTransition { action: "start", .. }));
    }

    #[test]
    fn stop_while_idle_is_rejected() {
        let mut s = CaptureLifecycle::new();
        let err = s.stop().unwrap_err();
        assert!(matches!(err, CaptureError::InvalidTransition { action: "stop", .. }));
    }

    #[test]
    fn frames_after_stop_are_ignored() {
        let mut s = CaptureLifecycle::new();
        s.start(cfg(PixelFormat::Rgb24)).unwrap();
        s.stop().unwrap();
        assert_eq!(s.on_frame(), None);
        assert_eq!(s.frames_emitted(), 0);
    }

    #[test]
    fn zero_geometry_is_rejected() {
        let mut s = CaptureLifecycle::new();
        let mut bad = cfg(PixelFormat::Yuy2);
        bad.width = 0;
        assert!(matches!(s.start(bad), Err(CaptureError::InvalidConfig(_))));
    }
}
