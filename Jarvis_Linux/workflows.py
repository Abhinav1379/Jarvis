"""Local reminder and routine state; no desktop or voice dependencies."""
from __future__ import annotations

import json
import math
import os
import re
import tempfile
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from core import fail, ok


class StateFile:
    def __init__(self, path: Path, default, validate):
        self.path = path
        self.error = None
        self.data = default
        self._lease = None
        self._lock = threading.RLock()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._lease = open(path.with_suffix(".lock"), "a+b")
            if os.name == "nt":  # Allows the fake-platform suite to run on Windows too.
                import msvcrt
                if self._lease.seek(0, os.SEEK_END) == 0:
                    self._lease.write(b"\0")
                    self._lease.flush()
                self._lease.seek(0)
                msvcrt.locking(self._lease.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, ValueError) as e:
            self.close()
            self.error = f"Cannot lock {path}: {e}. Check permissions and close other Jarvis instances."
            return
        try:
            self.data = json.loads(path.read_text(encoding="utf-8"))
            validate(self.data)
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError) as e:
            self.error = f"Cannot read {path}: {e}. Repair or move this file before changing it."

    def save(self, data):
        with self._lock:
            if self.error:
                raise OSError(self.error)
            if self._lease is None:
                raise OSError("State file is closed; restart Jarvis before changing it")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent,
                                                 prefix=self.path.name, suffix=".tmp", delete=False) as f:
                    temporary = Path(f.name)
                    os.chmod(temporary, 0o600)
                    json.dump(data, f, indent=2, allow_nan=False)
                    f.write("\n")
                    f.flush()
                    os.fsync(f.fileno())
                temporary.replace(self.path)
                self.data = data
            finally:
                if temporary and temporary.exists():
                    temporary.unlink()

    def close(self):
        with self._lock:
            if self._lease:
                self._lease.close()
                self._lease = None


def validate_reminders(data):
    if not isinstance(data, list) or len(data) > 100:
        raise ValueError("expected a list of at most 100 reminders")
    ids = set()
    for item in data:
        if not isinstance(item, dict) or set(item) != {"id", "due", "text"}:
            raise ValueError("invalid reminder")
        identifier, due, text = item["id"], item["due"], item["text"]
        if not isinstance(identifier, str) or not re.fullmatch(r"[a-f0-9]{32}", identifier) or identifier in ids:
            raise ValueError("invalid or duplicate reminder ID")
        ids.add(identifier)
        if isinstance(due, bool) or not isinstance(due, (int, float)) or not math.isfinite(due):
            raise ValueError("invalid reminder time")
        try:
            datetime.fromtimestamp(due)
        except (ValueError, OverflowError, OSError) as e:
            raise ValueError("invalid reminder date") from e
        if not isinstance(text, str) or not text.strip() or len(text) > 2000:
            raise ValueError("reminder text must contain 1–2000 characters")


