"""Pipeline stages — each stage is an independent, testable unit.

Each stage: Product → StageResult[Product]
"""

from __future__ import annotations

from typing import Generic, TypeVar

from pydantic import BaseModel

T = TypeVar("T")


class StageResult(BaseModel, Generic[T]):
    success: bool
    data: T | None = None
    error: str | None = None

    @property
    def failed(self) -> bool:
        return not self.success

    @classmethod
    def ok(cls, data: T) -> StageResult[T]:
        return cls(success=True, data=data)

    @classmethod
    def fail(cls, error: str, data: T | None = None) -> StageResult[T]:
        return cls(success=False, data=data, error=error)
