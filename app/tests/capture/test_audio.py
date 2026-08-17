import asyncio

from app.capture.audio import AudioDecoder


class _FailingStdin:
    def write(self, _chunk: bytes) -> None:
        raise BrokenPipeError

    async def drain(self) -> None:
        raise AssertionError("drain should not run after write fails")

    def close(self) -> None:
        raise BrokenPipeError

    async def wait_closed(self) -> None:
        raise AssertionError("wait_closed should not run after close fails")


class _FailingStdout:
    async def read(self, _size: int) -> bytes:
        raise asyncio.IncompleteReadError(partial=b"", expected=1)


class _FailingProcess:
    stdin = _FailingStdin()
    stdout = _FailingStdout()

    def terminate(self) -> None:
        raise ProcessLookupError


def test_audio_decoder_ignores_expected_stream_shutdown_errors() -> None:
    decoder = AudioDecoder()
    decoder.process = _FailingProcess()

    asyncio.run(decoder.send_encoded_chunk(b"audio"))
    assert asyncio.run(decoder.read_pcm_chunk()) == b""
    asyncio.run(decoder.close())
