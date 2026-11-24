#!/usr/bin/env python3
"""
Popamp Album List
-----------------
A lightweight GTK3 album-browser companion for Popamp on Linux.

Features
  * Cover-grid and table views
  * Search by artist, album, and year
  * Double-click to play an album; right-click to enqueue/open its folder
  * Quick Scan: checks the library and reuses cached metadata for unchanged albums
  * Full Scan: rereads tags and artwork for every album
  * Winamp .wsz / Popamp .psz palette and classic PLEDIT-sheet support
  * Skin-coloured GTK controls when a skin is selected

Arch/CachyOS dependencies:
    sudo pacman -S python-gobject gtk3 python-mutagen python-pillow

Run:
    python3 popamp_album_list.py

Configuration:
    ~/.config/popamp-album-list/config.json
Cache:
    ~/.cache/popamp-album-list/
"""

from __future__ import annotations

import configparser
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
import os
import re
import subprocess
import tempfile
import threading
import tomllib
import zipfile
import warnings
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Optional

import gi

gi.require_version("Gdk", "3.0")
gi.require_version("Gtk", "3.0")
gi.require_version("Pango", "1.0")
from gi.repository import Gdk, GdkPixbuf, GLib, Gtk, Pango

try:
    import mutagen
    from mutagen.flac import FLAC
    from mutagen.id3 import ID3
    from mutagen.mp4 import MP4

    HAS_MUTAGEN = True
except ImportError:
    HAS_MUTAGEN = False

try:
    from PIL import Image

    HAS_PIL = True
except ImportError:
    HAS_PIL = False


# =============================================================================
# Paths and constants
# =============================================================================

APP_NAME = "popamp-album-list"
CONFIG_DIR = Path.home() / ".config" / APP_NAME
CACHE_DIR = Path.home() / ".cache" / APP_NAME
COVER_CACHE_DIR = CACHE_DIR / "covers"
CONFIG_FILE = CONFIG_DIR / "config.json"
LIBRARY_CACHE_FILE = CACHE_DIR / "library-index.json"

AUDIO_EXTENSIONS = {
    ".mp3", ".flac", ".ogg", ".opus", ".m4a", ".aac", ".wav",
    ".wv", ".ape", ".mpc", ".wma", ".aiff", ".aif",
}

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}

COVER_FILENAMES = [
    "cover.jpg", "cover.jpeg", "cover.png", "cover.webp",
    "folder.jpg", "folder.jpeg", "folder.png", "folder.webp",
    "front.jpg", "front.jpeg", "front.png", "front.webp",
    "album.jpg", "album.png", "albumart.jpg", "albumart.png",
]

DEFAULT_THEME: dict[str, object] = {
    "name": "Classic Winamp",
    "normal_bg": "#181822",
    "normal_text": "#d0d0d0",
    "current_text": "#ffffff",
    "selected_bg": "#244b80",
    "header_bg": "#282838",
    "border_light": "#555568",
    "border_dark": "#0c0c12",
    "pledit_slices": None,
}

POPAMP_VECTOR_DEFAULT: dict[str, object] = {
    "name": "Popamp Default",
    "normal_bg": "#232527",
    "normal_text": "#b8bec4",
    "current_text": "#ffffff",
    "selected_bg": "#3c4248",
    "header_bg": "#2e3134",
    "border_light": "#52565c",
    "border_dark": "#141517",
    "pledit_slices": None,
}


# =============================================================================
# Data model
# =============================================================================

@dataclass
class AlbumEntry:
    album_id: str
    artist: str
    album: str
    year: str
    folder_path: str
    tracks: list[str] = field(default_factory=list)
    cover_cache_path: Optional[str] = None

    @property
    def display_title(self) -> str:
        return f"{self.album} ({self.year})" if self.year else self.album

    @property
    def search_blob(self) -> str:
        return f"{self.artist} {self.album} {self.year}".lower()

    @classmethod
    def from_dict(cls, data: dict) -> "AlbumEntry":
        return cls(
            album_id=str(data.get("album_id", "")),
            artist=str(data.get("artist", "Unknown Artist")),
            album=str(data.get("album", "Unknown Album")),
            year=str(data.get("year", "")),
            folder_path=str(data.get("folder_path", "")),
            tracks=[str(p) for p in data.get("tracks", [])],
            cover_cache_path=data.get("cover_cache_path"),
        )


# =============================================================================
# Cover art and metadata helpers
# =============================================================================

def _save_cached_cover(album_key: str, raw_bytes: bytes) -> Optional[str]:
    """Save cover art; include the artwork bytes in the digest so updates refresh."""
    if not raw_bytes:
        return None

    try:
        COVER_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha1(
            album_key.encode("utf-8", errors="ignore") + raw_bytes
        ).hexdigest()[:20]
        out_path = COVER_CACHE_DIR / f"{digest}.jpg"
        if out_path.is_file():
            return str(out_path)

        if HAS_PIL:
            with Image.open(io.BytesIO(raw_bytes)) as source:
                img = source.convert("RGB")
                img.thumbnail((400, 400), Image.Resampling.LANCZOS)
                img.save(out_path, format="JPEG", quality=90, optimize=True)
        else:
            # GdkPixbuf detects the actual format from the file contents.
            out_path.write_bytes(raw_bytes)
        return str(out_path)
    except Exception as exc:
        print(f"[Cover Cache] Could not save artwork for {album_key}: {exc}")
        return None


def _extract_embedded_art(audio_path: Path) -> Optional[bytes]:
    """Extract common embedded artwork formats supported by Mutagen."""
    if not HAS_MUTAGEN:
        return None

    try:
        ext = audio_path.suffix.lower()
        if ext == ".flac":
            audio = FLAC(audio_path)
            if audio.pictures:
                return audio.pictures[0].data

        elif ext == ".mp3":
            tags = ID3(audio_path)
            for apic in tags.getall("APIC"):
                if apic.data:
                    return apic.data

        elif ext in {".m4a", ".mp4", ".aac"}:
            audio = MP4(audio_path)
            covr = audio.tags.get("covr") if audio.tags else None
            if covr:
                return bytes(covr[0])

        else:
            media = mutagen.File(audio_path)
            if media:
                pictures = getattr(media, "pictures", None)
                if pictures:
                    return pictures[0].data

                tags = getattr(media, "tags", None)
                if tags and hasattr(tags, "getall"):
                    for frame_name in ("APIC", "covr"):
                        frames = tags.getall(frame_name)
                        if frames:
                            frame_data = getattr(frames[0], "data", None)
                            if frame_data:
                                return bytes(frame_data)
    except Exception:
        pass

    return None


def _find_folder_cover(folder: Path) -> Optional[str]:
    """Find a conventional folder cover, then a deterministic image fallback."""
    try:
        entries = {p.name.lower(): p for p in folder.iterdir() if p.is_file()}
        for candidate in COVER_FILENAMES:
            match = entries.get(candidate)
            if match:
                return str(match)

        for name in sorted(entries):
            if Path(name).suffix.lower() in IMAGE_EXTENSIONS:
                return str(entries[name])
    except (OSError, PermissionError):
        pass
    return None


def _read_album_metadata(sample_track: Path, folder: Path) -> tuple[str, str, str]:
    """Read albumartist/album/date, with useful folder-name fallbacks."""
    artist = folder.parent.name if folder.parent else "Unknown Artist"
    album = folder.name or "Unknown Album"
    year = ""

    prefix = re.match(r"^(\d{4})\s*[-–.]\s*(.+)$", album)
    suffix = re.match(r"^(.+?)\s*[([](\d{4})[)\]]$", album)
    if prefix:
        year, album = prefix.group(1), prefix.group(2).strip()
    elif suffix:
        album, year = suffix.group(1).strip(), suffix.group(2)

    if HAS_MUTAGEN:
        try:
            media = mutagen.File(sample_track, easy=True)
            if media and media.tags:
                tags = media.tags
                tag_artist = tags.get("albumartist") or tags.get("artist")
                tag_album = tags.get("album")
                tag_date = (
                    tags.get("originaldate")
                    or tags.get("date")
                    or tags.get("year")
                )

                if tag_artist and str(tag_artist[0]).strip():
                    artist = str(tag_artist[0]).strip()
                if tag_album and str(tag_album[0]).strip():
                    album = str(tag_album[0]).strip()
                if tag_date and str(tag_date[0]).strip():
                    match = re.search(r"\d{4}", str(tag_date[0]))
                    if match:
                        year = match.group(0)
        except Exception:
            pass

    return artist, album, year


def _file_stat_signature(path: Path) -> tuple[str, int, int] | None:
    try:
        stat = path.stat()
        return (str(path), stat.st_size, stat.st_mtime_ns)
    except OSError:
        return None


