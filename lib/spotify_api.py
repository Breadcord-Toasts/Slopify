import datetime

import aiohttp
from yarl import URL

from .types import AIOLoadable, BadResponseError

import breadcord

class SpotifyAPI(AIOLoadable):
    def __init__(self, *, settings: breadcord.config.SettingsGroup) -> None:
        self.module_settings = settings
        self.session: aiohttp.ClientSession = aiohttp.ClientSession()

        self._token: str | None = None
        self._token_expires_at: datetime.datetime = datetime.datetime.min

    async def load(self) -> None:
        pass

    async def close(self) -> None:
        if self.session and not self.session.closed:
            await self.session.close()

    async def update_spotify_token(self) -> None:
        # We add a minute so that we have a bit more breathing room
        if self._token_expires_at > datetime.datetime.now() + datetime.timedelta(minutes=1):
            return

        async with self.session.post(
            "https://accounts.spotify.com/api/token",
            data={
                "grant_type": "client_credentials",
                "client_id": self.module_settings.client_id.value,
                "client_secret": self.module_settings.client_secret.value,
            },
        ) as response:
            data = await response.json()
            if data.get("error") == "invalid_client":
                raise ValueError("Invalid spotify client id or secret")
            self._token = data["access_token"]
            self._token_expires_at = datetime.datetime.now() + datetime.timedelta(seconds=data["expires_in"])

    @property
    def _auth_header(self) -> dict[str, str]:
        if self._token is None:
            raise ValueError("No spotify token")
        return {"Authorization": f"Bearer {self._token}"}

    async def fetch_track_data(self, track_id: str) -> dict:
        await self.update_spotify_token()
        async with self.session.get(
            f"https://api.spotify.com/v1/tracks/{track_id}",
            headers=self._auth_header,
        ) as response:
            if response.status == 401:
                raise BadResponseError("Invalid spotify token")
            elif response.status != 200:
                raise BadResponseError("Could not get track data")
            elif not response.ok:
                raise BadResponseError(f"{response.status} Could not get track data: {response.reason}")
            return await response.json()

    async def search_for(self, query: str) -> dict:
        await self.update_spotify_token()
        async with self.session.get(
            URL("https://api.spotify.com/v1/search") % {
                "q": query,
                "type": "track",
            },
            headers=self._auth_header,
        ) as response:
            if response.status == 401:
                raise BadResponseError("Invalid spotify token")
            elif response.status != 200:
                raise BadResponseError("Could not search for track")
            elif not response.ok:
                raise BadResponseError(f"{response.status} Could not search for track: {response.reason}")
            return await response.json()

