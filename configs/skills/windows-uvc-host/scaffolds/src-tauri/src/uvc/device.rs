// OP-1817 (Track B / B-1) — host-side UVC device enumeration.
//
// Step 1 of the host capability: list the USB Video Class capture devices
// attached to the machine and the pixel formats / frame sizes each one
// advertises, so the Tauri frontend can present a camera + format picker
// before a capture session starts.
//
// Platform split: the device walk is backend-specific — Media Foundation
// `IMFActivate` enumeration on Windows, `AVCaptureDevice` on macOS, V4L2
// `VIDIOC_ENUM_FMT`/`ENUM_FRAMESIZES` on Linux. That lives behind the
// [`UvcEnumerator`] trait so the IPC layer and the pure metadata model
// below stay platform-agnostic and unit-testable with no hardware. The
// scaffold ships a [`NullEnumerator`] (returns an empty list) as the
// default binding; a follow-on wires the real Media Foundation backend.

use std::fmt;

use serde::{Deserialize, Serialize};

/// FourCC helpers — a pixel format is identified on the wire by a 4-byte
/// code (e.g. `YUY2`, `NV12`, `MJPG`). We keep formats as `String` in the
/// serializable model and normalise through these pure helpers so the
/// validation logic unit-tests without a device.
pub mod fourcc {
    /// Normalise a raw FourCC to its canonical 4-byte, upper-cased form.
    ///
    /// FourCCs are exactly four bytes; shorter codes are right-padded with
    /// spaces (`Y8` → `"Y8  "`). Returns `None` for an empty code, one
    /// longer than four bytes, or one carrying a non-ASCII-alphanumeric,
    /// non-space byte.
    pub fn normalize(raw: &str) -> Option<String> {
        let trimmed = raw.trim_end();
        if trimmed.is_empty() || trimmed.len() > 4 {
            return None;
        }
        if !trimmed
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b' ')
        {
            return None;
        }
        Some(format!("{:<4}", trimmed.to_ascii_uppercase()))
    }

    /// Whether `raw` is a syntactically valid FourCC.
    pub fn is_valid(raw: &str) -> bool {
        normalize(raw).is_some()
    }
}

/// A single frame geometry a format supports.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct FrameSize {
    pub width: u32,
    pub height: u32,
}

/// One pixel format a device advertises, with the geometries and frame
/// rates it offers for that format.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct UvcFormat {
    /// Canonical FourCC (see [`fourcc::normalize`]).
    pub fourcc: String,
    pub sizes: Vec<FrameSize>,
    /// Frame rates (fps) the device reports for this format.
    pub frame_rates: Vec<u32>,
}

/// An enumerated UVC capture device + the formats it exposes.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct UvcDevice {
    /// Stable, backend-assigned identifier used by the capture commands
    /// to address this device (Media Foundation symbolic link, V4L2 node,
    /// AVFoundation uniqueID).
    pub id: String,
    pub name: String,
    pub vendor_id: u16,
    pub product_id: u16,
    pub formats: Vec<UvcFormat>,
}

/// Failure modes the enumeration layer reports.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum UvcError {
    /// The platform capture backend is not available / not built in.
    Unsupported(String),
    /// The OS device walk failed (permission, transient driver error).
    Backend(String),
}

impl fmt::Display for UvcError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            UvcError::Unsupported(msg) => write!(f, "uvc backend unsupported: {msg}"),
            UvcError::Backend(msg) => write!(f, "uvc backend error: {msg}"),
        }
    }
}

impl std::error::Error for UvcError {}

/// Platform-agnostic enumeration boundary. The real implementations
/// (Media Foundation / V4L2 / AVFoundation) implement this; the IPC
/// command layer holds it as a trait object so swapping the backend never
/// touches the command surface.
pub trait UvcEnumerator: Send + Sync {
    fn enumerate(&self) -> Result<Vec<UvcDevice>, UvcError>;
}

/// Scaffold default: a backend that finds no devices and never errors, so
/// a freshly rendered project boots and the enumerate IPC returns an empty
/// list rather than failing. A follow-on replaces this with the Media
/// Foundation enumerator behind the same trait.
#[derive(Debug, Default, Clone, Copy)]
pub struct NullEnumerator;

impl UvcEnumerator for NullEnumerator {
    fn enumerate(&self) -> Result<Vec<UvcDevice>, UvcError> {
        Ok(Vec::new())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fourcc_normalizes_case_and_pads() {
        assert_eq!(fourcc::normalize("yuy2").as_deref(), Some("YUY2"));
        assert_eq!(fourcc::normalize("Y8").as_deref(), Some("Y8  "));
        assert_eq!(fourcc::normalize("mjpg").as_deref(), Some("MJPG"));
    }

    #[test]
    fn fourcc_rejects_empty_and_overlong() {
        assert_eq!(fourcc::normalize(""), None);
        assert_eq!(fourcc::normalize("    "), None);
        assert_eq!(fourcc::normalize("TOOLONG"), None);
        assert!(!fourcc::is_valid("y/u2"));
        assert!(fourcc::is_valid("NV12"));
    }

    #[test]
    fn null_enumerator_returns_empty_list() {
        assert_eq!(NullEnumerator.enumerate().unwrap(), Vec::new());
    }

    #[test]
    fn device_round_trips_through_serde() {
        let dev = UvcDevice {
            id: "\\\\?\\usb#vid_046d".to_string(),
            name: "Test Camera".to_string(),
            vendor_id: 0x046d,
            product_id: 0x0825,
            formats: vec![UvcFormat {
                fourcc: "YUY2".to_string(),
                sizes: vec![FrameSize { width: 1280, height: 720 }],
                frame_rates: vec![30, 60],
            }],
        };
        let json = serde_json::to_string(&dev).unwrap();
        let back: UvcDevice = serde_json::from_str(&json).unwrap();
        assert_eq!(dev, back);
    }
}
