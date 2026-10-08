#!/usr/bin/env python3
"""
Litefy - a minimal Spotify player for the Linux terminal.

Controls your Spotify account through the Web API and routes playback only to
the local spotifyd Connect receiver (Spotify Premium required).

Setup:
    ./install.sh
    # Add Spotify Web API credentials to .env, then run:
    ./litefy

Layers (top to bottom):
    models   Track, Playback, Row         plain immutable data
    Player   state polling + commands     talks to Spotify, no UI code
    Library  Spotify lists -> Rows        playlists, liked, queue, devices, search
    Browser  list model                   rows, selection, loading flag
    AlbumArt optional cover renderer      Pillow, fetched in the background
    UI       curses view + key bindings   no Spotify calls except via Player
"""
from __future__ import annotations

import curses
import io
import os
import shutil
import sys
import subprocess
import threading
import time
import unicodedata
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from functools import partial
from typing import Callable, Optional

try:
    import spotipy
    from spotipy.oauth2 import SpotifyOAuth
except ImportError:
    sys.exit("Missing dependency. Run: pip install spotipy")

try:
    from PIL import Image
except ImportError:
    Image = None

SCOPE = ("user-read-playback-state user-modify-playback-state "
         "user-read-currently-playing playlist-read-private "
         "playlist-read-collaborative playlist-modify-private playlist-modify-public "
         "user-library-read user-library-modify")
REPEAT_NEXT = {"off": "context", "context": "track", "track": "off"}
REPEAT_NAME = {"off": "off", "context": "all", "track": "one"}
POLL_SECONDS = 2.0
SEEK_MS = 10_000
VOLUME_STEP = 5
LIKED_LIMIT = 300      # how many liked songs to load (newest first)
SAVED_BATCH = 40       # max URIs per 'is saved?' request


