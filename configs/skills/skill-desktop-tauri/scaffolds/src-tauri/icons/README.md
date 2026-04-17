# Icons

Drop the following files here before running `pnpm tauri build`:

- `32x32.png`
- `128x128.png`
- `128x128@2x.png`
- `icon.icns` — macOS bundle icon
- `icon.ico` — Windows installer + EXE icon

`pnpm tauri icon path/to/source.png` regenerates the full set from a
single 1024×1024 source PNG.

The scaffold ships this directory empty so operators can drop in
their own brand assets. `tauri build` will fail until the files
above exist; that's intentional — releasing with the default Tauri
logo is a desktop-tauri role anti-pattern.
