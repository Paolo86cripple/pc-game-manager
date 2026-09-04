from __future__ import annotations

import hashlib
import re
import shutil
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

try:
    import yaml
except Exception:
    yaml = None

# The runtime registry is an implementation detail: the manager exposes runtime families,
# not third-party frontends. URLs can later be moved into a signed application manifest.
RUNTIME_MIRRORS = (
    "https://cloud-mirror.usebottles.com/repo/components/runners",
    "https://mirror.usebottles.com/repo/components/runners",
    "https://salix.mirror.garr.it/bottles/repo/components/runners",
)
GITHUB_API = "https://api.github.com"
GITHUB_COMPONENTS = "https://raw.githubusercontent.com/bottlesdevs/components/main"


def _http_text(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "pc-game-manager-runtime/2.9.1"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode("utf-8", errors="replace")


def _http_json(url: str):
    import json
    req = urllib.request.Request(url, headers={"User-Agent": "pc-game-manager-runtime/2.9.1", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def _download(url: str, target: Path) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "pc-game-manager-runtime/2.9.1"})
    with urllib.request.urlopen(req, timeout=60) as r, target.open("wb") as out:
        shutil.copyfileobj(r, out)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._") or "runtime"


def _safe_extract_tar(archive: Path, target: Path) -> None:
    import tarfile
    with tarfile.open(archive, "r:*") as tf:
        root = target.resolve()
        for member in tf.getmembers():
            dest = (target / member.name).resolve()
            if root != dest and root not in dest.parents:
                raise RuntimeError(f"archive contains unsafe path: {member.name}")
        tf.extractall(target)


def _github_manifest_url(name: str, category: str = "wine") -> str:
    return f"{GITHUB_COMPONENTS}/runners/{category}/{urllib.parse.quote(name)}.yml"


def _mirror_manifest_urls(name: str, category: str = "wine") -> list[str]:
    quoted = urllib.parse.quote(name)
    return [f"{base}/{category}/{quoted}.yml" for base in RUNTIME_MIRRORS]


def _manifest_url(name: str, category: str = "wine") -> str:
    # Official repository is authoritative. Mirrors are fallback only.
    return _github_manifest_url(name, category)


def _http_text_first(urls: list[str]) -> tuple[str, str]:
    errors = []
    for url in urls:
        try:
            return _http_text(url), url
        except Exception as exc:
            errors.append(f"{url}: {exc}")
    raise RuntimeError("nessuna fonte runtime raggiungibile: " + " | ".join(errors))


def _parse_component_manifest(name: str, category: str = "wine", provider: str = "Runtime registry", kind: str = "wine") -> dict | None:
    if yaml is None:
        raise RuntimeError("python-yaml è necessario per il catalogo runtime")
    urls = [_github_manifest_url(name, category), *_mirror_manifest_urls(name, category)]
    text, used_url = _http_text_first(urls)
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        return None
    # Per-runner manifests are either a flat object (Name/Provider/File) or
    # legacy {runner-name: {...}} mappings. Support both forms.
    if "Name" in data or "File" in data or "Files" in data:
        item = data
    else:
        item = next(iter(data.values())) if data else {}
    if not isinstance(item, dict):
        item = {}
    files = item.get("File") or item.get("Files") or []
    if isinstance(files, dict):
        files = [files]
    meta = files[0] if files else {}
    if not isinstance(meta, dict):
        meta = {}
    return {
        "id": name,
        "name": item.get("Name") or name,
        "source": provider,
        "kind": kind,
        "channel": item.get("Channel", "stable"),
        "url": meta.get("url"),
        "filename": meta.get("file_name") or meta.get("filename") or meta.get("rename"),
        "md5": item.get("file_checksum") or meta.get("file_checksum"),
        "sha256": meta.get("sha256") or meta.get("file_sha256") or item.get("sha256"),
        "size": item.get("file_size") or meta.get("file_size"),
        "manifest_url": used_url,
        "category": category,
    }


def _github_runner_names(category: str, prefix: str) -> list[str]:
    data = _http_json(f"{GITHUB_API}/repos/bottlesdevs/components/contents/runners/{category}?ref=main")
    if not isinstance(data, list):
        return []
    out = []
    for item in data:
        name = str(item.get("name", ""))
        if not name.lower().endswith(".yml"):
            continue
        stem = Path(name).stem
        if stem.lower().startswith(prefix.lower()):
            out.append(stem)
    return sorted(set(out))


def _mirror_index_names(category: str, prefix: str) -> list[str]:
    errors = []
    for base in RUNTIME_MIRRORS:
        url = f"{base}/{category}/"
        try:
            text = _http_text(url)
            names = sorted(set(re.findall(r'href=["\']([^"\']+\.yml)["\']', text, flags=re.I)))
            found = [Path(n).stem for n in names if Path(n).stem.lower().startswith(prefix.lower())]
            if found:
                return found
        except Exception as exc:
            errors.append(f"{url}: {exc}")
    if errors:
        raise RuntimeError("mirror runner non raggiungibili: " + " | ".join(errors))
    return []


def _runner_names(category: str, prefix: str) -> list[str]:
    try:
        names = _github_runner_names(category, prefix)
        if names:
            return names
    except Exception as github_exc:
        try:
            return _mirror_index_names(category, prefix)
        except Exception as mirror_exc:
            raise RuntimeError(f"GitHub components: {github_exc}; mirror: {mirror_exc}") from mirror_exc
    return _mirror_index_names(category, prefix)

def _version_key(name: str) -> tuple:
    return tuple((0, int(x)) if x.isdigit() else (1, x) for x in re.findall(r"\d+|[A-Za-z]+", name.lower()))


def live_registry_runtimes(limit_per_family: int = 12) -> list[dict]:
    out: list[dict] = []
    for prefix, category, kind, family in (
        ("soda-", "wine", "wine", "Soda"),
        ("protosoda-", "proton", "proton", "ProtoSoda"),
    ):
        names = [n for n in _runner_names(category, prefix) if "experimental" not in n.lower()]
        for name in sorted(names, key=_version_key, reverse=True)[:limit_per_family]:
            out.append({
                "id": name, "name": name, "source": "Runtime registry",
                "kind": kind, "family": family, "channel": "stable",
                "category": category, "manifest_url": _github_manifest_url(name, category),
            })
    return out


def resolve_registry_runtime(item: dict) -> dict:
    if item.get("url") and item.get("filename"):
        return dict(item)
    name = str(item.get("name") or item.get("id") or "")
    category = str(item.get("category") or ("proton" if name.lower().startswith("protosoda-") else "wine"))
    kind = str(item.get("kind") or ("proton" if category == "proton" else "wine"))
    meta = _parse_component_manifest(name, category, item.get("source", "Runtime registry"), kind)
    if not meta:
        raise RuntimeError(f"impossibile leggere il manifest del runtime {name}")
    return meta


def github_catalog(owner: str, repo: str, display_source: str, kind: str, prefix: str = "x86_64") -> list[dict]:
    data = _http_json(f"{GITHUB_API}/repos/{owner}/{repo}/releases")
    out: list[dict] = []
    if not isinstance(data, list):
        return out
    for rel in data[:12]:
        if rel.get("draft") or rel.get("prerelease"):
            continue
        chosen = None
        for asset in rel.get("assets") or []:
            n = str(asset.get("name", ""))
            if prefix in n and n.endswith((".tar.gz", ".tar.xz", ".tar.zst")):
                chosen = asset
        if not chosen:
            continue
        digest = str(chosen.get("digest") or "")
        if digest.startswith("sha256:"):
            digest = digest.split(":", 1)[1]
        out.append({"id": rel.get("tag_name"), "name": rel.get("name") or rel.get("tag_name"),
                    "source": display_source, "kind": kind, "url": chosen.get("browser_download_url"),
                    "filename": chosen.get("name"), "sha256": digest or None, "published": rel.get("published_at")})
    return out


def proton_ge_catalog() -> list[dict]:
    return github_catalog("GloriousEggroll", "proton-ge-custom", "Proton-GE", "proton")


def proton_cachyos_catalog() -> list[dict]:
    """Return current Proton-CachyOS releases from the official repository."""
    data = _http_json(f"{GITHUB_API}/repos/CachyOS/proton-cachyos/releases")
    out: list[dict] = []
    if not isinstance(data, list):
        return out
    for rel in data:
        if rel.get("draft") or rel.get("prerelease"):
            continue
        assets = rel.get("assets") or []
        chosen = None
        # Prefer the regular x86_64 SLR archive; fall back to any x86_64
        # Proton archive because the repository has used several naming schemes.
        for asset in assets:
            n = str(asset.get("name", ""))
            nl = n.lower()
            if "x86_64" not in nl or not n.endswith((".tar.gz", ".tar.xz", ".tar.zst")):
                continue
            if "slr" in nl and "proton" in nl:
                chosen = asset
                break
            if chosen is None and "proton" in nl:
                chosen = asset
        if not chosen:
            continue
        digest = str(chosen.get("digest") or "")
        if digest.startswith("sha256:"):
            digest = digest.split(":", 1)[1]
        tag = str(rel.get("tag_name") or "")
        out.append({
            "id": tag or str(rel.get("name") or "Proton-CachyOS"),
            "name": str(rel.get("name") or tag),
            "source": "Proton-CachyOS",
            "kind": "proton",
            "family": "Proton-CachyOS",
            "channel": "stable",
            "category": "proton",
            "url": chosen.get("browser_download_url"),
            "filename": chosen.get("name"),
            "sha256": digest or None,
            "published": rel.get("published_at"),
        })
    return out


def component_runner_catalog(limit_per_family: int = 12) -> list[dict]:
    """Read the current runner catalog published by the runtime component project.

    This keeps Soda/Caffe/Vaniglia and the compatible Proton families visible without
    baking a specific release number into the manager. Individual archives are still
    resolved through the component manifest at install time.
    """
    data = _http_json(f"{GITHUB_API}/repos/bottlesdevs/components/contents")
    versions = []
    for item in data if isinstance(data, list) else []:
        name = str(item.get("name", ""))
        if re.fullmatch(r"\d+(?:\.\d+)*\.yml", name):
            versions.append(name)
    if not versions:
        return []
    versions.sort(key=_version_key, reverse=True)
    raw = _http_text(f"{GITHUB_COMPONENTS}/{urllib.parse.quote(versions[0])}")
    if yaml is None:
        raise RuntimeError("python-yaml è necessario per il catalogo runtime")
    parsed = yaml.safe_load(raw)
    if not isinstance(parsed, dict):
        return []
    groups: dict[str, list[dict]] = {}
    for name, meta in parsed.items():
        if not isinstance(meta, dict):
            continue
        if str(meta.get("Category", "")).lower() != "runners":
            continue
        if str(meta.get("Channel", "stable")).lower() not in {"stable", "rc"}:
            continue
        sub = str(meta.get("Sub-category", "wine")).lower()
        if sub not in {"wine", "proton"}:
            continue
        n = str(name)
        # The project component catalog contains old third-party naming families as well.
        # Keep the native families useful to our manager, while deliberately excluding
        # legacy frontend-specific runner families.
        family = ("Soda" if n.lower().startswith("soda-") else
                  "ProtoSoda" if n.lower().startswith("protosoda-") else
                  "Caffe" if n.lower().startswith("caffe-") else
                  "Vaniglia" if n.lower().startswith("vaniglia-") else
                  "GE-Proton" if n.lower().startswith("ge-proton-") else
                  "Wine-GE" if n.lower().startswith("wine-ge-proton") else
                  None)
        if family is None:
            continue
        groups.setdefault(family, []).append({
            "id": n, "name": n, "source": "Runtime catalog",
            "kind": "proton" if sub == "proton" else "wine", "family": family,
            "channel": str(meta.get("Channel", "stable")), "category": sub,
            "manifest_url": _manifest_url(n, "proton" if sub == "proton" else "wine"),
        })
    out: list[dict] = []
    for family, entries in groups.items():
        entries.sort(key=lambda x: _version_key(x["name"]), reverse=True)
        out.extend(entries[:limit_per_family])
    return out


def catalog_all() -> list[dict]:
    items: list[dict] = []
    errors: list[str] = []
    providers = (component_runner_catalog, live_registry_runtimes, proton_ge_catalog, proton_cachyos_catalog)
    for fn in providers:
        try:
            items.extend(fn())
        except Exception as exc:
            errors.append(f"{fn.__name__}: {exc}")
    seen = set(); out = []
    for item in items:
        key = (item.get("source"), item.get("name"))
        if key not in seen:
            seen.add(key); out.append(item)
    out.sort(key=lambda x: (str(x.get("kind")), str(x.get("family", "")), str(x.get("source")), str(x.get("name"))))
    return out, errors


def download_and_install(item: dict, destination: Path, allow_unverified: bool = False) -> Path:
    if not item.get("url") and (item.get("manifest_url") or str(item.get("source", "")).lower() in {"runtime registry", "runtime catalog"}):
        item = resolve_registry_runtime(item)
    else:
        item = dict(item)
    url = str(item.get("url") or "")
    if not url:
        raise RuntimeError("runtime catalog entry has no download URL")
    filename = safe_name(item.get("filename") or Path(urllib.parse.urlparse(url).path).name)
    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="pc-game-manager-download-") as td:
        archive = Path(td) / filename
        _download(url, archive)
        expected = str(item.get("sha256") or "").strip().lower()
        if expected:
            actual = _sha256(archive)
            if actual != expected:
                raise RuntimeError(f"SHA-256 mismatch: expected {expected}, got {actual}")
        elif not allow_unverified:
            raise RuntimeError("runtime senza checksum pubblicato")
        install_root = destination / safe_name(str(item.get("name") or Path(filename).stem))
        if install_root.exists():
            shutil.rmtree(install_root)
        install_root.mkdir(parents=True)
        _safe_extract_tar(archive, install_root)
        children = list(install_root.iterdir())
        if len(children) == 1 and children[0].is_dir():
            tmp = children[0]
            for child in tmp.iterdir():
                shutil.move(str(child), str(install_root / child.name))
            tmp.rmdir()
        return install_root
