from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

import orjson
from redis.asyncio import Redis
from redis.asyncio.client import PubSub

from scribe.application.progress import (
    Message,
    ProgressPublisher,
    ProgressSubscriber,
    ProgressSubscription,
)

STATE_TTL_SECONDS = 7 * 24 * 3600
SUBSCRIBE_TIMEOUT_SEC = 5.0

# Atomic "store the state and broadcast it, unless a newer state is already stored".
# KEYS: state key, channel. ARGV: JSON message, TTL in seconds.
#
# Order within one run: status < progress < final state. Between two progress messages the
# position (stage, attempt of that stage, percent) decides: a late message can never move the
# state back, but a retried stage may start again from 0 in a later attempt.
# The first final state (completed / failed / cancelled) of a run wins.
_STORE_AND_PUBLISH = """
local ranks = {status = 0, progress = 1, completed = 2, failed = 2, cancelled = 2}
local stages = {extract_audio = 0, normalize_and_chunk = 1, transcribe = 2,
                build_subtitles = 3, summarize = 4, index = 5}

local function rank(message)
  return ranks[message.type] or 1
end

local function position(message)
  return {stages[message.stage] or 0, message.attempt or 1, message.percent or 0}
end

local function is_older(a, b)
  for i = 1, 3 do
    if a[i] ~= b[i] then
      return a[i] < b[i]
    end
  end
  return false
end

local incoming = cjson.decode(ARGV[1])
local current = redis.call('GET', KEYS[1])
if current then
  local stored = cjson.decode(current)
  local stored_run = stored.run or 0
  local incoming_run = incoming.run or 0
  if incoming_run < stored_run then
    return 0
  end
  if incoming_run == stored_run then
    local incoming_rank = rank(incoming)
    local stored_rank = rank(stored)
    if incoming_rank < stored_rank then
      return 0
    end
    if incoming.type == 'status' and stored.type == 'status'
        and incoming.status == stored.status then
      return 0
    end
    if incoming_rank == 2 and stored_rank == 2 then
      return 0
    end
    if incoming_rank == 1 and stored_rank == 1
        and is_older(position(incoming), position(stored)) then
      return 0
    end
  end
end
redis.call('SET', KEYS[1], ARGV[1], 'EX', tonumber(ARGV[2]))
redis.call('PUBLISH', KEYS[2], ARGV[1])
return 1
"""


def channel_name(recording_id: UUID) -> str:
    return f"rec:{recording_id}"


def state_key(recording_id: UUID) -> str:
    return f"progress:{recording_id}"


def _parse(raw: Any) -> Message | None:
    if raw is None:
        return None
    try:
        data = orjson.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


class _RedisSubscription(ProgressSubscription):
    def __init__(self, redis: Redis, pubsub: PubSub, recording_id: UUID) -> None:
        self._redis = redis
        self._pubsub = pubsub
        self._recording_id = recording_id

    async def last_state(self) -> Message | None:
        return _parse(await self._redis.get(state_key(self._recording_id)))

    async def next_message(self, timeout: float) -> Message | None:  # noqa: ASYNC109
        message = await self._pubsub.get_message(ignore_subscribe_messages=True, timeout=timeout)
        if message is None:
            return None
        return _parse(message.get("data"))


class RedisProgressBus(ProgressPublisher, ProgressSubscriber):
    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def publish_state(self, recording_id: UUID, message: Message) -> None:
        await self._redis.eval(
            _STORE_AND_PUBLISH,
            2,
            state_key(recording_id),
            channel_name(recording_id),
            orjson.dumps(message),
            STATE_TTL_SECONDS,
        )

    async def publish_transient(self, recording_id: UUID, message: Message) -> None:
        await self._redis.publish(channel_name(recording_id), orjson.dumps(message))

    @asynccontextmanager
    async def subscribe(self, recording_id: UUID) -> AsyncGenerator[ProgressSubscription]:
        pubsub = self._redis.pubsub()
        try:
            await pubsub.subscribe(channel_name(recording_id))
            # Wait for the confirmation: only then is every later message guaranteed to arrive
            confirmation = await pubsub.get_message(
                ignore_subscribe_messages=False, timeout=SUBSCRIBE_TIMEOUT_SEC
            )
            if confirmation is None or confirmation.get("type") != "subscribe":
                raise RuntimeError("Redis did not confirm the subscription")
            yield _RedisSubscription(self._redis, pubsub, recording_id)
        finally:
            await pubsub.aclose()  # type: ignore[no-untyped-call]
