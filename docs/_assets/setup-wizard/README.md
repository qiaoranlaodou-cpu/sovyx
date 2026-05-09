# Setup wizard screenshots

This directory holds the visual aids referenced from
[`docs/getting-started.md`](../../getting-started.md). Each screenshot has a
fixed filename so the markdown image refs resolve automatically once the
PNG lands here — no docs edit needed at capture time.

## Capture checklist

Run a fresh setup against real hardware (`sovyx start` in a clean data
dir, then open the dashboard at `http://localhost:7777`) and capture the
following frames. Save each as PNG with the exact filename shown.

| # | Filename                              | What it shows                                                                                |
|---|---------------------------------------|----------------------------------------------------------------------------------------------|
| 1 | `01-plugin-card-configure.png`        | Plugin card on the Plugins page with the **Configure** button visible.                       |
| 2 | `02-wizard-provider-selector.png`     | Plugin/provider wizard modal with the provider selector dropdown open.                       |
| 3 | `03-form-field-types.png`             | Wizard form showing the dynamic field types (text input, password, dropdown, toggle).        |
| 4 | `04-test-connection-success.png`      | "Test connection" result panel in success state (green check, latency reading).              |
| 5 | `05-test-connection-error.png`        | "Test connection" result panel in error state (red badge, structured error message).         |
| 6 | `06-voice-hardware-detection.png`     | Voice setup wizard with hardware detection: enumerated mics + diagnosis hints.               |
| 7 | `07-missing-deps-panel.png`           | Missing-dependencies banner (e.g. `pip install moonshine-voice`, `apt install libportaudio2`). |
| 8 | `08-audio-error-panel.png`            | Audio error panel (mic not reachable, APO interference, capture inoperative, etc.).          |

## Recommended capture flow

1. **Start clean.** Use a throwaway data dir so plugins are unconfigured:
   ```bash
   sovyx --data-dir /tmp/sovyx-screenshots start
   ```
2. **Configure** flow → screenshots 1-5: open Plugins page, pick any
   provider plugin, click Configure, walk the modal, run Test Connection
   in both success and error states (e.g. paste a bad API key).
3. **Voice** flow → screenshots 6-8: trigger the voice setup wizard.
   For #7 + #8, temporarily uninstall `moonshine-voice` /
   `sounddevice` to force the missing-deps + audio-error states.

## Style notes

- 1280×800 viewport recommended (matches typical desktop rendering).
- Light or dark theme — pick one and stay consistent across all 8 frames.
- Crop tight to the card / modal — no full browser chrome, no taskbar.
- PNG, 8-bit, lossless. Compress with `oxipng -o4` before commit if size
  matters.

## When you're done

Drop the PNGs in this directory and the existing image references in
`docs/getting-started.md` (`![Plugin card with Configure button](_assets/setup-wizard/01-plugin-card-configure.png)`
etc.) will start rendering. No further docs edits required.
