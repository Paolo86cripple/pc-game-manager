from __future__ import annotations

import os
from pathlib import Path


def _looks_like_wine_runner(path: Path) -> bool:
    return any((path / rel).is_file() for rel in ("bin/wine", "dist/bin/wine", "files/bin/wine", "wine"))


def _looks_like_proton(path: Path) -> bool:
    return (path / "compatibilitytool.vdf").is_file() and any(
        (path / rel).exists() for rel in ("proton", "files/bin/wine", "dist/bin/wine")
    )


def classify(path: Path, source: str | None = None) -> dict:
    path = path.expanduser().resolve()
    if _looks_like_proton(path):
        kind = "proton"
    elif _looks_like_wine_runner(path):
        kind = "wine"
    else:
        kind = "unknown"
    return {
        "name": path.name,
        "path": str(path),
        "kind": kind,
        "source": source or "Sistema/Personalizzato",
    }


def _runner_dirs(base: Path):
    if not base.is_dir():
        return
    for p in sorted(base.iterdir()):
        if p.is_dir():
            yield p


def discover() -> list[dict]:
    home = Path.home()
    xdg_data = Path(os.environ.get("XDG_DATA_HOME", home / ".local/share"))
    candidates: list[tuple[Path, str]] = [
        (home / ".steam/root/compatibilitytools.d", "Steam/compatibilitytools.d"),
        (home / ".steam/steam/compatibilitytools.d", "Steam/compatibilitytools.d"),
        (home / ".local/share/Steam/compatibilitytools.d", "Steam/compatibilitytools.d"),
        (home / ".local/share/compatibilitytools.d", "Runtime personali"),
        (home / ".local/share/bottles/runners", "Runtime personali"),
        (home / ".var/app/com.usebottles.bottles/data/bottles/runners", "Runtime personali"),
        (xdg_data / "pc-game-manager/runners", "Runtime personali"),
        (Path("/usr/share/steam/compatibilitytools.d"), "Sistema"),
        (Path("/usr/local/share/steam/compatibilitytools.d"), "Sistema"),
    ]
    found: dict[str, dict] = {}
    for base, source in candidates:
        for p in _runner_dirs(base):
            if _looks_like_proton(p) or _looks_like_wine_runner(p):
                item = classify(p, source)
                found[str(p)] = item
    wine = Path("/usr/bin/wine")
    if wine.exists():
        found["/usr/bin/wine"] = {
            "name": "Wine di sistema",
            "path": "/usr/bin/wine",
            "kind": "wine-system",
            "source": "Sistema",
        }
    return sorted(found.values(), key=lambda x: (x["kind"], x["name"].lower()))
