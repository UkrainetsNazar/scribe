import asyncio
import json
import math

import structlog

from scribe.application.interfaces import MediaInfo, MediaProbe
from scribe.domain.errors import DependencyUnavailableError, InvalidMediaError

log = structlog.get_logger()


class FfprobeMediaProbe(MediaProbe):
    def __init__(self, binary: str = "ffprobe", timeout_sec: float = 30.0) -> None:
        self._binary = binary
        self._timeout = timeout_sec

    async def probe(self, source_url: str) -> MediaInfo:
        try:
            proc = await asyncio.create_subprocess_exec(
                self._binary,
                "-v",
                "error",
                "-print_format",
                "json",
                "-show_format",
                "-show_streams",
                "-protocol_whitelist",
                "http,https,tcp,tls,crypto",
                source_url,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise DependencyUnavailableError("ffprobe is not installed") from exc

        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=self._timeout)
        except TimeoutError as exc:
            proc.kill()
            await proc.wait()
            raise InvalidMediaError("Media probing timed out") from exc

        if proc.returncode != 0:
            log.warning("ffprobe.failed", stderr=stderr.decode(errors="replace")[:500])
            raise InvalidMediaError("File is not a valid audio/video file")
        return _parse(stdout)


def _parse(stdout: bytes) -> MediaInfo:
    try:
        data = json.loads(stdout)
    except ValueError as exc:
        raise InvalidMediaError("Cannot read media information") from exc

    audio_streams = [s for s in data.get("streams", []) if s.get("codec_type") == "audio"]
    if not audio_streams:
        raise InvalidMediaError("File has no audio track")

    raw_duration = data.get("format", {}).get("duration") or audio_streams[0].get("duration")
    try:
        duration = float(raw_duration)
    except (TypeError, ValueError) as exc:
        raise InvalidMediaError("Cannot determine media duration") from exc
    if not math.isfinite(duration) or duration <= 0:
        raise InvalidMediaError("Cannot determine media duration")
    return MediaInfo(duration_sec=duration)
