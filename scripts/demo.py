"""Walks through the whole Scribe flow against a running stack and narrates it.

    docker compose up --build -d
    uv run python scripts/demo.py [--base-url http://localhost:8010] [--file talk.mp3] [--delete]

Without --file it uploads a generated tone. The default stub transcriber returns placeholder text;
set TRANSCRIBER=whisper (and WORKER_EXTRAS) in .env for real speech recognition.
"""

import argparse
import asyncio
import io
import json
import math
import struct
import sys
import time
import uuid
import wave
from pathlib import Path
from typing import Any

import httpx
from websockets.asyncio.client import connect

API = "/api/v1"
PASSWORD = "demo-password-1"  # noqa: S105
CONTENT_TYPES = {
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
    ".ogg": "audio/ogg",
    ".flac": "audio/flac",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mov": "video/quicktime",
}
LINKS = {
    "Grafana (dashboards)": "http://localhost:3030",
    "Prometheus (alerts)": "http://localhost:9090/alerts",
    "RabbitMQ": "http://localhost:15672",
    "MinIO console": "http://localhost:9101",
}
JAEGER = "http://localhost:16686"


def step(title: str) -> None:
    print(f"\n\033[1m== {title}\033[0m")


def tone(seconds: int = 8, rate: int = 16000) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        frames = (
            struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * i / rate)))
            for i in range(rate * seconds)
        )
        wav.writeframes(b"".join(frames))
    return buffer.getvalue()


def show(message: dict[str, Any]) -> None:
    kind = message["type"]
    if kind == "status":
        print(f"  status: {message['status']}")
    elif kind == "progress":
        line = (
            f"  {message['stage']:<22}{message['percent']:>3}%   overall {message['overall']:>3}%"
        )
        print(f"\r{line}", end="", flush=True)
    elif kind == "segment":
        print(f"\n    [{message['start']:6.1f}s - {message['end']:6.1f}s] {message['text']}")
    else:
        print(f"\n  {kind}" + (f": {message.get('error')}" if kind == "failed" else ""))


async def wait_until_ready(client: httpx.AsyncClient, timeout: float = 90.0) -> None:  # noqa: ASYNC109
    deadline = time.monotonic() + timeout
    while True:
        try:
            if (await client.get("/health/ready")).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        if time.monotonic() > deadline:
            raise SystemExit("The stack is not ready. Did you run `docker compose up --build -d`?")
        await asyncio.sleep(2)


