from abc import ABC, abstractmethod


class BadResponseError(Exception):
    pass


class AIOLoadable(ABC):
    @abstractmethod
    async def load(self) -> None:
        pass

    @abstractmethod
    async def close(self) -> None:
        pass


