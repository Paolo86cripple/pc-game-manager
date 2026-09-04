from __future__ import annotations

import hashlib
import json
import re
import shutil
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path

GITHUB_API = "https://api.github.com"
USER_AGENT = "pc-game-manager-renderer/2.12"

RENDERER_SOURCES = {
    "dxvk": ("doitsujin", "dxvk", "DXVK"),
    "d7vk": ("WinterSnowfall", "d7vk", "D7VK"),
    "vkd3d": ("HansKristian-Work", "vkd3d-proton", "VKD3D-Proton"),
    "dxvk-nvapi": ("jp7677", "dxvk-nvapi", "DXVK-NVAPI"),
    "dgvoodoo": ("dege-diosg", "dgVoodoo2", "dgVoodoo2"),
}


def _http_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def _download(url: str, target: Path) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=90) as r, target.open("wb") as out:
        shutil.copyfileobj(r, out)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._") or "renderer"


def _version_key(value: str) -> tuple:
    return tuple((0, int(x)) if x.isdigit() else (1, x.lower()) for x in re.findall(r"\d+|[A-Za-z]+", value))


def _pick_asset(kind: str, assets: list[dict]) -> dict | None:
    candidates = []
    for asset in assets:
        name = str(asset.get("name") or "")
        low = name.lower()
        if not low.endswith((".tar.gz", ".tar.xz", ".tar.zst", ".tgz", ".zip")):
            continue
        if any(x in low for x in ("source", "symbols", "debug")):
            continue
        score = 0
        if kind in low:
            score += 20
        if low.endswith(".tar.gz"):
            score += 5
        if "x86_64" in low or "x64" in low:
            score += 2
        candidates.append((score, asset))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


def catalog(kind: str, limit: int = 12) -> list[dict]:
    kind = kind.lower()
    if kind not in RENDERER_SOURCES:
        raise ValueError(f"renderer sconosciuto: {kind}")
    owner, repo, display = RENDERER_SOURCES[kind]
    data = _http_json(f"{GITHUB_API}/repos/{owner}/{repo}/releases")
    if not isinstance(data, list):
        return []
    out = []
    for rel in data:
        if rel.get("draft") or rel.get("prerelease"):
            continue
        asset = _pick_asset(kind, rel.get("assets") or [])
        if not asset:
            continue
        digest = str(asset.get("digest") or "")
        if digest.startswith("sha256:"):
            digest = digest.split(":", 1)[1]
        tag = str(rel.get("tag_name") or rel.get("name") or "")
        out.append({
            "id": tag,
            "name": str(rel.get("name") or tag),
            "version": tag,
            "kind": kind,
            "source": display,
            "repo": f"{owner}/{repo}",
            "url": asset.get("browser_download_url"),
            "filename": asset.get("name"),
            "sha256": digest or None,
            "published": rel.get("published_at"),
        })
        if len(out) >= limit:
            break
    return out


def catalog_all(limit: int = 12) -> tuple[list[dict], list[str]]:
    items: list[dict] = []
    errors: list[str] = []
    for kind in ("dxvk", "d7vk", "vkd3d", "dxvk-nvapi", "dgvoodoo"):
        try:
            items.extend(catalog(kind, limit))
        except Exception as exc:
            errors.append(f"{kind.upper()}: {exc}")
    items.sort(key=lambda x: (x.get("kind", ""), _version_key(str(x.get("version", "")))), reverse=True)
    return items, errors


def _safe_extract_tar(archive: Path, target: Path) -> None:
    with tarfile.open(archive, "r:*") as tf:
        root = target.resolve()
        for member in tf.getmembers():
            dest = (target / member.name).resolve()
            if dest != root and root not in dest.parents:
                raise RuntimeError(f"archive contiene percorso non sicuro: {member.name}")
        try:
            tf.extractall(target, filter="data")
        except TypeError:
            tf.extractall(target)


def _safe_extract_zip(archive: Path, target: Path) -> None:
    root = target.resolve()
    with zipfile.ZipFile(archive) as zf:
        for member in zf.infolist():
            dest = (target / member.filename).resolve()
            if dest != root and root not in dest.parents:
                raise RuntimeError(f"archive contiene percorso non sicuro: {member.filename}")
        zf.extractall(target)


def _contains_renderer(root: Path, kind: str) -> bool:
    required = {
        "dxvk": {"d3d11.dll", "dxgi.dll"},
        "d7vk": {"ddraw.dll"},
        "vkd3d": {"d3d12.dll"},
        "dxvk-nvapi": {"nvapi.dll"},
        "dgvoodoo": {"ddraw.dll"},
    }[kind]
    found = {p.name.lower() for p in root.rglob("*.dll") if p.is_file()}
    return required.issubset(found)


def install(item: dict, data_root: Path | None = None) -> Path:
    kind = str(item.get("kind") or "").lower()
    if kind not in RENDERER_SOURCES:
        raise RuntimeError("voce catalogo renderer non valida")
    url = str(item.get("url") or "")
    filename = str(item.get("filename") or Path(url).name or f"{kind}.tar.gz")
    if not url:
        raise RuntimeError("release senza URL di download")
    root = data_root or (Path.home() / ".local/share/pc-game-manager")
    base = root / kind
    base.mkdir(parents=True, exist_ok=True)
    version = safe_name(str(item.get("version") or item.get("id") or Path(filename).stem))
    destination = base / version

    with tempfile.TemporaryDirectory(prefix=f"pcgm-{kind}-") as td:
        td_path = Path(td)
        archive = td_path / filename
        _download(url, archive)
        expected = str(item.get("sha256") or "").lower().strip()
        if expected and _sha256(archive).lower() != expected:
            raise RuntimeError("checksum SHA-256 del renderer non corrispondente")
        unpack = td_path / "unpack"
        unpack.mkdir()
        low = filename.lower()
        if low.endswith(".zip"):
            _safe_extract_zip(archive, unpack)
        else:
            _safe_extract_tar(archive, unpack)
        if not _contains_renderer(unpack, kind):
            raise RuntimeError(f"l'archivio {filename} non contiene le DLL attese per {kind.upper()}")
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(unpack, destination)
    return destination


def discover(data_root: Path | None = None) -> list[dict]:
    root = data_root or (Path.home() / ".local/share/pc-game-manager")
    out: list[dict] = []
    for kind in ("dxvk", "d7vk", "vkd3d", "dxvk-nvapi", "dgvoodoo"):
        base = root / kind
        if not base.is_dir():
            continue
        for child in base.iterdir():
            if child.is_dir() and _contains_renderer(child, kind):
                out.append({"kind": kind, "name": child.name, "version": child.name, "path": str(child), "source": "PC Game Manager"})
    out.sort(key=lambda x: (x["kind"], _version_key(x["version"])), reverse=True)
    return out


def remove(path: str | Path, data_root: Path | None = None) -> None:
    root = (data_root or (Path.home() / ".local/share/pc-game-manager")).resolve()
    p = Path(path).expanduser().resolve()
    if root != p and root not in p.parents:
        raise RuntimeError("rifiutata rimozione fuori dalla directory PC Game Manager")
    if p.is_dir():
        shutil.rmtree(p)
