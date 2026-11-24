from __future__ import annotations

import asyncio
import threading
import time
from contextlib import suppress

import sounddevice as sd


class AudioIO:
    """16 kHz PCM microphone, 24 kHz PCM playback, bounded queues and immediate flush."""

    def __init__(self):
        self.input: asyncio.Queue[bytes] = asyncio.Queue(maxsize=50)
        self._loop = asyncio.get_running_loop()
        self._lock = threading.Lock()
        self._playback = bytearray()
        self._responding = False
        self._echo_until = 0.0
        self._muted = False
        self._closed = False
        self._microphone = sd.RawInputStream(
            samplerate=16000, blocksize=320, channels=1, dtype="int16",
            callback=self._capture,
        )
        try:
            self._speaker = sd.RawOutputStream(
                samplerate=24000, blocksize=480, channels=1, dtype="int16",
                callback=self._play,
            )
            self._microphone.start()
            self._speaker.start()
        except BaseException:
            self._microphone.close()
            if hasattr(self, "_speaker"):
                self._speaker.close()
            raise

    def _capture(self, data, frames, timing, status):
        if not self._closed:
            self._loop.call_soon_threadsafe(self._enqueue, bytes(data))

    def _enqueue(self, data):
        if self._closed:
            return
        if self.input.full():
            self.input.get_nowait()
        self.input.put_nowait(self.microphone_chunk(data))

    def _play(self, output, frames, timing, status):
        size = len(output)
        with self._lock:
            data = bytes(self._playback[:size])
            del self._playback[:size]
            if data:
                self._echo_until = time.monotonic() + .35
        output[:] = data + b"\0" * (size - len(data))

    def feed(self, data: bytes):
        with self._lock:
            self._responding = True
            self._playback.extend(data)
            # Bound latency and memory if the audio device cannot keep up.
            if len(self._playback) > 24000 * 2 * 20:
                self._playback.clear()

    def clear_output(self):
        with self._lock:
            self._playback.clear()
            self._responding = False

    def finish_response(self):
        with self._lock:
            self._responding = False

    def microphone_chunk(self, data: bytes) -> bytes:
        # Until AEC is implemented, do not feed speaker playback back into Gemini.
        # Send silence instead of stopping the stream so server VAD can end a turn.
        with self._lock:
            blocked = (self._muted or self._responding or bool(self._playback)
                       or time.monotonic() < self._echo_until)
        return bytes(len(data)) if blocked else data

    @property
    def output_pending(self) -> bool:
        with self._lock:
            return bool(self._playback)

    def clear_input(self):
        while not self.input.empty():
            self.input.get_nowait()

    def toggle_mute(self) -> bool:
        self._muted = not self._muted
        self.clear_input()
        return self._muted

    def close(self):
        self._closed = True
        for stream in (self._microphone, self._speaker):
            with suppress(sd.PortAudioError):
                stream.stop()
            with suppress(sd.PortAudioError):
                stream.close()
        self.clear_output()
        self.clear_input()
