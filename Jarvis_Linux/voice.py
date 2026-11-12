"""Optional voice input (STT) and output (TTS), toggled independently.

Output backends (tried in this order for "auto"):
    piper (if voice.piper_model is set) -> espeak-ng -> espeak -> spd-say -> pyttsx3
Input backends (voice.stt_backend), all via SpeechRecognition + PyAudio for the mic:
    google  - free web recogniser, needs internet
    vosk    - offline, needs `pip install vosk` and a model folder (voice.vosk_model_path)
    whisper - offline, needs `pip install faster-whisper`

Nothing here is imported at module load except the standard library, so Jarvis
runs text-only on a machine with none of these installed.
"""
from __future__ import annotations

import io
import json
import logging
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time

from core import fail, ok

log = logging.getLogger("jarvis.voice")

TTS_ORDER = ("piper", "espeak-ng", "espeak", "spd-say", "pyttsx3")
PLAYERS = (["pw-play"], ["paplay"], ["aplay", "-q"])


def clean_for_speech(text: str, limit: int = 400) -> str:
    text = re.sub(r"[`*_#>|]", " ", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


class Voice:
    def __init__(self, settings, on_command=None, on_heard=None):
        self.settings = settings
        self.on_command = on_command          # called with command text from the mic
        self.on_heard = on_heard              # optional: raw transcript echo
        self.output_enabled = False
        self.input_enabled = False
        self._tts_backend: str | None = None
        self._q: queue.Queue = queue.Queue()
        self._tts_thread: threading.Thread | None = None
        self._proc: subprocess.Popen | None = None
        self._pyttsx = None
        self._speaking = threading.Event()
        self._speech_epoch = 0                # bumps whenever speech starts/ends (echo guard)
        self._listen_thread: threading.Thread | None = None
        self._stop_listen = threading.Event()
        self._armed_until = 0.0
        self._stt_model = None
        self._net_error_logged = False
        self.last_error: str | None = None

    # ── output ────────────────────────────────────────────────────────────
    def detect_tts(self) -> str | None:
        want = str(self.settings.get("voice.tts_backend") or "auto").lower()
        order = TTS_ORDER if want == "auto" else (want,)
        for name in order:
            if name == "piper":
                model = os.path.expanduser(str(self.settings.get("voice.piper_model") or ""))
                binary = str(self.settings.get("voice.piper_binary") or "piper")
                if model and os.path.isfile(model) and shutil.which(binary) and self._player():
                    return name
            elif name == "pyttsx3":
                try:
                    import pyttsx3  # noqa: F401
                    return name
                except Exception:
                    continue
            elif shutil.which(name):
                return name
        return None

    @staticmethod
    def _player() -> list[str] | None:
        for p in PLAYERS:
            if shutil.which(p[0]):
                return p
        return None

    def set_output(self, enabled: bool) -> dict:
        if not enabled:
            self.output_enabled = False
            self.stop_speaking()
            return ok("Voice output off.")
        backend = self.detect_tts()
        if not backend:
            self.output_enabled = False
            return fail("No text-to-speech engine found. Install one: sudo apt install espeak-ng",
                        error="no tts backend")
        self._tts_backend = backend
        self.output_enabled = True
        if not (self._tts_thread and self._tts_thread.is_alive()):
            self._tts_thread = threading.Thread(target=self._tts_worker, name="jarvis-tts", daemon=True)
            try:
                self._tts_thread.start()
            except RuntimeError as e:
                self.output_enabled = False
                self.last_error = str(e)
                return fail("Couldn't start voice output.", error=str(e))
        return ok(f"Voice output on ({backend}).")

    def say(self, text: str) -> None:
        if self.output_enabled and text:
            self._q.put(clean_for_speech(text))

    def is_speaking(self) -> bool:
        return self._speaking.is_set()

    def stop_speaking(self) -> None:
        self._speech_epoch += 1
        while not self._q.empty():
            try:
                self._q.get_nowait()
            except queue.Empty:
                break
        proc = self._proc
        try:
            if proc and proc.poll() is None:
                proc.terminate()
        except OSError:
            pass                              # process exited between poll and terminate
        if self._pyttsx is not None:
            try:
                self._pyttsx.stop()
            except Exception:
                log.exception("couldn't stop pyttsx3 output")

    def _tts_worker(self) -> None:
        while True:
            text = self._q.get()
            if text is None:
                return
            if not self.output_enabled:
                continue
            self._speaking.set()
            self._speech_epoch += 1
            try:
                self._speak_now(text)
            except Exception:
                log.exception("TTS failed")
                self.last_error = "Speech output failed; check the Jarvis log."
            finally:
                self._speaking.clear()
                self._speech_epoch += 1

    def _run_proc(self, args: list[str], stdin_text: str | None = None, timeout: float = 60) -> int | None:
        proc = None
        try:
            proc = subprocess.Popen(args, stdin=subprocess.PIPE if stdin_text else subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True)
            self._proc = proc
            proc.communicate(stdin_text, timeout=timeout)
            if proc.returncode:
                self.last_error = f"{args[0]} exited with code {proc.returncode}"
            return proc.returncode
        except subprocess.TimeoutExpired:
            self.last_error = f"{args[0]} timed out"
            try:
                proc.kill()
                proc.communicate(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                log.warning("couldn't finish stopping TTS process %s", args[0])
            return None
        except OSError as e:
            log.warning("TTS process %s failed: %s", args[0], e)
            self.last_error = str(e)
            return None
        finally:
            self._proc = None

    def _speak_now(self, text: str) -> None:
        epoch = self._speech_epoch
        b = self._tts_backend
        rate = int(self.settings.get("voice.tts_rate") or 170)
        vname = str(self.settings.get("voice.tts_voice") or "en")
        if b in ("espeak-ng", "espeak"):
            self._run_proc([b, "-s", str(rate), "-v", vname, "--", text])
        elif b == "spd-say":
            rel = max(-100, min(100, int((rate - 170) / 1.5)))    # spd-say uses -100..100
            self._run_proc(["spd-say", "-w", "-r", str(rel), "--", text])
        elif b == "piper":
            model = os.path.expanduser(str(self.settings.get("voice.piper_model")))
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                wav = tmp.name
            try:
                if self._run_proc([str(self.settings.get("voice.piper_binary") or "piper"), "--model", model,
                                   "--output_file", wav], stdin_text=text) == 0:
                    player = self._player()
                    if player and epoch == self._speech_epoch:
                        self._run_proc(player + [wav])
            finally:
                try:
                    os.unlink(wav)
                except OSError:
                    pass
        elif b == "pyttsx3":
            import pyttsx3
            if self._pyttsx is None:
                self._pyttsx = pyttsx3.init()
            self._pyttsx.setProperty("rate", rate)
            self._pyttsx.say(text)
            self._pyttsx.runAndWait()

    # ── input ─────────────────────────────────────────────────────────────
    def detect_stt(self) -> tuple[bool, str]:
        backend = str(self.settings.get("voice.stt_backend") or "google").lower()
        if backend not in ("google", "vosk", "whisper"):
            return False, f"unknown stt_backend '{backend}'"
        try:
            import speech_recognition  # noqa: F401
        except (ImportError, OSError):
            return False, "SpeechRecognition isn't installed (pip install -r requirements-voice.txt)"
        try:
            import pyaudio  # noqa: F401
        except (ImportError, OSError):
            return False, "PyAudio isn't installed (sudo apt install portaudio19-dev; pip install PyAudio)"
        if backend == "vosk":
            try:
                import vosk  # noqa: F401
            except (ImportError, OSError):
                return False, "vosk isn't installed (pip install vosk)"
            path = os.path.expanduser(str(self.settings.get("voice.vosk_model_path") or ""))
            if not os.path.isdir(path):
                return False, "voice.vosk_model_path doesn't point to a Vosk model folder"
        elif backend == "whisper":
            try:
                import faster_whisper  # noqa: F401
            except (ImportError, OSError):
                return False, "faster-whisper isn't installed (pip install faster-whisper)"
        return True, backend

    def set_input(self, enabled: bool) -> dict:
        if not enabled:
            self.input_enabled = False
            self._stop_listen.set()
            self._armed_until = 0.0
            return ok("Voice input off.")
        if self._listen_thread and self._listen_thread.is_alive():
            if self._stop_listen.is_set():
                return fail("The microphone is still stopping; try again shortly.", error="stopping")
            return ok("Already listening.")
        good, info = self.detect_stt()
        if not good:
            self.last_error = info
            return fail(f"Voice input unavailable: {info}.", error=info)
        if self.settings.get("voice.require_wake_word") and not str(self.settings.get("voice.wake_word") or "").strip():
            return fail("Set a nonempty wake word before enabling voice input.", error="empty wake word")
        self.last_error = None
        self._stt_model = None
        self._net_error_logged = False
        self._armed_until = 0.0
        self._stop_listen.clear()
        self.input_enabled = True
        self._listen_thread = threading.Thread(target=self._listen_loop, name="jarvis-stt", daemon=True)
        try:
            self._listen_thread.start()
        except RuntimeError as e:
            self.input_enabled = False
            self.last_error = str(e)
            return fail("Couldn't start voice input.", error=str(e))
        ww = self.settings.get("voice.wake_word")
        hint = f" Start commands with '{ww}'." if self.settings.get("voice.require_wake_word") else ""
        return ok(f"Voice input on ({info}).{hint}")

    def extract_command(self, heard: str, now: float | None = None) -> str | None:
        """Apply wake-word rules to a transcript. Returns the command or None."""
        now = time.monotonic() if now is None else now
        text = (heard or "").strip()
        if not text:
            return None
        if not self.settings.get("voice.require_wake_word"):
            return text
        wake_word = str(self.settings.get("voice.wake_word") or "").strip()
        if not wake_word:
            return None
        ww = re.escape(wake_word)
        m = re.match(rf"^(?:hey|ok|okay)?[\s,]*{ww}\b[\s,.:!?]*(.*)$", text, re.I)
        if not m:
            if now < self._armed_until:
                self._armed_until = 0.0
                return text
            return None
        rest = m.group(1).strip()
        if not rest:
            self._armed_until = now + 8
            self.say("Yes?")
            return None
        self._armed_until = 0.0
        return rest

    def _listen_loop(self) -> None:
        try:
            import speech_recognition as sr
            r = sr.Recognizer()
            r.dynamic_energy_threshold = True
            r.operation_timeout = 10
            idx = int(self.settings.get("voice.mic_device_index", -1))
            mic = sr.Microphone(device_index=None if idx < 0 else idx)
            with mic as source:
                r.adjust_for_ambient_noise(source, duration=1)
                log.info("listening (energy threshold %.0f)", r.energy_threshold)
                while not self._stop_listen.is_set():
                    epoch = self._speech_epoch
                    if self._speaking.is_set():
                        time.sleep(0.1)
                        continue
                    try:
                        audio = r.listen(source, timeout=1,
                                         phrase_time_limit=int(self.settings.get("voice.phrase_time_limit") or 8))
                    except sr.WaitTimeoutError:
                        continue
                    if self._stop_listen.is_set() or self._speech_epoch != epoch or self._speaking.is_set():
                        continue                      # we heard ourselves
                    text = self._transcribe(r, audio)
                    if not text or self._stop_listen.is_set() or self._speech_epoch != epoch or self._speaking.is_set():
                        continue
                    log.info("heard: %r", text)
                    if self.on_heard:
                        self.on_heard(text)
                    cmd = self.extract_command(text)
                    if cmd and self.on_command:
                        try:
                            self.on_command(cmd)
                        except Exception:
                            log.exception("voice command dispatch failed")
        except Exception as e:
            log.exception("microphone loop stopped")
            self.last_error = str(e)
            if self.on_heard:
                self.on_heard(f"[voice input stopped: {e}]")
        finally:
            self.input_enabled = False

    def _transcribe(self, r, audio) -> str | None:
        import speech_recognition as sr
        backend = str(self.settings.get("voice.stt_backend") or "google").lower()
        try:
            if backend == "google":
                text = r.recognize_google(audio, language=str(self.settings.get("voice.language") or "en-US"))
                self._net_error_logged = False
                self.last_error = None
                return text
            if backend == "vosk":
                from vosk import KaldiRecognizer, Model
                if self._stt_model is None:
                    self._stt_model = Model(os.path.expanduser(str(self.settings.get("voice.vosk_model_path"))))
                rec = KaldiRecognizer(self._stt_model, 16000)
                rec.AcceptWaveform(audio.get_raw_data(convert_rate=16000, convert_width=2))
                return json.loads(rec.FinalResult()).get("text") or None
            if backend == "whisper":
                from faster_whisper import WhisperModel
                if self._stt_model is None:
                    self._stt_model = WhisperModel(str(self.settings.get("voice.whisper_model") or "base.en"),
                                                   device="cpu", compute_type="int8")
                segments, _ = self._stt_model.transcribe(io.BytesIO(audio.get_wav_data()), beam_size=1)
                return " ".join(s.text for s in segments).strip() or None
        except sr.UnknownValueError:
            return None
        except sr.RequestError as e:
            self.last_error = f"Speech service unavailable: {e}"
            if not self._net_error_logged:
                log.warning("speech service unreachable: %s", e)
                if self.on_heard:
                    self.on_heard(self.last_error)
                self._net_error_logged = True
            return None
        except Exception as e:
            self.last_error = f"Transcription failed ({backend}): {e}"
            log.exception("transcription failed (%s)", backend)
        return None

    # ── lifecycle ─────────────────────────────────────────────────────────
    def status(self) -> dict:
        good, info = self.detect_stt()
        return {"output": self.output_enabled, "tts": self._tts_backend or self.detect_tts(),
                "input": self.input_enabled, "stt": info if good else f"unavailable: {info}",
                "error": self.last_error}

    def shutdown(self) -> None:
        self.set_input(False)
        self.output_enabled = False
        self.stop_speaking()
        if self._tts_thread and self._tts_thread.is_alive():
            self._q.put(None)
        for thread in (self._listen_thread, self._tts_thread):
            if thread and thread.is_alive() and thread is not threading.current_thread():
                thread.join(timeout=2)
