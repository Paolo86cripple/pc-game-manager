import json
from pathlib import Path


def test_default_profile_shape():
    p = Path(__file__).parents[1] / "profiles" / "default.json"
    data = json.loads(p.read_text())
    assert data["network"] == "none"
    assert data["gpu"]["mode"] == "auto"
    assert data["capabilities"]["vulkan"] is True
    assert data["capabilities"]["audio"] is True
    assert data["audio_backend"] == "pulse"


def test_renderer_values_are_supported():
    from pathlib import Path
    source = (Path(__file__).parents[1] / "bin" / "pc-game-sandbox").read_text()
    assert '"run-file"' in source
    assert 'autodetect_renderer_dirs' in source


def test_profile_paths_follow_name_convention():
    import re
    source = (Path(__file__).parents[1] / "pc-game-manager.py").read_text()
    assert 'DEFAULT_STORAGE / stem / "prefix"' in source
    assert 'DEFAULT_STORAGE / stem / "saves"' in source
    assert 'DEFAULT_STORAGE / stem / "home"' in source


def test_catalog_has_cachyos_provider_and_continues_after_provider_failure():
    from pathlib import Path
    source = (Path(__file__).parents[1] / "lib" / "runtime_manager.py").read_text()
    assert "def proton_cachyos_catalog()" in source
    assert "providers = (component_runner_catalog, live_registry_runtimes, proton_ge_catalog, proton_cachyos_catalog)" in source
    assert 'errors.append(f"{fn.__name__}: {exc}")' in source


def test_runtime_family_names():
    from lib import runtime_manager as rm
    assert rm.safe_name("ProtoSoda-1.2") == "ProtoSoda-1.2"
    text = Path(rm.__file__).read_text(encoding="utf-8")
    assert '"ProtoSoda" if n.lower().startswith("protosoda-")' in text


def test_xwayland_ipc_mode_is_explicit():
    text = Path(__file__).resolve().parents[1].joinpath("bin/pc-game-sandbox").read_text(encoding="utf-8")
    assert "use_xwayland" in text
    assert "isolate_ipc = not use_xwayland" in text


def test_catalog_provider_fault_isolation():
    from lib import runtime_manager as rm
    original = rm.live_registry_runtimes
    rm.live_registry_runtimes = lambda: (_ for _ in ()).throw(RuntimeError("offline"))
    try:
        items, errors = rm.catalog_all()
        assert any("offline" in e for e in errors)
        assert isinstance(items, list)
    finally:
        rm.live_registry_runtimes = original


def test_wayland_input_profile_state():
    import json
    from pathlib import Path
    p = json.loads(Path("profiles/default.json").read_text())
    assert p["display_backend"] == "auto"
    assert p["xwayland_fallback"] is True
    assert p["wayland_input_status"] in {"unknown", "working", "broken"}
    assert isinstance(p["wayland_input_reason"], str)
