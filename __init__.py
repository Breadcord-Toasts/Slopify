import datetime
import json
import re
from abc import ABC, abstractmethod
from logging import getLogger
from typing import cast

import aiohttp
import discord
from discord.ext import commands
from yarl import URL

import breadcord

# Taken from discord source with added edge guards
DISCORD_URL_REGEX = re.compile(
    r"""
    (?<!<)
    (https?://[^\s<]+[^<.,:;"')\]\s])
    (?!>)
    """,
    re.VERBOSE,
)

class BadResponseError(Exception):
    pass


class AIOLoadable(ABC):
    @abstractmethod
    async def load(self) -> None:
        pass

    @abstractmethod
    async def close(self) -> None:
        pass


class SpotifyAPI(AIOLoadable):
    def __init__(self, *, settings: breadcord.config.SettingsGroup) -> None:
        self.module_settings = settings
        self.session: aiohttp.ClientSession = aiohttp.ClientSession()

        self._token: str | None = None
        self._token_expires_at: datetime.datetime = datetime.datetime.min

    async def load(self) -> None:
        pass

    async def close(self) -> None:
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


def readable_delta(delta: datetime.timedelta) -> str:
    parts: list[str] = str(delta).split(":")
    if parts[0] == "0":
        parts = parts[1:]
    return ":".join(parts)


class SlopifyCog(breadcord.helpers.HTTPModuleCog):
    def __init__(self, module_id: str) -> None:
        super().__init__(module_id)
        self.spotify: SpotifyAPI = SpotifyAPI(settings=cast(breadcord.config.SettingsGroup, self.settings))
        self.invidious: InvidiousAPI = InvidiousAPI()

        self.embed_track_ctx_menu = discord.app_commands.ContextMenu(
            name="Embed Track",
            callback=self.embed_track_callback,
        )
        self.bot.tree.add_command(self.embed_track_ctx_menu)

    async def cog_load(self) -> None:
        await super().cog_load()
        try:
            await self.spotify.load()
            await self.invidious.load()
        except Exception as error:
            self.logger.error(f"Failed to load: {error}")
            await self.cog_unload()

    async def cog_unload(self) -> None:
        await super().cog_unload()
        await self.spotify.close()
        await self.invidious.close()

        self.bot.tree.remove_command(self.embed_track_ctx_menu.name, type=self.embed_track_ctx_menu.type)

    async def handle_track(self, message: discord.Message, track_id: str) -> None:
        track_data = await self.spotify.fetch_track_data(track_id)

        if any(
            embed.type == "link"
            and embed.provider.name == "Spotify"
            and embed.url and track_id in embed.url
            for embed in message.embeds
        ):
            return

        await message.reply(
            embed=await self.construct_spotify_track_embed(track_data),
            silent=True,
            mention_author=False,
        )

    async def embed_track_callback(self, interaction: discord.Interaction, message: discord.Message) -> None:
        urls = map(URL, DISCORD_URL_REGEX.findall(message.content))

        done_anything = False
        for url in urls:
            if not url.host:
                continue

            if url.host.endswith("spotify.com"):
                track_data = await self.spotify.fetch_track_data(url.path.split("/", 4)[2])
                await interaction.response.send_message(embed=await self.construct_spotify_track_embed(track_data))
                done_anything = True
            elif url.host.endswith(("youtube.com", "youtu.be")):
                video_id = url.query.get("v") or url.path.split("/", 2)[-1]
                video_data = await self.invidious.get_video(video_id)
                await interaction.response.send_message(embed=await self.construct_youtube_track_embed(video_data))
                done_anything = True

        if not done_anything:
            await interaction.response.send_message("No Spotify or YouTube links found", ephemeral=True)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if not (matches := DISCORD_URL_REGEX.findall(message.content)):
            return

        for match in matches:
            url = URL(match)
            if not url.host or not url.host.endswith("spotify.com"):
                continue

            if url.path.startswith("/track/"):
                await self.handle_track(message, url.path.split("/", 4)[2])

    async def construct_spotify_track_embed(self, track_data: dict) -> discord.Embed:
        embed = discord.Embed(
            title=(":underage: " if track_data["explicit"] else "") + track_data["name"],
            url=track_data["external_urls"]["spotify"],
            description="\n".join(line for line in (
                "**Artists:** " + ", ".join([artist["name"] for artist in track_data["artists"]]),
                f"**Album:** {track_data['album']['name']}"
                f" (track {track_data['disc_number']}/{track_data['album']['total_tracks']})"
                if track_data["album"]["total_tracks"] > 1 else None,
                f"**Length:** {readable_delta(datetime.timedelta(seconds=int(track_data['duration_ms'] / 1000)))}",
                f"**Popularity:** {track_data['popularity']:,}",
                f"**YT:** [Link](https://youtu.be/{await self.spotify_to_yt(track_data['id'])})",
            ) if line is not None),
        ).set_thumbnail(url=max(
            track_data["album"]["images"],
            key=lambda image: image.get("width", 0) * image.get("height", 0)
        )["url"])

        translated = await self.translate(track_data["name"])
        if translated.lower().strip() != track_data["name"].lower().strip():
            embed.set_footer(text=f"Translated title: {translated}")

        return embed

    async def construct_youtube_track_embed(self, track_data: dict) -> discord.Embed:
        print(json.dumps(track_data, indent=4))
        embed = discord.Embed(
            title=track_data["title"],
            url=f"https://youtu.be/{track_data['videoId']}",
            description="\n".join(line for line in (
                f"**Channel:** {track_data['author']}",
                f"**Length:** {readable_delta(datetime.timedelta(seconds=int(track_data['lengthSeconds'])))}",
                f"**Views:** {track_data['viewCount']:,}",
                f"**Likes:** {track_data['likeCount']:,}",
                f"**Spotify:** "
                f"[Link](https://open.spotify.com/track/{await self.yt_to_spotify(track_data['videoId'])})",
            ) if line is not None),
        ).set_thumbnail(url=max(
            track_data["videoThumbnails"],
            key=lambda thumbnail: thumbnail.get("width", 0) * thumbnail.get("height", 0)
        )["url"])

        translated = await self.translate(track_data["title"])
        if translated.lower().strip() != track_data["title"].lower().strip():
            embed.set_footer(text=f"Translated title: {translated}")

        return embed


    async def translate(self, text: str) -> str:
        url = URL("https://translate.googleapis.com/translate_a/single").with_query({
            "client": "gtx",
            "sl": "auto",       # Auto detect language
            "tl": "en",         # Translate to English
            "dt": "t",          # Make it... translate
            "dj": "1",          # JSON response
            "source": "input",
            "q": text
        })
        if self.session is None:
            raise ValueError("No aiohttp session")
        async with self.session.get(url) as response:
            if not response.ok:
                raise BadResponseError(f"{response.status} Could not translate text: {response.reason}")
            data = await response.json()
            return data["sentences"][0]["trans"]

    async def spotify_to_yt(self, track_id: str) -> str:
        track_data = await self.spotify.fetch_track_data(track_id)
        query = f"{track_data['name']} {' '.join(artist['name'] for artist in track_data['artists'])}"
        search = await self.invidious.search_for(query)
        return search[0]["videoId"]

    async def yt_to_spotify(self, video_id: str) -> str:
        video_data = await self.invidious.get_video(video_id)
        query = video_data["title"]
        search = await self.spotify.search_for(query)
        return search["tracks"]["items"][0]["id"]


async def setup(bot: breadcord.Bot, module: breadcord.module.Module) -> None:
    await bot.add_cog(SlopifyCog(module.id))
