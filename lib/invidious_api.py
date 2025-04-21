import datetime
from logging import getLogger

import aiohttp
from yarl import URL

from .types import AIOLoadable, BadResponseError

# Stolen from polyplayer... I should really figure out how to make a shared lib for these
class InvidiousAPI(AIOLoadable):
    def __init__(self, host_url: str | None = None) -> None:
        self.host_url: URL | None = URL(host_url) if host_url else None
        self.logger = getLogger("slopify.Invidious")
        self.session: aiohttp.ClientSession = aiohttp.ClientSession()

    async def load(self) -> None:
        if not self.host_url:
            self.host_url = await self.find_best_host()
        self.logger.debug(f"Using invidious instance: {self.host_url}")

    async def close(self) -> None:
        if self.session and not self.session.closed:
            await self.session.close()

    async def find_best_host(self) -> URL:
        async with self.session.get("https://api.invidious.io/instances.json") as response:
            instances_list: list[dict] = [
                instance
                for _, instance in await response.json()
                if all((
                    instance.get("stats") is not None,
                    instance.get("api"),
                    instance.get("type") == "https",
                    instance.get("uri"),
                ))
            ]
        best: URL | None = None
        while not best and instances_list:
            candidate = instances_list.pop(0)["uri"]
            try:
                await self.search_for("Never Gonna Give You Up", host_url=URL(candidate))
            except (BadResponseError, aiohttp.ClientError):
                self.logger.warning(f"Failed to connect to {candidate}, trying next")
            else:
                best = URL(candidate)
        if not best:
            raise RuntimeError("No invidious instances available")
        return best

    async def get_video(self, video_id: str, *, host_url: URL | None = None) -> dict:
        url = (host_url or self.host_url)
        if url is None: raise ValueError("No host url")
        async def inner():
            self.logger.debug(f"Fetching video with ID: {video_id}")
            async with self.session.get(url / "/api/v1/videos/{video_id}") as response:
                data = await response.json()
                if error := data.get("error"):
                    raise BadResponseError(f"Error fetching video: {error}")
                return data
        # The "The video returned by YouTube isn't the requested one" errors seems to be quite common
        # It seems like it can sometimes be fixed by just trying again?
        try:
            return await inner()
        except BadResponseError:
            self.logger.warning("Failed to fetch video, trying once more")
            return await inner()

    async def search_for(self, query: str, *, host_url: URL | None = None) -> list[dict]:
        self.logger.debug(f"Searching for: {query}")
        url = (host_url or self.host_url)
        if url is None: raise ValueError("No host url")
        async with self.session.get(
            URL(url / "api/v1/search") % {
                "q": query,
                "sort_by": "relevance",
                "type": "video",
            },
        ) as response:
            if not response.ok:
                raise BadResponseError(f"Error fetching search results: {response.reason}")
            return await response.json()

    async def get_audio_url(self, video: dict, *, host_url: URL | None = None) -> URL:
        url = (host_url or self.host_url)
        if url is None: raise ValueError("No host url")
        best = max(
            (frmt for frmt in video["adaptiveFormats"] if frmt["type"].startswith("audio/")),
            key=lambda frmt: frmt["bitrate"],
        )
        async with self.session.get(
            url / "latest_version" % {
                "id": video["videoId"],
                "itag": best["itag"],
                "local": "true",
            },
        ) as response:
            if not response.ok:
                raise BadResponseError(f"Error fetching audio: {response.reason}")
            return response.url
