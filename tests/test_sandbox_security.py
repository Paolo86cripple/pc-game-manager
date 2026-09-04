import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path
import struct

import pytest

ROOT = Path(__file__).resolve().parents[1]
SANDBOX_PATH = ROOT / "bin" / "pc-game-sandbox"


def load_sandbox():
    loader = SourceFileLoader("pc_game_sandbox_test", str(SANDBOX_PATH))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _write_pe(path: Path, machine: int) -> None:
    blob = bytearray(256)
    blob[0:2] = b"MZ"
    struct.pack_into("<I", blob, 0x3C, 0x80)
    blob[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<H", blob, 0x84, machine)
    path.write_bytes(blob)


def test_install_targets_are_confined_to_install():
    mod = load_sandbox()
    assert mod._validated_install_target("/install/mods", "mods") == "/install/mods"
    with pytest.raises(SystemExit):
        mod._validated_install_target("/install/../prefix", "bad")
    with pytest.raises(SystemExit):
        mod._validated_install_target("/prefix/injected", "bad")


def test_transient_payload_is_read_only():
    text = SANDBOX_PATH.read_text(encoding="utf-8")
    block = text.split("def add_transient_payload", 1)[1].split("def run_file", 1)[0]
    assert '"--ro-bind", str(parent), "/payload"' in block
    assert '"--bind", str(parent), "/payload"' not in block


def test_new_security_defaults_and_network_disclosure():
    text = SANDBOX_PATH.read_text(encoding="utf-8")
    assert 'env["USER"] = "game"' in text
    assert 'network_mode not in {"none", "host"}' in text
    assert "Internet e LAN" in text
    profile = __import__("json").loads((ROOT / "profiles/default.json").read_text(encoding="utf-8"))
    assert profile["game_root_readonly"] is True
    assert profile["network"] == "none"


def test_pe_bitness_and_renderer_arch_filter(tmp_path):
    mod = load_sandbox()
    exe32 = tmp_path / "game32.exe"
    exe64 = tmp_path / "game64.exe"
    _write_pe(exe32, 0x014C)
    _write_pe(exe64, 0x8664)
    assert mod.pe_bitness(exe32) == 32
    assert mod.pe_bitness(exe64) == 64

    dxvk = tmp_path / "dxvk"
    for sub in ("x32", "x64"):
        (dxvk / sub).mkdir(parents=True)
        (dxvk / sub / "d3d9.dll").write_bytes(sub.encode())
        (dxvk / sub / "dxgi.dll").write_bytes(sub.encode())
    dlls32 = mod.renderer_dlls(str(dxvk), "dxvk", 32)
    dlls64 = mod.renderer_dlls(str(dxvk), "dxvk", 64)
    assert dlls32 and all("x32" in p.parts for p in dlls32)
    assert dlls64 and all("x64" in p.parts for p in dlls64)
    assert len({p.name.lower() for p in dlls32}) == len(dlls32)


def test_gui_is_split_into_less_crowded_sections():
    text = (ROOT / "pc-game-manager.py").read_text(encoding="utf-8")
    build_tabs = text.split("def build_tabs", 1)[1].split("def scrolled_box", 1)[0]
    assert 'Gtk.Label(label="Audio")' not in build_tabs
    runtime = text.split("def tab_runtime", 1)[1].split("def tab_graphics", 1)[0]
    assert 'Gtk.Label(label="Wine / Proton")' in runtime
    assert 'Gtk.Label(label="Componenti grafici")' in runtime
    deps = text.split("def tab_dependencies", 1)[1].split("def tab_access", 1)[0]
    assert 'label="Codec video retro"' in deps
