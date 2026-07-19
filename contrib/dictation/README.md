# Voice dictation on a deck key (community recipe)

Press a key on your deck, speak (English or French — Whisper auto-detects),
press again: your words are **typed into whatever window has focus**. Runs
100% locally — Whisper on your GPU (or CPU), audio never leaves your machine.

This is a recipe, not an app feature: it composes the app's ordinary
**Run shell command** action with two small scripts. Set it up once, then
bind it to any key.

## Prerequisites

- fifine Control Deck (any version with *Run shell command* — 0.5+)
- Wayland with a running `ydotoold` (the app's own *Type text* action uses
  the same mechanism — if that works, you're set). X11 users: replace the
  `ydotool` line in `fifine-dictate` with `xdotool type --file -`.
- PulseAudio/PipeWire (`parecord`), `speech-dispatcher` (`spd-say`) for the
  audio cues, a microphone, and Python 3.10+.
- Optional but recommended: an NVIDIA GPU (transcription in ~0.2 s once warm).

## Install (one time, ~600 MB download)

```bash
mkdir -p ~/.local/share/fifine-dictation ~/.local/bin
cp server.py ~/.local/share/fifine-dictation/
cp fifine-dictate ~/.local/bin/ && chmod +x ~/.local/bin/fifine-dictate

cd ~/.local/share/fifine-dictation
python3 -m venv venv
./venv/bin/pip install faster-whisper
# NVIDIA GPU only — the CUDA runtime libraries Whisper needs:
./venv/bin/pip install nvidia-cublas-cu12 nvidia-cudnn-cu12
```

The Whisper model (`small`, multilingual) downloads automatically on first
use. No GPU? It falls back to CPU automatically (a few seconds per phrase
instead of a fraction of one).

## Bind it to a key

1. Open fifine Control Deck.
2. Drag **Run shell command** onto a key.
3. Command: `~/.local/bin/fifine-dictate` (use the absolute path).
4. Pick the **mic** icon from the Library.

Press → "listening" → talk → press → your words type themselves and Enter is
pressed (auto-send). Use `fifine-dictate --no-enter` as the command instead
if you dictate into documents where a newline would be unwanted.

## Tuning

- **Vocabulary**: edit `initial_prompt` in `server.py` — listing your own
  jargon and proper nouns there dramatically improves their recognition.
- **Languages**: auto-detected per utterance. Tested with English and French.
- **Model**: swap `"small"` in `server.py` for `"medium"` (better, slower)
  or `"base"` (faster, rougher).
- The first transcription after boot takes a few seconds (model load); the
  server then stays warm (~1 GB VRAM) and answers in ~0.2 s.

## Troubleshooting

- Log: `/tmp/fifine-dictate-server.log`
- Wrong microphone? `pactl list sources short`, then
  `pactl set-default-source <name>`.
- Stop the warm server:
  `for p in $(pgrep -f "venv/bin/python serve[r]"); do kill $p; done`
- `libcublas`/`libcudnn` errors are handled automatically (the server
  re-executes itself with the venv's NVIDIA libraries on the loader path) —
  if you still see them, re-run the two `nvidia-*` pip installs above.

## Privacy

Everything is local: recording, transcription, typing. The temporary wav in
`/tmp` is overwritten on each use and never uploaded anywhere.
