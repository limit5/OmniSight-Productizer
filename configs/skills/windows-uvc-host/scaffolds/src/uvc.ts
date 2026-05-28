// OP-1817 (Track B / B-1) — UVC host frontend client.
//
// Typed wrappers over the `uvc_*` #[tauri::command] handlers in
// `src-tauri/src/uvc/commands.rs`. Reuses the skeleton's `call<T>` helper
// (`useTauri.ts`) so failures arrive as the stable `{ kind, message }`
// CommandError shape the rest of the frontend already handles. Both the
// React and Vue variants import this module.

import { call } from "./useTauri";

/** One frame geometry a device supports for a given format. */
export type FrameSize = {
  width: number;
  height: number;
};

/** A pixel format a device advertises, with its geometries + frame rates. */
export type UvcFormat = {
  /** Canonical FourCC, e.g. "YUY2", "NV12", "MJPG". */
  fourcc: string;
  sizes: FrameSize[];
  frameRates: number[];
};

/** An enumerated UVC capture device. */
export type UvcDevice = {
  id: string;
  name: string;
  vendorId: number;
  productId: number;
  formats: UvcFormat[];
};

/** Capture parameters sent when starting a session. */
export type CaptureConfig = {
  deviceId: string;
  width: number;
  height: number;
  fps: number;
  /** FourCC of the requested pixel format (e.g. "YUY2"). */
  pixelFormat: string;
};

export type CaptureState = "idle" | "running" | "stopped";

export type CaptureStatus = {
  deviceId: string;
  state: CaptureState;
  framesEmitted: number;
};

/** List attached UVC capture devices. */
export const enumerateDevices = () => call<UvcDevice[]>("uvc_enumerate");

/** Start capture for the given config; resolves with the new session status. */
export const startCapture = (config: CaptureConfig) =>
  call<CaptureStatus>("uvc_start_capture", { config });

/** Stop capture for a device. */
export const stopCapture = (deviceId: string) =>
  call<CaptureStatus>("uvc_stop_capture", { deviceId });

/** Query a device's current capture-session status. */
export const captureStatus = (deviceId: string) =>
  call<CaptureStatus>("uvc_capture_status", { deviceId });
