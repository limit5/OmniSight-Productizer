# scaffolds/ - OP-1815 skeleton

This Case-2 pack ships no scaffolder or templates of its own. The
dispatcher routes `windows-uvc-host` to the existing
`backend.tauri_scaffolder` via the explicit override in
`backend/skill_registry.py`.

UVC host enumeration, capture, and preview templates land with sequenced
follow-on tickets in this same pack. This directory exists so the
`scaffolds` artifact declared in `skill.yaml` validates.
