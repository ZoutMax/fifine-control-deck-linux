#!/usr/bin/env python3
"""Dictation server: holds the Whisper model warm and transcribes on demand.

Listens on a unix socket; protocol: one line with a wav path in, one line of
transcript out (empty line on failure). Started lazily by fifine-dictate."""
import glob
import os
import socket
import sys

# ctranslate2 dlopens cuBLAS/cuDNN; the loader only honors LD_LIBRARY_PATH
# from process start, so re-exec once with the venv's nvidia libs on the path.
if "FIFINE_DICT_REEXEC" not in os.environ:
    here = os.path.dirname(os.path.abspath(__file__))
    libs = glob.glob(os.path.join(here, "venv/lib/python*/site-packages/nvidia/*/lib"))
    if libs:
        cur = os.environ.get("LD_LIBRARY_PATH", "")
        os.environ["LD_LIBRARY_PATH"] = ":".join(libs + ([cur] if cur else []))
    os.environ["FIFINE_DICT_REEXEC"] = "1"
    os.execv(sys.executable, [sys.executable] + sys.argv)

SOCK = "/tmp/fifine-dictate.sock"

def main():
    from faster_whisper import WhisperModel
    try:
        model = WhisperModel("small", device="cuda", compute_type="float16")
    except Exception:
        model = WhisperModel("small", device="cpu", compute_type="int8")
    try:
        os.unlink(SOCK)
    except FileNotFoundError:
        pass
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(SOCK)
    os.chmod(SOCK, 0o600)
    srv.listen(1)
    print("ready", flush=True)
    while True:
        conn, _ = srv.accept()
        try:
            path = conn.makefile().readline().strip()
            text = ""
            if path and os.path.exists(path):
                segments, _info = model.transcribe(
                    path, vad_filter=True,
                    # bias the vocabulary toward the project's proper nouns
                    initial_prompt="Reddit, GitHub, Flathub, snap, PPA, "
                                   "fifine, AmpliGame, Linux, Claude, deck")
                text = " ".join(s.text.strip() for s in segments).strip()
            conn.sendall((text + "\n").encode())
        except Exception as e:
            try:
                conn.sendall(b"\n")
            except Exception:
                pass
            print(f"error: {e}", file=sys.stderr, flush=True)
        finally:
            conn.close()

if __name__ == "__main__":
    main()
