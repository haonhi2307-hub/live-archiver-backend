from __future__ import annotations
from abc import ABC, abstractmethod
import httpx
from ..models import ResolveResult

class Resolver(ABC):
    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    @abstractmethod
    async def resolve(self, url: str) -> ResolveResult:
        raise NotImplementedError