def _folder_signature(
    folder: Path,
    track_paths: list[str],
    cover_path: Optional[str],
) -> str:
    """A quick-scan fingerprint based on track and cover file metadata."""
    try:
        folder_mtime = folder.stat().st_mtime_ns
    except OSError:
        folder_mtime = 0

    tracks_signature = []
    for track in track_paths:
        signature = _file_stat_signature(Path(track))
        if signature:
            tracks_signature.append(signature)

    cover_signature = _file_stat_signature(Path(cover_path)) if cover_path else None
    payload = json.dumps(
        [folder_mtime, tracks_signature, cover_signature],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8", errors="replace")
    return hashlib.sha256(payload).hexdigest()


def load_library_cache() -> dict:
    try:
        data = json.loads(LIBRARY_CACHE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_library_cache(cache: dict) -> None:
    """Atomically write the scan index so an interrupted scan cannot corrupt it."""
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix="library-index-", suffix=".tmp", dir=str(CACHE_DIR)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(cache, handle, ensure_ascii=False, separators=(",", ":"))
            os.replace(temp_name, LIBRARY_CACHE_FILE)
        finally:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
    except OSError as exc:
        print(f"[Library Cache] Could not save scan index: {exc}")


def albums_from_library_cache(cached_index: dict, music_dirs: list[str]) -> list[AlbumEntry]:
    """Convert cached records to albums; safe to run outside the GTK thread."""
    roots: list[Path] = []
    for item in music_dirs:
        try:
            roots.append(Path(item).expanduser().resolve())
        except OSError:
            continue

    cached_albums: list[AlbumEntry] = []
    seen_ids: set[str] = set()
    for folder_key, record in cached_index.items():
        if not isinstance(record, dict):
            continue
        album_data = record.get("album")
        if not isinstance(album_data, dict):
            continue
        try:
            folder = Path(album_data.get("folder_path") or folder_key).expanduser().resolve()
        except OSError:
            continue

        inside_library = False
        for root in roots:
            try:
                if os.path.commonpath((str(root), str(folder))) == str(root):
                    inside_library = True
                    break
            except ValueError:
                continue
        if not inside_library:
            continue

        album = AlbumEntry.from_dict(album_data)
        if not album.album_id or album.album_id in seen_ids:
            continue
        seen_ids.add(album.album_id)
        cached_albums.append(album)

    cached_albums.sort(
        key=lambda item: (item.artist.casefold(), item.year or "9999", item.album.casefold())
    )
    return cached_albums


def scan_music_library(
    music_dirs: list[str],
    quick_scan: bool = True,
    cached_index: Optional[dict] = None,
    progress_cb: Optional[Callable[[str], None]] = None,
) -> tuple[list[AlbumEntry], dict, dict[str, int]]:
    """
    Scan music folders.

    Quick Scan still checks the directory tree and file timestamps/sizes, but
    avoids rereading tags and decoding cover art for unchanged album folders.
    Full Scan refreshes tags and cover art for every discovered album folder.
    """
    cached_index = cached_index or {}
    new_index: dict = {}
    albums: list[AlbumEntry] = []
    seen_folders: set[str] = set()
    reused = 0
    refreshed = 0
    folders_checked = 0

    for root_dir in music_dirs:
        root_path = Path(root_dir).expanduser()
        if not root_path.is_dir():
            if progress_cb:
                progress_cb(f"Folder not found: {root_path}")
            continue

        for dirpath, dirnames, filenames in os.walk(root_path):
            dirnames.sort()
            folder = Path(dirpath)
            folder_key = str(folder.resolve())
            if folder_key in seen_folders:
                continue
            seen_folders.add(folder_key)
            folders_checked += 1

            tracks = sorted(
                str(folder / filename)
                for filename in filenames
                if Path(filename).suffix.lower() in AUDIO_EXTENSIONS
            )
            if not tracks:
                continue

            cover_path = _find_folder_cover(folder)
            signature = _folder_signature(folder, tracks, cover_path)
            cached_record = cached_index.get(folder_key, {})
            cached_album_data = cached_record.get("album", {}) if isinstance(cached_record, dict) else {}
            cached_cover = cached_album_data.get("cover_cache_path") if isinstance(cached_album_data, dict) else None
            cache_is_usable = (
                quick_scan
                and isinstance(cached_record, dict)
                and cached_record.get("signature") == signature
                and isinstance(cached_album_data, dict)
                and (not cached_cover or Path(cached_cover).is_file())
            )

            if cache_is_usable:
                album = AlbumEntry.from_dict(cached_album_data)
                album.tracks = tracks
                reused += 1
            else:
                sample_track = Path(tracks[0])
                artist, album_name, year = _read_album_metadata(sample_track, folder)

                # A folder image takes priority; otherwise use embedded art.
                resolved_cover = cover_path
                if not resolved_cover:
                    art_bytes = _extract_embedded_art(sample_track)
                    if art_bytes:
                        resolved_cover = _save_cached_cover(folder_key, art_bytes)

                album = AlbumEntry(
                    album_id=hashlib.sha1(folder_key.encode("utf-8")).hexdigest()[:12],
                    artist=artist,
                    album=album_name,
                    year=year,
                    folder_path=folder_key,
                    tracks=tracks,
                    cover_cache_path=resolved_cover,
                )
                refreshed += 1

            albums.append(album)
            new_index[folder_key] = {
                "signature": signature,
                "album": asdict(album),
            }

            if progress_cb and (folders_checked % 30 == 0):
                mode = "Quick" if quick_scan else "Full"
                progress_cb(
                    f"{mode} scan: {len(albums)} albums found, "
                    f"{folders_checked} folders checked..."
                )

    albums.sort(key=lambda item: (item.artist.casefold(), item.year or "9999", item.album.casefold()))
    stats = {
        "reused": reused,
        "refreshed": refreshed,
        "folders_checked": folders_checked,
    }
    return albums, new_index, stats


# =============================================================================
# Popamp process controller
# =============================================================================

class PopampController:
    """Launch album tracks through Popamp's command-line interface."""

    @staticmethod
    def _launch(arguments: list[str]) -> bool:
        try:
            subprocess.Popen(arguments, start_new_session=True)
            return True
        except FileNotFoundError:
            print("[Popamp] The 'popamp' executable was not found in PATH.")
        except OSError as exc:
            print(f"[Popamp] Could not start Popamp: {exc}")
        return False

    @classmethod
    def play_album(cls, album: AlbumEntry) -> bool:
        return cls._launch(["popamp", *album.tracks]) if album.tracks else False

    @classmethod
    def enqueue_album(cls, album: AlbumEntry) -> bool:
        return cls._launch(["popamp", "/ADD", *album.tracks]) if album.tracks else False


# =============================================================================
# Winamp / Popamp skin parsing
# =============================================================================

def _pil_to_pixbuf(pil_img: "Image.Image") -> GdkPixbuf.Pixbuf:
    rgba = pil_img.convert("RGBA")
    width, height = rgba.size
    data = GLib.Bytes.new(rgba.tobytes())
    return GdkPixbuf.Pixbuf.new_from_bytes(
        data, GdkPixbuf.Colorspace.RGB, True, 8, width, height, width * 4
    )


def _slice_pledit_sheet(
    raw_bytes: bytes,
    is_svg: bool = False,
) -> Optional[dict[str, GdkPixbuf.Pixbuf]]:
    """Extract the classic 280px-wide Winamp PLEDIT frame regions.

    The coordinates follow the classic PLEDIT layout: a 25x20 titlebar tile,
    12x29 and 7x29 side strips, a 25x38 bottom filler, and the outer bottom
    edge/resize artwork. The large bottom control panels are intentionally
    omitted so the companion does not display unused Play/Next/playlist buttons.
    """
    temp_path = None
    try:
        if is_svg:
            with tempfile.NamedTemporaryFile(suffix=".svg", delete=False) as temp:
                temp.write(raw_bytes)
                temp_path = temp.name
            sheet_pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(
                temp_path, 280, -1, True
            )
            if not sheet_pb:
                return None
            sheet_width = sheet_pb.get_width()
            sheet_height = sheet_pb.get_height()

            def crop(x1: int, y1: int, x2: int, y2: int) -> GdkPixbuf.Pixbuf:
                x1 = max(0, min(sheet_width - 1, x1))
                y1 = max(0, min(sheet_height - 1, y1))
                x2 = max(x1 + 1, min(sheet_width, x2))
                y2 = max(y1 + 1, min(sheet_height, y2))
                return sheet_pb.new_subpixbuf(x1, y1, x2 - x1, y2 - y1)

        else:
            if not HAS_PIL:
                return None
            with Image.open(io.BytesIO(raw_bytes)) as source:
                sheet = source.convert("RGBA")
            if sheet.width <= 0:
                return None
            # Normalize 1x/2x sheets to the classic 280px skin coordinate width.
            normalized_height = max(1, round(sheet.height * 280 / sheet.width))
            if sheet.width != 280 or sheet.height != normalized_height:
                sheet = sheet.resize((280, normalized_height), Image.Resampling.LANCZOS)
            sheet_width, sheet_height = sheet.size

            def crop(x1: int, y1: int, x2: int, y2: int) -> GdkPixbuf.Pixbuf:
                return _pil_to_pixbuf(sheet.crop((x1, y1, x2, y2)))

        if sheet_width < 276 or sheet_height < 110:
            print(
                "[Skin Slicer] PLEDIT sheet is not large enough for the classic "
                f"layout ({sheet_width}x{sheet_height}); using palette-only skinning."
            )
            return None

        return {
            # Active top frame.
            "top_left": crop(0, 0, 25, 20),
            "top_title": crop(26, 0, 126, 20),
            "top_fill": crop(127, 0, 152, 20),
            "top_right": crop(153, 0, 178, 20),

            # Vertical frame strips.
            "left_border": crop(0, 42, 12, 71),
            "right_border": crop(44, 42, 51, 71),

            # Minimal bottom frame: tile, left edge, and resize grip only.
            # The large bottom panels contain Winamp transport/playlist buttons;
            # we intentionally do not draw them in this companion app.
            "bottom_fill": crop(179, 0, 204, 38),
            "bottom_left_edge": crop(0, 72, 12, 110),
            "resize_grip": crop(257, 89, 276, 110),
        }

    except Exception as exc:
        print(f"[Skin Slicer] Could not slice PLEDIT sheet: {exc}")
        return None
    finally:
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass


def parse_skin_archive(skin_path: str) -> dict[str, object]:
    """Read skin palette and an explicitly named classic PLEDIT sprite sheet."""
    path = Path(skin_path)
    is_psz = path.suffix.lower() == ".psz"
    theme = (POPAMP_VECTOR_DEFAULT if is_psz else DEFAULT_THEME).copy()
    if not path.is_file():
        return theme

    def clean_hex(value: object, fallback: str) -> str:
        if not isinstance(value, str):
            return fallback
        candidate = value.strip()
        if candidate.startswith("0x"):
            candidate = candidate[2:]
        candidate = candidate.lstrip("#")
        if re.fullmatch(r"[0-9a-fA-F]{6}", candidate):
            return f"#{candidate.lower()}"
        return fallback

    try:
        with zipfile.ZipFile(path, "r") as archive:
            names = archive.namelist()
            name_map = {name.lower(): name for name in names}
            manifest_data = {}
            base_prefix = ""

            manifest_name = next(
                (name for name in names if Path(name).name.lower() == "skin.toml"),
                None,
            )
            if manifest_name:
                try:
                    manifest_data = tomllib.loads(
                        archive.read(manifest_name).decode("utf-8", errors="replace")
                    )
                    base_prefix = str(Path(manifest_name).parent)
                    if base_prefix == ".":
                        base_prefix = ""
                except Exception as exc:
                    print(f"[Skin Parser] Could not read skin.toml: {exc}")

            meta = manifest_data.get("meta", {}) if isinstance(manifest_data, dict) else {}
            if isinstance(meta, dict) and meta.get("name"):
                theme["name"] = str(meta["name"])

            playlist = manifest_data.get("playlist", {}) if isinstance(manifest_data, dict) else {}
            if isinstance(playlist, dict):
                theme["normal_bg"] = clean_hex(playlist.get("normal_bg"), str(theme["normal_bg"]))
                theme["normal_text"] = clean_hex(playlist.get("normal"), str(theme["normal_text"]))
                theme["current_text"] = clean_hex(playlist.get("current"), str(theme["current_text"]))
                theme["selected_bg"] = clean_hex(playlist.get("selected_bg"), str(theme["selected_bg"]))

            text_section = manifest_data.get("text", {}) if isinstance(manifest_data, dict) else {}
            if isinstance(text_section, dict) and "normal" not in playlist:
                theme["normal_text"] = clean_hex(text_section.get("color"), str(theme["normal_text"]))

            # Only use a sheet explicitly named pledit (manifest) or pledit.*.
            # Do not cut arbitrary playlist-window SVGs into assumed frame pieces.
            pledit_member = None
            sheets = manifest_data.get("sheets", {}) if isinstance(manifest_data, dict) else {}
            if isinstance(sheets, dict) and isinstance(sheets.get("pledit"), str):
                requested = sheets["pledit"]
                candidate = f"{base_prefix}/{requested}".lstrip("/") if base_prefix else requested.lstrip("/")
                pledit_member = name_map.get(candidate.lower()) or name_map.get(requested.lower())

            if not pledit_member:
                pledit_member = next(
                    (
                        name for name in names
                        if Path(name).stem.lower() == "pledit"
                        and Path(name).suffix.lower() in {".bmp", ".png", ".svg"}
                    ),
                    None,
                )

            # Classic Winamp playlist text colours.
            pledit_txt = next(
                (name for name in names if Path(name).name.lower() == "pledit.txt"),
                None,
            )
            if pledit_txt:
                try:
                    parser = configparser.ConfigParser(interpolation=None, strict=False)
                    parser.read_string(archive.read(pledit_txt).decode("latin-1", errors="replace"))
                    section = next(
                        (parser[section_name] for section_name in parser.sections()
                         if section_name.lower() == "text"),
                        None,
                    )
                    if section:
                        theme["normal_bg"] = clean_hex(section.get("NormalBG"), str(theme["normal_bg"]))
                        theme["normal_text"] = clean_hex(section.get("Normal"), str(theme["normal_text"]))
                        theme["current_text"] = clean_hex(section.get("Current"), str(theme["current_text"]))
                        theme["selected_bg"] = clean_hex(section.get("SelectedBG"), str(theme["selected_bg"]))
                except Exception as exc:
                    print(f"[Skin Parser] Could not parse pledit.txt: {exc}")

            if pledit_member:
                suffix = Path(pledit_member).suffix.lower()
                if suffix in {".bmp", ".png", ".svg"}:
                    theme["pledit_slices"] = _slice_pledit_sheet(
                        archive.read(pledit_member), is_svg=(suffix == ".svg")
                    )

            # Derive complementary frame colours from the playlist background.
            bg = str(theme["normal_bg"]).lstrip("#")
            red, green, blue = (int(bg[index:index + 2], 16) for index in (0, 2, 4))
            theme["header_bg"] = f"#{min(255, red + 18):02x}{min(255, green + 18):02x}{min(255, blue + 24):02x}"
            theme["border_light"] = f"#{min(255, red + 48):02x}{min(255, green + 48):02x}{min(255, blue + 58):02x}"
            theme["border_dark"] = f"#{max(0, red - 16):02x}{max(0, green - 16):02x}{max(0, blue - 16):02x}"

            if theme["name"] in {DEFAULT_THEME["name"], POPAMP_VECTOR_DEFAULT["name"]}:
                theme["name"] = path.stem

    except (zipfile.BadZipFile, OSError) as exc:
        print(f"[Skin Parser] Could not open {skin_path}: {exc}")

    return theme


def find_popamp_skins() -> list[Path]:
    search_dirs = [
        Path.home() / ".local" / "share" / "popamp" / "skins",
        Path.home() / ".config" / "popamp" / "skins",
        Path("/usr/share/popamp/skins"),
        Path("/opt/popamp/skins"),
        Path.home() / "Downloads",
    ]
    found: list[Path] = []
    seen: set[str] = set()
    for directory in search_dirs:
        if not directory.is_dir():
            continue
        try:
            for file_path in directory.rglob("*"):
                if file_path.is_file() and file_path.suffix.lower() in {".wsz", ".psz"}:
                    key = str(file_path.resolve())
                    if key not in seen:
                        seen.add(key)
                        found.append(file_path)
        except OSError:
            continue
    return sorted(found, key=lambda item: item.name.casefold())


# =============================================================================
# Main GTK window
# =============================================================================

class AlbumListWindow(Gtk.Window):
    def __init__(self) -> None:
        super().__init__(title="Popamp Album List")

        # Give the running window the same identity as its desktop launcher.
        # WM_CLASS is used by X11/XWayland task lists; the GLib program name
        # set in main() provides the matching application ID on Wayland.
        self.set_icon_name("popamp-album-list")
        try:
            # Gtk3 has no non-deprecated WM_CLASS setter. Keep this for X11 /
            # XWayland taskbar matching, while silencing its harmless warning.
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                self.set_wmclass("popamp-album-list", "popamp-album-list")
        except (AttributeError, TypeError):
            pass

        self.set_default_size(620, 520)
        self.set_size_request(440, 340)
        self.set_border_width(0)
        self.set_decorated(False)
        self.set_resizable(True)

        self.config = self._load_config()
        self._geometry_save_source = None
        self._geometry_ready = False
        self._supports_window_position = self._display_supports_window_position()
        self._last_window_size = None
        self._last_window_position = None
        saved_size = self.config.get("window_size")
        if self._valid_geometry_pair(saved_size, minimum=1):
            saved_width = max(440, int(saved_size[0]))
            saved_height = max(340, int(saved_size[1]))
            # Set a pre-map default and also apply resize() after the window is
            # mapped in _restore_window_position(). Some WMs ignore late defaults.
            self.set_default_size(saved_width, saved_height)
            self._last_window_size = (saved_width, saved_height)

        self.connect("configure-event", self._on_configure_event)
        self.connect("delete-event", self._on_window_delete_event)
        self.connect("destroy", self._on_window_destroy)

        self.theme = DEFAULT_THEME.copy()
        skin_path = self.config.get("skin_path", "")
        if skin_path and Path(skin_path).is_file():
            self.theme = parse_skin_archive(skin_path)

        self.all_albums: list[AlbumEntry] = []
        self.filtered_albums: list[AlbumEntry] = []

        # UI rendering is incremental. Existing album cards are retained and
        # updated only when their visible data changes.
        self._grid_cards: dict[str, Gtk.FlowBoxChild] = {}
        self._grid_visible_ids: set[str] = set()
        self._flow_order: dict[str, int] = {}
        self._render_generation = 0
        self._list_render_generation = 0
        self._pixbuf_cache: dict[tuple, GdkPixbuf.Pixbuf] = {}
        self._pixbuf_cache_lock = threading.Lock()
        self._cover_executor = ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="PopampCover"
        )

        self.view_mode = self.config.get("view_mode", "grid")
        if self.view_mode not in {"grid", "list"}:
            self.view_mode = "grid"

        self.scan_in_progress = False
        self.scan_summary = "Library not scanned yet"
        self.css_provider = Gtk.CssProvider()
        self.css_installed = False

        self._build_ui()
        self._apply_theme_css()

        # The initial library index is read in the scan worker, not on GTK's
        # main thread. Cached albums are handed back in small UI batches.
        GLib.idle_add(self.start_quick_scan)

    # -------------------------------------------------------------------------
    # Configuration and window geometry
    # -------------------------------------------------------------------------

    @staticmethod
    def _display_supports_window_position() -> bool:
        """Return True when the active GDK backend exposes X11-style positions."""
        display = Gdk.Display.get_default()
        if display is None:
            return False
        backend_type = getattr(display, "__gtype__", None)
        backend_name = " ".join((
            type(display).__name__,
            getattr(backend_type, "name", ""),
        )).lower()
        display_name = (display.get_name() or "").lower()
        return "wayland" not in backend_name and "wayland" not in display_name

    def _load_config(self) -> dict:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        default_music = Path.home() / "Music"
        defaults = {
            "music_dirs": [str(default_music)] if default_music.is_dir() else [],
            "skin_path": "",
            "view_mode": "grid",
            "double_click_action": "play",
        }
        try:
            loaded = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                defaults.update(loaded)
        except (OSError, json.JSONDecodeError):
            pass

        if not isinstance(defaults.get("music_dirs"), list):
            defaults["music_dirs"] = []
        defaults["music_dirs"] = [str(item) for item in defaults["music_dirs"]]
        return defaults

    def _save_config(self) -> None:
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            CONFIG_FILE.write_text(
                json.dumps(self.config, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError as exc:
            print(f"[Config] Could not save settings: {exc}")

    @staticmethod
    def _valid_geometry_pair(value, minimum: int = 0) -> bool:
        return (
            isinstance(value, (list, tuple))
            and len(value) == 2
            and all(isinstance(item, int) and not isinstance(item, bool) for item in value)
            and value[0] >= minimum
            and value[1] >= minimum
        )

    def _on_configure_event(self, _window, event) -> bool:
        """Remember the latest mapped geometry and debounce disk writes."""
        if not self._geometry_ready or event.width < 440 or event.height < 340:
            return False

        self._last_window_size = (int(event.width), int(event.height))
        if self._supports_window_position:
            try:
                x, y = self.get_position()
                self._last_window_position = (int(x), int(y))
            except Exception:
                pass

        if self._geometry_save_source is not None:
            GLib.source_remove(self._geometry_save_source)
        self._geometry_save_source = GLib.timeout_add(350, self._save_window_geometry)
        return False

    def _restore_window_position(self) -> bool:
        """Restore saved size and, when supported, the previous X11 position."""
        saved_size = self.config.get("window_size")
        if self._valid_geometry_pair(saved_size, minimum=1):
            width = max(440, int(saved_size[0]))
            height = max(340, int(saved_size[1]))
            try:
                self.resize(width, height)
                self._last_window_size = (width, height)
            except Exception as exc:
                print(f"[Geometry] Could not restore window size: {exc}")

        if self._supports_window_position:
            saved_position = self.config.get("window_position")
            if self._valid_geometry_pair(saved_position, minimum=-100000):
                x, y = int(saved_position[0]), int(saved_position[1])
                screen = self.get_screen()
                if screen is not None and screen.get_n_monitors() > 0:
                    monitor_rects = [
                        screen.get_monitor_geometry(index)
                        for index in range(screen.get_n_monitors())
                    ]
                    visible = False
                    width, height = self.get_size()
                    for rect in monitor_rects:
                        overlap_w = max(0, min(x + width, rect.x + rect.width) - max(x, rect.x))
                        overlap_h = max(0, min(y + height, rect.y + rect.height) - max(y, rect.y))
                        if overlap_w >= min(100, width) and overlap_h >= min(60, height):
                            visible = True
                            break

                    if not visible:
                        primary = screen.get_primary_monitor()
                        if primary < 0:
                            primary = 0
                        rect = monitor_rects[primary]
                        x = max(rect.x - width + 100, min(x, rect.x + rect.width - 100))
                        y = max(rect.y, min(y, rect.y + rect.height - 80))

                try:
                    self.move(x, y)
                    self._last_window_position = (x, y)
                except Exception as exc:
                    print(f"[Geometry] Could not restore window position: {exc}")

        # Start listening only after the initial default geometry has been
        # applied, otherwise the startup size could overwrite saved settings.
        self._geometry_ready = True
        return False

    def _save_window_geometry(self) -> bool:
        """Save the last valid mapped size and the position on X11/XWayland."""
        self._geometry_save_source = None

        # While mapped, query GTK directly. During teardown use the last valid
        # configure-event values because some WMs report 1x1 after destruction.
        try:
            if self.get_mapped():
                width, height = self.get_size()
                if width >= 440 and height >= 340:
                    self._last_window_size = (int(width), int(height))
                if self._supports_window_position:
                    x, y = self.get_position()
                    self._last_window_position = (int(x), int(y))
        except Exception:
            pass

        changed = False
        if self._last_window_size:
            size = [int(self._last_window_size[0]), int(self._last_window_size[1])]
            if self.config.get("window_size") != size:
                self.config["window_size"] = size
                changed = True

        if self._supports_window_position and self._last_window_position:
            position = [int(self._last_window_position[0]), int(self._last_window_position[1])]
            if self.config.get("window_position") != position:
                self.config["window_position"] = position
                changed = True

        if changed:
            self._save_config()
        return False

    def _on_window_delete_event(self, _window, _event) -> bool:
        """Save geometry before GTK's default close handler destroys the window."""
        if self._geometry_save_source is not None:
            GLib.source_remove(self._geometry_save_source)
            self._geometry_save_source = None
        self._save_window_geometry()
        return False

    def _on_window_destroy(self, _window) -> None:
        """Final geometry flush and non-blocking shutdown of cover workers."""
        if self._geometry_save_source is not None:
            GLib.source_remove(self._geometry_save_source)
            self._geometry_save_source = None
        self._save_window_geometry()
        try:
            self._cover_executor.shutdown(wait=False, cancel_futures=True)
        except TypeError:  # Compatibility with older Python versions.
            self._cover_executor.shutdown(wait=False)

    # -------------------------------------------------------------------------
    # Theme CSS
    # -------------------------------------------------------------------------

    def _apply_theme_css(self) -> None:
        theme = self.theme
        has_slices = bool(theme.get("pledit_slices"))

        if has_slices:
            slices = theme["pledit_slices"]
            self.top_chrome.set_size_request(-1, slices["top_left"].get_height())
            self.left_chrome.set_size_request(slices["left_border"].get_width(), -1)
            self.right_chrome.set_size_request(slices["right_border"].get_width(), -1)
            self.bottom_chrome.set_size_request(-1, slices["bottom_fill"].get_height())
            self.resize_grip.set_size_request(
                slices["resize_grip"].get_width(), slices["resize_grip"].get_height()
            )
            toolbar_border = "none"
            side_bg = "transparent"
            side_left = "none"
            side_right = "none"
            title_bg = "transparent"
            titlebar_padding = "0px 5px 0px 0px"
            footer_bg = "transparent"
            footer_border = "none"
            control_bg = "transparent"
            control_color = "transparent"
            control_border = "none"
            control_width = 9
            control_height = 20
        else:
            self.top_chrome.set_size_request(-1, 22)
            self.left_chrome.set_size_request(7, -1)
            self.right_chrome.set_size_request(7, -1)
            self.bottom_chrome.set_size_request(-1, 26)
            self.resize_grip.set_size_request(16, 16)
            toolbar_border = f"1px solid {theme['border_dark']}"
            side_bg = theme["header_bg"]
            side_left = f"1px solid {theme['border_light']}"
            side_right = f"1px solid {theme['border_dark']}"
            title_bg = theme["header_bg"]
            titlebar_padding = "0px 5px"
            footer_bg = theme["header_bg"]
            footer_border = f"1px solid {theme['border_dark']}"
            control_bg = theme["header_bg"]
            control_color = theme["current_text"]
            control_border = f"1px solid {theme['border_dark']}"
            control_width = 19
            control_height = 16

        # The classic PLEDIT upper-right artwork already contains the two
        # titlebar buttons. With a sprite sheet loaded, GTK buttons become
        # transparent hit targets over those exact graphics.
        if hasattr(self, "minimize_button"):
            self.minimize_button.set_label("" if has_slices else "—")
            self.close_button.set_label("" if has_slices else "×")
            self.minimize_button.set_size_request(control_width, control_height)
            self.close_button.set_size_request(control_width, control_height)

        css = f"""
        window, .winamp-root {{
            background-color: {theme['normal_bg']};
            color: {theme['normal_text']};
        }}

        .winamp-titlebar {{
            background-color: {title_bg};
            border: none;
            padding: {titlebar_padding};
            min-height: 20px;
        }}
        .winamp-title-label {{
            color: {theme['current_text']};
            font-weight: bold;
            font-size: 10px;
            letter-spacing: 0.5px;
        }}

        .winamp-toolbar {{
            background-color: {theme['normal_bg']};
            border-bottom: {toolbar_border};
            padding: 4px 5px;
        }}

        .winamp-side-border {{
            background-color: {side_bg};
            border-left: {side_left};
            border-right: {side_right};
        }}

        entry {{
            background-color: {theme['border_dark']};
            color: {theme['normal_text']};
            border: 1px solid {theme['border_light']};
            border-radius: 0px;
            padding: 3px 6px;
            caret-color: {theme['current_text']};
            font-size: 11px;
            min-height: 20px;
        }}
        entry:focus {{
            border-color: {theme['current_text']};
        }}

        button.skin-button {{
            background-image: none;
            background-color: {theme['header_bg']};
            color: {theme['current_text']};
            border-top: 1px solid {theme['border_light']};
            border-left: 1px solid {theme['border_light']};
            border-right: 1px solid {theme['border_dark']};
            border-bottom: 1px solid {theme['border_dark']};
            border-radius: 0px;
            padding: 2px 6px;
            min-height: 20px;
            font-size: 9px;
            font-weight: bold;
        }}
        button.skin-button:hover {{
            background-color: {theme['selected_bg']};
            color: {theme['current_text']};
        }}
        button.skin-button:active {{
            background-color: {theme['normal_bg']};
            border-top: 1px solid {theme['border_dark']};
            border-left: 1px solid {theme['border_dark']};
            border-right: 1px solid {theme['border_light']};
            border-bottom: 1px solid {theme['border_light']};
        }}
        button.skin-button:disabled {{
            color: {theme['normal_text']};
            opacity: 0.45;
        }}

        button.window-control {{
            background-image: none;
            background-color: {control_bg};
            color: {control_color};
            border: {control_border};
            border-radius: 0px;
            padding: 0px;
            margin: 0px;
            min-width: {control_width}px;
            min-height: {control_height}px;
            font-size: 10px;
            font-weight: bold;
            box-shadow: none;
            outline: none;
        }}
        button.window-control:hover,
        button.window-control:active,
        button.window-control:checked {{
            background-color: {control_bg};
            color: {control_color};
            border: {control_border};
            box-shadow: none;
        }}

        scrolledwindow, viewport, treeview, flowbox {{
            background-color: {theme['normal_bg']};
            color: {theme['normal_text']};
            border: none;
        }}
        flowboxchild {{
            padding: 5px;
            border-radius: 0px;
            border: 1px solid transparent;
        }}
        flowboxchild:selected, treeview:selected {{
            background-color: {theme['selected_bg']};
            color: {theme['current_text']};
            border: 1px solid {theme['border_light']};
        }}
        .album-title {{
            color: {theme['current_text']};
            font-weight: bold;
            font-size: 11px;
        }}
        .album-artist {{
            color: {theme['normal_text']};
            font-size: 10px;
        }}
        treeview.view {{
            background-color: {theme['normal_bg']};
            color: {theme['normal_text']};
        }}
        treeview.view:selected {{
            background-color: {theme['selected_bg']};
            color: {theme['current_text']};
        }}
        treeview header button {{
            background-image: none;
            background-color: {theme['header_bg']};
            color: {theme['current_text']};
            border-radius: 0px;
            border: 1px solid {theme['border_dark']};
            padding: 3px;
            font-size: 10px;
        }}

        .winamp-statusbar {{
            background-color: {footer_bg};
            border: none;
            border-top: {footer_border if not has_slices else 'none'};
            color: {theme['normal_text']};
            padding: 0px 4px;
            font-size: 10px;
        }}
        .winamp-status-label {{
            color: {theme['normal_text']};
            font-size: 10px;
        }}
        """

        self.css_provider.load_from_data(css.encode("utf-8"))
        screen = Gdk.Screen.get_default()
        if screen and not self.css_installed:
            Gtk.StyleContext.add_provider_for_screen(
                screen, self.css_provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )
            self.css_installed = True

        if hasattr(self, "footer"):
            self.footer.set_margin_start(14 if has_slices else 6)
            self.footer.set_margin_end(22 if has_slices else 6)

        for area in (
            self.top_chrome,
            self.left_chrome,
            self.right_chrome,
            self.bottom_chrome,
        ):
            area.queue_draw()
        self.resize_grip.queue_draw()

    # -------------------------------------------------------------------------
    # Skin frame drawing
    # -------------------------------------------------------------------------

    @staticmethod
    def _paint_pixbuf(cr, pixbuf: GdkPixbuf.Pixbuf, x: int, y: int) -> None:
        Gdk.cairo_set_source_pixbuf(cr, pixbuf, x, y)
        cr.paint()

    def _draw_top_chrome(self, _widget, cr) -> bool:
        slices = self.theme.get("pledit_slices")
        if not slices:
            return False

        width = self.top_chrome.get_allocated_width()
        left = slices["top_left"]
        title = slices["top_title"]
        fill = slices["top_fill"]
        right = slices["top_right"]

        for x in range(0, width, max(1, fill.get_width())):
            self._paint_pixbuf(cr, fill, x, 0)

        if width > left.get_width() + right.get_width():
            self._paint_pixbuf(cr, title, (width - title.get_width()) // 2, 0)

        self._paint_pixbuf(cr, left, 0, 0)
        self._paint_pixbuf(cr, right, max(0, width - right.get_width()), 0)
        return False

    def _draw_side_chrome(self, widget, cr, side: str) -> bool:
        slices = self.theme.get("pledit_slices")
        if not slices:
            return False

        pixbuf = slices["left_border"] if side == "left" else slices["right_border"]
        height = widget.get_allocated_height()
        tile_height = max(1, pixbuf.get_height())
        for y in range(0, height, tile_height):
            self._paint_pixbuf(cr, pixbuf, 0, y)
        return False

    def _draw_bottom_chrome(self, _widget, cr) -> bool:
        slices = self.theme.get("pledit_slices")
        if not slices:
            return False

        width = self.bottom_chrome.get_allocated_width()
        fill = slices["bottom_fill"]
        left_edge = slices["bottom_left_edge"]

        # Tile the quiet bottom filler across the whole width. The full Winamp
        # bottom control panels are deliberately omitted to keep this app clean.
        tile_width = max(1, fill.get_width())
        for x in range(0, width, tile_width):
            self._paint_pixbuf(cr, fill, x, 0)

        self._paint_pixbuf(cr, left_edge, 0, 0)
        grip = slices["resize_grip"]
        self._paint_pixbuf(
            cr, grip, max(0, width - grip.get_width()),
            max(0, self.bottom_chrome.get_allocated_height() - grip.get_height()),
        )
        return False

    def _draw_resize_grip(self, widget, cr) -> bool:
        slices = self.theme.get("pledit_slices")
        if slices:
            return False  # The skin's resize-grip sprite is already painted below.

        width = widget.get_allocated_width()
        height = widget.get_allocated_height()
        cr.set_source_rgba(0.75, 0.75, 0.75, 0.75)
        cr.set_line_width(1)
        for offset in (5, 9, 13):
            cr.move_to(width - offset, height - 1)
            cr.line_to(width - 1, height - offset)
        cr.stroke()
        return False

    # -------------------------------------------------------------------------
    # Window dragging and resizing (no docking)
    # -------------------------------------------------------------------------

    def _on_drag_area_press(self, _widget, event) -> bool:
        if event.button == 1:
            self.begin_move_drag(
                event.button,
                int(event.x_root),
                int(event.y_root),
                event.time,
            )
            return True
        return False

    def _on_resize_press(self, _widget, event) -> bool:
        if event.button == 1:
            self.begin_resize_drag(
                Gdk.WindowEdge.SOUTH_EAST,
                event.button,
                int(event.x_root),
                int(event.y_root),
                event.time,
            )
            return True
        return False

    # -------------------------------------------------------------------------
    # Build UI
    # -------------------------------------------------------------------------

    def _make_button(self, label: str, tooltip: str, callback) -> Gtk.Button:
        button = Gtk.Button(label=label)
        button.set_tooltip_text(tooltip)
        button.get_style_context().add_class("skin-button")
        button.set_relief(Gtk.ReliefStyle.NONE)
        button.connect("clicked", callback)
        return button

    def _build_ui(self) -> None:
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        root.get_style_context().add_class("winamp-root")
        self.add(root)

        # Top skin frame with an empty drag region, title, and minimal window controls.
        top_overlay = Gtk.Overlay()
        self.top_chrome = Gtk.DrawingArea()
        self.top_chrome.set_size_request(-1, 20)
        self.top_chrome.connect("draw", self._draw_top_chrome)
        top_overlay.add(self.top_chrome)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        header.get_style_context().add_class("winamp-titlebar")
        header.set_valign(Gtk.Align.FILL)

        title_label = Gtk.Label(label="POPAMP ALBUM LIST")
        title_label.get_style_context().add_class("winamp-title-label")
        header.pack_start(title_label, False, False, 4)

        drag_area = Gtk.EventBox()
        drag_area.set_visible_window(False)
        drag_area.set_hexpand(True)
        drag_area.add_events(Gdk.EventMask.BUTTON_PRESS_MASK)
        drag_area.connect("button-press-event", self._on_drag_area_press)
        header.pack_start(drag_area, True, True, 0)

        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)

        # Classic PLEDIT titlebars have two 9x9 controls embedded in the
        # upper-right corner artwork: WindowShade on the left and Close on
        # the right. When a skin is present these buttons are transparent
        # click targets, revealing the original bitmap buttons underneath.
        # The WindowShade artwork retains this companion's minimize action.
        has_skin_controls = bool(self.theme.get("pledit_slices"))

        self.minimize_button = Gtk.Button(label="" if has_skin_controls else "—")
        self.minimize_button.set_tooltip_text("Minimize Popamp Album List")
        self.minimize_button.get_style_context().add_class("window-control")
        self.minimize_button.set_relief(Gtk.ReliefStyle.NONE)
        self.minimize_button.set_can_focus(False)
        self.minimize_button.set_size_request(9, 20)
        self.minimize_button.connect("clicked", lambda _button: self.iconify())
        controls.pack_start(self.minimize_button, False, False, 0)

        self.close_button = Gtk.Button(label="" if has_skin_controls else "×")
        self.close_button.set_tooltip_text("Close album list")
        self.close_button.get_style_context().add_class("window-control")
        self.close_button.set_relief(Gtk.ReliefStyle.NONE)
        self.close_button.set_can_focus(False)
        self.close_button.set_size_request(9, 20)
        self.close_button.connect("clicked", lambda _button: self.close())
        controls.pack_start(self.close_button, False, False, 0)

        header.pack_end(controls, False, False, 0)
        top_overlay.add_overlay(header)
        root.pack_start(top_overlay, False, False, 0)

        # Main content sits between the two vertical skin rails.
        middle = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)

        self.left_chrome = Gtk.DrawingArea()
        self.left_chrome.set_size_request(7, -1)
        self.left_chrome.connect("draw", self._draw_side_chrome, "left")
        self.left_chrome.get_style_context().add_class("winamp-side-border")
        middle.pack_start(self.left_chrome, False, False, 0)

        center = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        toolbar.get_style_context().add_class("winamp-toolbar")

        self.search_entry = Gtk.SearchEntry()
        self.search_entry.set_placeholder_text("Filter artist, album, year...")
        self.search_entry.set_tooltip_text("Search artist, album, or year (Ctrl+F)")
        self.search_entry.connect("search-changed", self._on_search_changed)
        toolbar.pack_start(self.search_entry, True, True, 0)

        self.btn_quick_scan = self._make_button(
            "QUICK", "Quick Scan: reuse cached metadata for unchanged albums",
            lambda _button: self.start_quick_scan(),
        )
        toolbar.pack_start(self.btn_quick_scan, False, False, 0)

        self.btn_full_scan = self._make_button(
            "FULL", "Full Scan: reread album tags and artwork throughout the library",
            lambda _button: self.start_full_scan(),
        )
        toolbar.pack_start(self.btn_full_scan, False, False, 0)

        self.btn_view = self._make_button(
            "LIST" if self.view_mode == "grid" else "GRID",
            "Switch between cover grid and table view",
            self._on_toggle_view,
        )
        toolbar.pack_start(self.btn_view, False, False, 0)

        self.btn_folder = self._make_button(
            "+DIR", "Add another music folder to the library",
            self._on_choose_folder,
        )
        toolbar.pack_start(self.btn_folder, False, False, 0)

        self.btn_skin = self._make_button(
            "SKIN", "Load a Winamp (.wsz) or Popamp (.psz) skin",
            self._on_choose_skin,
        )
        toolbar.pack_start(self.btn_skin, False, False, 0)

        center.pack_start(toolbar, False, False, 0)

        # Grid and list are held in a stack so switching does not rebuild the window.
        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.stack.set_transition_duration(100)

        grid_scroll = Gtk.ScrolledWindow()
        grid_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.flowbox = Gtk.FlowBox()
        self.flowbox.set_valign(Gtk.Align.START)
        self.flowbox.set_max_children_per_line(12)
        self.flowbox.set_min_children_per_line(2)
        self.flowbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.flowbox.set_activate_on_single_click(False)
        self.flowbox.set_sort_func(self._sort_flow_children)
        self.flowbox.set_column_spacing(6)
        self.flowbox.set_row_spacing(6)
        self.flowbox.connect("child-activated", self._on_grid_album_activated)
        self.flowbox.connect("button-press-event", self._on_grid_button_press)
        grid_scroll.add(self.flowbox)
        self.stack.add_named(grid_scroll, "grid")

        list_scroll = Gtk.ScrolledWindow()
        list_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self.list_store = Gtk.ListStore(GdkPixbuf.Pixbuf, str, str, str, int, int)
        self.treeview = Gtk.TreeView(model=self.list_store)
        self.treeview.set_headers_visible(True)
        self.treeview.connect("row-activated", self._on_list_row_activated)
        self.treeview.connect("button-press-event", self._on_list_button_press)

        art_renderer = Gtk.CellRendererPixbuf()
        art_column = Gtk.TreeViewColumn("Art", art_renderer, pixbuf=0)
        art_column.set_sizing(Gtk.TreeViewColumnSizing.FIXED)
        art_column.set_fixed_width(46)
        self.treeview.append_column(art_column)

        for index, label in ((1, "Artist"), (2, "Album"), (3, "Year"), (4, "Tracks")):
            renderer = Gtk.CellRendererText()
            renderer.set_property("ellipsize", Pango.EllipsizeMode.END)
            column = Gtk.TreeViewColumn(label, renderer, text=index)
            column.set_resizable(True)
            column.set_sort_column_id(index)
            if index in (1, 2):
                column.set_expand(True)
            self.treeview.append_column(column)

        list_scroll.add(self.treeview)
        self.stack.add_named(list_scroll, "list")
        center.pack_start(self.stack, True, True, 0)
        self.stack.set_visible_child_name(self.view_mode)

        middle.pack_start(center, True, True, 0)

        self.right_chrome = Gtk.DrawingArea()
        self.right_chrome.set_size_request(7, -1)
        self.right_chrome.connect("draw", self._draw_side_chrome, "right")
        self.right_chrome.get_style_context().add_class("winamp-side-border")
        middle.pack_end(self.right_chrome, False, False, 0)

        root.pack_start(middle, True, True, 0)

        # Minimal footer: only library status. Skin artwork supplies its own frame.
        bottom_overlay = Gtk.Overlay()
        self.bottom_chrome = Gtk.DrawingArea()
        self.bottom_chrome.set_size_request(-1, 26)
        self.bottom_chrome.connect("draw", self._draw_bottom_chrome)
        bottom_overlay.add(self.bottom_chrome)

        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self.footer = footer
        footer.get_style_context().add_class("winamp-statusbar")
        footer.set_valign(Gtk.Align.FILL)

        self.status_label = Gtk.Label(label="Ready")
        self.status_label.set_xalign(0.0)
        self.status_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.status_label.get_style_context().add_class("winamp-status-label")
        footer.pack_start(self.status_label, True, True, 0)

        # Keep the status line inside the minimal bottom frame.
        has_skin_frame = bool(self.theme.get("pledit_slices"))
        footer.set_margin_start(14 if has_skin_frame else 6)
        footer.set_margin_end(22 if has_skin_frame else 6)
        bottom_overlay.add_overlay(footer)

        self.resize_grip = Gtk.EventBox()
        self.resize_grip.set_visible_window(False)
        self.resize_grip.set_halign(Gtk.Align.END)
        self.resize_grip.set_valign(Gtk.Align.END)
        self.resize_grip.set_size_request(16, 16)
        self.resize_grip.add_events(Gdk.EventMask.BUTTON_PRESS_MASK)
        self.resize_grip.connect("button-press-event", self._on_resize_press)
        self.resize_grip.connect("draw", self._draw_resize_grip)
        bottom_overlay.add_overlay(self.resize_grip)

        root.pack_start(bottom_overlay, False, False, 0)

        self.connect("key-press-event", self._on_key_press)

    def _show_cached_library_while_scanning(self, cached_albums: list[AlbumEntry]) -> bool:
        """Display a pre-parsed cache snapshot while the scanner reconciles it."""
        if self.all_albums or not cached_albums:
            return False

        self.all_albums = cached_albums
        self.scan_summary = "Saved library loaded — checking for changes..."
        self._apply_filter(self.search_entry.get_text())
        self.status_label.set_text(
            f"Showing {len(self.all_albums)} saved albums while checking for changes..."
        )
        return False

    # -------------------------------------------------------------------------
    # Scan lifecycle
    # -------------------------------------------------------------------------

    def start_quick_scan(self) -> None:
        self._start_scan(full_scan=False)

    def start_full_scan(self) -> None:
        self._start_scan(full_scan=True)

    def _set_status_from_worker(self, message: str) -> bool:
        if self.status_label:
            self.status_label.set_text(message)
        return False

    def _start_scan(self, full_scan: bool) -> None:
        if self.scan_in_progress:
            self.status_label.set_text("A scan is already running...")
            return

        music_dirs = [str(Path(p).expanduser()) for p in self.config.get("music_dirs", [])]
        if not music_dirs:
            self.status_label.set_text("No music folder configured. Use +DIR to add one.")
            return

        self.scan_in_progress = True
        self.btn_quick_scan.set_sensitive(False)
        self.btn_full_scan.set_sensitive(False)
        scan_name = "Full" if full_scan else "Quick"
        self.status_label.set_text(f"{scan_name} scan starting...")

        def progress(message: str) -> None:
            GLib.idle_add(self._set_status_from_worker, message)

        def worker() -> None:
            try:
                # Filesystem checks can stall on slow or removable mounts too;
                # keep them off GTK's main thread.
                valid_dirs = [folder for folder in music_dirs if Path(folder).is_dir()]
                if not valid_dirs:
                    GLib.idle_add(
                        self._on_scan_finished, [], {}, scan_name,
                        "No valid music folder. Use +DIR to add one.",
                    )
                    return

                # Reading a large JSON index can itself take noticeable time;
                # keep it off GTK's main thread along with directory scanning.
                cached_index = load_library_cache()
                if cached_index:
                    cached_albums = albums_from_library_cache(cached_index, valid_dirs)
                    if cached_albums:
                        GLib.idle_add(
                            self._show_cached_library_while_scanning, cached_albums
                        )

                quick_scan = (not full_scan) and bool(cached_index)
                albums, new_index, stats = scan_music_library(
                    valid_dirs,
                    quick_scan=quick_scan,
                    cached_index=cached_index,
                    progress_cb=progress,
                )
                save_library_cache(new_index)
                GLib.idle_add(
                    self._on_scan_finished,
                    albums,
                    stats,
                    scan_name,
                    None,
                )
            except Exception as exc:
                GLib.idle_add(self._on_scan_finished, [], {}, scan_name, str(exc))

        threading.Thread(target=worker, name="PopampLibraryScan", daemon=True).start()

    def _on_scan_finished(
        self,
        albums: list[AlbumEntry],
        stats: dict,
        scan_name: str,
        error: Optional[str],
    ) -> bool:
        self.scan_in_progress = False
        self.btn_quick_scan.set_sensitive(True)
        self.btn_full_scan.set_sensitive(True)

        if error:
            self.status_label.set_text(f"{scan_name} scan failed: {error}")
            return False

        # Keep the previous snapshot on screen throughout the scan. Only now
        # replace it with the reconciled result (including additions/removals).
        self.all_albums = albums
        reused = int(stats.get("reused", 0))
        refreshed = int(stats.get("refreshed", 0))
        total_tracks = sum(len(album.tracks) for album in albums)
        if scan_name == "Quick":
            self.scan_summary = (
                f"Quick scan: {len(albums)} albums, {refreshed} refreshed, "
                f"{reused} cached"
            )
        else:
            self.scan_summary = f"Full scan: {len(albums)} albums refreshed"

        self._apply_filter(self.search_entry.get_text())
        # Include track count in the status summary without cluttering the toolbar.
        self.status_label.set_text(
            f"{self.scan_summary}  |  {total_tracks} tracks"
        )
        return False

    def _apply_filter(self, query: str) -> None:
        needle = query.strip().casefold()
        if needle:
            self.filtered_albums = [
                album for album in self.all_albums
                if needle in album.search_blob
            ]
        else:
            self.filtered_albums = list(self.all_albums)

        self._populate_views()
        if needle:
            self.status_label.set_text(
                f"{len(self.filtered_albums)} of {len(self.all_albums)} albums  |  {self.scan_summary}"
            )
        elif self.all_albums:
            total_tracks = sum(len(album.tracks) for album in self.filtered_albums)
            self.status_label.set_text(
                f"{self.scan_summary}  |  {total_tracks} tracks"
            )

    # -------------------------------------------------------------------------
    # Incremental album rendering
    # -------------------------------------------------------------------------

    def _create_placeholder_pixbuf(self, size: int) -> GdkPixbuf.Pixbuf:
        pixbuf = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, False, 8, size, size)
        pixbuf.fill(0x30343AFF)
        return pixbuf

    def _load_pixbuf(self, path: Optional[str], size: int) -> GdkPixbuf.Pixbuf:
        """Load and memoize a scaled cover, invalidating it when the file changes."""
        if not path:
            return self._create_placeholder_pixbuf(size)

        try:
            cover_path = Path(path)
            stat = cover_path.stat()
            key = (str(cover_path), stat.st_mtime_ns, stat.st_size, size)
        except OSError:
            return self._create_placeholder_pixbuf(size)

        with self._pixbuf_cache_lock:
            cached = self._pixbuf_cache.get(key)
        if cached is not None:
            return cached

        try:
            pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(
                str(cover_path), size, size, True
            )
        except Exception:
            pixbuf = self._create_placeholder_pixbuf(size)

        with self._pixbuf_cache_lock:
            self._pixbuf_cache[key] = pixbuf
            # Bound retained pixbuf memory; GTK widgets keep their own references.
            while len(self._pixbuf_cache) > 700:
                self._pixbuf_cache.pop(next(iter(self._pixbuf_cache)))
        return pixbuf

    def _request_cover(self, image_widget: Gtk.Image, path: Optional[str], size: int) -> None:
        """Decode a cover off the GTK thread and update its Gtk.Image on idle."""
        image_widget._cover_path = path
        request_token = getattr(image_widget, "_cover_request_token", 0) + 1
        image_widget._cover_request_token = request_token
        image_widget.set_from_pixbuf(self._create_placeholder_pixbuf(size))
        if not path:
            return

        future = self._cover_executor.submit(self._load_pixbuf, path, size)

        def finished(done_future) -> None:
            try:
                pixbuf = done_future.result()
            except Exception:
                return

            def update_widget() -> bool:
                try:
                    # Discard stale results if this card has since been refreshed.
                    if (
                        getattr(image_widget, "_cover_path", None) == path
                        and getattr(image_widget, "_cover_request_token", None) == request_token
                    ):
                        image_widget.set_from_pixbuf(pixbuf)
                except (RuntimeError, AttributeError):
                    pass  # The card may have been removed while decoding.
                return False

            GLib.idle_add(update_widget)

        future.add_done_callback(finished)

    @staticmethod
    def _album_visual_signature(album: AlbumEntry) -> tuple:
        cover_mtime = 0
        cover_size = 0
        if album.cover_cache_path:
            try:
                stat = Path(album.cover_cache_path).stat()
                cover_mtime = stat.st_mtime_ns
                cover_size = stat.st_size
            except OSError:
                pass
        return (
            album.artist,
            album.album,
            album.year,
            len(album.tracks),
            album.cover_cache_path,
            cover_mtime,
            cover_size,
        )

    def _sort_flow_children(self, first, second, _user_data=None) -> int:
        """Keep FlowBox cards in the same order as the sorted album list."""
        first_album = getattr(first, "album_entry", None)
        second_album = getattr(second, "album_entry", None)
        first_index = self._flow_order.get(first_album.album_id, 10**9) if first_album else 10**9
        second_index = self._flow_order.get(second_album.album_id, 10**9) if second_album else 10**9
        return (first_index > second_index) - (first_index < second_index)

    def _create_grid_card(self, album: AlbumEntry) -> Gtk.FlowBoxChild:
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        card.set_size_request(146, 184)

        cover = Gtk.Image.new_from_pixbuf(self._create_placeholder_pixbuf(136))
        self._request_cover(cover, album.cover_cache_path, 136)
        card.pack_start(cover, False, False, 0)

        album_label = Gtk.Label(label=album.display_title)
        album_label.set_ellipsize(Pango.EllipsizeMode.END)
        album_label.set_max_width_chars(19)
        album_label.get_style_context().add_class("album-title")
        card.pack_start(album_label, False, False, 0)

        artist_label = Gtk.Label(label=album.artist)
        artist_label.set_ellipsize(Pango.EllipsizeMode.END)
        artist_label.set_max_width_chars(19)
        artist_label.get_style_context().add_class("album-artist")
        card.pack_start(artist_label, False, False, 0)

        child = Gtk.FlowBoxChild()
        child.album_entry = album
        child._album_cover = cover
        child._album_title = album_label
        child._album_artist = artist_label
        child._album_visual_signature = self._album_visual_signature(album)
        child.add(card)
        return child

    def _update_grid_card(self, child: Gtk.FlowBoxChild, album: AlbumEntry) -> None:
        signature = self._album_visual_signature(album)
        child.album_entry = album
        if getattr(child, "_album_visual_signature", None) == signature:
            return

        self._request_cover(child._album_cover, album.cover_cache_path, 136)
        child._album_title.set_text(album.display_title)
        child._album_artist.set_text(album.artist)
        child._album_visual_signature = signature

    def _populate_grid_incrementally(self) -> None:
        """Reconcile only changed/new/deleted cards, adding them in short batches."""
        self._render_generation += 1
        generation = self._render_generation

        all_ids = {album.album_id for album in self.all_albums}
        target_ids = {album.album_id for album in self.filtered_albums}
        self._flow_order = {
            album.album_id: index for index, album in enumerate(self.filtered_albums)
        }

        # Remove cards only when an album has actually disappeared from the
        # library. Filtered-out cards are detached but retained for reuse.
        for album_id in list(self._grid_cards):
            if album_id not in all_ids:
                child = self._grid_cards.pop(album_id)
                if album_id in self._grid_visible_ids:
                    self.flowbox.remove(child)
                    self._grid_visible_ids.discard(album_id)

        for album_id in list(self._grid_visible_ids - target_ids):
            child = self._grid_cards.get(album_id)
            if child is not None:
                self.flowbox.remove(child)
            self._grid_visible_ids.discard(album_id)

        # Retained cards are reattached and updated as part of the batches below.
        self.flowbox.invalidate_sort()
        albums_to_process = list(self.filtered_albums)
        cursor = 0
        batch_size = 8

        def render_batch() -> bool:
            nonlocal cursor
            if generation != self._render_generation:
                return False

            stop = min(cursor + batch_size, len(albums_to_process))
            while cursor < stop:
                album = albums_to_process[cursor]
                album_id = album.album_id
                child = self._grid_cards.get(album_id)
                if child is None:
                    child = self._create_grid_card(album)
                    self._grid_cards[album_id] = child
                else:
                    self._update_grid_card(child, album)

                if album_id not in self._grid_visible_ids:
                    self.flowbox.add(child)
                    self._grid_visible_ids.add(album_id)
                child.show_all()
                cursor += 1

            if cursor < len(albums_to_process):
                return True  # Continue on the next GTK idle iteration.
            return False

        if albums_to_process:
            GLib.idle_add(render_batch)

    def _populate_list_incrementally(self) -> None:
        """Build the table in small batches when the user actually opens List view."""
        self._list_render_generation += 1
        generation = self._list_render_generation
        albums = list(self.filtered_albums)
        self.list_store.clear()
        cursor = 0
        batch_size = 32

        def render_batch() -> bool:
            nonlocal cursor
            if generation != self._list_render_generation:
                return False
            stop = min(cursor + batch_size, len(albums))
            while cursor < stop:
                index = cursor
                album = albums[index]
                self.list_store.append([
                    self._load_pixbuf(album.cover_cache_path, 36),
                    album.artist,
                    album.album,
                    album.year,
                    len(album.tracks),
                    index,
                ])
                cursor += 1
            if cursor < len(albums):
                return True
            return False

        if albums:
            GLib.idle_add(render_batch)

    def _populate_views(self) -> None:
        if self.view_mode == "grid":
            self._list_render_generation += 1
            self._populate_grid_incrementally()
        else:
            self._render_generation += 1
            self._populate_list_incrementally()

    # -------------------------------------------------------------------------
    # Album actions and context menus
    # -------------------------------------------------------------------------

    def _play_album(self, album: AlbumEntry) -> None:
        if PopampController.play_album(album):
            self.status_label.set_text(f"Playing: {album.artist} — {album.album}")
        else:
            self.status_label.set_text("Could not start Popamp. Check that 'popamp' is in PATH.")

    def _on_grid_album_activated(self, _flowbox, child) -> None:
        album = getattr(child, "album_entry", None)
        if album:
            self._play_album(album)

    def _on_list_row_activated(self, _treeview, path, _column) -> None:
        model = self.treeview.get_model()
        row = model[path]
        index = int(row[5])
        if 0 <= index < len(self.filtered_albums):
            self._play_album(self.filtered_albums[index])

    def _show_album_context_menu(self, album: AlbumEntry, event) -> None:
        menu = Gtk.Menu()

        play_item = Gtk.MenuItem(label=f"Play '{album.album}'")
        play_item.connect("activate", lambda _item: self._play_album(album))
        menu.append(play_item)

        enqueue_item = Gtk.MenuItem(label="Enqueue in Popamp")
        enqueue_item.connect(
            "activate",
            lambda _item: self.status_label.set_text(
                f"Enqueued: {album.album}" if PopampController.enqueue_album(album)
                else "Could not enqueue album in Popamp."
            ),
        )
        menu.append(enqueue_item)

        menu.append(Gtk.SeparatorMenuItem())
        open_item = Gtk.MenuItem(label="Open album folder")
        open_item.connect("activate", lambda _item: self._open_album_folder(album))
        menu.append(open_item)

        menu.show_all()
        menu.popup_at_pointer(event)

    def _open_album_folder(self, album: AlbumEntry) -> None:
        try:
            subprocess.Popen(["xdg-open", album.folder_path], start_new_session=True)
        except OSError as exc:
            self.status_label.set_text(f"Could not open folder: {exc}")

    def _on_grid_button_press(self, _widget, event) -> bool:
        if event.button == 3:
            child = self.flowbox.get_child_at_pos(int(event.x), int(event.y))
            if child and hasattr(child, "album_entry"):
                self.flowbox.select_child(child)
                self._show_album_context_menu(child.album_entry, event)
                return True
        return False

    def _on_list_button_press(self, _widget, event) -> bool:
        if event.button == 3:
            hit = self.treeview.get_path_at_pos(int(event.x), int(event.y))
            if hit:
                path = hit[0]
                self.treeview.get_selection().select_path(path)
                row = self.list_store[path]
                index = int(row[5])
                if 0 <= index < len(self.filtered_albums):
                    self._show_album_context_menu(self.filtered_albums[index], event)
                    return True
        return False

    # -------------------------------------------------------------------------
    # Toolbar actions
    # -------------------------------------------------------------------------

    def _on_search_changed(self, entry) -> None:
        self._apply_filter(entry.get_text())

    def _on_toggle_view(self, _button) -> None:
        self.view_mode = "list" if self.view_mode == "grid" else "grid"
        self.config["view_mode"] = self.view_mode
        self._save_config()
        self.stack.set_visible_child_name(self.view_mode)
        self.btn_view.set_label("LIST" if self.view_mode == "grid" else "GRID")
        self._populate_views()

    def _on_choose_folder(self, _button) -> None:
        dialog = Gtk.FileChooserDialog(
            title="Add Music Folder",
            parent=self,
            action=Gtk.FileChooserAction.SELECT_FOLDER,
        )
        dialog.add_buttons(
            Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
            Gtk.STOCK_OPEN, Gtk.ResponseType.OK,
        )
        response = dialog.run()
        if response == Gtk.ResponseType.OK:
            selected = dialog.get_filename()
            if selected:
                current = [str(Path(item).expanduser()) for item in self.config.get("music_dirs", [])]
                normalized = str(Path(selected).expanduser().resolve())
                if normalized not in {str(Path(item).resolve()) for item in current if Path(item).exists()}:
                    current.append(normalized)
                    self.config["music_dirs"] = current
                    self._save_config()
                self.start_quick_scan()
        dialog.destroy()

    def _on_choose_skin(self, _button) -> None:
        dialog = Gtk.FileChooserDialog(
            title="Select Popamp (.psz) or Winamp (.wsz) Skin",
            parent=self,
            action=Gtk.FileChooserAction.OPEN,
        )
        dialog.add_buttons(
            "Reset", Gtk.ResponseType.REJECT,
            Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
            Gtk.STOCK_OPEN, Gtk.ResponseType.OK,
        )

        current_skin = self.config.get("skin_path", "")
        if current_skin and Path(current_skin).is_file():
            dialog.set_filename(current_skin)
        else:
            installed_skins = find_popamp_skins()
            if installed_skins:
                dialog.set_current_folder(str(installed_skins[0].parent))

        file_filter = Gtk.FileFilter()
        file_filter.set_name("Popamp and Winamp skins (*.psz, *.wsz)")
        file_filter.add_pattern("*.psz")
        file_filter.add_pattern("*.PSZ")
        file_filter.add_pattern("*.wsz")
        file_filter.add_pattern("*.WSZ")
        dialog.add_filter(file_filter)

        response = dialog.run()
        if response == Gtk.ResponseType.OK:
            selected = dialog.get_filename()
            if selected:
                self.config["skin_path"] = selected
                self._save_config()
                self.theme = parse_skin_archive(selected)
                self._apply_theme_css()
                self.status_label.set_text(f"Skin loaded: {self.theme['name']}")
        elif response == Gtk.ResponseType.REJECT:
            self.config["skin_path"] = ""
            self._save_config()
            self.theme = DEFAULT_THEME.copy()
            self._apply_theme_css()
            self.status_label.set_text("Default theme restored")

        dialog.destroy()

    def _on_key_press(self, _widget, event) -> bool:
        if (event.state & Gdk.ModifierType.CONTROL_MASK) and event.keyval == Gdk.KEY_f:
            self.search_entry.grab_focus()
            return True
        if event.keyval == Gdk.KEY_F5:
            self.start_quick_scan()
            return True
        if event.keyval == Gdk.KEY_F6:
            self.start_full_scan()
            return True
        return False


# =============================================================================
# Entry point
# =============================================================================

def main() -> None:
    # Set a stable desktop identity before creating any GTK windows.
    # This should match popamp-album-list.desktop and its Icon= name.
    GLib.set_prgname("popamp-album-list")
    GLib.set_application_name("Popamp Album List")

    window = AlbumListWindow()
    window.connect("destroy", Gtk.main_quit)
    window.show_all()
    # Restore after mapping so X11 window managers have a real window to move.
    GLib.idle_add(window._restore_window_position)
    Gtk.main()


if __name__ == "__main__":
    main()
