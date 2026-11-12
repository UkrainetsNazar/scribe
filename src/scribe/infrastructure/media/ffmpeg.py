import asyncio
from pathlib import Path

import structlog

from scribe.application.pipeline_ports import AudioProcessor
from scribe.domain.pipeline import PermanentStageError

log = structlog.get_logger()


class FfmpegAudioProcessor(AudioProcessor):
    """Works on local files only, so a failure here is always about the file itself
    (storage problems surface earlier, as transient errors, during download)."""

    def __init__(self, binary: str = "ffmpeg", timeout_sec: float = 3600.0) -> None:
        self._binary = binary
        self._timeout = timeout_sec

    async def extract_audio(self, source: Path, target: Path) -> None:
        await self._run("-i", str(source), "-map", "0:a:0", "-vn", "-c:a", "flac", str(target))

    async def split_into_chunks(self, source: Path, out_dir: Path, chunk_sec: int) -> list[Path]:
        await self._run(
            "-i", str(source),
            "-vn",
            "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
            "-ar", "16000",
            "-ac", "1",
            "-c:a", "pcm_s16le",
            "-f", "segment",
            "-segment_time", str(chunk_sec),
            "-reset_timestamps", "1",
            str(out_dir / "chunk_%04d.wav"),
        )  # fmt: skip
        chunks = await asyncio.to_thread(self._list_chunks, out_dir)
        if not chunks:
            raise PermanentStageError("ffmpeg produced no audio")
        return chunks

    @staticmethod
    def _list_chunks(out_dir: Path) -> list[Path]:
        return sorted(out_dir.glob("chunk_*.wav"))

    async def _run(self, *args: str) -> None:
        try:
            proc = await asyncio.create_subprocess_exec(
                self._binary,
                "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                *args,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )  # fmt: skip
        except FileNotFoundError as exc:
            raise PermanentStageError("ffmpeg is not installed") from exc

        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=self._timeout)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise  # a timeout is treated as a transient error and retried

        if proc.returncode != 0:
            tail = stderr.decode(errors="replace").strip()[-500:]
            log.warning("ffmpeg.failed", stderr=tail)
            raise PermanentStageError(f"ffmpeg failed: {tail}")
