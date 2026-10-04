import asyncio

from starlette.websockets import WebSocketDisconnect

from app.capture.agent import _is_client_disconnect_exception
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


def test_is_client_disconnect_exception() -> None:
    assert _is_client_disconnect_exception(None) is False
    assert _is_client_disconnect_exception(WebSocketDisconnect(code=1000)) is True
    assert _is_client_disconnect_exception(WebSocketDisconnect(code=1001)) is True
    assert (
        _is_client_disconnect_exception(
            RuntimeError('Cannot call "receive" once a disconnect message has been received.')
        )
        is True
    )
    assert _is_client_disconnect_exception(RuntimeError("Other error")) is False
    assert _is_client_disconnect_exception(ValueError("error")) is False
