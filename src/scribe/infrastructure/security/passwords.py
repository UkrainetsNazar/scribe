import asyncio

from argon2 import PasswordHasher as Argon2
from argon2.exceptions import InvalidHashError, VerificationError

from scribe.application.interfaces import PasswordHasher


class Argon2PasswordHasher(PasswordHasher):
    def __init__(self) -> None:
        self._argon2 = Argon2()
        self._dummy_hash = self._argon2.hash("dummy-password-for-timing")

    @property
    def dummy_hash(self) -> str:
        return self._dummy_hash

    async def hash(self, password: str) -> str:
        return await asyncio.to_thread(self._argon2.hash, password)

    async def verify(self, password: str, hashed: str) -> bool:
        return await asyncio.to_thread(self._verify, password, hashed)

    def _verify(self, password: str, hashed: str) -> bool:
        try:
            return self._argon2.verify(hashed, password)
        except (VerificationError, InvalidHashError):
            return False
