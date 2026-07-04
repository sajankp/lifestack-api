"""Client-audio decoding for the voice-capture bridge.

Extracted verbatim from ``agent.py`` (D3): an ffmpeg subprocess that decodes
the browser's encoded audio (WebM/Opus etc.) to 16 kHz mono s16le PCM for
Gemini. Behavior unchanged.
"""

import asyncio


class AudioDecoder:
    def __init__(self):
        self.process = None

    async def start(self):
        self.process = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-i",
            "pipe:0",
            "-f",
            "s16le",
            "-acodec",
            "pcm_s16le",
            "-ar",
            "16000",
            "-ac",
            "1",
            "pipe:1",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )

    async def send_encoded_chunk(self, chunk: bytes):
        if self.process and self.process.stdin:
            try:
                self.process.stdin.write(chunk)
                await self.process.stdin.drain()
            except Exception:
                pass

    async def read_pcm_chunk(self, size: int = 1024) -> bytes:
        if self.process and self.process.stdout:
            try:
                return await self.process.stdout.read(size)
            except Exception:
                return b""
        return b""

    async def close(self):
        if self.process:
            try:
                if self.process.stdin:
                    self.process.stdin.close()
                    await self.process.stdin.wait_closed()
            except Exception:
                pass
            try:
                self.process.terminate()
                await self.process.wait()
            except Exception:
                pass
