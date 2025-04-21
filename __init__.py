import contextlib
import datetime
import json
import re
from typing import cast

import discord
from discord.ext import commands
from yarl import URL

import breadcord
from .lib import SpotifyAPI, InvidiousAPI
from .lib.types import BadResponseError

# Taken from discord source with added edge guards
DISCORD_URL_REGEX = re.compile(
    r"""
    (?<!<)
    (https?://[^\s<]+[^<.,:;"')\]\s])
    (?!>)
    """,
    re.VERBOSE,
)

def readable_delta(delta: datetime.timedelta) -> str:
    parts: list[str] = str(delta).split(":")
    if parts[0] == "0":
        parts = parts[1:]
    return ":".join(parts)


class SlopifyCog(breadcord.helpers.HTTPModuleCog):
    def __init__(self, module_id: str) -> None:
        super().__init__(module_id)
        self.spotify: SpotifyAPI = SpotifyAPI(settings=cast(breadcord.config.SettingsGroup, self.settings))
        self.invidious: InvidiousAPI = InvidiousAPI(host_url=cast(str, self.settings.invidious_host.value) or None)

        self.embed_track_ctx_menu = discord.app_commands.ContextMenu(
            name="Embed Spotify Track",
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
            raise
            
    async def cog_unload(self) -> None:
        await super().cog_unload()
        self.bot.tree.remove_command(self.embed_track_ctx_menu.name, type=self.embed_track_ctx_menu.type)
        
        await self.spotify.close()
        await self.invidious.close()

    async def handle_track(self, message: discord.Message, track_id: str) -> None:
        self.logger.debug(f"Handling spotify track {track_id} in message {message.id}")
        try:
            track_data = await self.spotify.fetch_track_data(track_id)
        except BadResponseError as error:
            self.logger.warning(f"Failed to fetch track data: {error}")
            await message.reply("Failed to fetch track data", mention_author=False)
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
        yt_id = await self.spotify_to_yt(track_data['id'])
        embed = discord.Embed(
            title=(":underage: " if track_data["explicit"] else "") + track_data["name"],
            url=track_data["external_urls"]["spotify"],
            description="\n".join(line for line in (
                "**Artists:** " + ", ".join([artist["name"] for artist in track_data["artists"]]),
                f"**Album:** {track_data['album']['name']}"
                f" (track {track_data['disc_number']}/{track_data['album']['total_tracks']})"
                if track_data["album"]["total_tracks"] > 1 else None,
                f"**Length:** {readable_delta(datetime.timedelta(seconds=int(track_data['duration_ms'] / 1000)))}",
                f"**Other platforms:** " + ", ".join([
                    f"[YouTube](https://youtu.be/{yt_id})", 
                    f"[YT Music](https://music.youtube.com/watch?v={yt_id})",
                ]),
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
