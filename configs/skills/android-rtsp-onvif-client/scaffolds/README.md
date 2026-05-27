# scaffolds/ — intentionally empty in the OP-1799 skeleton

This Case-1 pack ships **no scaffolder or templates of its own**. The
dispatcher (`scripts/scaffold.py`) routes `android-rtsp-onvif-client` to
the existing `backend.android_scaffolder` via the explicit override in
`backend/skill_registry.py` (`_SCAFFOLDER_MODULE_OVERRIDES`). Rendering
this pack therefore emits the standard Android app skeleton from
`configs/skills/skill-android/scaffolds/`.

The ONVIF-discovery and RTSP-playback **client** scaffolds (Compose
player screen, ONVIF WS-Discovery probe, Media3/ExoPlayer RTSP source)
are sequenced **follow-on** tickets in this same pack and will add their
templates here. This directory exists so the `scaffolds` artifact
declared in `skill.yaml` validates (`skill_manifest`).