def fmt_time(ms: float) -> str:
    s = max(0, int(ms // 1000))
    return f"{s // 60}:{s % 60:02d}"


def clip(text: str, width: int) -> str:
    text = text or ""
    if width <= 0:
        return ""
    return text if len(text) <= width else text[: width - 1] + "…"


def terminal_text(text: object) -> str:
    """Keep dynamic text on one terminal row and strip control characters."""
    normalized = []
    for char in str(text or ""):
        if char.isspace() or unicodedata.category(char).startswith("C"):
            normalized.append(" ")
        else:
            normalized.append(char)
    return "".join(normalized)


# ============================== models ==============================

@dataclass(frozen=True)
class Track:
    uri: str
    name: str
    artists: str
    album: str
    duration_ms: int
    art_url: Optional[str]
    artist_id: Optional[str] = None

    @classmethod
    def from_api(cls, d: dict) -> "Track":
        album = d.get("album") or {}
        images = album.get("images") or d.get("images") or []
        # Spotify usually returns images largest first. Explicitly choose the
        # highest-resolution cover so the terminal renderer has enough detail.
        best = max(images, key=lambda im: (im.get("width") or 0) * (im.get("height") or 0),
                   default=None)
        art = best.get("url") if best else None
        artist_items = d.get("artists", [])
        artists = ", ".join(a["name"] for a in artist_items)
        return cls(d.get("uri", ""), d.get("name", "?"),
                   artists or (d.get("show") or {}).get("name", ""),
                   album.get("name", ""), d.get("duration_ms", 0), art,
                   artist_items[0].get("id") if artist_items else None)


@dataclass(frozen=True)
class Playback:
    track: Optional[Track]
    is_playing: bool
    progress_ms: int
    shuffle: bool
    repeat: str
    volume: Optional[int]
    device_id: str
    device_name: str
    context_uri: Optional[str]
    fetched_at: float

    @classmethod
    def from_api(cls, d: Optional[dict]) -> Optional["Playback"]:
        if not d:
            return None
        dev = d.get("device") or {}
        return cls(
            track=Track.from_api(d["item"]) if d.get("item") else None,
            is_playing=bool(d.get("is_playing")),
            progress_ms=d.get("progress_ms") or 0,
            shuffle=bool(d.get("shuffle_state")),
            repeat=d.get("repeat_state", "off"),
            volume=dev.get("volume_percent"),
            device_id=dev.get("id", ""),
            device_name=dev.get("name", ""),
            context_uri=(d.get("context") or {}).get("uri"),
            fetched_at=time.time(),
        )

    def position_ms(self) -> float:
        """Interpolated position, so the bar moves between polls."""
        pos = self.progress_ms
        if self.is_playing:
            pos += (time.time() - self.fetched_at) * 1000
        return min(pos, self.track.duration_ms) if self.track else 0


@dataclass(frozen=True)
class Row:
    label: str
    detail: str
    right: str
    uri: str
    play: Callable[[], None]
    enqueue: Optional[Callable[[], None]] = None
    children: Optional[Callable[[], list]] = None   # makes the row openable
    image_url: Optional[str] = None
    artist_id: Optional[str] = None


@dataclass(frozen=True)
class SearchResults:
    songs: list[Row]
    artists: list[Row]


class Notifier:
    """One short status message with an expiry time."""
    def __init__(self):
        self._text, self._until = "", 0.0
        self._lock = threading.Lock()

    def say(self, text: str, seconds: float = 4):
        # Replace the previous notice atomically instead of accumulating
        # messages from background requests or the polling thread.
        clean = " ".join(terminal_text(text).split())
        with self._lock:
            self._text = clip(clean, 110)
            self._until = time.time() + seconds

    def current(self) -> str:
        with self._lock:
            return self._text if time.time() < self._until else ""


def spotifyd_device_name() -> str:
    """Read the Connect name spotifyd uses, defaulting to Litefy."""
    override = os.environ.get("LITEFY_SPOTIFYD_DEVICE_NAME")
    if override:
        return override
    config_dir = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
    config_path = os.path.join(config_dir, "spotifyd", "spotifyd.conf")
    try:
        with open(config_path, encoding="utf-8") as config:
            for line in config:
                line = line.strip()
                if line.startswith("device_name") and "=" in line:
                    _, value = line.split("=", 1)
                    return value.strip().strip("\"'") or "Litefy"
    except OSError:
        pass
    return "Litefy"


# ============================== player ==============================

class Player:
    """Owns the Spotify client, the latest Playback snapshot and all commands.

    `state` is immutable and replaced atomically, so the UI can read it
    without locking. Commands update it optimistically, then poll for truth.
    """

    def __init__(self, sp: spotipy.Spotify, notifier: Notifier):
        self.sp, self.notify = sp, notifier
        self.local_device_name = spotifyd_device_name()
        self.state: Optional[Playback] = None
        self.saved: dict[str, bool] = {}     # track uri -> in Your Library?
        self._probe_failed = ""
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=4)
        self._wake = threading.Event()
        self._stopped = False
        self._local_device_process = None
        self._device_lock = threading.Lock()
        self.stop_error = None

    # --- lifecycle ---
    def start(self):
        threading.Thread(target=self._poll_loop, daemon=True).start()
        self._pool.submit(self._activate_spotifyd)

    def stop(self, stop_spotifyd: bool = False):
        self._stopped = True
        self._wake.set()
        self._pool.shutdown(wait=False, cancel_futures=True)
        if not stop_spotifyd:
            return

        # Pause the local receiver first so playback stops cleanly even when
        # spotifyd was started outside Litefy. Only terminate a process that
        # this Player instance launched itself.
        with self._device_lock:
            try:
                local_devices = self._spotifyd_devices()
                if local_devices:
                    self.sp.pause_playback(device_id=local_devices[0]["id"])
            except Exception:
                self.stop_error = "Could not pause Spotifyd playback; it may still be playing."

            process = self._local_device_process
            if process and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()

    def _poll_loop(self):
        while not self._stopped:
            try:
                state = Playback.from_api(self.sp.current_playback())
                if state and state.device_name != self.local_device_name:
                    state = None
                with self._lock:
                    self.state = state
                self._probe_saved(state)
            except Exception as e:
                self.notify.say(f"sync failed: {str(e)[:60]}")
            self._wake.wait(POLL_SECONDS)
            self._wake.clear()

    # --- plumbing ---
    def _patch(self, **changes):
        with self._lock:
            if self.state:
                self.state = replace(self.state, **changes)

    def _send(self, fn, *args, **kwargs):
        """Run a playback command only after Litefy's local spotifyd is active."""
        def job():
            try:
                local = self._ensure_spotifyd_device()
                if not local:
                    raise RuntimeError("Spotifyd is unavailable. Run ./setup_spotifyd.sh")
                if getattr(fn, "__name__", "") == "transfer_playback":
                    # Device selection is constrained to the configured local
                    # spotifyd receiver; never transfer Litefy to another device.
                    if args and args[0] != local["id"]:
                        raise RuntimeError("Litefy only supports its local Spotifyd device")
                    fn(local["id"], force_play=kwargs.get("force_play", True))
                else:
                    if not local.get("is_active"):
                        self.sp.transfer_playback(local["id"], force_play=False)
                        time.sleep(0.8)
                    try:
                        fn(*args, **kwargs)
                    except spotipy.SpotifyException as e:
                        if e.http_status != 404:
                            raise
                        self.sp.transfer_playback(local["id"], force_play=False)
                        time.sleep(0.8)
                        fn(*args, **kwargs)
            except Exception as e:
                self.notify.say(f"error: {str(e)[:70]}", 8)
            time.sleep(0.4)
            self._wake.set()
        self._pool.submit(job)

    def _spotifyd_devices(self) -> list:
        return [d for d in self.sp.devices().get("devices", [])
                if d.get("name") == self.local_device_name]

    def _ensure_spotifyd_device(self) -> Optional[dict]:
        """Find or start the configured local spotifyd Connect receiver."""
        with self._device_lock:
            if self._stopped:
                return None
            try:
                devices = self._spotifyd_devices()
            except Exception:
                devices = []
            if devices:
                return devices[0]
            devices = self._start_local_device()
            return devices[0] if devices else None

    def _activate_spotifyd(self):
        try:
            local = self._ensure_spotifyd_device()
            if not local:
                self.notify.say("Spotifyd not found. Run ./setup_spotifyd.sh", 8)
                return
            if not local.get("is_active"):
                self.sp.transfer_playback(local["id"], force_play=False)
                self._wake.set()
        except Exception as e:
            self.notify.say(f"Spotifyd startup failed: {str(e)[:55]}", 8)

    def _start_local_device(self) -> list:
        """Start spotifyd when its configured local Connect device is absent."""
        if not self._local_device_process or self._local_device_process.poll() is not None:
            executable = shutil.which("spotifyd")
            local_bin = os.path.expanduser("~/.local/bin/spotifyd")
            if not executable and os.path.isfile(local_bin) and os.access(local_bin, os.X_OK):
                executable = local_bin
            if not executable:
                return []
            try:
                self._local_device_process = subprocess.Popen(
                    [executable, "--no-daemon"],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, start_new_session=True,
                )
            except OSError:
                return []

        # Spotify Connect discovery takes a moment after spotifyd starts.
        for _ in range(15):
            if self._stopped:
                break
            time.sleep(1)
            if self._stopped:
                break
            try:
                devices = self.sp.devices().get("devices", [])
            except Exception:
                devices = []
            local = [d for d in devices if d.get("name") == self.local_device_name]
            if local:
                return local
            if self._local_device_process.poll() is not None:
                break
        return []

    # --- commands ---
    def toggle(self):
        if self.state and self.state.is_playing:
            self._patch(is_playing=False)
            self._send(self.sp.pause_playback)
        else:
            self._send(self.sp.start_playback)

    def next(self):
        self._send(self.sp.next_track)

    def previous(self):
        self._send(self.sp.previous_track)

    def seek_by(self, delta_ms: int):
        st = self.state
        if st and st.track:
            target = max(0, min(st.track.duration_ms - 1000, st.position_ms() + delta_ms))
            self._send(self.sp.seek_track, int(target))

    def change_volume(self, delta: int):
        st = self.state
        if st and st.volume is not None:
            vol = max(0, min(100, st.volume + delta))
            self._patch(volume=vol)
            self._send(self.sp.volume, vol)

    def toggle_shuffle(self):
        if self.state:
            on = not self.state.shuffle
            self._patch(shuffle=on)
            self._send(self.sp.shuffle, on)

    def cycle_repeat(self):
        if self.state:
            mode = REPEAT_NEXT.get(self.state.repeat, "off")
            self._patch(repeat=mode)
            self._send(self.sp.repeat, mode)

    def play(self, context_uri: str = None, uris: list = None, offset_uri: str = None):
        offset = {"uri": offset_uri} if offset_uri else None
        self._send(self.sp.start_playback, context_uri=context_uri, uris=uris, offset=offset)

    def enqueue(self, uri: str):
        self._send(self.sp.add_to_queue, uri)

    def add_to_playlist(self, playlist_id: str, uri: str):
        """Add a song to one of the user's playlists."""
        if not uri.startswith("spotify:track:"):
            self.notify.say("Only songs can be added to playlists")
            return

        def job():
            try:
                self.sp.playlist_add_items(playlist_id, [uri])
            except AttributeError:  # compatibility with older Spotipy versions
                self.sp.user_playlist_add_tracks(None, playlist_id, [uri])
            self.notify.say("Added to playlist", 3)
        self._background(job)

    def transfer(self, device_id: str):
        self._send(self.sp.transfer_playback, device_id, force_play=True)

    # --- your library (liked songs) ---
    def _background(self, job: Callable[[], None]):
        def guarded():
            try:
                job()
            except Exception as e:
                self.notify.say(f"error: {str(e)[:70]}", 8)
        self._pool.submit(guarded)

    def _saved_flags(self, uris: list) -> list:
        try:
            return self.sp._get("me/library/contains", uris=",".join(uris))
        except spotipy.SpotifyException as e:
            if e.http_status != 404:
                raise
            return self.sp.current_user_saved_tracks_contains([u.split(":")[-1] for u in uris])

    def _library_write(self, save: bool, uri: str):
        """Save/remove through the consolidated /me/library endpoint (Feb 2026)."""
        call = self.sp._put if save else self.sp._delete
        try:
            try:
                call("me/library", uris=uri)                       # URIs as query parameter
            except spotipy.SpotifyException as e:
                if e.http_status != 400:
                    raise
                call("me/library", payload={"uris": [uri]})        # or as JSON body
        except spotipy.SpotifyException as e:
            if e.http_status != 404:
                raise
            legacy = (self.sp.current_user_saved_tracks_add if save
                      else self.sp.current_user_saved_tracks_delete)
            legacy([uri.split(":")[-1]])

    def set_saved(self, uri: str, save: Optional[bool] = None):
        """Add (True), remove (False) or toggle (None) a song in Your Library."""
        if not uri.startswith("spotify:track:"):
            self.notify.say("Only songs can be saved")
            return

        def job():
            target = save
            if target is None:
                known = self.saved.get(uri)
                if known is None:
                    known = bool(self._saved_flags([uri])[0])
                target = not known
            self.saved[uri] = target              # show the heart right away
            try:
                self._library_write(target, uri)
            except Exception:
                self.saved[uri] = not target      # undo on failure
                raise
            self.notify.say("♥ Added to your library" if target else "Removed from your library", 3)
        self._background(job)

    def toggle_like(self):
        st = self.state
        if st and st.track:
            self.set_saved(st.track.uri)
        else:
            self.notify.say("Nothing playing")

    def prefetch_saved(self, uris: list):
        """Learn which of these songs are saved, so the list can show hearts."""
        todo = [u for u in uris if u.startswith("spotify:track:") and u not in self.saved]

        def job():
            try:
                for i in range(0, len(todo), SAVED_BATCH):
                    chunk = todo[i:i + SAVED_BATCH]
                    self.saved.update(zip(chunk, map(bool, self._saved_flags(chunk))))
            except Exception:
                pass
        self._pool.submit(job)

    def _probe_saved(self, state: Optional[Playback]):
        t = state.track if state else None
        if (t and t.uri.startswith("spotify:track:") and t.uri not in self.saved
                and t.uri != self._probe_failed):
            try:
                self.saved[t.uri] = bool(self._saved_flags([t.uri])[0])
            except Exception:
                self._probe_failed = t.uri        # don't retry on every poll


# ============================== library ==============================

class Library:
    """Turns Spotify lists into Rows. Methods block, so call them off the UI thread."""

    def __init__(self, player: Player):
        self.player, self.sp = player, player.sp

    def _track_row(self, t: Track, play: Callable) -> Row:
        detail = f"{t.artists} · {t.album}" if t.album else t.artists
        return Row(t.name, detail, fmt_time(t.duration_ms), t.uri, play,
                   partial(self.player.enqueue, t.uri), image_url=t.art_url,
                   artist_id=t.artist_id)

    def _from_tracks(self, items: list, sequence: bool, prefetch: bool = True) -> list[Row]:
        tracks = [Track.from_api(t) for t in items if t and t.get("uri")]
        uris = [t.uri for t in tracks]
        if prefetch:
            self.player.prefetch_saved(uris)
        return [self._track_row(
                    t, partial(self.player.play,
                               uris=uris[i:i + 100] if sequence else [t.uri]))  # API max 100
                for i, t in enumerate(tracks)]

    def playlists(self) -> list[Row]:
        rows = []
        for p in self.sp.current_user_playlists(limit=50)["items"]:
            if not p:
                continue
            total = (p.get("items") or p.get("tracks") or {}).get("total", "")
            rows.append(Row(p["name"], (p.get("owner") or {}).get("display_name", ""),
                            f"{total} tracks" if total != "" else "", p["uri"],
                            partial(self.player.play, context_uri=p["uri"]),
                            children=partial(self.playlist_tracks, p["uri"])))
        return rows

    def playlist_destinations(self, track_uri: str) -> list[Row]:
        """List playlists the current user owns or can edit for adding a song."""
        user_id = self.sp.current_user()["id"]
        rows = []
        offset = 0
        while True:
            page = self.sp.current_user_playlists(limit=50, offset=offset)
            for p in page.get("items", []):
                if not p:
                    continue
                owner_id = (p.get("owner") or {}).get("id")
                if owner_id != user_id and not p.get("collaborative"):
                    continue
                rows.append(Row(
                    p["name"], "Add this song here", "playlist", p["uri"],
                    partial(self.player.add_to_playlist, p["id"], track_uri)))
            if not page.get("next"):
                break
            offset += len(page.get("items", [])) or 50
        return rows

    def _playlist_page(self, playlist_id: str, offset: int) -> dict:
        try:   # Spotify renamed /tracks to /items in Feb 2026
            return self.sp._get(f"playlists/{playlist_id}/items", limit=50, offset=offset)
        except spotipy.SpotifyException as e:
            if e.http_status != 404:
                raise
            return self.sp.playlist_items(playlist_id, limit=50, offset=offset)

    def playlist_tracks(self, playlist_uri: str) -> list[Row]:
        playlist_id = playlist_uri.split(":")[-1]
        tracks = []
        for page in range(4):                       # up to 200 tracks
            data = self._playlist_page(playlist_id, page * 50)
            for entry in data.get("items", []):
                t = entry.get("item") or entry.get("track")   # 'item' since Feb 2026
                if t and t.get("uri"):
                    tracks.append((entry.get("added_at") or "", Track.from_api(t)))
            if not data.get("next"):
                break
        if not tracks:
            raise RuntimeError("No tracks available for this playlist")
        tracks.sort(key=lambda item: item[0], reverse=True)
        return [self._track_row(t, partial(self.player.play, context_uri=playlist_uri,
                                           offset_uri=t.uri)) for _, t in tracks]

    def liked(self) -> list[Row]:
        saved = []
        for page in range(LIKED_LIMIT // 50):
            batch = self.sp.current_user_saved_tracks(limit=50, offset=page * 50)
            saved += batch.get("items", [])
            if not batch.get("next"):
                break
        saved.sort(key=lambda s: s.get("added_at", ""), reverse=True)    # newest first
        rows = self._from_tracks([s.get("track") or s.get("item") for s in saved], sequence=True,
                                prefetch=False)       # we already know they're saved
        self.player.saved.update({r.uri: True for r in rows})
        return rows

    def queue(self) -> list[Row]:
        return self._from_tracks((self.sp.queue() or {}).get("queue", [])[:50], sequence=False)

    def search(self, query: str) -> SearchResults:
        found = []
        artists = {}
        for offset in (0, 10, 20):                  # Spotify caps search at 10 per request
            result = self.sp.search(q=query, type="track,artist", limit=10, offset=offset)
            page = result.get("tracks", {}).get("items", [])
            found.extend(page)
            for artist in result.get("artists", {}).get("items", []):
                if artist and artist.get("id"):
                    artists.setdefault(artist["id"], artist)
            if len(page) < 10 and len(result.get("artists", {}).get("items", [])) < 10:
                break
        songs = self._from_tracks(found, sequence=False)
        artist_rows = [
            Row(a["name"], "Artist", "",
                a["uri"], lambda: None,
                children=partial(self.artist_profile, a["id"]),
                image_url=(a.get("images") or [{}])[0].get("url"), artist_id=a["id"])
            for a in artists.values() if a.get("uri")
        ]
        return SearchResults(songs=songs, artists=artist_rows)

    def artist_profile(self, artist_id: str) -> list[Row]:
        """Show the artist's released albums and popular songs as sublists."""
        artist = self.sp.artist(artist_id)
        name = artist.get("name", "Artist")
        return [
            Row("Albums", "Released albums, singles and compilations", "", artist.get("uri", ""),
                lambda: None, children=partial(self.artist_albums, artist_id)),
            Row("Popular songs", f"Top tracks by {name}", "", artist.get("uri", ""),
                lambda: None, children=partial(self.artist_songs, artist_id)),
        ]

    def artist_albums(self, artist_id: str) -> list[Row]:
        albums = []
        offset = 0
        while True:
            page = self.sp.artist_albums(artist_id, include_groups="album,single,compilation",
                                         limit=10, offset=offset)
            albums.extend(page.get("items", []))
            if not page.get("next"):
                break
            offset += len(page.get("items", [])) or 10
        rows = []
        seen = set()
        for album in albums:
            album_id = album.get("id")
            if not album_id or album_id in seen:
                continue
            seen.add(album_id)
            rows.append(Row(
                album.get("name", "Untitled album"),
                f"{album.get('album_type', 'album').title()} · {album.get('release_date', '')}",
                str((album.get("total_tracks") or "")) + " tracks", album.get("uri", ""),
                partial(self.player.play, context_uri=album.get("uri")),
                children=partial(self.album_songs, album_id, album.get("uri", "")),
                image_url=(album.get("images") or [{}])[0].get("url"),
                artist_id=(album.get("artists") or [{}])[0].get("id")))
        return rows

    def album_songs(self, album_id: str, album_uri: str) -> list[Row]:
        tracks = self.sp.album_tracks(album_id, limit=50).get("items", [])
        # Album track responses omit album metadata, but playback can target the
        # album context and the selected track offset.
        rows = []
        for item in tracks:
            if not item or not item.get("uri"):
                continue
            track = Track.from_api(item)
            rows.append(self._track_row(
                track, partial(self.player.play, context_uri=album_uri, offset_uri=track.uri)))
        self.player.prefetch_saved([r.uri for r in rows])
        return rows

    def artist_songs(self, artist_id: str) -> list[Row]:
        try:
            items = self.sp.artist_top_tracks(artist_id).get("tracks", [])
            return self._from_tracks(items, sequence=True)
        except spotipy.SpotifyException:
            # Spotify has deprecated the artist top-tracks endpoint. Build a
            # useful songs list from the artist's released albums if it is no
            # longer available to this app/account.
            songs, seen = [], set()
            for album in self.artist_albums(artist_id):
                album_id = album.uri.rsplit(":", 1)[-1]
                album_tracks = self.sp.album_tracks(album_id, limit=50).get("items", [])
                for item in album_tracks:
                    if not item or not item.get("uri") or item["uri"] in seen:
                        continue
                    seen.add(item["uri"])
                    track = replace(Track.from_api(item), album=album.label)
                    songs.append(self._track_row(
                        track, partial(self.player.play, context_uri=album.uri,
                                       offset_uri=track.uri)))
            self.player.prefetch_saved([row.uri for row in songs])
            if not songs:
                raise RuntimeError("Spotify returned no songs for this artist")
            return songs

    def devices(self) -> list[Row]:
        return [Row(d["name"], "Spotifyd · local device",
                    "active" if d["is_active"] else "", d["id"],
                    partial(self.player.transfer, d["id"]))
                for d in self.sp.devices().get("devices", [])
                if d.get("name") == self.player.local_device_name]


# ============================== browser ==============================

class Browser:
    """The list shown under the player: rows, selection and loading state."""

    def __init__(self, notifier: Notifier):
        self.notify = notifier
        self.title, self.rows, self.sel = "", [], 0
        self.loading, self.active = False, False
        self._pool = ThreadPoolExecutor(max_workers=2)
        self._generation = 0
        self._stack: list = []          # previous (title, rows, sel) when drilled in
        self.tabs: list[tuple[str, list[Row]]] = []
        self.tab_index = 0

    def load(self, title: str, fetch: Callable[[], list]):
        """Open a top-level list (clears any drill-down history)."""
        self._stack.clear()
        self._start(title, fetch)

    def descend(self, title: str, fetch: Callable[[], list]):
        """Open a list inside the current one (e.g. a playlist's tracks)."""
        self._stack.append((self.title, self.rows, self.sel, self.tabs, self.tab_index))
        self._start(f"{self.title} › {title}", fetch)

    def back(self):
        self._generation += 1
        if self._stack:
            self.title, self.rows, self.sel, self.tabs, self.tab_index = self._stack.pop()
            self.loading = False
        else:
            self.active = self.loading = False

    def _start(self, title: str, fetch: Callable[[], list]):
        self._generation += 1
        gen = self._generation
        self.title, self.rows, self.sel = title, [], 0
        self.tabs, self.tab_index = [], 0
        self.loading = self.active = True

        def job():
            try:
                rows = fetch()
                if gen == self._generation:      # ignore stale results
                    if isinstance(rows, SearchResults):
                        self.tabs = [("Songs", rows.songs), ("Artists", rows.artists)]
                        self.rows = self.tabs[0][1]
                    else:
                        self.rows = rows
            except Exception as e:
                if gen == self._generation:
                    if isinstance(e, spotipy.SpotifyException):
                        message = f"Spotify {e.http_status}: {e.msg}"
                    else:
                        message = str(e)
                    self.notify.say(f"Artist/list error: {message}", 8)
            if gen == self._generation:
                self.loading = False
        self._pool.submit(job)

    def close(self):
        self._generation += 1
        self._stack.clear()
        self.active = self.loading = False

    def shutdown(self):
        self._pool.shutdown(wait=False, cancel_futures=True)

    def move(self, delta: int):
        if self.rows:
            self.sel = max(0, min(len(self.rows) - 1, self.sel + delta))

    def switch_tab(self, delta: int):
        if not self.tabs:
            return False
        self.tab_index = (self.tab_index + delta) % len(self.tabs)
        self.rows = self.tabs[self.tab_index][1]
        self.sel = 0
        return True

    def remove_row(self, uri: str):
        self.rows = [r for r in self.rows if r.uri != uri]
        self.sel = min(self.sel, max(0, len(self.rows) - 1))

    def selected(self) -> Optional[Row]:
        return self.rows[self.sel] if self.rows else None


# ============================== album art ==============================

ART_ROWS, ART_COLS = 9, 18


def _xterm256(rgb) -> int:
    r, g, b = rgb[:3]
    if max(r, g, b) - min(r, g, b) < 10:
        return 232 + min(23, int((r + g + b) / 3 / 256 * 24))
    return 16 + 36 * round(r / 255 * 5) + 6 * round(g / 255 * 5) + round(b / 255 * 5)


class AlbumArt:
    """Downloads covers in the background and converts them to 256-color cells."""
    enabled = Image is not None

    def __init__(self):
        self._cache: dict = {}
        self._busy: set = set()

    def get(self, url: Optional[str], cols: int = ART_COLS, rows: int = ART_ROWS):
        if not (self.enabled and url):
            return None
        key = (url, cols, rows)
        if key not in self._cache and key not in self._busy:
            self._busy.add(key)
            threading.Thread(target=self._fetch, args=(key, url, cols, rows), daemon=True).start()
        return self._cache.get(key) or None

    def _fetch(self, key, url: str, cols: int, rows: int):
        grid = None
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Litefy"})
            data = urllib.request.urlopen(req, timeout=8).read()
            img = Image.open(io.BytesIO(data)).convert("RGB")
            # Fit the complete source inside the preview dimensions without
            # changing its aspect ratio; center any unused area on black.
            img.thumbnail((cols, rows * 2), Image.LANCZOS)
            canvas = Image.new("RGB", (cols, rows * 2), (0, 0, 0))
            canvas.paste(img, ((cols - img.width) // 2,
                               (rows * 2 - img.height) // 2))
            img = canvas
            grid = [[_xterm256(img.getpixel((c, r))) for c in range(cols)]
                    for r in range(rows * 2)]
        except Exception:
            pass
        self._cache[key] = grid
        self._busy.discard(key)


# ============================== UI ==============================

HINT_HOME = "q exit (music on) · Q stop+exit · space play · o artist · n/p skip"
HINT_HOME_LIB = "←/→ seek · +/- vol · s/r · l/f lib · / search · u queue · d devices · h/m"
HINT_LIST = "q exit (music on) · Q stop+exit · ↑/↓ move · ←/→ tabs · enter open/play · P all · a queue"
LIBRARY_KEYS = "l playlists    f liked    / search    u queue    d devices"
MIN_H, MIN_W = 18, 60


class UI:
    def __init__(self, player: Player, library: Library, notifier: Notifier):
        self.player, self.library, self.notify = player, library, notifier
        self.browser = Browser(notifier)
        self.art = AlbumArt()
        self.running = True
        self.stop_spotifyd_on_exit = False
        self.scr = None
        self._pairs: dict = {}
        self._artist_images: dict[str, Optional[str]] = {}
        self._artist_image_loading: set[str] = set()
        self._artist_image_pool = ThreadPoolExecutor(max_workers=1)

        p = player
        self.global_keys = {
            ord("q"): self.quit, ord("Q"): self.quit_and_stop,
            ord(" "): p.toggle, ord("n"): p.next, ord("p"): p.previous,
            curses.KEY_RIGHT: lambda: p.seek_by(SEEK_MS),
            curses.KEY_LEFT: lambda: p.seek_by(-SEEK_MS),
            ord("+"): lambda: p.change_volume(VOLUME_STEP),
            ord("="): lambda: p.change_volume(VOLUME_STEP),
            ord("-"): lambda: p.change_volume(-VOLUME_STEP),
            ord("s"): p.toggle_shuffle, ord("r"): p.cycle_repeat, ord("h"): p.toggle_like,
            ord("o"): self.open_current_artist,
            ord("l"): lambda: self.open("Playlists", library.playlists),
            ord("f"): lambda: self.open("Liked songs · newest first", library.liked),
            ord("u"): lambda: self.open("Up next", library.queue),
            ord("d"): lambda: self.open("Devices", library.devices),
            ord("/"): self.search,
            ord("m"): self.add_song_to_playlist,
        }
        b = self.browser
        self.list_keys = {
            curses.KEY_DOWN: lambda: b.move(1), ord("j"): lambda: b.move(1),
            curses.KEY_UP: lambda: b.move(-1), ord("k"): lambda: b.move(-1),
            curses.KEY_LEFT: lambda: self.switch_search_tab(-1),
            curses.KEY_RIGHT: lambda: self.switch_search_tab(1),
            curses.KEY_NPAGE: lambda: b.move(10), curses.KEY_PPAGE: lambda: b.move(-10),
            10: self.activate, 13: self.activate, curses.KEY_ENTER: self.activate,
            ord("P"): self.play_selected,
            ord("a"): self.enqueue_selected,
            ord("A"): lambda: self.save_selected(True),
            ord("m"): self.add_song_to_playlist,
            ord("X"): lambda: self.save_selected(False),
            27: b.back, 127: b.back, curses.KEY_BACKSPACE: b.back,
        }

    # --- actions ---
    def quit(self):
        self.running = False

    def quit_and_stop(self):
        self.stop_spotifyd_on_exit = True
        self.running = False

    def open(self, title: str, fetch: Callable):
        self.browser.load(title, fetch)

    def search(self):
        query = self.prompt("search ▸ ")
        if query:
            self.open(f"Search: {query}", partial(self.library.search, query))

    def open_current_artist(self):
        state = self.player.state
        track = state.track if state else None
        if not track or not track.artist_id:
            self.notify.say("No current artist profile available")
            return
        artist_name = track.artists.split(",", 1)[0] or "Artist"
        self.open(f"Artist: {artist_name}",
                  partial(self.library.artist_profile, track.artist_id))

    def activate(self):
        """Enter: open playlists, play everything else."""
        row = self.browser.selected()
        if row and row.children:
            self.browser.descend(row.label, row.children)
        else:
            self.play_selected()

    def switch_search_tab(self, delta: int):
        if not self.browser.switch_tab(delta):
            self.player.seek_by(-SEEK_MS if delta < 0 else SEEK_MS)

    def play_selected(self):
        row = self.browser.selected()
        if row:
            row.play()
            self.notify.say("Playing…", 2)

    def save_selected(self, save: bool):
        """A / X: add the selected song to, or remove it from, Your Library."""
        row = self.browser.selected()
        if not row:
            return
        self.player.set_saved(row.uri, save)
        if not save and self.browser.title.startswith("Liked"):
            self.browser.remove_row(row.uri)

    def add_song_to_playlist(self):
        row = self.browser.selected() if self.browser.active else None
        st = self.player.state
        track = row.uri if row else (st.track.uri if st and st.track else None)
        if not track or not track.startswith("spotify:track:"):
            self.notify.say("Select a song or start playback first")
            return
        self.open("Choose a playlist", partial(self.library.playlist_destinations, track))

    def enqueue_selected(self):
        row = self.browser.selected()
        if row and row.enqueue:
            row.enqueue()
            self.notify.say("Added to queue", 2)

    # --- drawing helpers ---
    def put(self, y: int, x: int, text: str, attr: int = 0):
        h, w = self.scr.getmaxyx()
        if 0 <= y < h and 0 <= x < w and text:
            try:
                # API messages and remote metadata may contain newlines or
                # terminal control characters; never let them move the cursor
                # into another widget row.
                safe = terminal_text(text)
                self.scr.addstr(y, x, safe[: w - x], attr)
            except curses.error:
                pass

    def color(self, fg: int, bg: int) -> int:
        key = (fg, bg)
        if key not in self._pairs:
            n = len(self._pairs) + 10
            if n >= curses.COLOR_PAIRS:
                return 0
            curses.init_pair(n, fg, bg)
            self._pairs[key] = n
        return curses.color_pair(self._pairs[key])

    # --- drawing ---
    def draw(self):
        self.scr.erase()
        h, w = self.scr.getmaxyx()
        if h < MIN_H or w < MIN_W:
            self.put(0, 0, f"Terminal too small (need {MIN_W}x{MIN_H})")
        else:
            self.draw_topline(w)
            text_x = self.draw_art_slot(w)
            preview_left = self.draw_preview_art(w)
            sep = self.draw_now_playing(text_x, preview_left)
            self.draw_list(sep, h, w)
            self.draw_footer(h)
        self.scr.refresh()

    def draw_topline(self, w: int):
        """Give the player a clear identity and show the current connection."""
        accent, dim = curses.color_pair(1), curses.A_DIM
        self.put(0, 2, "♫ LITEFY", accent | curses.A_BOLD)
        st = self.player.state
        if st and st.device_name:
            playback = "PLAYING" if st.is_playing else "PAUSED"
            status = f"{playback} · {st.device_name}"
        else:
            status = "WAITING FOR SPOTIFY"
        status = clip(status, w - 18)
        self.put(0, max(12, w - len(status) - 3), status, dim)

    def draw_art_slot(self, w: int) -> int:
        """Draws the cover (if available) and returns the x where text starts."""
        if not (self.art.enabled and w >= 70 and curses.COLORS >= 256):
            return 2
        st = self.player.state
        grid = self.art.get(st.track.art_url) if st and st.track else None
        if grid:
            for r in range(ART_ROWS):
                for c in range(ART_COLS):
                    self.put(1 + r, 2 + c, "▀", self.color(grid[2 * r][c], grid[2 * r + 1][c]))
        return 2 + ART_COLS + 3

    def _artist_image_url(self, artist_id: Optional[str]) -> Optional[str]:
        if not artist_id:
            return None
        if artist_id not in self._artist_images and artist_id not in self._artist_image_loading:
            self._artist_image_loading.add(artist_id)

            def fetch():
                url = None
                try:
                    images = self.library.sp.artist(artist_id).get("images") or []
                    url = images[0].get("url") if images else None
                except Exception:
                    pass
                self._artist_images[artist_id] = url
                self._artist_image_loading.discard(artist_id)

            self._artist_image_pool.submit(fetch)
        return self._artist_images.get(artist_id)

    def draw_preview_art(self, w: int) -> int:
        """Show selected row artwork, falling back to the current artist image."""
        cols = min(ART_COLS, max(10, ((w - 50) // 2) * 2))
        rows = cols // 2
        box_w, box_x = cols + 2, w - cols - 4
        row = self.browser.selected() if self.browser.active and not self.browser.loading else None
        url = row.image_url if row else None
        artist_id = row.artist_id if row else None
        state = self.player.state
        if not url:
            artist_id = artist_id or (state.track.artist_id if state and state.track else None)
            url = self._artist_image_url(artist_id)

        dim = curses.A_DIM
        self.put(1, box_x, "┌" + "─" * cols + "┐", dim)
        grid = self.art.get(url, cols=cols, rows=rows)
        for r in range(rows):
            if grid:
                for c in range(cols):
                    attr = self.color(grid[2 * r][c], grid[2 * r + 1][c])
                    self.put(2 + r, box_x + 1 + c, "▀", attr)
            else:
                self.put(2 + r, box_x + 1, "·" * cols, dim)
            self.put(2 + r, box_x, "│", dim)
            self.put(2 + r, box_x + box_w - 1, "│", dim)
        self.put(rows + 2, box_x, "└" + "─" * cols + "┘", dim)
        return box_x - 2

    def draw_now_playing(self, x: int, right_edge: int) -> int:
        accent, dim = curses.color_pair(1), curses.A_DIM
        st = self.player.state
        self.put(1, x, "NOW PLAYING", accent | curses.A_BOLD)
        if not (st and st.track):
            self.put(3, x, "Ready when you are", curses.A_BOLD)
            self.put(4, x, clip("Choose a library below, or press space", right_edge - x), dim)
            self.put(5, x, "to resume Spotify playback.", dim)
            return ART_ROWS + 3
        t = st.track
        title = clip(t.name, right_edge - x - 3)
        self.put(3, x, title, curses.A_BOLD)
        if self.player.saved.get(t.uri):
            self.put(3, x + len(title) + 1, "♥", accent | curses.A_BOLD)
        self.put(4, x, clip(t.artists, right_edge - x), accent)
        self.put(5, x, clip(t.album, right_edge - x), dim)

        pos = st.position_ms()
        left, right_time = fmt_time(pos), fmt_time(t.duration_ms)
        bar_w = max(1, right_edge - x - len(left) - len(right_time) - 4)
        filled = int(bar_w * pos / t.duration_ms) if t.duration_ms else 0
        self.put(7, x, left, dim)
        self.put(7, x + len(left) + 1, "━" * filled, accent | curses.A_BOLD)
        self.put(7, x + len(left) + 1 + filled, "─" * (bar_w - filled), dim)
        self.put(7, x + len(left) + bar_w + 2, right_time, dim)

        parts = ["▶ playing" if st.is_playing else "⏸ paused"]
        if st.volume is not None:
            parts.append(f"vol {st.volume}%")
        if st.shuffle:
            parts.append("shuffle")
        if st.repeat != "off":
            parts.append(f"repeat {REPEAT_NAME.get(st.repeat, st.repeat)}")
        if st.device_name:
            parts.append(st.device_name)
        self.put(8, x, clip("  ·  ".join(parts), right_edge - x), dim)
        return ART_ROWS + 3

    def draw_list(self, sep: int, h: int, w: int):
        accent, dim = curses.color_pair(1), curses.A_DIM
        b = self.browser
        self.put(sep, 2, "─" * (w - 4), dim)
        if not b.active:
            self.put(sep + 2, 4, LIBRARY_KEYS, accent)
            return
        count = f"  {b.sel + 1}/{len(b.rows)}" if b.rows else ""
        tabs = ""
        if b.tabs:
            tabs = "  " + "  ".join(
                f"[{name}]" if i == b.tab_index else name
                for i, (name, _) in enumerate(b.tabs)) + "  ←/→"
        self.put(sep, 3, f" {b.title}{tabs}{count} ", accent | curses.A_BOLD)

        if b.loading:
            self.put(sep + 2, 4, "Loading…", dim)
            return
        if not b.rows:
            self.put(sep + 2, 4, "Nothing here.", dim)
            return

        # Reserve the footer, separator, spacer, and status row so the list
        # never overlaps the footer area.
        visible = h - sep - 5
        start = max(0, min(b.sel - visible // 2, len(b.rows) - visible))
        st = self.player.state
        playing = set()
        if st:
            playing |= {st.device_id, st.context_uri}
            if st.track:
                playing.add(st.track.uri)
        right_w = max(len(r.right) for r in b.rows) + 2          # +2 for the heart
        avail = w - 4 - 2 - right_w - 2
        label_w = int(avail * 0.5)
        detail_w = avail - label_w - 2

        for i, row in enumerate(b.rows[start:start + visible]):
            y = sep + 1 + i
            current = row.uri in playing
            marker = "♪ " if current else "  "
            if start + i == b.sel:
                self.put(y, 2, " " * (w - 4), curses.A_REVERSE | curses.A_BOLD)
                base = curses.A_REVERSE | curses.A_BOLD
            else:
                base = (accent | curses.A_BOLD) if current else 0
            self.put(y, 2, marker + clip(row.label, label_w), base)
            self.put(y, 4 + label_w + 2, clip(row.detail, detail_w), base or dim)
            heart = "♥ " if self.player.saved.get(row.uri) else "  "
            self.put(y, w - 3 - right_w, heart + row.right, base or dim)

    def draw_footer(self, h: int):
        msg = self.notify.current()
        hint = HINT_LIST if self.browser.active else HINT_HOME
        self.put(h - 5, 2, "─" * (self.scr.getmaxyx()[1] - 4), curses.A_DIM)
        if self.browser.active:
            self.put(h - 3, 2, hint, curses.A_DIM)
        else:
            self.put(h - 4, 2, hint, curses.A_DIM)
            self.put(h - 3, 2, HINT_HOME_LIB, curses.A_DIM)
        # h - 2 is intentionally blank as a small gap before the message row.
        if msg:
            self.put(h - 1, 2, msg, curses.color_pair(3) | curses.A_BOLD)

    # --- input ---
    def prompt(self, label: str) -> str:
        h, w = self.scr.getmaxyx()
        self.scr.timeout(-1)
        curses.echo()
        curses.curs_set(1)
        self.put(h - 1, 0, " " * (w - 1))
        self.put(h - 1, 2, label, curses.color_pair(1) | curses.A_BOLD)
        self.scr.refresh()
        try:
            text = self.scr.getstr(h - 1, 2 + len(label), 100).decode("utf-8", "ignore")
        except curses.error:
            text = ""
        curses.noecho()
        curses.curs_set(0)
        self.scr.timeout(200)
        return text.strip()

    def handle_key(self, ch: int):
        if self.browser.active and ch in self.list_keys:
            self.list_keys[ch]()
        elif ch in self.global_keys:
            self.global_keys[ch]()

    # --- main loop ---
    def run(self, scr):
        self.scr = scr
        curses.curs_set(0)
        curses.use_default_colors()
        curses.init_pair(1, 41 if curses.COLORS >= 256 else curses.COLOR_GREEN, -1)
        curses.init_pair(3, curses.COLOR_YELLOW, -1)
        scr.keypad(True)
        scr.timeout(200)
        self.player.start()
        try:
            if not AlbumArt.enabled:
                self.notify.say("Tip: pip install pillow for album art", 6)
            while self.running:
                self.draw()
                ch = scr.getch()
                if ch not in (-1, curses.KEY_RESIZE):
                    self.handle_key(ch)
        finally:
            self.player.stop(stop_spotifyd=self.stop_spotifyd_on_exit)
            self.browser.shutdown()
            self._artist_image_pool.shutdown(wait=False, cancel_futures=True)


# ============================== entry point ==============================

def load_local_env() -> None:
    """Load simple KEY=VALUE settings from the project-local .env file."""
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    try:
        with open(env_path, encoding="utf-8") as env_file:
            for raw_line in env_file:
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[7:].lstrip()
                key, separator, value = line.partition("=")
                if not separator:
                    continue
                key, value = key.strip(), value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                if key and key not in os.environ:
                    os.environ[key] = value
    except FileNotFoundError:
        pass


def make_client() -> spotipy.Spotify:
    load_local_env()
    for var in ("SPOTIPY_CLIENT_ID", "SPOTIPY_CLIENT_SECRET", "SPOTIPY_REDIRECT_URI"):
        if not os.environ.get(var):
            sys.exit(f"Set {var} in the project .env file. See README.md for setup steps.")
    cache = os.path.expanduser("~/.cache/soloist")
    os.makedirs(cache, exist_ok=True)
    auth = SpotifyOAuth(scope=SCOPE, cache_path=os.path.join(cache, "token.json"))
    sp = spotipy.Spotify(auth_manager=auth)
    sp.current_user()          # first run: opens the browser to log in
    return sp


def main():
    sp = make_client()
    notifier = Notifier()
    player = Player(sp, notifier)
    ui = UI(player, Library(player), notifier)
    os.environ.setdefault("ESCDELAY", "25")
    curses.wrapper(ui.run)
    if player.stop_error:
        print(player.stop_error, file=sys.stderr)


if __name__ == "__main__":
    main()