class Reminders:
    def __init__(self, path: Path, on_due, on_error):
        self.store = StateFile(path, [], validate_reminders)
        self.on_due, self.on_error = on_due, on_error
        self._lock = threading.RLock()
        self._timers = {}
        self._closed = False

    def items(self):
        with self._lock:
            return sorted(self.store.data, key=lambda r: r["due"]) if not self.store.error else []

    def _schedule(self, item, missed=False):
        delay = 0 if missed else min(60, max(0, item["due"] - time.time()))
        timer = threading.Timer(delay, self._fire, args=(item["id"], missed))
        timer.daemon = True
        self._timers[item["id"]] = timer
        timer.start()

    def resume(self):
        if self.store.error:
            self.on_error(self.store.error)
            return
        errors = []
        with self._lock:
            if self._closed:
                return
            for item in self.items():
                if item["id"] in self._timers:
                    continue
                try:
                    self._schedule(item, missed=item["due"] <= time.time())
                except RuntimeError as e:
                    self._timers.pop(item["id"], None)
                    errors.append(f"Reminder retained but its timer couldn't start: {e}. Restart Jarvis to retry.")
        for error in errors:
            self.on_error(error)

    def _fire(self, identifier, missed=False):
        error = None
        with self._lock:
            if self._closed:
                return
            item = next((r for r in self.store.data if r["id"] == identifier), None)
            if item is None:
                return
            if not missed and item["due"] > time.time():
                try:
                    self._schedule(item)
                except RuntimeError as e:
                    self._timers.pop(identifier, None)
                    error = f"Reminder retained but its timer couldn't start: {e}. Restart Jarvis to retry."
                else:
                    return
            else:
                try:
                    self.store.save([r for r in self.store.data if r["id"] != identifier])
                except OSError as e:
                    error = f"Couldn't complete reminder '{item['text']}': {e}. Will retry in a minute."
                    try:
                        timer = threading.Timer(60, self._fire, args=(identifier, missed))
                        timer.daemon = True
                        self._timers[identifier] = timer
                        timer.start()
                    except RuntimeError:
                        self._timers.pop(identifier, None)
                        error = f"Couldn't complete reminder '{item['text']}': {e}. Restart Jarvis to retry."
                else:
                    self._timers.pop(identifier, None)
        if error:
            self.on_error(error)
        else:
            self.on_due(item, missed)

    def add(self, text, due):
        with self._lock:
            if self._closed:
                return fail("Reminders are stopping.", error="stopping")
            if self.store.error:
                return fail(self.store.error, error="unreadable state")
            item = {"id": uuid.uuid4().hex, "due": due, "text": text}
            updated = self.store.data + [item]
            try:
                validate_reminders(updated)
                self.store.save(updated)
            except (OSError, ValueError) as e:
                return fail(f"Couldn't set reminder: {e}", error=str(e))
            try:
                self._schedule(item)
            except RuntimeError as e:
                self._timers.pop(item["id"], None)
                return fail(f"Reminder saved, but its timer couldn't start: {e}. Restart Jarvis to resume it.", error=str(e))
            when = datetime.fromtimestamp(due).strftime("%Y-%m-%d %H:%M")
            return ok(f"Reminder saved for {when}: {text}", speech=f"Reminder set for {when}.")

    def cancel(self, number=None):
        with self._lock:
            if self.store.error:
                return fail(self.store.error, error="unreadable state")
            items = self.items()
            if number is not None and not 1 <= number <= len(items):
                return fail("No reminder with that number. Say 'list reminders'.", error="bad index")
            removed = items if number is None else [items[number - 1]]
            ids = {r["id"] for r in removed}
            try:
                self.store.save([r for r in items if r["id"] not in ids])
            except OSError as e:
                return fail(f"Couldn't cancel reminders: {e}", error=str(e))
            for identifier in ids:
                timer = self._timers.pop(identifier, None)
                if timer:
                    timer.cancel()
            return ok(f"Cancelled {len(removed)} reminder{'s' * (len(removed) != 1)}.")

    def shutdown(self):
        with self._lock:
            self._closed = True
            for timer in self._timers.values():
                timer.cancel()
            self._timers.clear()
            self.store.close()


def routine_name(name):
    name = re.sub(r"\s+", " ", name.strip()).lower()
    if not re.fullmatch(r"[\w][\w -]{0,59}", name):
        raise ValueError("routine names need 1–60 letters, numbers, spaces, underscores or hyphens")
    return name


def validate_routines(data):
    if not isinstance(data, dict) or len(data) > 100:
        raise ValueError("expected an object with at most 100 routines")
    for name, commands in data.items():
        if routine_name(name) != name:
            raise ValueError("invalid routine name")
        if not isinstance(commands, list) or not 1 <= len(commands) <= 20:
            raise ValueError("routines need 1–20 commands")
        if any(not isinstance(c, str) or not c.strip() or len(c) > 2000 for c in commands):
            raise ValueError("each routine command needs 1–2000 characters")


class Routines:
    def __init__(self, path: Path):
        self.store = StateFile(path, {}, validate_routines)

    def save(self, name, commands):
        if self.store.error:
            return fail(self.store.error, error="unreadable state")
        try:
            name = routine_name(name)
            if name in self.store.data:
                return fail("That routine already exists. Delete it before saving a replacement.", error="exists")
            updated = {**self.store.data, name: commands}
            validate_routines(updated)
            self.store.save(updated)
        except (ValueError, OSError) as e:
            return fail(f"Couldn't save routine: {e}", error=str(e))
        return ok(f"Saved routine '{name}' with {len(commands)} commands.")

    def delete(self, name):
        if self.store.error:
            return fail(self.store.error, error="unreadable state")
        if name not in self.store.data:
            return fail(f"No routine called '{name}'.", error="not_found")
        try:
            self.store.save({k: v for k, v in self.store.data.items() if k != name})
        except OSError as e:
            return fail(f"Couldn't delete routine: {e}", error=str(e))
        return ok(f"Deleted routine '{name}'.")