async def run(args: argparse.Namespace) -> int:
    base = args.base_url.rstrip("/")
    async with httpx.AsyncClient(base_url=base, timeout=30) as client:
        step("Waiting for the stack")
        await wait_until_ready(client)
        print("  /health/ready: ok")

        step("Register and log in")
        email = f"demo-{uuid.uuid4().hex[:8]}@example.com"
        await client.post(f"{API}/auth/register", json={"email": email, "password": PASSWORD})
        login = await client.post(f"{API}/auth/login", json={"email": email, "password": PASSWORD})
        login.raise_for_status()
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
        me = (await client.get(f"{API}/users/me", headers=headers)).json()
        print(f"  {email}, plan {me['plan']}: {me['monthly_minutes']} minutes per month")

        step("Create a project and a recording")
        project = await client.post(f"{API}/projects", json={"name": "Demo"}, headers=headers)
        project_id = project.json()["id"]
        if args.file:
            path = Path(args.file)
            data, filename = path.read_bytes(), path.name
            content_type = CONTENT_TYPES.get(path.suffix.lower())
            if content_type is None:
                raise SystemExit(f"Unsupported file type: {path.suffix}")
        else:
            data, filename, content_type = tone(), "tone.wav", "audio/wav"
        created = await client.post(
            f"{API}/recordings",
            json={
                "project_id": project_id,
                "title": "Demo recording",
                "filename": filename,
                "content_type": content_type,
                "size_bytes": len(data),
            },
            headers=headers,
        )
        created.raise_for_status()
        recording_id = created.json()["recording"]["id"]
        print(f"  recording {recording_id}, {len(data) / 1024:.0f} KiB")

        step("Upload straight to object storage (the API does not proxy the bytes)")
        async with httpx.AsyncClient(timeout=120) as raw:
            uploaded = await raw.put(created.json()["upload_url"], content=data)
        uploaded.raise_for_status()
        print("  uploaded")

        step("Complete the upload and watch the live progress")
        token = headers["Authorization"].removeprefix("Bearer ")
        ws_url = base.replace("http", "ws", 1) + f"/ws/recordings/{recording_id}?token={token}"
        final: dict[str, Any] = {}
        trace_id = ""
        async with connect(ws_url) as ws:
            show(json.loads(await ws.recv()))
            accepted = await client.post(
                f"{API}/recordings/{recording_id}/complete-upload", headers=headers
            )
            if accepted.status_code != 202:
                print(f"  rejected: {accepted.json().get('detail')}")
                return 1
            trace_id = accepted.headers.get("x-trace-id", "")
            async with asyncio.timeout(args.timeout):
                async for raw_message in ws:
                    message = json.loads(raw_message)
                    show(message)
                    if message["type"] in {"completed", "failed", "cancelled"}:
                        final = message
                        break
        if final.get("type") != "completed":
            return 1

        step("Transcript")
        page = (
            await client.get(f"{API}/recordings/{recording_id}/transcript?limit=5", headers=headers)
        ).json()
        for item in page["items"]:
            print(f"  [{item['start']:6.1f}s] {item['text']}")
        print(f"  language: {page['language']}, more pages: {page['next_cursor'] is not None}")

        step("Summary")
        summary = (
            await client.get(f"{API}/recordings/{recording_id}/summary", headers=headers)
        ).json()
        print(f"  {summary['summary'] or '(empty)'}")
        print(f"  topics: {', '.join(summary['topics']) or '-'}")

        step("Search")
        words = [w for item in page["items"] for w in item["text"].split() if len(w) > 3]
        query = words[0].strip(".,!?") if words else "transcript"
        found = (await client.get(f"{API}/search", params={"q": query}, headers=headers)).json()
        print(f"  '{query}': {len(found['items'])} hit(s)")
        for hit in found["items"][:3]:
            segment = hit["segment"]
            print(f"  {segment['start']:6.1f}s - {segment['end']:6.1f}s  {hit['snippet']}")

        step(f"Exports (saved to {args.output}/)")
        out = Path(args.output)
        out.mkdir(parents=True, exist_ok=True)
        for fmt in ("txt", "srt", "vtt", "json"):
            base_path = f"{API}/recordings/{recording_id}/exports"
            await client.post(base_path, json={"format": fmt}, headers=headers)
            link = (await client.get(f"{base_path}/{fmt}", headers=headers)).json()
            async with httpx.AsyncClient() as raw:
                content = (await raw.get(link["url"])).content
            (out / f"{recording_id}.{fmt}").write_bytes(content)
            print(f"  {fmt}: {len(content)} bytes")

        step("Where to look next")
        if trace_id:
            print(f"  Jaeger trace of the whole request chain: {JAEGER}/trace/{trace_id}")
        for name, url in LINKS.items():
            print(f"  {name}: {url}")

        if args.delete:
            step("Delete the recording (files, documents and rows are removed asynchronously)")
            deleted = await client.delete(f"{API}/recordings/{recording_id}", headers=headers)
            print(f"  DELETE: {deleted.status_code}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", default="http://localhost:8010")
    parser.add_argument("--file", help="audio or video file to upload (default: a generated tone)")
    parser.add_argument("--output", default="demo-output", help="directory for the exports")
    parser.add_argument("--timeout", type=float, default=600, help="seconds to wait for processing")
    parser.add_argument("--delete", action="store_true", help="delete the recording at the end")
    sys.exit(asyncio.run(run(parser.parse_args())))


if __name__ == "__main__":
    main()
