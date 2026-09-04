#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path


import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, GLib

APP_ID = "org.pcgamemanager.Manager"
APP_NAME = "PC Game Manager"
BASE = Path(__file__).resolve().parent
LIB_DIR = BASE / "lib"
sys.path.insert(0, str(LIB_DIR))
from runners import discover as discover_runners  # noqa: E402
from runtime_manager import catalog_all, download_and_install  # noqa: E402
from renderer_manager import catalog_all as renderer_catalog_all, discover as discover_renderers, install as install_renderer, remove as remove_renderer  # noqa: E402

PROFILE_DIR = Path.home() / ".config" / "pc-game-manager" / "profiles"
DEFAULT_STORAGE = Path("/run/media") / os.environ.get("USER", Path.home().name) / "Data" / "PCGameManager"
if not DEFAULT_STORAGE.parent.exists():
    DEFAULT_STORAGE = Path.home() / "PCGameManager"

CAPABILITIES = ("wayland", "xwayland", "vulkan", "audio", "input", "cdemu", "udisks2")
RENDERERS = ("auto", "wined3d", "dxvk", "d7vk", "dgvoodoo")
ARCHES = ("win64", "win32")
WINDOWS = (
    ("Windows 11", "windows11"), ("Windows 10", "windows10"), ("Windows 8.1", "windows81"),
    ("Windows 8", "windows8"), ("Windows 7", "windows7"), ("Windows XP 64", "windowsxp64"),
    ("Windows XP", "windowsxp"), ("Windows 2003", "windows2003"), ("Windows 2000", "windows2000"),
    ("Windows ME", "windowsme"), ("Windows 98", "windows98"), ("Windows 95", "windows95"),
)
DEFAULT_DEPS = ["corefonts", "vcrun2022", "d3dcompiler_47", "faudio"]


def capture(argv: list[str]) -> tuple[int, str]:
    try:
        p = subprocess.run(argv, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
        return p.returncode, p.stdout.strip()
    except FileNotFoundError:
        return 127, f"Comando non trovato: {argv[0]}"
    except Exception as exc:
        return 1, str(exc)


def safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._") or "profilo"


def detect_gpus() -> list[dict]:
    code, out = capture(["lspci", "-Dnn"])
    if code != 0:
        return [{"mode": "auto", "label": "Automatico / predefinita", "model": "Automatico"}]
    found: list[dict] = []
    for line in out.splitlines():
        if not re.search(r"(?:VGA compatible controller|3D controller|Display controller)", line, re.I):
            continue
        m = re.match(r"([0-9a-fA-F:.]+)\s+", line)
        pairs = re.findall(r"\[([0-9a-fA-F]{4}):([0-9a-fA-F]{4})\]", line)
        if not m or not pairs:
            continue
        pci = m.group(1).lower(); vid, did = pairs[-1]
        desc = line[m.end():].strip()
        desc = re.sub(r"\s*\[[0-9a-fA-F]{4}:[0-9a-fA-F]{4}\].*$", "", desc).strip()
        desc = re.sub(r"^(VGA compatible controller|3D controller|Display controller)\s*:\s*", "", desc, flags=re.I)
        found.append({"mode": "pci", "pci": pci, "vendor_device": f"{vid.lower()}:{did.lower()}", "label": f"{desc} â€” {pci}", "model": desc})
    found.sort(key=lambda x: (x["model"].lower(), x["pci"]))
    return [{"mode": "auto", "label": "Automatico / predefinita", "model": "Automatico"}] + found


def file_dialog_folder(parent, callback):
    Gtk.FileDialog().select_folder(parent, None, callback)


def file_dialog_file(parent, callback):
    Gtk.FileDialog().open(parent, None, callback)


class PrefixWizard(Gtk.Window):
    def __init__(self, parent: "ManagerWindow"):
        super().__init__(title="Nuovo gioco e nuovo ambiente", transient_for=parent, modal=True, default_width=920, default_height=780)
        self.parent_window = parent
        self.pages: list[Gtk.Widget] = []
        self.step = 0
        self.runner_options: list[dict] = []
        self._path_defaults: dict[str, str] = {}
        self._make_ui()

    def _make_ui(self):
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        root.set_margin_top(16); root.set_margin_bottom(16); root.set_margin_start(16); root.set_margin_end(16)
        self.set_child(root)
        self.heading = Gtk.Label(xalign=0); self.heading.add_css_class("title-2"); root.append(self.heading)
        self.stack = Gtk.Stack(); self.stack.set_vexpand(True); self.stack.set_hexpand(True); root.append(self.stack)
        self.page_game(); self.page_storage(); self.page_runtime(); self.page_graphics(); self.page_sandbox(); self.page_deps(); self.page_summary()
        nav = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8); root.append(nav)
        self.back = Gtk.Button(label="Indietro"); self.back.connect("clicked", lambda _b: self.show_step(self.step - 1)); nav.append(self.back)
        cancel = Gtk.Button(label="Annulla"); cancel.connect("clicked", lambda _b: self.close()); nav.append(cancel)
        self.next = Gtk.Button(label="Avanti"); self.next.add_css_class("suggested-action"); self.next.connect("clicked", self.next_clicked); nav.append(self.next)
        self.show_step(0)

    def page(self, title: str) -> Gtk.Box:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_top(10); box.set_margin_bottom(10); box.set_margin_start(4); box.set_margin_end(4)
        sc = Gtk.ScrolledWindow(); sc.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC); sc.set_vexpand(True); sc.set_child(box)
        self.pages.append(sc); self.stack.add_titled(sc, str(len(self.pages) - 1), title)
        return box

    def entry(self, box, label, value="", folder=False, file=False):
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8); box.append(row)
        row.append(Gtk.Label(label=label, xalign=0)); e = Gtk.Entry(); e.set_hexpand(True); e.set_text(value); row.append(e)
        if folder or file:
            b = Gtk.Button(label="Scegliâ€¦"); row.append(b)
            if folder:
                b.connect("clicked", lambda _b: file_dialog_folder(self, lambda d, r: self._finish_folder(d, r, e)))
            else:
                b.connect("clicked", lambda _b: file_dialog_file(self, lambda d, r: self._finish_file(d, r, e)))
        return e

    def _finish_folder(self, d, r, e):
        try: e.set_text(d.select_folder_finish(r).get_path())
        except GLib.Error: pass

    def _finish_file(self, d, r, e):
        try: e.set_text(d.open_finish(r).get_path())
        except GLib.Error: pass

    def page_game(self):
        box = self.page("Gioco")
        self.name = self.entry(box, "Nome gioco/profilo")
        self.name.connect("changed", self._profile_name_changed)
        self.game_root = self.entry(box, "Directory del gioco", folder=True)
        self.exe = self.entry(box, "Eseguibile Windows", file=True)
        self.exe_label = Gtk.Label(label="L'eseguibile viene montato dentro la sandbox sotto /game.", xalign=0, wrap=True); self.exe_label.add_css_class("dim-label"); box.append(self.exe_label)

    def page_storage(self):
        box = self.page("Archiviazione")
        root = str(DEFAULT_STORAGE)
        self.storage = self.entry(box, "Radice dati manager", root, folder=True)
        self.prefix = self.entry(box, "Prefix permanente", str(DEFAULT_STORAGE / "nuovo" / "prefix"), folder=True)
        self.saves = self.entry(box, "Salvataggi", str(DEFAULT_STORAGE / "nuovo" / "saves"), folder=True)
        self.home = self.entry(box, "HOME sandbox", str(DEFAUL_STORAGE / "nuovo" / "home"), folder=True)
        self._path_defaults = {"prefix": self.prefix.get_text(), "saves": self.saves.get_text(), "home": self.home.get_text()}
        self.separate_game = Gtk.CheckButton(label="Mantieni separati gioco, prefix, HOME e salvataggi"); self.separate_game.set_active(True); box.append(self.separate_game)
        self.game_ro = Gtk.CheckButton(label="Monta la directory del gioco in sola lettura (consigliato)"); self.game_ro.set_active(True); box.append(self.game_ro)
        n = Gtk.Label(label="I percorsi sono permanenti sull'host. Il gioco non riceve accesso alla HOME reale.", xalign=0, wrap=True); n.add_css_class("dim-label"); box.append(n)

    def _profile_name_changed(self, _entry):
        raw = self.name.get_text().strip()
        stem = safe_name(raw) if raw else "nuovo"
        values = {
            "prefix": str(DEFAULT_STORAGE / stem / "prefix"),
            "saves": str(DEFAULT_STORAGE / stem / "saves"),
            "home": str(DEFAULT_STORAGE / stem / "home"),
        }
        for key, widget in (("prefix", self.prefix), ("saves", self.saves), ("home", self.home)):
            if widget.get_text() == self._path_defaults.get(key) or widget.get_text().endswith("/nuovo/" + key):
                widget.set_text(values[key])
            self._path_defaults[key] = values[key]

    def page_runtime(self):
        box = self.page("Runtime")
        self.arch = Gtk.DropDown.new_from_strings(list(ARCHES)); self.arch.set_selected(0); self._row(box, "Architetura", self.arch)
        self.winver = Gtk.DropDown.new_from_strings([x[0] for x in WINDOWS]); self.winver.set_selected(1); self._row(box, "Versione Windows", self.winver)
        self.runner_combo = Gtk.DropDown.new_from_strings(["Wine di sistema"]); self._row(box, "Runtime", self.runner_combo)
        refresh = Gtk.Button(label="Aggiorna runtime locali"); refresh.connect("clicked", lambda _b: self.refresh_runners()); box.append(refresh)
        self.bootstrap = Gtk.CheckButton(label="Crea e inizializza automaticamente il prefix"); self.bootstrap.set_active(True); box.append(self.bootstrap)
        self.refresh_runners()

    def refresh_runners(self):
        self.runner_options = discover_runners()
        labels = ["Wine di sistema"] + [f"{x['name']} â€” {x['source']}" for x in self.runner_options if x.get("kind") != "wine-system"]
        self.runner_combo.set_model(Gtk.StringList.new(labels)); self.runner_combo.set_selected(0)

    def _row(self, box, label, widget):
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12); row.append(Gtk.Label(label=label, xalign=0)); widget.set_hexpand(True); row.append(widget); box.append(row)

    def page_graphics(self):
        box = self.page("Grafica")
        self.gpu = Gtk.DropDown.new_from_strings([x["label"] for x in self.parent_window.gpu_devices]); self.gpu.set_selected(0); self._row(box, "GPU", self.gpu)
        self.renderer = Gtk.DropDown.new_from_strings(["Auto", "WineD3D", "DXVK", "D7VK", "dgVoodoo2"]); self.renderer.set_selected(1); self._row(box, "Renderer", self.renderer)
        self.renderer_path = self.entry(box, "Directory runtime renderer", "", folder=True)
        self.display_backend = Gtk.DropDown.new_from_strings(["Auto (Wayland â†’ XWayland)", "Wayland nativo", "XWayland"]); self.display_backend.set_selected(0); self._row(box, "Backend display/input", self.display_backend)
        self.xwayland_fallback = Gtk.CheckButton(label="Fallback automatico a XWayland"); self.xwayland_fallback.set_active(True); box.append(self.xwayland_fallback)
        self.cap_vulkan = Gtk.CheckButton(label="Abilita Vulkan"); self.cap_vulkan.set_active(True); box.append(self.cap_vulkan)
        note = Gtk.Label(label="La GPU viene salvata tramite PCI/vendor-device, mai tramite revision code.", xalign=0, wrap=True); note.add_css_class("dim-label"); box.append(note)

    def _audio_test(self, _b):
        self.audio_status.set_text("Test eseguito dal manager: verifica disponibile nel registro Log.")
        self.parent_window.run_host_test_audio()

    def page_sandbox(self):
        box = self.page("Sandbox")
        self.cap_wayland = self._check(box, "Wayland", True)
        self.cap_xwayland = self._check(box, "XWayland", True)
        self.cap_input = self._check(box, "Controller / input selezionato", True)
        self.cap_cdemu = self._check(box, "CDEmu (non ancora implementato)", False); self.cap_cdemu.set_sensitive(False)
        self.cap_udisks = self._check(box, "UDisks2 mediato (non ancora implementato)", False); self.cap_udisks.set_sensitive(False)
        self.network = Gtk.DropDown.new_from_strings(["Rete isolata", "Condividi rete host (Internet + LAN)"]); self.network.set_selected(0); self._row(box, "Rete", self.network)
        box.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
        audio = Gtk.Label(label="Audio", xalign=0); audio.add_css_class("title-4"); box.append(audio)
        self.audio_backend = Gtk.DropDown.new_from_strings(["PipeWire / PulseAudio", "Disabilitato"]); self.audio_backend.set_selected(0); self._row(box, "Backend", self.audio_backend)
        self.cap_audio = Gtk.CheckButton(label="Esponi PipeWire/PulseAudio al gioco"); self.cap_audio.set_active(True); box.append(self.cap_audio)
        test = Gtk.Button(label="Test audio host â†’ sandbox"); test.connect("clicked", self._audio_test); box.append(test)
        self.audio_status = Gtk.Label(label="Non testato", xalign=0); box.append(self.audio_status)
        n = Gtk.Label(label="Dipendenze/codec usano temporaneamente la rete host. Il runtime Wine non riceve /dev/snd/seq.", xalign=0, wrap=True); n.add_css_class("dim-label"); box.append(n)

    def _check(self, box, text, active):
        cb = Gtk.CheckButton(label=text); cb.set_active(active); box.append(cb); return cb

    def page_deps(self):
        box = self.page("Dipendenze")
        self.deps: dict[str, Gtk.CheckButton] = {}
        labels = [("corefonts", "Microsoft Core Fonts"), ("vcrun2022", "Visual C++ 2015â€“2022"), ("d3dcompiler_47", "Direct3D compiler 47"), ("faudio", "FAudio / XAudio"), ("xact", "XACT audio runtime"), ("dotnet48", ".NET Framework 4.8")]
        for key, label in labels:
            self.deps[key] = self._check(box, label, key in DEFAULT_DEPS)
        self.custom_dep = self.entry(box, "Winetricks verb personalizzato")
        n = Gtk.Label(label="Le dipendenze vengono installate dopo il bootstrap, usando una sessione di rete temporanea.", xalign=0, wrap=True); n.add_css_class("dim-label"); box.append(n)

    def page_summary(self):
        box = self.page("Riepilogo")
        self.summary = Gtk.Label(xalign=0, yalign=0, wrap=True); box.append(self.summary)

    def show_step(self, idx):
        self.step = max(0, min(idx, len(self.pages) - 1)); self.stack.set_visible_child(self.pages[self.step])
        titles = ["1. Gioco", "2. Archiviazione", "3. Runtime", "4. Grafica", "5. Sandbox e audio", "6. Dipendenze", "7. Riepilogo"]
        self.heading.set_text(titles[self.step]); self.back.set_sensitive(self.step > 0); self.next.set_label("Crea ambiente" if self.step == len(self.pages) - 1 else "Avanti")
        if self.step == len(self.pages) - 1:
            self.summary.set_text(self.summary_text())

    def summary_text(self) -> str:
        gpu = self.parent_window.gpu_devices[self.gpu.get_selected()] if self.gpu.get_selected() < len(self.parent_window.gpu_devices) else {"label": "Auto"}
        deps = [k for k, cb in self.deps.items() if cb.get_active()]
        custom = self.custom_dep.get_text().strip()
        if custom: deps.extend(custom.split())
        runtime = "Wine di sistema" if self.runner_combo.get_selected() == 0 else self.runner_options[self.runner_combo.get_selected() - 1].get("name", "runtime")
        return "\n".join([
            f"Profilo: {self.name.get_text().strip()}", f"Gioco: {self.game_root.get_text().strip()}", f"EZE: {self.exe.get_text().strip()}",
            f"Prefix: {self.prefix.get_text().strip()}", f"HOME sandbox: {self.home.get_text().strip()}", f"Salvataggi: {self.saves.get_text().strip()}",
            f"Runtime: {runtime}", f"Architettura: {ARCHES[self.arch.get_selected()]}", f"Windows: {self.winver.get_selected_item().get_string() if self.winver.get_selected_item() else 'Windows 10'}",
            f"GPU: {gpu.get('label', 'Auto')}", f"Renderer: {RENDERERS[self.renderer.get_selected()]}", f"Display/input: {self.display_backend.get_selected() == 0 and 'Auto (Wayland â†’ XWayland)' or self.display_backend.get_selected() == 1 and 'Wayland nativo' or 'XWayland'}",
            f"Audio: {'PipeWire/PulseAudio' if self.audio_backend.get_selected() == 0 else 'disabilitato'}",
            f"Dipendenze: {', '.join(deps) if deps else 'nessuna'}", f"Rete runtime: {'consentita' if self.network.get_selected() == 1 else 'negata'}",
            "\nCrea ambiente eseguirÃ  bootstrap del prefix e poi installerÃ  le dipendenze selezionate.",
        ])

    def next_clicked(self, _b):
        if self.step < len(self.pages) - 1:
            if self.step == 0:
                if not all(x.get_text().strip() for x in (self.name, self.game_root, self.exe)):
                    self.parent_window.log("Compila nome, directory gioco ed eseguibile."); return
            self.show_step(self.step + 1)
        else:
            self.create_profile()

    def create_profile(self):
        name = safe_name(self.name.get_text().strip())
        path = PROFILE_DIR / f"{name}.json"; i = 2
        while path.exists(): path = PROFILE_DIR / f"{name}-{i}.json"; i += 1
        storage = Path(self.storage.get_text().strip()).expanduser().resolve()
        root = storage / path.stem
        prefix = Path(self.prefix.get_text().strip() or root / "prefix").expanduser().resolve()
        saves = Path(self.saves.get_text().strip() or root / "saves").expanduser().resolve()
        home = Path(self.home.get_text().strip() or root / "home").expanduser().resolve()
        gpu = self.parent_window.gpu_devices[self.gpu.get_selected()]
        renderer = RENDERERS[self.renderer.get_selected()]
        selected = None if self.runner_combo.get_selected() == 0 else self.runner_options[self.runner_combo.get_selected() - 1]
        deps = [k for k, cb in self.deps.items() if cb.get_active()]
        custom = self.custom_dep.get_text().strip()
        if custom: deps.extend(custom.split())
        data = {
            "name": self.name.get_text().strip(), "game_root": str(Path(self.game_root.get_text().strip()).expanduser().resolve()),
            "executable": str(Path(self.exe.get_text().strip()).expanduser().resolve()), "prefix_root": str(prefix), "save_root": str(saves), "sandbox_home": str(home),
            "game_root_readonly": self.game_ro.get_active(),
            "wineprefix": "/prefix", "wine_arch": ARCHES[self.arch.get_selected()],
            "windows_version": WINDOWS[self.winver.get_selected()][1], "selected_runner": selected,
            "gpu": {k: gpu[k] for k in ("mode", "pci", "vendor_device", "model") if k in gpu},
            "renderer": renderer, "renderer_path": self.renderer_path.get_text().strip(),
            "display_backend": ("auto", "wayland", "xwayland")[self.display_backend.get_selected()],
            "xwayland_fallback": self.xwayland_fallback.get_active(),
            "network": "host" if self.network.get_selected() == 1 else "none", "audio_backend": "pulse" if self.audio_backend.get_selected() == 0 else "disabled",
            "capabilities": {"wayland": self.cap_wayland.get_active(), "xwayland": self.cap_xwayland.get_active(), "vulkan": self.cap_vulkan.get_active(),
                             "audio": self.cap_audio.get_active(), "input": self.cap_input.get_active(), "cdemu": self.cap_cdemu.get_active(), "udisks2": self.cap_udisks.get_active()},
            "input_devices": [], "allowed_paths": [], "discs": [], "dependencies": deps, "prefix_bootstrap": self.bootstrap.get_active(),
        }
        try:
            Path(data["game_root"]).mkdir(parents=True, exist_ok=True) if False else None
            for d in (prefix, saves, home): d.mkdir(parents=True, exist_ok=True)
            PROFILE_DIR.mkdir(parents=True, exist_ok=True); path.write_text(json.dumps(data, indent=2), encoding="utf-8")
            self.close(); self.parent_window.load_profiles(select_path=path); self.parent_window.bootstrap_and_deps(path, deps, self.bootstrap.get_active())
        except Exception as exc:
            self.parent_window.log(f"Creazione profilo fallita: {exc}")


class ManagerWindow(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title=APP_NAME, default_width=1320, default_height=900)
        self.current_profile: Path | None = None; self.profile: dict = {}; self.gpu_devices = detect_gpus(); self.runner_catalog: list[dict] = []; self.runner_local: list[dict] = []
        self._loading = False; self.build_ui(); self.load_profiles(); self.refresh_runtime_local(); self.refresh_catalog(); self.refresh_renderer_local(); self.refresh_renderer_catalog(); self.refresh_gpus()

    def build_ui(self):
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL); self.set_child(root)
        hb = Gtk.HeaderBar(); title = Gtk.Label(label=APP_NAME); title.add_css_class("title-3"); hb.set_title_widget(title); root.append(hb)
        main = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL); main.set_position(285); main.set_vexpand(True); root.append(main)
        side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8); side.set_margin_top(12); side.set_margin_bottom(12); side.set_margin_start(12); side.set_margin_end(12); main.set_start_child(side)
        side.append(Gtk.Label(label="Giochi e profili", xalign=0))
        self.profile_list = Gtk.ListBox(); self.profile_list.connect("row-selected", self.profile_selected); sc = Gtk.ScrolledWindow(); sc.set_vexpand(True); sc.set_child(self.profile_list); side.append(sc)
        nb = Gtk.Button(label="+ Nuovo gioco / ambiente"); nb.add_css_class("suggested-action"); nb.connect("clicked", lambda _b: PrefixWizard(self).present()); side.append(nb)
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6); side.append(row)
        for label, cb, css in (("Elimina", self.delete_profile, "destructive-action"), ("Rebuild", self.rebuild_profile, "")):
            b = Gtk.Button(label=label); b.connect("clicked", cb); b.add_css_class(css) if css else None; row.append(b)
        main.set_end_child(self.build_tabs())

    def build_tabs(self):
        tabs = Gtk.Notebook(); tabs.set_vexpand(True); tabs.set_hexpand(True)
        tabs.append_page(self.tab_launch(), Gtk.Label(label="Avvio"))
        tabs.append_page(self.tab_profile(), Gtk.Label(label="Profilo"))
        tabs.append_page(self.tab_runtime(), Gtk.Label(label="Runtime"))
        tabs.append_page(self.tab_graphics(), Gtk.Label(label="Grafica"))
        tabs.append_page(self.tab_sandbox(), Gtk.Label(label="Sandbox"))
        tabs.append_page(self.tab_dependencies(), Gtk.Label(label="Dipendenze"))
        tabs.append_page(self.tab_access(), Gtk.Label(label="File e dischi"))
        tabs.append_page(self.tab_logs(), Gtk.Label(label="Log"))
        self.tabs = tabs
        return tabs

    def scrolled_box(self):
        s = Gtk.ScrolledWindow(); s.set_vexpand(True); s.set_hexpand(True); b = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12); b.set_margin_top(16); b.set_margin_bottom(16); b.set_margin_start(18); b.set_margin_end(18); s.set_child(b); return s, b

    def tab_launch(self):
        s, b = self.scrolled_box(); self.launch_status = Gtk.Label(label="Seleziona un gioco", xalign=0, wrap=True); b.append(self.launch_status)
        self.exe_override = self.mk_entry(b, "Eseguibile principale", browse_file=True)
        r = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8); b.append(r)
        go = Gtk.Button(label="Avvia gioco"); go.add_css_class("suggested-action"); go.connect("clicked", self.launch_game); r.append(go)
        cfg = Gtk.Button(label="winecfg"); cfg.connect("clicked", lambda _b: self.run_profile_tool(["winecfg"], "winecfg")); r.append(cfg)
        term = Gtk.Button(label="Shell sandbox"); term.connect("clicked", lambda _b: self.run_profile_tool(["sh"], "shell")); r.append(term)
        b.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
        b.append(Gtk.Label(label="Installazione e programmi nel prefix", xalign=0))
        self.prefix_exe = self.mk_entry(b, "EXE / installer", browse_file=True)
        rr = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8); b.append(rr)
        ie = Gtk.Button(label="Installa programma / gioco"); ie.connect("clicked", lambda _b: self._run_selected_file("installer")); rr.append(ie)
        rp = Gtk.Button(label="Esegui EXE nel prefix"); rp.connect("clicked", lambda _b: self._run_selected_file("prefix-exe")); rr.append(rp)
        note = Gtk.Label(label="L'azione monta temporaneamente la directory dell'EXE nella sandbox e avvia l'eseguibile con il prefix del profilo. Il prefix resta scrivibile; la rete segue le impostazioni del profilo.", xalign=0, wrap=True); note.add_css_class("dim-label"); b.append(note)
        return s

    def tab_profile(self):
        s, b = self.scrolled_box()
        identity = Gtk.Label(label="Gioco e percorsi", xalign=0); identity.add_css_class("title-4"); b.append(identity)
        self.p_name = self.mk_entry(b, "Nome")
        self.p_game = self.mk_entry(b, "Directory gioco", True)
        self.p_exe = self.mk_entry(b, "Eseguibile", browse_file=True)
        self.p_prefix = self.mk_entry(b, "Prefix", True)
        self.p_home = self.mk_entry(b, "HOME sandbox", True)
        self.p_saves = self.mk_entry(b, "Salvataggi", True)
        b.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
        compat = Gtk.Label(label="CompatibilitÃ  Wine", xalign=0); compat.add_css_class("title-4"); b.append(compat)
        self.p_arch_combo = Gtk.DropDown.new_from_strings(["64 bit (WoW64)", "32 bit"]); self._row(b, "Architettura prefix", self.p_arch_combo)
        self.p_windows_combo = Gtk.DropDown.new_from_strings([x[0] for x in WINDOWS]); self._row(b, "Versione Windows", self.p_windows_combo)
        note = Gtk.Label(label="La versione Windows puÃ² essere applicata a un prefix esistente. Cambiare 64/32 bit richiede invece la ricreazione del prefix.", xalign=0, wrap=True); note.add_css_class("dim-label"); b.append(note)
        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8); b.append(actions)
        save = Gtk.Button(label="Salva modifiche"); save.connect("clicked", self.save_profile); actions.append(save)
        apply_compat = Gtk.Button(label="Applica impostazioni Wine al prefix"); apply_compat.connect("clicked", self.apply_wine_profile_settings); actions.append(apply_compat)
        return s

    def tab_runtime(self):
        tabs = Gtk.Notebook(); tabs.set_vexpand(True); tabs.set_hexpand(True)
        s, b = self.scrolled_box(); self.runtime_status = Gtk.Label(label="Runtime locali", xalign=0); b.append(self.runtime_status)
        self.runtime_choice = Gtk.DropDown.new_from_strings(["Wine di sistema"]); self.runtime_choice.connect("notify::selected", self.runtime_choice_changed); b.append(self.runtime_choice)
        self.runtime_list = Gtk.ListBox(); self.runtime_list.set_vexpand(True); b.append(self.runtime_list)
        rr = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8); b.append(rr)
        for label, cb in (("Aggiorna locali", self.refresh_runtime_local), ("Aggiorna catalogo", self.refresh_catalog), ("Installa selezionato", self.install_selected_runtime), ("Disinstalla selezionato", self.remove_selected_runtime)):
            x = Gtk.Button(label=label); x.connect("clicked", cb); rr.append(x)
        self.catalog_status = Gtk.Label(label="Catalogo non ancora caricato", xalign=0, wrap=True); b.append(self.catalog_status); self.catalog_list = Gtk.ListBox(); self.catalog_list.set_vexpand(True); b.append(self.catalog_list)
        tabs.append_page(s, Gtk.Label(label="Wine / Proton"))

        gs, gb = self.scrolled_box()
        title = Gtk.Label(label="Componenti grafici condivisi", xalign=0); title.add_css_class("title-4"); gb.append(title)
        note = Gtk.Label(label="Installa qui DXVK, D7VK, VKD3D-Proton, DXVK-NVAPI e dgVoodoo2. La scheda Grafica decide quali componenti usa il singolo profilo.", xalign=0, wrap=True); note.add_css_class("dim-label"); gb.append(note)
        self.renderer_local_status = Gtk.Label(label="Componenti locali", xalign=0, wrap=True); gb.append(self.renderer_local_status)
        self.renderer_local_list = Gtk.ListBox(); self.renderer_local_list.set_vexpand(True); gb.append(self.renderer_local_list)
        lr = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8); gb.append(lr)
        for label, cb in (("Aggiorna locali", self.refresh_renderer_local), ("Usa nel profilo", self.use_selected_renderer), ("Rimuovi selezionato", self.remove_selected_renderer)):
            bt = Gtk.Button(label=label); bt.connect("clicked", cb); lr.append(bt)
        self.renderer_catalog_status = Gtk.Label(label="Catalogo grafico non ancora caricato", xalign=0, wrap=True); gb.append(self.renderer_catalog_status)
        self.renderer_catalog_list = Gtk.ListBox(); self.renderer_catalog_list.set_vexpand(True); gb.append(self.renderer_catalog_list)
        cr = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8); gb.append(cr)
        for label, cb in (("Aggiorna catalogo", self.refresh_renderer_catalog), ("Installa selezionato", self.install_selected_renderer)):
            bt = Gtk.Button(label=label); bt.connect("clicked", cb); cr.append(bt)
        tabs.append_page(gs, Gtk.Label(label="Componenti grafici"))
        return tabs

    def tab_graphics(self):
        s, b = self.scrolled_box(); self.gpu_combo = Gtk.DropDown.new_from_strings([]); self.gpu_combo.connect("notify::selected", self.changed_save); self._row(b, "GPU", self.gpu_combo)
        renderer_title = Gtk.Label(label="Renderer del profilo", xalign=0); renderer_title.add_css_class("title-4"); b.append(renderer_title)
        self.renderer_combo = Gtk.DropDown.new_from_strings(["Auto", "WineD3D (nessun override)", "DXVK", "D7VK", "dgVoodoo2"]); self.renderer_combo.connect("notify::selected", self._renderer_changed); self._row(b, "Renderer", self.renderer_combo)
        self.renderer_path_entry = self.mk_entry(b, "Runtime renderer", True); self.renderer_auto_status = Gtk.Label(xalign=0, wrap=True); self.renderer_auto_status.add_css_class("dim-label"); b.append(self.renderer_auto_status)
        renderer_actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8); b.append(renderer_actions)
        detect = Gtk.Button(label="Rileva automaticamente"); detect.connect("clicked", lambda _b: self.autodetect_renderer_path()); renderer_actions.append(detect)
        disable_renderer = Gtk.Button(label="Nessun renderer esterno"); disable_renderer.connect("clicked", self.disable_external_renderer); renderer_actions.append(disable_renderer)
        b.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
        addon_title = Gtk.Label(label="Componenti grafici aggiuntivi", xalign=0); addon_title.add_css_class("title-4"); b.append(addon_title)
        self.vkd3d_enable = Gtk.CheckButton(label="Abilita VKD3D-Proton per Direct3D 12"); self.vkd3d_enable.connect("toggled", self.changed_save); b.append(self.vkd3d_enable)
        self.vkd3d_path_entry = self.mk_entry(b, "Runtime VKD3D-Proton", True)
        self.nvapi_enable = Gtk.CheckButton(label="Abilita DXVK-NVAPI"); self.nvapi_enable.connect("toggled", self.changed_save); b.append(self.nvapi_enable)
        self.nvapi_path_entry = self.mk_entry(b, "Runtime DXVK-NVAPI", True)
        clear_addons = Gtk.Button(label="Disattiva e deseleziona componenti opzionali"); clear_addons.connect("clicked", self.clear_graphics_addons); b.append(clear_addons)
        dg_note = Gtk.Label(label="dgVoodoo2 Ã¨ disponibile come renderer/wrapper per DirectDraw, vecchi Direct3D e Glide. PuÃ² essere combinato con un backend D3D moderno in revisioni successive.", xalign=0, wrap=True); dg_note.add_css_class("dim-label"); b.append(dg_note)
        b.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
        display_title = Gtk.Label(label="Display e input finestra", xalign=0); display_title.add_css_class("title-4"); b.append(display_title)
        self.display_backend_combo = Gtk.DropDown.new_from_strings(["Auto (Wayland â†’ XWayland)", "Wayland nativo", "XWayland"]); self.display_backend_combo.connect("notify::selected", self.changed_save); self._row(b, "Backend display/input", self.display_backend_combo)
        self.xwayland_fallback_cb = Gtk.CheckButton(label="Fallback automatico a XWayland se Wayland non avvia correttamente"); self.xwayland_fallback_cb.connect("toggled", self.changed_save); b.append(self.xwayland_fallback_cb)
        self.wayland_status_label = Gtk.Label(label="Stato input Wayland: non verificato", xalign=0, wrap=True); self.wayland_status_label.add_css_class("dim-label"); b.append(self.wayland_status_label)
        wr = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8); b.append(wr)
        mark_broken = Gtk.Button(label="Segna Wayland non funzionante"); mark_broken.connect("clicked", self.mark_wayland_broken); wr.append(mark_broken)
        retry_wayland = Gtk.Button(label="Riprova Wayland"); retry_wayland.connect("clicked", self.retry_wayland); wr.append(retry_wayland)
        mark_working = Gtk.Button(label="Segna Wayland funzionante"); mark_working.connect("clicked", self.mark_wayland_working); wr.append(mark_working)
        debug_wayland = Gtk.Button(label="Diagnostica input Wayland"); debug_wayland.connect("clicked", self.debug_wayland_input); wr.append(debug_wayland)
        self.gpu_info = Gtk.Label(xalign=0, wrap=True); b.append(self.gpu_info); save = Gtk.Button(label="Salva grafica"); save.connect("clicked", self.save_profile); b.append(save); return s

    def tab_sandbox(self):
        s, b = self.scrolled_box(); self.cap_checks = {}
        policy = Gtk.Label(label="Policy di isolamento", xalign=0); policy.add_css_class("title-4"); b.append(policy)
        self.game_root_ro = Gtk.CheckButton(label="Directory del gioco in sola lettura (consigliato)"); self.game_root_ro.connect("toggled", self._sandbox_policy_changed); b.append(self.game_root_ro)
        self.network_combo = Gtk.DropDown.new_from_strings(["Isolata: nessuna rete", "Condividi rete host: Internet + LAN"]); self.network_combo.connect("notify::selected", self._sandbox_policy_changed); self._row(b, "Rete gioco", self.network_combo)
        self.sandbox_policy_info = Gtk.Label(xalign=0, wrap=True); self.sandbox_policy_info.add_css_class("dim-label"); b.append(self.sandbox_policy_info)

        b.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
        exposure = Gtk.Label(label="Interfacticce gesposte", xalign=0); exposure.add_css_class("title-4"); b.append(exposure)
        for key, label, default in (("wayland", "Wayland", True), ("xwayland", "XWayland", True), ("vulkan", "GPU/Vulkan (/dev/dri)", True), ("input", "Controller selezionati (/dev/input esplicito)", True), ("cdemu", "CDEmu (non ancora implementato)", False), ("udisks2", "UDisks2 mediato (non ancora implementato)", False)):
            cb = Gtk.CheckButton(label=label); cb.connect("toggled", self._sandbox_policy_changed); self.cap_checks[key] = cb; b.append(cb)
            if key in {"cdemu", "udisks2"}: cb.set_sensitive(False)
        self.cap_checks["audio"] = Gtk.CheckButton(label="Audio")
        self.cap_checks["audio"].set_visible(False)

        b.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
        audio_title = Gtk.Label(label="Audio", xalign=0); audio_title.add_css_class("title-4"); b.append(audio_title)
        self.audio_combo = Gtk.DropDown.new_from_strings(["PipeWire / PulseAudio", "Disabilitato"]); self.audio_combo.connect("notify::selected", self._sandbox_policy_changed); self._row(b, "Backend", self.audio_combo)
        self.audio_cap = Gtk.CheckButton(label="Esponi socket audio al gioco"); self.audio_cap.set_active(True); self.audio_cap.connect("toggled", self._sandbox_policy_changed); b.append(self.audio_cap)
        test = Gtk.Button(label="Testa audio nella sandbox"); test.connect("clicked", lambda _b: self.run_host_test_audio()); b.append(test)
        self.audio_info = Gtk.Label(label="Il gioco non riceve /dev/snd/seq; Wine usa il socket PulseAudio/PipeWire.", xalign=0, wrap=True); self.audio_info.add_css_class("dim-label"); b.append(self.audio_info)
        save = Gtk.Button(label="Salva sandbox"); save.connect("clicked", self.save_profile); b.append(save)
        self._refresh_sandbox_policy_info()
        return s

    def tab_dependencies(self):
        s, b = self.scrolled_box()
        deps_title = Gtk.Label(label="Runtime e librerie Wine", xalign=0); deps_title.add_css_class("title-4"); b.append(deps_title)
        self.dep_list = Gtk.ListBox(); b.append(self.dep_list); self.dep_input = self.mk_entry(b, "Componente custom / winetricks verb"); self.dep_selected = Gtk.Label(label="Seleziona le dipendenze da installare/riparare", xalign=0); b.append(self.dep_selected)
        for key, label in (("corefonts", "Microsoft Core Fonts"), ("vcrun2022", "Visual C++ 2015â€“2022"), ("d3dcompiler_47", "Direct3D compiler 47"), ("faudio", "FAudio / XAudio"), ("xact", "XACT"), ("dotnet48", ".NET Framework 4.8")):
            cb = Gtk.CheckButton(label=label); cb._dep_key = key; self.dep_list.append(cb)
        r = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8); b.append(r); zh=Gtk.Button(label="Installa/Ripara selezionate"); zx.connect("clicked", self.install_dependencies); r.append(xx); rb = Gtk.Button(label="Ricrea e reinstalla tutto"); rb.connect("clicked", self.rebuild_and_deps); r.append(rb)
        b.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
        codec_title = Gtk.Label(label="Codec video retro", xalign=0); codec_title.add_css_class("title-4"); b.append(codec_title)
        self.retro_codec_combo = Gtk.DropDown.new_from_strings([
            "Pacchetto completo codec retro (allcodecs)", "Indeo (icodecs)", "Cinepak", "MP3 DirectShow (l3codecx)",
            "ffdshow", "Xvid", "LAV Filters", "DirectShow Quartz", "AMStream", "AVIFile32", "Bink Video runtime"
        ]); b.append(self.retro_codec_combo)
        codec_note = Gtk.Label(label="Installazione tramite Winetricks nel prefix. Durante questa operazione viene condivisa temporaneamente la rete host (Internet + LAN).", xalign=0, wrap=True); codec_note.add_css_class("dim-label"); b.append(codec_note)
        codec_btn = Gtk.Button(label="Installa codec selezionato"); codec_btn.connect("clicked", self.install_retro_codec); b.append(codec_btn)
        return s

    def tab_access(self):
        s, b = self.scrolled_box(); self.access_list = Gtk.ListBox(); self.access_list.set_vexpand(True); b.append(self.access_list)
        r = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6); b.append(r)
        for label, ro, directory in (("Aggiungi file RO", True, False), ("Aggiungi file RW", False, False), ("Aggiungi dir RO", True, True), ("Aggiungi dir RW", False, True)):
            x = Gtk.Button(label=label); x.connect("clicked", lambda _b, ro=ro, directory=directory: self.add_access(ro, directory)); r.append(x)
        rm = Gtk.Button(label="Rimuovi selezionato"); rm.connect("clicked", self.remove_access); b.append(rm)
        self.disc_status = Gtk.Label(label="Dischi: gestione immagini/profilo", xalign=0); b.append(self.disc_status)
        disc = self.mk_entry(b, "Immagine disco", browse_file=True); self.disc_entry = disc; dr = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6); b.append(dr)
        for label, cb in (("Aggiungi immagine", self.add_disc), ("Rimuovi", self.remove_disc)):
            x = Gtk.Button(label=label); x.connect("clicked", cb); dr.append(x)
        return s

    def tab_logs(self):
        s, b = self.scrolled_box()
        self.log_view = Gtk.TextView()
        self.log_view.set_editable(False)
        self.log_view.set_monospace(True)
        self.log_view.set_vexpand(True)
        b.append(self.log_view)
        r = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        copy_btn = Gtk.Button(label="Copia log")
        copy_btn.set_tooltip_text("Copia tutto il log negli appunti")
        copy_btn.connect("clicked", self.copy_log)
        r.append(copy_btn)
        clear = Gtk.Button(label="Pulisci log")
        clear.connect("clicked", lambda _b: self.log_view.get_buffer().set_text(""))
        r.append(clear)
        b.append(r)
        return s

    def copy_log(self, _button):
        buf = self.log_view.get_buffer()
        text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)
        clipboard = self.get_clipboard()
        clipboard.set(text)
        self.log("[gui] log copiato negli appunti")

    def _row(self, b, label, widget):
        r = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12); r.append(Gtk.Label(label=label, xalign=0)); widget.set_hexpand(True); r.append(widget); b.append(r)

    def mk_entry(self, b, label, folder=False, browse_file=False):
        r = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8); b.append(r); r.append(Gtk.Label(label=label, xalign=0)); e = Gtk.Entry(); e.set_hexpand(True); r.append(e)
        if folder or browse_file:
            bt = Gtk.Button(label="Scegliâ€¦"); r.append(bt)
            bt.connect("clicked", lambda _b: file_dialog_folder(self, lambda d, rr: self._finish_entry_folder(d, rr, e)) if folder else file_dialog_file(self, lambda d, rr: self._finish_entry_file(d, rr, e)))
        return e

    def _finish_entry_folder(self, d, r, e):
        try: e.set_text(d.select_folder_finish(r).get_path())
        except GLib.Error: pass

    def _finish_entry_file(self, d, r, e):
        try: e.set_text(d.open_finish(r).get_path())
        except GLib.Error: pass

    def log(self, text: str):
        print(text)
        def add():
            buf = self.log_view.get_buffer(); end = buf.get_end_iter(); buf.insert(end, text.rstrip() + "\n")
            return False
        GLib.idle_add(add)

    def load_profiles(self, select_path: Path | None = None):
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        for row in list(self.profile_list): self.profile_list.remove(row)
        paths = sorted(PROFILE_DIR.glob("*.json"))
        for p in paths:
            try: data = json.loads(p.read_text(encoding="utf-8")); name = data.get("name") or p.stem
            except Exception: name = p.stem
            row = Gtk.ListBoxRow(); row._path = p; row.set_child(Gtk.Label(label=name, xalign=0)); self.profile_list.append(row)
            if select_path and p == select_path: self.profile_list.select_row(row)
        if paths and not select_path and not self.current_profile: self.profile_list.select_row(self.profile_list.get_row_at_index(0))

    def profile_selected(self, _list, row):
        if not row: return
        try: data = json.loads(row._path.read_text(encoding="utf-8"))
        except Exception as exc: self.log(f"Profilfehler: {exc}"); return
        self.current_profile = row._path; self.profile = data; self._loading = True
        self.p_name.set_text(data.get("name", row._path.stem)); self.p_game.set_text(data.get("game_root", "")); self.p_exe.set_text(data.get("executable", "")); self.exe_override.set_text(data.get("executable", "")); self.p_prefix.set_text(data.get("prefix_root", "")); self.p_home.set_text(data.get("sandbox_home", "")); self.p_saves.set_text(data.get("save_root", ""))
        self.p_arch_combo.set_selected(1 if data.get("wine_arch", "win64") == "win32" else 0)
        win_value = str(data.get("windows_version", "windows10"))
        win_index = next((i for i, item in enumerate(WINDOWS) if item[1] == win_value), 1)
        self.p_windows_combo.set_selected(win_index)
        self.game_root_ro.set_active(bool(data.get("game_root_readonly", False)))
        self._select_gpu(data.get("gpu", {"mode": "auto"})); self.renderer_combo.set_selected(RENDITER_INDEX(data.get("renderer", "wined3d"))); self.renderer_path_entry.set_text(data.get("renderer_path", "")); 
        legacy_backend = data.get("display_backend")
        if legacy_backend not in {"auto", "wayland", "xwayland"}:
            legacy_backend = "wayland" if data.get("prefer_wayland", True) else "xwayland"
        self.display_backend_combo.set_selected({"auto": 0, "wayland": 1, "xwayland": 2}[legacy_backend]); self.xwayland_fallback_cb.set_active(bool(data.get("xwayland_fallback", True))); self.vkd3d_enable.set_active(bool(data.get("vkd3d_enabled", False))); self.vkd3d_path_entry.set_text(data.get("vkd3d_path", "")); self.nvapi_enable.set_active(bool(data.get("nvapi_enabled", False))); self.nvapi_path_entry.set_text(data.get("nvapi_path", "")); self._renderer_changed(); self._refresh_wayland_status()
        caps = data.get("capabilities", {})
        for k, cb in self.cap_checks.items(): cb.set_active(False if k in {"cdemu", "udisks2"} else bool(caps.get(k, False)))
        self.network_combo.set_selected(1 if data.get("network") == "host" else 0); self.audio_combo.set_selected(0 if data.get("audio_backend", "pulse") != "disabled" else 1); self.audio_cap.set_active(bool(caps.get("audio", True)))
        self.refresh_access(); self.refresh_discs(); self.refresh_dependencies(); self._restore_runtime_choice(); self._refresh_sandbox_policy_info(); self._loading = False
        self.launch_status.set_text(f"{self.p_name.get_text()} â€” {sÍ•±˜¹Á}•á”¹•Ñ}Ñ•áÐ ¥ôˆ¤ìÍ•±˜¹±½œ¡˜‰AÉ½™¥±¼…É¥…Ñ¼èíÉ½Ü¹}Á…Ñ ¹¹…µ•ôˆ¤((€€€‘•˜}É•™É•Í¡}Ý…å±…¹‘}ÍÑ…ÑÕÌ¡Í•±˜¤è(€€€€€€€¥˜¹½Ð¡…Í…ÑÑÈ¡Í•±˜°€‰Ý…å±…¹‘}ÍÑ…ÑÕÍ}±…‰•°ˆ¤è(€€€€€€€€€€€É•ÑÕÉ¸(€€€€€€€ÍÑ…ÑÕÌ€ôÍÑÈ¡Í•±˜¹ÁÉ½™¥±”¹•Ð ‰Ý…å±…¹‘}¥¹ÁÕÑ}ÍÑ…ÑÕÌˆ°€‰Õ¹­¹½Ý¸ˆ¤¤¥˜¥Í¥¹ÍÑ…¹”¡Í•±˜¹ÁÉ½™¥±”°‘¥Ð¤•±Í”€‰Õ¹­¹½Ý¸ˆ(€€€€€€€É•…Í½¸€ôÍÑÈ¡Í•±˜¹ÁÉ½™¥±”¹•Ð ‰Ý…å±…¹‘}¥¹ÁÕÑ}É•…Í½¸ˆ°€ˆˆ¤¤¹ÍÑÉ¥À ¤¥˜¥Í¥¹ÍÑ…¹”¡Í•±˜¹ÁÉ½™¥±”°‘¥Ð¤•±Í”€ˆˆ(€€€€€€€±…‰•±Ì€ôì‰Ý½É­¥¹œˆè€‰™Õ¹é¥½¹…¹Ñ”ˆ°€‰‰É½­•¸ˆè€‰9=8™Õ¹é¥½¹…¹Ñ”ˆ°€‰Õ¹­¹½Ý¸ˆè€‰¹½¸Ù•É¥™¥…Ñ¼‰ô(€€€€€€€Ñ•áÐ€ô˜‰MÑ…Ñ¼¥¹ÁÕÐ]…å±…¹èí±…‰•±Ì¹•Ð¡ÍÑ…ÑÕÌ°ÍÑ…ÑÕÌ¥ôˆ(€€€€€€€¥˜É•…Í½¸è(€€€€€€€€€€€Ñ•áÐ€¬ô˜ˆƒŠPíÉ•…Í½¹ôˆ(€€€€€€€¥˜ÍÑ…ÑÕÌ€ôô€‰‰É½­•¸ˆè(€€€€€€€€€€€Ñ•áÐ€¬ô€ˆƒŠPÕÑ¼ÕÍ•Ë€a]…å±…¹ˆ(€€€€€€€Í•±˜¹Ý…å±…¹‘}ÍÑ…ÑÕÍ}±…‰•°¹Í•Ñ}Ñ•áÐ¡Ñ•áÐ¤((€€€‘•˜}Í•Ñ}Ý…å±…¹‘}ÍÑ…ÑÕÌ¡Í•±˜°ÍÑ…ÑÕÌèÍÑÈ°É•…Í½¸èÍÑÈ€ô€ˆˆ¤è(€€€€€€€¥˜¹½ÐÍ•±˜¹ÕÉÉ•¹Ñ}ÁÉ½™¥±”è(€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹±½œ ‰M•±•é¥½¹„Õ¸ÁÉ½™¥±¼¸ˆ¤(€€€€€€€Í•±˜¹ÁÉ½™¥±•l‰Ý…å±…¹‘}¥¹ÁÕÑ}ÍÑ…ÑÕÌ‰t€ôÍÑ…ÑÕÌ(€€€€€€€Í•±˜¹ÁÉ½™¥±•l‰Ý…å±…¹‘}¥¹ÁÕÑ}É•…Í½¸‰t€ôÉ•…Í½¸(€€€€€€€Í•±˜¹ÕÉÉ•¹Ñ}ÁÉ½™¥±”¹ÝÉ¥Ñ•}Ñ•áÐ¡©Í½¸¹‘ÕµÁÌ¡Í•±˜¹ÁÉ½™¥±”°¥¹‘•¹ÐôÈ¤°•¹½‘¥¹œô‰ÕÑ˜´àˆ¤(€€€€€€€Í•±˜¹}É•™É•Í¡}Ý…å±…¹‘}ÍÑ…ÑÕÌ ¤(€€€€€€€Í•±˜¹±½œ¡˜‰]…å±…¹¥¹ÁÕÐÁ•ÈíÍ•±˜¹ÕÉÉ•¹Ñ}ÁÉ½™¥±”¹¹…µ•ôèíÍÑ…ÑÕÍôˆ€¬€¡˜ˆƒŠPíÉ•…Í½¹ôˆ¥˜É•…Í½¸•±Í”€ˆˆ¤¤((€€€‘•˜µ…É­}Ý…å±…¹‘}‰É½­•¸¡Í•±˜°}ˆõ9½¹”¤è(€€€€€€€Í•±˜¹}Í•Ñ}Ý…å±…¹‘}ÍÑ…ÑÕÌ ‰‰É½­•¸ˆ°€‰¥¹ÁÕÐÑ…ÍÑ¥•É„¹½¸‘¥ÍÁ½¹¥‰¥±”½¸]¥¹•]…å±…¹Á•ÈÅÕ•ÍÑ¼¥½¼ˆ¤((€€€‘•˜µ…É­}Ý…å±…¹‘}Ý½É­¥¹œ¡Í•±˜°}ˆõ9½¹”¤è(€€€€€€€Í•±˜¹}Í•Ñ}Ý…å±…¹‘}ÍÑ…ÑÕÌ ‰Ý½É­¥¹œˆ°€‰¥¹ÁÕÐÑ…ÍÑ¥•É„Ù•É¥™¥…Ñ¼½¸]¥¹•]…å±…¹ˆ¤((€€€‘•˜É•ÑÉå}Ý…å±…¹¡Í•±˜°}ˆõ9½¹”¤è(€€€€€€€Í•±˜¹}Í•Ñ}Ý…å±…¹‘}ÍÑ…ÑÕÌ ‰Õ¹­¹½Ý¸ˆ°€ˆˆ¤(€€€€€€€Í•±˜¹±½œ ‰]…å±…¹Ù•ÉË€É¥Ñ•¹Ñ…Ñ¼…°ÁÉ½ÍÍ¥µ¼…ÙÙ¥¼¥¸µ½‘…±¥Ó ÕÑ¼¸ˆ¤((€€€‘•˜‘•‰Õ}Ý…å±…¹‘}¥¹ÁÕÐ¡Í•±˜°}ˆõ9½¹”¤è(€€€€€€€¥˜¹½ÐÍ•±˜¹ÕÉÉ•¹Ñ}ÁÉ½™¥±”è(€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹±½œ ‰M•±•é¥½¹„Õ¸ÁÉ½™¥±¼¸ˆ¤(€€€€€€€Í•±˜¹Í…Ù•}ÁÉ½™¥±”¡9½¹”¤(€€€€€€€¡•±Á•È€ôÍ¡ÕÑ¥°¹Ý¡¥  ‰ÁŒµ…µ”µÍ…¹‘‰½àˆ¤½ÈÍÑÈ¡	M€¼€‰‰¥¸ˆ€¼€‰ÁŒµ…µ”µÍ…¹‘‰½àˆ¤(€€€€€€€Í•±˜¹}ÍÁ…Ý¹}±½•¡m¡•±Á•È°€‰¥¹ÁÕÐµ‘•‰Õœˆ°ÍÑÈ¡Í•±˜¹ÕÉÉ•¹Ñ}ÁÉ½™¥±”¥t°€‰Ý…å±…¹µ¥¹ÁÕÐˆ¤((€€€‘•˜}Í•±•Ñ}ÁÔ¡Í•±˜°ÁÔ¤è(€€€€€€€Í•±˜¹ÁÕ}½µ‰¼¹Í•Ñ}Í•±•Ñ• À¤(€€€€€€€¥˜ÁÔ¹•Ð ‰µ½‘”ˆ¤€ôô€‰Á¤ˆè(€€€€€€€€€€€™½È¤°¥¸•¹Õµ•É…Ñ”¡Í•±˜¹ÁÕ}‘•Ù¥•Ì¤è(€€€€€€€€€€€€€€€¥˜¹•Ð ‰Á¤ˆ¤€ôôÁÔ¹•Ð ‰Á¤ˆ¤…¹¹•Ð ‰Ù•¹‘½É}‘•Ù¥”ˆ¤€ôôÁÔ¹•Ð ‰Ù•¹‘½É}‘•Ù¥”ˆ¤èÍ•±˜¹ÁÕ}½µ‰¼¹Í•Ñ}Í•±•Ñ•¡¤¤ì‰É•…¬(€€€€€€€¥‘à€ôÍ•±˜¹ÁÕ}½µ‰¼¹•Ñ}Í•±•Ñ• ¤ì€ôÍ•±˜¹ÁÕ}‘•Ù¥•Ím¥‘át¥˜¥‘à€ð±•¸¡Í•±˜¹ÁÕ}‘•Ù¥•Ì¤•±Í”Í•±˜¹ÁÕ}‘•Ù¥•ÍlÁtìÍ•±˜¹ÁÕ}¥¹™¼¹Í•Ñ}Ñ•áÐ¡˜‰5½‘•±±¼èí¹•Ð µ½‘•°œ¥õq¹A$èí¹•Ð Á¤œ°€…ÕÑ¼œ¥õq¹Y•¹‘½Èé•Ù¥”èí¹•Ð Ù•¹‘½É}‘•Ù¥”œ°€…ÕÑ¼œ¥ôˆ¤((€€€‘•˜É•™É•Í¡}ÁÕÌ¡Í•±˜¤è(€€€€€€€Í•±˜¹ÁÕ}‘•Ù¥•Ì€ô‘•Ñ•Ñ}ÁÕÌ ¤ìÍ•±˜¹ÁÕ}½µ‰¼¹Í•Ñ}µ½‘•°¡Ñ¬¹MÑÉ¥¹1¥ÍÐ¹¹•Ü¡m‘l‰±…‰•°‰t™½È¥¸Í•±˜¹ÁÕ}‘•Ù¥•Ít¤¤ìÍ•±˜¹}Í•±•Ñ}ÁÔ¡Í•±˜¹ÁÉ½™¥±”¹•Ð ‰ÁÔˆ°ì‰µ½‘”ˆè€‰…ÕÑ¼‰ô¤¤ìÍ•±˜¹±½œ¡˜‰ATÉ¥±•Ù…Ñ”èíµ…à À°±•¸¡Í•±˜¹ÁÕ}‘•Ù¥•Ì¤´Ä¥ôˆ¤((€€€‘•˜}É•¹‘•É•É}¡…¹•¡Í•±˜°€©}…ÉÌ¤è(€€€€€€€¥˜Í•±˜¹}±½…‘¥¹œè(€€€€€€€€€€€É•ÑÕÉ¸(€€€€€€€¥‘à€ôÍ•±˜¹É•¹‘•É•É}½µ‰¼¹•Ñ}Í•±•Ñ• ¤(€€€€€€€É•¹‘•É•È€ôI9IIMm¥‘át¥˜¥‘à€ð±•¸¡I9IIL¤•±Í”€‰Ý¥¹”Í‘ˆ(€€€€€€€¥˜É•¹‘•É•È¥¸€ ‰…ÕÑ¼ˆ°€‰Ý¥¹”Í‘ˆ¤è(€€€€€€€€€€€Í•±˜¹É•¹‘•É•É}…ÕÑ½}ÍÑ…ÑÕÌ¹Í•Ñ}Ñ•áÐ ‰9•ÍÍÕ¹„10•ÍÑ•É¹„¹••ÍÍ…É¥„¸ˆ¤(€€€€€€€€€€€É•ÑÕÉ¸(€€€€€€€Á…Ñ €ôÍ•±˜¹É•¹‘•É•É}Á…Ñ¡}•¹ÑÉä¹•Ñ}Ñ•áÐ ¤¹ÍÑÉ¥À ¤(€€€€€€€¥˜¹½ÐÁ…Ñ è(€€€€€€€€€€€‘•Ñ•Ñ•€ôÍ•±˜¹…ÕÑ½‘•Ñ•Ñ}É•¹‘•É•É}Á…Ñ ¡±½œõ…±Í”¤(€€€€€€€€€€€¥˜‘•Ñ•Ñ•è(€€€€€€€€€€€€€€€Í•±˜¹É•¹‘•É•É}Á…Ñ¡}•¹ÑÉä¹Í•Ñ}Ñ•áÐ¡ÍÑÈ¡‘•Ñ•Ñ•¤¤(€€€€€€€€€€€€€€€Á…Ñ €ôÍÑÈ¡‘•Ñ•Ñ•¤(€€€€€€€Í•±˜¹É•¹‘•É•É}…ÕÑ½}ÍÑ…ÑÕÌ¹Í•Ñ}Ñ•áÐ¡˜‰íÉ•¹‘•É•È¹ÕÁÁ•È ¥ôèíÁ…Ñ ¥˜Á…Ñ •±Í”€¹•ÍÍÕ¸½µÁ½¹•¹Ñ”±½…±”É¥±•Ù…Ñ¼ôˆ¤((€€€‘•˜…ÕÑ½‘•Ñ•Ñ}É•¹‘•É•É}Á…Ñ ¡Í•±˜°±½œõQÉÕ”¤è(€€€€€€€¥‘à€ôÍ•±˜¹É•¹‘•É•É}½µ‰¼¹•Ñ}Í•±•Ñ• ¤(€€€€€€€É•¹‘•É•È€ôI9IIMm¥‘át¥˜¥‘à€ð±•¸¡I9IIL¤•±Í”€‰Ý¥¹”Í‘ˆ(€€€€€€€¥˜É•¹‘•É•È¥¸€ ‰…ÕÑ¼ˆ°€‰Ý¥¹”Í‘ˆ¤è(€€€€€€€€€€€É•ÑÕÉ¸9½¹”(€€€€€€€É•ÅÕ¥É•€ôì‰‘áÙ¬ˆèì‰ÍÄÄ¹‘±°ˆ°€‰‘á¤¹‘±°‰ô°€‰ÝÙ¬ˆèì‰‘‘É…Ü¹‘±°‰ô°€‰‘Ù½½‘½¼ˆèì‰‘‘É…Ü¹‘±°‰õô¹•Ð¡É•¹‘•É•È°Í•Ð ¤¤(€€€€€€€¥˜¹½ÐÉ•ÅÕ¥É•è(€€€€€€€€€€€É•ÑÕÉ¸9½¹”(€€€€€€€…¹‘¥‘…Ñ•Ì€ômt(€€€€€€€¡½µ”€ôA…Ñ ¹¡½µ” ¤(€€€€€€€…¹‘¥‘…Ñ•Ì€¬ôm¡½µ”€¼€ˆ¹±½…°½Í¡…É”½ÁŒµ…µ”µµ…¹…•È½‘áÙ¬ˆ°¡½µ”€¼€ˆ¹±½…°½Í¡…É”½ÁŒµ…µ”µµ…¹…•È½ÝÙ¬ˆ°A…Ñ  ˆ½ÕÍÈ½Í¡…É”½‘áÙ¬ˆ¤°A…Ñ  ˆ½ÕÍÈ½Í¡…É”½ÝÙ¬ˆ¤°A…Ñ  ˆ½½ÁÐ½‘áÙ¬ˆ¤°A…Ñ  ˆ½½ÁÐ½ÝÙ¬ˆ¥t(€€€€€€€Á…µ…¸€ôÍ¡ÕÑ¥°¹Ý¡¥  ‰Á…µ…¸ˆ¤(€€€€€€€¥˜Á…µ…¸è(€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€½ÕÐ€ôÍÕ‰ÁÉ½•ÍÌ¹¡•­}½ÕÑÁÕÐ¡mÁ…µ…¸°€ˆµE°ˆ°€‰‘áÙ¬ˆ¥˜É•¹‘•É•È€ôô€‰‘áÙ¬ˆ•±Í”€‰ÝÙ¬‰t°Ñ•áÐõQÉÕ”°ÍÑ‘•ÉÈõÍÕ‰ÁÉ½•ÍÌ¹Y9U10¤¥˜É•¹‘•É•È¥¸€ ‰‘áÙ¬ˆ°€‰ÝÙ¬ˆ¤•±Í”€ˆˆ(€€€€€€€€€€€€€€€…¹‘¥‘…Ñ•Ì€¬ômA…Ñ ¡±¥¹”¹ÍÁ±¥Ð¡9½¹”°€Ä¥lÅt¤™½È±¥¹”¥¸½ÕÐ¹ÍÁ±¥Ñ±¥¹•Ì ¤¥˜€ˆ€ˆ¥¸±¥¹”…¹A…Ñ ¡±¥¹”¹ÍÁ±¥Ð¡9½¹”°€Ä¥lÅt¤¹•á¥ÍÑÌ ¥t(€€€€€€€€€€€•á•ÁÐá•ÁÑ¥½¸è(€€€€€€€€€€€€€€€Á…ÍÌ(€€€€€€€™½È‰…Í”¥¸…¹‘¥‘…Ñ•Ìè(€€€€€€€€€€€¥˜‰…Í”¹¥Í}™¥±” ¤…¹‰…Í”¹¹…µ”¹±½Ý•È ¤¥¸íà¹±½Ý•È ¤™½Èà¥¸É•ÅÕ¥É•‘ôè(€€€€€€€€€€€€€€€‰…Í”€ô‰…Í”¹Á…É•¹Ð(€€€€€€€€€€€¥˜‰…Í”¹¥Í}‘¥È ¤è(€€€€€€€€€€€€€€€™½Õ¹€ôíÀ¹¹…µ”¹±½Ý•È ¤™½ÈÀ¥¸‰…Í”¹É±½ˆ ˆ¨¹‘±°ˆ¤¥˜À¹¥Í}™¥±” ¥ô(€€€€€€€€€€€€€€€¥˜É•ÅÕ¥É•¹¥ÍÍÕ‰Í•Ð¡™½Õ¹¤è(€€€€€€€€€€€€€€€€€€€Í•±˜¹É•¹‘•É•É}Á…Ñ¡}•¹ÑÉä¹Í•Ñ}Ñ•áÐ¡ÍÑÈ¡‰…Í”¤¤(€€€€€€€€€€€€€€€€€€€Í•±˜¹É•¹‘•É•É}…ÕÑ½}ÍÑ…ÑÕÌ¹Í•Ñ}Ñ•áÐ¡˜‰íÉ•¹‘•É•È¹ÕÁÁ•È ¥ôÉ¥±•Ù…Ñ¼èí‰…Í•ôˆ¤(€€€€€€€€€€€€€€€€€€€¥˜±½œèÍ•±˜¹±½œ¡˜‰I•¹‘•É•ÈíÉ•¹‘•É•ÉôÉ¥±•Ù…Ñ¼¥¸í‰…Í•ôˆ¤(€€€€€€€€€€€€€€€€€€€É•ÑÕÉ¸‰…Í”(€€€€€€€¥˜±½œèÍ•±˜¹±½œ¡˜‰9•ÍÍÕ¸½µÁ½¹•¹Ñ”íÉ•¹‘•É•È¹ÕÁÁ•È ¥ô±½…±”É¥±•Ù…Ñ¼ìÁÕ½¤¥¹‘¥…É”µ…¹Õ…±µ•¹Ñ”±„‘¥É•Ñ½Éä10¸ˆ¤(€€€€€€€Í•±˜¹É•¹‘•É•É}…ÕÑ½}ÍÑ…ÑÕÌ¹Í•Ñ}Ñ•áÐ¡˜‰íÉ•¹‘•É•È¹ÕÁÁ•È ¥ôè¹•ÍÍÕ¸½µÁ½¹•¹Ñ”±½…±”É¥±•Ù…Ñ¼ˆ¤(€€€€€€€É•ÑÕÉ¸9½¹”((€€€‘•˜}ÉÕ¹}Í•±•Ñ•‘}™¥±”¡Í•±˜°µ½‘”èÍÑÈ¤è(€€€€€€€¥˜¹½ÐÍ•±˜¹ÕÉÉ•¹Ñ}ÁÉ½™¥±”è(€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹±½œ ‰É•„½Í•±•é¥½¹„Õ¸ÁÉ½™¥±¼ÁÉ¥µ„‘¤•Í•Õ¥É”Õ¸¥¹ÍÑ…±±•È¼a¸ˆ¤(€€€€€€€¡½ÍÐ€ôÍ•±˜¹ÁÉ•™¥á}•á”¹•Ñ}Ñ•áÐ ¤¹ÍÑÉ¥À ¤(€€€€€€€¥˜¹½Ð¡½ÍÐè(€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹±½œ ‰M•±•é¥½¹„Õ¸a½¥¹ÍÑ…±±•È¸ˆ¤(€€€€€€€¡•±Á•È€ôÍ¡ÕÑ¥°¹Ý¡¥  ‰ÁŒµ…µ”µÍ…¹‘‰½àˆ¤½ÈÍÑÈ¡	M€¼€‰‰¥¸ˆ€¼€‰ÁŒµ…µ”µÍ…¹‘‰½àˆ¤(€€€€€€€Í•±˜¹Í…Ù•}ÁÉ½™¥±”¡9½¹”¤(€€€€€€€Í•±˜¹}ÍÁ…Ý¹}±½•¡m¡•±Á•È°€‰ÉÕ¸µ™¥±”ˆ°ÍÑÈ¡Í•±˜¹ÕÉÉ•¹Ñ}ÁÉ½™¥±”¤°¡½ÍÑt°µ½‘”¤((€€€‘•˜}ÁÉ½™¥±•}‘…Ñ…}™É½µ}Õ¤¡Í•±˜¤€´ø‘¥Ðè(€€€€€€€€ô‘¥Ð¡Í•±˜¹ÁÉ½™¥±”¤(€€€€€€€¹ÕÁ‘…Ñ”¡ì‰¹…µ”ˆèÍ•±˜¹Á}¹…µ”¹•Ñ}Ñ•áÐ ¤¹ÍÑÉ¥À ¤°€‰…µ•}É½½ÐˆèÍ•±˜¹Á}…µ”¹•Ñ}Ñ•áÐ ¤¹ÍÑÉ¥À ¤°€‰•á•ÕÑ…‰±”ˆèÍ•±˜¹Á}•á”¹•Ñ}Ñ•áÐ ¤¹ÍÑÉ¥À ¤°€‰ÁÉ•™¥á}É½½ÐˆèÍ•±˜¹Á}ÁÉ•™¥à¹•Ñ}Ñ•áÐ ¤¹ÍÑÉ¥À ¤°€‰Í…¹‘‰½á}¡½µ”ˆèÍ•±˜¹Á}¡½µ”¹•Ñ}Ñ•áÐ ¤¹ÍÑÉ¥À ¤°€‰Í…Ù•}É½½ÐˆèÍ•±˜¹Á}Í…Ù•Ì¹•Ñ}Ñ•áÐ ¤¹ÍÑÉ¥À ¥ô¤(€€€€€€€‘l‰Ý¥¹•}…É ‰t€ôI!MmÍ•±˜¹Á}…É¡}½µ‰¼¹•Ñ}Í•±•Ñ• ¤¥˜Í•±˜¹Á}…É¡}½µ‰¼¹•Ñ}Í•±•Ñ• ¤€ð±•¸¡I!L¤•±Í”€Át(€€€€€€€Ý¤€ôÍ•±˜¹Á}Ý¥¹‘½ÝÍ}½µ‰¼¹•Ñ}Í•±•Ñ• ¤¥˜Í•±˜¹Á}Ý¥¹‘½ÝÍ}½µ‰¼¹•Ñ}Í•±•Ñ• ¤€ð±•¸¡]%9=]L¤•±Í”€Ä(€€€€€€€‘l‰Ý¥¹‘½ÝÍ}Ù•ÉÍ¥½¸‰t€ô]%9=]MmÝ¥ulÅt(€€€€€€€‘l‰…µ•}É½½Ñ}É•…‘½¹±ä‰t€ôÍ•±˜¹…µ•}É½½Ñ}É¼¹•Ñ}…Ñ¥Ù” ¤(€€€€€€€¤€ôÍ•±˜¹ÁÕ}½µ‰¼¹•Ñ}Í•±•Ñ• ¤ìÁÔ€ôÍ•±˜¹ÁÕ}‘•Ù¥•Ím¥t¥˜¤€ð±•¸¡Í•±˜¹ÁÕ}‘•Ù¥•Ì¤•±Í”Í•±˜¹ÁÕ}‘•Ù¥•ÍlÁtì‘l‰ÁÔ‰t€ôí¬èÁÕm­t™½È¬¥¸€ ‰µ½‘”ˆ°€‰Á¤ˆ°€‰Ù•¹‘½É}‘•Ù¥”ˆ°€‰µ½‘•°ˆ¤¥˜¬¥¸ÁÕô(€€€€€€€‘l‰É•¹‘•É•È‰t€ôI9IIMmÍ•±˜¹É•¹‘•É•É}½µ‰¼¹•Ñ}Í•±•Ñ• ¤¥˜Í•±˜¹É•¹‘•É•É}½µ‰¼¹•Ñ}Í•±•Ñ• ¤€ð±•¸¡I9IIL¤•±Í”€Åtì‘l‰É•¹‘•É•É}Á…Ñ ‰t€ôÍ•±˜¹É•¹‘•É•É}Á…Ñ¡}•¹ÑÉä¹•Ñ}Ñ•áÐ ¤¹ÍÑÉ¥À ¤ì‘l‰Ù­Í‘}•¹…‰±•‰t€ôÍ•±˜¹Ù­Í‘}•¹…‰±”¹•Ñ}…Ñ¥Ù” ¤ì‘l‰Ù­Í‘}Á…Ñ ‰t€ôÍ•±˜¹Ù­Í‘}Á…Ñ¡}•¹ÑÉä¹•Ñ}Ñ•áÐ ¤¹ÍÑÉ¥À ¤ì‘l‰¹Ù…Á¥}•¹…‰±•‰t€ôÍ•±˜¹¹Ù…Á¥}•¹…‰±”¹•Ñ}…Ñ¥Ù” ¤ì‘l‰¹Ù…Á¥}Á…Ñ ‰t€ôÍ•±˜¹¹Ù…Á¥}Á…Ñ¡}•¹ÑÉä¹•Ñ}Ñ•áÐ ¤¹ÍÑÉ¥À ¤ì‘l‰‘¥ÍÁ±…å}‰…­•¹‰t€ô€ ‰…ÕÑ¼ˆ°€‰Ý…å±…¹ˆ°€‰áÝ…å±…¹ˆ¥mÍ•±˜¹‘¥ÍÁ±…å}‰…­•¹‘}½µ‰¼¹•Ñ}Í•±•Ñ• ¤¥˜Í•±˜¹‘¥ÍÁ±…å}‰…­•¹‘}½µ‰¼¹•Ñ}Í•±•Ñ• ¤€ð€Ì•±Í”€Átì‘l‰áÝ…å±…¹‘}™…±±‰…¬‰t€ôÍ•±˜¹áÝ…å±…¹‘}™…±±‰…­}ˆ¹•Ñ}…Ñ¥Ù” ¤ì¹Í•Ñ‘•™…Õ±Ð ‰Ý…å±…¹‘}¥¹ÁÕÑ}ÍÑ…ÑÕÌˆ°Í•±˜¹ÁÉ½™¥±”¹•Ð ‰Ý…å±…¹‘}¥¹ÁÕÑ}ÍÑ…ÑÕÌˆ°€‰Õ¹­¹½Ý¸ˆ¤¥˜¥Í¥¹ÍÑ…¹”¡Í•±˜¹ÁÉ½™¥±”°‘¥Ð¤•±Í”€‰Õ¹­¹½Ý¸ˆ¤ì¹Í•Ñ‘•™…Õ±Ð ‰Ý…å±…¹‘}¥¹ÁÕÑ}É•…Í½¸ˆ°Í•±˜¹ÁÉ½™¥±”¹•Ð ‰Ý…å±…¹‘}¥¹ÁÕÑ}É•…Í½¸ˆ°€ˆˆ¤¥˜¥Í¥¹ÍÑ…¹”¡Í•±˜¹ÁÉ½™¥±”°‘¥Ð¤•±Í”€ˆˆ¤ì‘l‰ÁÉ•™•É}Ý…å±…¹‰t€ô‘l‰‘¥ÍÁ±…å}‰…­•¹‰t€„ô€‰áÝ…å±…¹ˆì‘l‰¹•ÑÝ½É¬‰t€ô€‰¡½ÍÐˆ¥˜Í•±˜¹¹•ÑÝ½É­}½µ‰¼¹•Ñ}Í•±•Ñ• ¤€ôô€Ä•±Í”€‰¹½¹”ˆì‘l‰…Õ‘¥½}‰…­•¹‰t€ô€‰ÁÕ±Í”ˆ¥˜Í•±˜¹…Õ‘¥½}½µ‰¼¹•Ñ}Í•±•Ñ• ¤€ôô€À•±Í”€‰‘¥Í…‰±•ˆì‘l‰…Á…‰¥±¥Ñ¥•Ì‰t€ôí¬èˆ¹•Ñ}…Ñ¥Ù” ¤™½È¬°ˆ¥¸Í•±˜¹…Á}¡•­Ì¹¥Ñ•µÌ ¥ôì‘l‰…Á…‰¥±¥Ñ¥•Ì‰ul‰…Õ‘¥¼‰t€ôÍ•±˜¹…Õ‘¥½}…À¹•Ñ}…Ñ¥Ù” ¤(€€€€€€€É•ÑÕÉ¸((€€€‘•˜…ÁÁ±å}Ý¥¹•}ÁÉ½™¥±•}Í•ÑÑ¥¹Ì¡Í•±˜°}ˆ¤è(€€€€€€€¥˜¹½ÐÍ•±˜¹ÕÉÉ•¹Ñ}ÁÉ½™¥±”è(€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹±½œ ‰M•±•é¥½¹„Õ¸ÁÉ½™¥±¼¸ˆ¤(€€€€€€€½±‘}…É €ôÍÑÈ¡Í•±˜¹ÁÉ½™¥±”¹•Ð ‰Ý¥¹•}…É ˆ°€‰Ý¥¸ØÐˆ¤¤(€€€€€€€¹•Ý}…É €ôI!MmÍ•±˜¹Á}…É¡}½µ‰¼¹•Ñ}Í•±•Ñ• ¤¥˜Í•±˜¹Á}…É¡}½µ‰¼¹•Ñ}Í•±•Ñ• ¤€ð±•¸¡I!L¤•±Í”€Át(€€€€€€€Í•±˜¹Í…Ù•}ÁÉ½™¥±”¡9½¹”¤(€€€€€€€¥˜½±‘}…É €„ô¹•Ý}…É è(€€€€€€€€€€€Í•±˜¹±½œ ‰É¡¥Ñ•ÑÕÉ„ÁÉ•™¥àµ½‘¥™¥…Ñ„èÁ•ÈÁ…ÍÍ…É”ÑÉ„Ý¥¸ØÐ”Ý¥¸ÌÈÕÍ„I•‰Õ¥±¸1„µ½‘¥™¥„ƒ ÍÑ…Ñ„Í…±Ù…Ñ„µ„¹½¸…ÁÁ±¥…Ñ„…°ÁÉ•™¥à•Í¥ÍÑ•¹Ñ”¸ˆ¤(€€€€€€€€€€€É•ÑÕÉ¸(€€€€€€€¡•±Á•È€ôÍ¡ÕÑ¥°¹Ý¡¥  ‰ÁŒµ…µ”µÍ…¹‘‰½àˆ¤½ÈÍÑÈ¡	M€¼€‰‰¥¸ˆ€¼€‰ÁŒµ…µ”µÍ…¹‘‰½àˆ¤(€€€€€€€Í•±˜¹}ÍÁ…Ý¹}±½•¡m¡•±Á•È°€‰ÁÉ•™¥àµÉ•…Ñ”ˆ°ÍÑÈ¡Í•±˜¹ÕÉÉ•¹Ñ}ÁÉ½™¥±”¥t°€‰Ý¥¹”µÍ•ÑÑ¥¹Ìˆ¤((€€€‘•˜‘¥Í…‰±•}•áÑ•É¹…±}É•¹‘•É•È¡Í•±˜°}ˆõ9½¹”¤è(€€€€€€€Í•±˜¹É•¹‘•É•É}½µ‰¼¹Í•Ñ}Í•±•Ñ• Ä¤(€€€€€€€Í•±˜¹É•¹‘•É•É}Á…Ñ¡}•¹ÑÉä¹Í•Ñ}Ñ•áÐ ˆˆ¤(€€€€€€€Í•±˜¹É•¹‘•É•É}…ÕÑ½}ÍÑ…ÑÕÌ¹Í•Ñ}Ñ•áÐ ‰9•ÍÍÕ¸É•¹‘•É•È•ÍÑ•É¹¼è]¥¹•Íˆ¤(€€€€€€€¥˜Í•±˜¹ÕÉÉ•¹Ñ}ÁÉ½™¥±”è(€€€€€€€€€€€Í•±˜¹Í…Ù•}ÁÉ½™¥±”¡9½¹”¤((€€€‘•˜±•…É}É…Á¡¥Í}…‘‘½¹Ì¡Í•±˜°}ˆõ9½¹”¤è(€€€€€€€Í•±˜¹Ù­Í‘}•¹…‰±”¹Í•Ñ}…Ñ¥Ù”¡…±Í”¤(€€€€€€€Í•±˜¹Ù­Í‘}Á…Ñ¡}•¹ÑÉä¹Í•Ñ}Ñ•áÐ ˆˆ¤(€€€€€€€Í•±˜¹¹Ù…Á¥}•¹…‰±”¹Í•Ñ}…Ñ¥Ù”¡…±Í”¤(€€€€€€€Í•±˜¹¹Ù…Á¥}Á…Ñ¡}•¹ÑÉä¹Í•Ñ}Ñ•áÐ ˆˆ¤(€€€€€€€Í•±˜¹±½œ ‰½µÁ½¹•¹Ñ¤É…™¥¤½Áé¥½¹…±¤‘¥Í…ÑÑ¥Ù…Ñ¤”‘•Í•±•é¥½¹…Ñ¤¸ˆ¤(€€€€€€€¥˜Í•±˜¹ÕÉÉ•¹Ñ}ÁÉ½™¥±”è(€€€€€€€€€€€Í•±˜¹Í…Ù•}ÁÉ½™¥±”¡9½¹”¤((€€€‘•˜}Í…¹‘‰½á}Á½±¥å}¡…¹•¡Í•±˜°€©}…ÉÌ¤è(€€€€€€€Í•±˜¹}É•™É•Í¡}Í…¹‘‰½á}Á½±¥å}¥¹™¼ ¤((€€€‘•˜}É•™É•Í¡}Í…¹‘‰½á}Á½±¥å}¥¹™¼¡Í•±˜¤è(€€€€€€€¥˜¹½Ð¡…Í…ÑÑÈ¡Í•±˜°€‰Í…¹‘‰½á}Á½±¥å}¥¹™¼ˆ¤è(€€€€€€€€€€€É•ÑÕÉ¸(€€€€€€€±¥¹•Ì€ôl‰!=5¡½ÍÐ”‰ÕÌ¡½ÍÐè¹½¸•ÍÁ½ÍÑ¤¸IÕ¹Ñ¥µ””Í¥ÍÑ•µ„èÍ½±„±•ÑÑÕÉ„¸‰t(€€€€€€€¥˜¡…Í…ÑÑÈ¡Í•±˜°€‰…µ•}É½½Ñ}É¼ˆ¤…¹Í•±˜¹…µ•}É½½Ñ}É¼¹•Ñ}…Ñ¥Ù” ¤è(€€€€€€€€€€€±¥¹•Ì¹…ÁÁ•¹ ‰¥É•Ñ½Éä¥½¼èÍ½±„±•ÑÑÕÉ„ìÁÉ•™¥à°!=5Í…¹‘‰½à”Í…±Ù…Ñ…¤É•ÍÑ…¹¼ÍÉ¥Ù¥‰¥±¤¸ˆ¤(€€€€€€€•±Í”è(€€€€€€€€€€€±¥¹•Ì¹…ÁÁ•¹ ‹Šj€¥É•Ñ½Éä¥½¼èÍÉ¥Ù¥‰¥±”Á•È½µÁ…Ñ¥‰¥±¥Ó€±•…ä¸ˆ¤(€€€€€€€¥˜¡…Í…ÑÑÈ¡Í•±˜°€‰¹•ÑÝ½É­}½µ‰¼ˆ¤…¹Í•±˜¹¹•ÑÝ½É­}½µ‰¼¹•Ñ}Í•±•Ñ• ¤€ôô€Äè(€€€€€€€€€€€±¥¹•Ì¹…ÁÁ•¹ ‹Šj€I•Ñ”è¹…µ•ÍÁ…”¡½ÍÐ½¹‘¥Ù¥Í¼ì¥°¥½¼Á×ÈÉ…¥Õ¹•É”%¹Ñ•É¹•Ð”‘¥ÍÁ½Í¥Ñ¥Ù¤18¸ˆ¤(€€€€€€€•±Í”è(€€€€€€€€€€€±¥¹•Ì¹…ÁÁ•¹ ‰I•Ñ”è¹…µ•ÍÁ…”¥Í½±…Ñ¼°¹•ÍÍÕ¸…•ÍÍ¼%¹Ñ•É¹•Ð½18¸ˆ¤(€€€€€€€¥˜¡…Í…ÑÑÈ¡Í•±˜°€‰‘¥ÍÁ±…å}‰…­•¹‘}½µ‰¼ˆ¤è(€€€€€€€€€€€‰…­•¹€ôÍ•±˜¹‘¥ÍÁ±…å}‰…­•¹‘}½µ‰¼¹•Ñ}Í•±•Ñ• ¤(€€€€€€€€€€€ÍÑ…ÑÕÌ€ôÍÑÈ¡Í•±˜¹ÁÉ½™¥±”¹•Ð ‰Ý…å±…¹‘}¥¹ÁÕÑ}ÍÑ…ÑÕÌˆ°€‰Õ¹­¹½Ý¸ˆ¤¤¥˜¥Í¥¹ÍÑ…¹”¡Í•±˜¹ÁÉ½™¥±”°‘¥Ð¤•±Í”€‰Õ¹­¹½Ý¸ˆ(€€€€€€€€€€€áÝ…å±…¹‘}•™™•Ñ¥Ù”€ô‰…­•¹€ôô€È½È€¡‰…­•¹€ôô€À…¹ÍÑ…ÑÕÌ€ôô€‰‰É½­•¸ˆ¤(€€€€€€€€€€€¥˜áÝ…å±…¹‘}•™™•Ñ¥Ù”è(€€€€€€€€€€€€€€€±¥¹•Ì¹…ÁÁ•¹ ‹Šj€a]…å±…¹è%A¡½ÍÐ½¹‘¥Ù¥Í¼Á•È½µÁ…Ñ¥‰¥±¥Ó€5%PµM!Lì±¤…±ÑÉ¤¹…µ•ÍÁ…”É•ÍÑ…¹¼¥Í½±…Ñ¤¸ˆ¤(€€€€€€€€€€€•±¥˜‰…­•¹€ôô€Àè(€€€€€€€€€€€€€€€±¥¹•Ì¹…ÁÁ•¹ ‰¥ÍÁ±…äÕÑ¼è]…å±…¹ÕÍ„%A¥Í½±…Ñ¼ìÕ¸™…±±‰…¬a]…å±…¹Á×È½¹‘¥Ù¥‘•É”%AÍ”¹••ÍÍ…É¥¼¸ˆ¤(€€€€€€€€€€€•±Í”è(€€€€€€€€€€€€€€€±¥¹•Ì¹…ÁÁ•¹ ‰]…å±…¹¹…Ñ¥Ù¼è%A¥Í½±…Ñ¼¸ˆ¤(€€€€€€€Í•±˜¹Í…¹‘‰½á}Á½±¥å}¥¹™¼¹Í•Ñ}Ñ•áÐ ‰q¸ˆ¹©½¥¸¡±¥¹•Ì¤¤((€€€‘•˜Í…Ù•}ÁÉ½™¥±”¡Í•±˜°}ˆ¤è(€€€€€€€¥˜¹½ÐÍ•±˜¹ÕÉÉ•¹Ñ}ÁÉ½™¥±”èÍ•±˜¹±½œ ‰M•±•é¥½¹„Õ¸ÁÉ½™¥±¼¸ˆ¤ìÉ•ÑÕÉ¸(€€€€€€€Í•±˜¹ÁÉ½™¥±”€ôÍ•±˜¹}ÁÉ½™¥±•}‘…Ñ…}™É½µ}Õ¤ ¤ìÍ•±˜¹ÕÉÉ•¹Ñ}ÁÉ½™¥±”¹ÝÉ¥Ñ•}Ñ•áÐ¡©Í½¸¹‘ÕµÁÌ¡Í•±˜¹ÁÉ½™¥±”°¥¹‘•¹ÐôÈ¤°•¹½‘¥¹œô‰ÕÑ˜´àˆ¤ìÍ•±˜¹±½œ¡˜‰AÉ½™¥±¼Í…±Ù…Ñ¼èíÍ•±˜¹ÕÉÉ•¹Ñ}ÁÉ½™¥±”¹¹…µ•ôˆ¤((€€€‘•˜¡…¹•‘}Í…Ù”¡Í•±˜°€©|¤è(€€€€€€€¥˜¹½ÐÍ•±˜¹}±½…‘¥¹œèÁ…ÍÌ((€€€‘•˜‘•±•Ñ•}ÁÉ½™¥±”¡Í•±˜°}ˆ¤è(€€€€€€€¥˜¹½ÐÍ•±˜¹ÕÉÉ•¹Ñ}ÁÉ½™¥±”èÉ•ÑÕÉ¸Í•±˜¹±½œ ‰M•±•é¥½¹„Õ¸ÁÉ½™¥±¼¸ˆ¤(€€€€€€€À€ôÍ•±˜¹ÕÉÉ•¹Ñ}ÁÉ½™¥±”ì‘…Ñ„€ôÍ•±˜¹ÁÉ½™¥±”ìÝ¥¸€ôÑ¬¹]¥¹‘½Ü¡Ñ¥Ñ±”ô‰±¥µ¥¹„ÁÉ½™¥±¼ˆ°ÑÉ…¹Í¥•¹Ñ}™½ÈõÍ•±˜°µ½‘…°õQÉÕ”°‘•™…Õ±Ñ}Ý¥‘Ñ ôÔÈÀ°‘•™…Õ±Ñ}¡•¥¡ÐôÈÈÀ¤ì‰½à€ôÑ¬¹	½à¡½É¥•¹Ñ…Ñ¥½¸õÑ¬¹=É¥•¹Ñ…Ñ¥½¸¹YIQ%0°ÍÁ…¥¹œôÄÈ¤ì‰½à¹Í•Ñ}µ…É¥¹}Ñ½À ÄØ¤ì‰½à¹Í•Ñ}µ…É¥¹}‰½ÑÑ½´ ÄØ¤ì‰½à¹Í•Ñ}µ…É¥¹}ÍÑ…ÉÐ ÄØ¤ì‰½à¹Í•Ñ}µ…É¥¹}•¹ ÄØ¤ìÝ¥¸¹Í•Ñ}¡¥±¡‰½à¤ì‰½à¹…ÁÁ•¹¡Ñ¬¹1…‰•°¡±…‰•°õ˜‰±¥µ¥¹…É”íÀ¹ÍÑ•µôü%°¥½¼½É¥¥¹…±”¹½¸Ù•ÉË Ñ½…Ñ¼¸ˆ°ÝÉ…ÀõQÉÕ”°á…±¥¸ôÀ¤¤ì‘•±‘…Ñ„€ôÑ¬¹¡•­	ÕÑÑ½¸¡±…‰•°ô‰±¥µ¥¹„…¹¡”ÁÉ•™¥à°!=5”Í…±Ù…Ñ…¤ˆ¤ì‰½à¹…ÁÁ•¹¡‘•±‘…Ñ„¤ìÈ€ôÑ¬¹	½à¡ÍÁ…¥¹œôà¤ì‰½à¹…ÁÁ•¹¡È¤ìŒ€ôÑ¬¹	ÕÑÑ½¸¡±…‰•°ô‰¹¹Õ±±„ˆ¤ìŒ¹½¹¹•Ð ‰±¥­•ˆ°±…µ‰‘„}ˆèÝ¥¸¹±½Í” ¤¤ìÈ¹…ÁÁ•¹¡Œ¤ì½¬€ôÑ¬¹	ÕÑÑ½¸¡±…‰•°ô‰±¥µ¥¹„ˆ¤ì½¬¹…‘‘}ÍÍ}±…ÍÌ ‰‘•ÍÑÉÕÑ¥Ù”µ…Ñ¥½¸ˆ¤ìÈ¹…ÁÁ•¹¡½¬¤(€€€€€€€‘•˜‘¼¡|¤è(€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€¥˜À¹•á¥ÍÑÌ ¤èÀ¹Õ¹±¥¹¬ ¤(€€€€€€€€€€€€€€€¥˜‘•±‘…Ñ„¹•Ñ}…Ñ¥Ù” ¤è(€€€€€€€€€€€€€€€€€€€™½È­•ä¥¸€ ‰ÁÉ•™¥á}É½½Ðˆ°€‰Í…¹‘‰½á}¡½µ”ˆ°€‰Í…Ù•}É½½Ðˆ¤è(€€€€€€€€€€€€€€€€€€€€€€€Ä€ôA…Ñ ¡ÍÑÈ¡‘…Ñ„¹•Ð¡­•ä°€ˆˆ¤¤¤¹•áÁ…¹‘ÕÍ•È ¤(€€€€€€€€€€€€€€€€€€€€€€€¥˜Ä¹¥Í}‘¥È ¤…¹±•¸¡Ä¹Á…ÉÑÌ¤€øô€Ð…¹Ä¹½Ð¥¸€¡A…Ñ ¹¡½µ” ¤°A…Ñ  ˆ¼ˆ¤¤èÍ¡ÕÑ¥°¹ÉµÑÉ•”¡Ä¤(€€€€€€€€€€€€€€€Í•±˜¹ÕÉÉ•¹Ñ}ÁÉ½™¥±”€ô9½¹”ìÍ•±˜¹ÁÉ½™¥±”€ôíôìÝ¥¸¹±½Í” ¤ìÍ•±˜¹±½…‘}ÁÉ½™¥±•Ì ¤ìÍ•±˜¹±½œ¡˜‰AÉ½™¥±¼•±¥µ¥¹…Ñ¼èíÀ¹¹…µ•ôˆ¤(€€€€€€€€€€€•á•ÁÐá•ÁÑ¥½¸…Ì•áŒèÍ•±˜¹±½œ¡˜‰±¥µ¥¹…é¥½¹”™…±±¥Ñ„èí•áôˆ¤(€€€€€€€½¬¹½¹¹•Ð ‰±¥­•ˆ°‘¼¤ìÝ¥¸¹ÁÉ•Í•¹Ð ¤((€€€‘•˜É•‰Õ¥±‘}ÁÉ½™¥±”¡Í•±˜°}ˆ¤è(€€€€€€€¥˜¹½ÐÍ•±˜¹ÕÉÉ•¹Ñ}ÁÉ½™¥±”èÉ•ÑÕÉ¸Í•±˜¹±½œ ‰M•±•é¥½¹„Õ¸ÁÉ½™¥±¼¸ˆ¤(€€€€€€€Í•±˜¹Í…Ù•}ÁÉ½™¥±”¡9½¹”¤ìÍ•±˜¹‰½½ÑÍÑÉ…Á}…¹‘}‘•ÁÌ¡Í•±˜¹ÕÉÉ•¹Ñ}ÁÉ½™¥±”°Í•±˜¹ÁÉ½™¥±”¹•Ð ‰‘•Á•¹‘•¹¥•Ìˆ°mt¤°QÉÕ”¤((€€€‘•˜ÉÕ¹}Í…¹‘‰½à¡Í•±˜°…ÉÌè±¥ÍÑmÍÑÉt°Ñ…œèÍÑÈ°¹•ÑÝ½É¬õ…±Í”¤è(€€€€€€€¥˜¹½ÐÍ•±˜¹ÕÉÉ•¹Ñ}ÁÉ½™¥±”èÉ•ÑÕÉ¸Í•±˜¹±½œ ‰M•±•é¥½¹„Õ¸ÁÉ½™¥±¼¸ˆ¤(€€€€€€€¡•±Á•È€ôÍ¡ÕÑ¥°¹Ý¡¥  ‰ÁŒµ…µ”µÍ…¹‘‰½àˆ¤½ÈÍÑÈ¡	M€¼€‰‰¥¸ˆ€¼€‰ÁŒµ…µ”µÍ…¹‘‰½àˆ¤(€€€€€€€Í•±˜¹}ÍÁ…Ý¹}±½•¡m¡•±Á•È°€©…ÉÌ°ÍÑÈ¡Í•±˜¹ÕÉÉ•¹Ñ}ÁÉ½™¥±”¥t°Ñ…œ¤((€€€‘•˜}ÍÁ…Ý¹}±½•¡Í•±˜°µè±¥ÍÑmÍÑÉt°Ñ…œèÍÑÈ¤è(€€€€€€€Í•±˜¹±½œ¡˜ˆìœ€œ¹©½¥¸¡µ¥ôˆ¤(€€€€€€€‘•˜Ý½É­•È ¤è(€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€À€ôÍÕ‰ÁÉ½•ÍÌ¹A½Á•¸¡µ°ÍÑ‘½ÕÐõÍÕ‰ÁÉ½•ÍÌ¹A%A°ÍÑ‘•ÉÈõÍÕ‰ÁÉ½•ÍÌ¹MQ=UP°Ñ•áÐõQÉÕ”¤(€€€€€€€€€€€€€€€¥˜À¹ÍÑ‘½ÕÐè(€€€€€€€€€€€€€€€€€€€™½È±¥¹”¥¸À¹ÍÑ‘½ÕÐè1¥ˆ¹¥‘±•}…‘¡Í•±˜¹±½œ°˜‰míÑ…õtí±¥¹”¹ÉÍÑÉ¥À ¥ôˆ¤(€€€€€€€€€€€€€€€½‘”€ôÀ¹Ý…¥Ð ¤ì1¥ˆ¹¥‘±•}…‘¡Í•±˜¹±½œ°˜‰míÑ…õt•á¥Ðõí½‘•ôˆ¤(€€€€€€€€€€€•á•ÁÐá•ÁÑ¥½¸…Ì•áŒè1¥ˆ¹¥‘±•}…‘¡Í•±˜¹±½œ°˜‰míÑ…õtí•áôˆ¤(€€€€€€€Ñ¡É•…‘¥¹œ¹Q¡É•…¡Ñ…É•ÐõÝ½É­•È°‘…•µ½¸õQÉÕ”¤¹ÍÑ…ÉÐ ¤((€€€‘•˜‰½½ÑÍÑÉ…Á}…¹‘}‘•ÁÌ¡Í•±˜°Á…Ñ èA…Ñ °‘•ÁÌè±¥ÍÑmÍÑÉt°‰½½ÑÍÑÉ…Àè‰½½°¤è(€€€€€€€¡•±Á•È€ôÍ¡ÕÑ¥°¹Ý¡¥  ‰ÁŒµ…µ”µÍ…¹‘‰½àˆ¤½ÈÍÑÈ¡	M€¼€‰‰¥¸ˆ€¼€‰ÁŒµ…µ”µÍ…¹‘‰½àˆ¤(€€€€€€€¥˜‰½½ÑÍÑÉ…ÀèÍ•±˜¹}ÍÁ…Ý¹}±½•¡m¡•±Á•È°€‰ÁÉ•™¥àµÉ•…Ñ”ˆ°ÍÑÈ¡Á…Ñ ¥t°€‰ÁÉ•™¥àˆ¤(€€€€€€€¥˜‘•ÁÌèÍ•±˜¹}ÍÁ…Ý¹}±½•¡m¡•±Á•È°€‰‘•ÁÌˆ°ÍÑÈ¡Á…Ñ ¤°€©‘•ÁÍt°€‰‘•ÁÌˆ¤((€€€‘•˜ÉÕ¹}ÁÉ½™¥±•}Ñ½½°¡Í•±˜°Á…å±½…è±¥ÍÑmÍÑÉt°Ñ…œèÍÑÈ¤è(€€€€€€€¡•±Á•È€ôÍ¡ÕÑ¥°¹Ý¡¥  ‰ÁŒµ…µ”µÍ…¹‘‰½àˆ¤½ÈÍÑÈ¡	M€¼€‰‰¥¸ˆ€¼€‰ÁŒµ…µ”µÍ…¹‘‰½àˆ¤ìÍ•±˜¹}ÍÁ…Ý¹}±½•¡m¡•±Á•È°€‰Ý¥¹”ˆ°ÍÑÈ¡Í•±˜¹ÕÉÉ•¹Ñ}ÁÉ½™¥±”¤°€ˆ´´ˆ°€©Á…å±½…‘t°Ñ…œ¤((€€€‘•˜±…Õ¹¡}…µ”¡Í•±˜°}ˆ¤è(€€€€€€€¥˜¹½ÐÍ•±˜¹ÕÉÉ•¹Ñ}ÁÉ½™¥±”èÉ•ÑÕÉ¸(€€€€€€€Í•±˜¹Í…Ù•}ÁÉ½™¥±”¡9½¹”¤ìÍ•±˜¹}ÍÁ…Ý¹}±½•¡l¡Í¡ÕÑ¥°¹Ý¡¥  ‰ÁŒµ…µ”µÍ…¹‘‰½àˆ¤½ÈÍÑÈ¡	M€¼€‰‰¥¸ˆ€¼€‰ÁŒµ…µ”µÍ…¹‘‰½àˆ¤¤°€‰ÉÕ¸ˆ°ÍÑÈ¡Í•±˜¹ÕÉÉ•¹Ñ}ÁÉ½™¥±”¥t°€‰…µ”ˆ¤((€€€‘•˜É•™É•Í¡}ÉÕ¹Ñ¥µ•}±½…°¡Í•±˜°}ˆõ9½¹”¤è(€€€€€€€Í•±˜¹ÉÕ¹¹•É}±½…°€ô‘¥Í½Ù•É}ÉÕ¹¹•ÉÌ ¤ì(€€€€€€€™½È¡¥±¥¸Í•±˜¹}¡¥±‘É•¸¡Í•±˜¹ÉÕ¹Ñ¥µ•}±¥ÍÐ¤èÍ•±˜¹ÉÕ¹Ñ¥µ•}±¥ÍÐ¹É•µ½Ù”¡¡¥±¤(€€€€€€€™½È¥Ñ•´¥¸Í•±˜¹ÉÕ¹¹•É}±½…°è(€€€€€€€€€€€É½Ü€ôÑ¬¹1¥ÍÑ	½áI½Ü ¤ìÉ½Ü¹}ÉÕ¹¹•È€ô¥Ñ•´ìÉ½Ü¹Í•Ñ}¡¥±¡Ñ¬¹1…‰•°¡±…‰•°õ˜‰í¥Ñ•µl¹…µ”uôƒŠPí¥Ñ•µl­¥¹uôƒŠPí¥ÍÕ•µl‰Í½ÕÉ”‰uõq¹í¥Ñ•µl‰Á…Ñ ‰uôˆ°á…±¥¸ôÀ°ÝÉ…ÀõQÉÕ”¤¤ìÍ•±˜¹ÉÕ¹Ñ¥µ•}±¥ÍÐ¹…ÁÁ•¹¡É½Ü¤(€€€€€€€±…‰•±Ì€ôl‰]¥¹”‘¤Í¥ÍÑ•µ„‰t€¬m˜‰íál¹…µ”uôƒŠPíálÍ½ÕÉ”uôˆ™½Èà¥¸Í•±˜¹ÉÕ¹¹•É}±½…°¥˜à¹•Ð ¥¹œ¤€„ô€Ý¥¹”µÍåÍÑ•´t(€€€€€€€Í•±˜¹ÉÕ¹Ñ¥µ•}¡½¥”¹Í•Ñ}µ½‘•°¡Ñ¬¹MÑÉ¥¹1¥ÍÐ¹¹•Ü¡±…‰•±Ì¤¤ìÍ•±˜¹ÉÕ¹Ñ¥µ•}¡½¥”¹Í•Ñ}Í•±•Ñ• À¤(€€€€€€€Í•±˜¹ÉÕ¹Ñ¥µ•}ÍÑ…ÑÕÌ¹Í•Ñ}Ñ•áÐ¡˜‰IÕ¹Ñ¥µ”±½…±¤É¥±•Ù…Ñ¤èí±•¸¡Í•±˜¹ÉÕ¹¹•É}±½…°¥ôˆ¤(€€€€€€€Í•±˜¹}É•ÍÑ½É•}ÉÕ¹Ñ¥µ•}¡½¥” ¤((€€€‘•˜}¡¥±‘É•¸¡Í•±˜°Ý¥‘•Ð¤è(€€€€€€€½ÕÐõmtìŒõÝ¥‘•Ð¹•Ñ}™¥ÉÍÑ}¡¥± ¤(€€€€€€€Ý¡¥±”Œè½ÕÐ¹…ÁÁ•¹¡Œ¤ìŒõŒ¹•Ñ}¹•áÑ}Í¥‰±¥¹œ ¤(€€€€€€€É•ÑÕÉ¸½ÕÐ((€€€‘•˜}É•ÍÑ½É•}ÉÕ¹Ñ¥µ•}¡½¥”¡Í•±˜¤è(€€€€€€€Ñ…É•Ð€ôÍ•±˜¹ÁÉ½™¥±”¹•Ð Í•±•Ñ•‘}ÉÕ¹¹•Èœ¤¥˜¥Í¥¹ÍÑ…¹”¡Í•±˜¹ÁÉ½™¥±”°‘¥Ð¤•±Í”9½¹”(€€€€€€€¥˜¹½Ð¥Í¥¹ÍÑ…¹”¡Ñ…É•Ð°‘¥Ð¤èÍ•±˜¹ÉÕ¹Ñ¥µ•}¡½¥”¹Í•Ñ}Í•±•Ñ• À¤ìÉ•ÑÕÉ¸(€€€€€€€™½È¤°¥Ñ•´¥¸•¹Õµ•É…Ñ”¡mà™½Èà¥¸Í•±˜¹ÉÕ¹¹•É}±½…°¥˜à¹•Ð ­¥¹œ¤€„ô€Ý¥¹”µÍåÍÑ•´t°ÍÑ…ÉÐôÄ¤è(€€€€€€€€€€€¥˜¥Ñ•´¹•Ð Á…Ñ œ¤€ôôÑ…É•Ð¹•Ð Á…Ñ œ¤è(€€€€€€€€€€€€€€€Í•±˜¹ÉÕ¹Ñ¥µ•}¡½¥”¹Í•Ñ}Í•±•Ñ•¡¤¤ìÉ•ÑÕÉ¸(€€€€€€€Í•±˜¹ÉÕ¹Ñ¥µ•}¡½¥”¹Í•Ñ}Í•±•Ñ• À¤((€€€‘•˜ÉÕ¹Ñ¥µ•}¡½¥•}¡…¹•¡Í•±˜°€©|¤è(€€€€€€€¥˜Í•±˜¹}±½…‘¥¹œ½È¹½ÐÍ•±˜¹ÕÉÉ•¹Ñ}ÁÉ½™¥±”èÉ•ÑÕÉ¸(€€€€€€€¥‘à€ôÍ•±˜¹ÉÕ¹Ñ¥µ•}¡½¥”¹•Ñ}Í•±•Ñ• ¤(€€€€€€€…¹‘¥‘…Ñ•Ì€ômà™½Èà¥¸Í•±˜¹ÉÕ¹¹•É}±½…°¥˜à¹•Ð ­¥¹œ¤€„ô€Ý¥¹”µÍåÍÑ•´t(€€€€€€€Í•±˜¹ÁÉ½™¥±•lÍ•±•Ñ•‘}ÉÕ¹¹•Èt€ô9½¹”¥˜¥‘à€ôô€À½È¥‘à´Ä€øô±•¸¡…¹‘¥‘…Ñ•Ì¤•±Í”…¹‘¥‘…Ñ•Ím¥‘à´Åt(€€€€€€€Í•±˜¹Í…Ù•}ÁÉ½™¥±”¡9½¹”¤((€€€‘•˜É•™É•Í¡}…Ñ…±½œ¡Í•±˜°}ˆõ9½¹”¤è(€€€€€€€Í•±˜¹…Ñ…±½}ÍÑ…ÑÕÌ¹Í•Ñ}Ñ•áÐ ‰¥½É¹…µ•¹Ñ¼…Ñ…±½¼É•µ½Ñ¿Š˜ˆ¤(€€€€€€€‘•˜Ý½É­•È ¤è(€€€€€€€€€€€¥Ñ•µÌ°•ÉÉ½ÉÌ€ô…Ñ…±½}…±° ¤ì1¥ˆ¹¥‘±•}…‘¡Í•±˜¹Í¡½Ý}…Ñ…±½œ°¥Ñ•µÌ°•ÉÉ½ÉÌ¤(€€€€€€€Ñ¡É•…‘¥¹œ¹Q¡É•…¡Ñ…É•ÐõÝ½É­•È°‘…•µ½¸õQÉÕ”¤¹ÍÑ…ÉÐ ¤((€€€‘•˜Í¡½Ý}…Ñ…±½œ¡Í•±˜°¥Ñ•µÌ°•ÉÉ½ÉÌ¤è(€€€€€€€Í•±˜¹ÉÕ¹¹•É}…Ñ…±½œ€ô¥Ñ•µÌ(€€€€€€€™½È¡¥±¥¸±¥ÍÐ¡Í•±˜¹…Ñ…±½}±¥ÍÐ¤èÍ•±˜¹…Ñ…±½}±¥ÍÐ¹É•µ½Ù”¡¡¥±¤(€€€€€€€™½È¥Ñ•´¥¸¥Ñ•µÌè(€€€€€€€€€€€É½Ü€ôÑ¬¹1¥ÍÑ	½áI½Ü ¤ìÉ½Ü¹}ÉÕ¹¹•È€ô¥Ñ•´ìÉ½Ü¹Í•Ñ}¡¥±¡Ñ¬¹1…‰•°¡±…‰•°õ˜‰í¥Ñ•´¹•Ð ¹…µ”œ¥ôƒ
Üí¥Ñ•´¹•Ð ™…µ¥±äœ°¥Ñ•´¹•Ð ­¥¹œ°œœ¤¥ôƒ
Üí¥Ñ•´¹•Ð Í½ÕÉ”œ°œœ¥õq¹í¥Ñ•´¹•Ð ™¥±•¹…µ”œ°€µ…¹¥™•ÍÐÉ•µ½Ñ¼œ¥ôˆ°á…±¥¸ôÀ°ÝÉ…ÀõQÉÕ”¤¤ìÍ•±˜¹…Ñ…±½}±¥ÍÐ¹…ÁÁ•¹¡É½Ü¤(€€€€€€€Í•±˜¹…Ñ…±½}ÍÑ…ÑÕÌ¹Í•Ñ}Ñ•áÐ¡˜‰…Ñ…±½¼É•µ½Ñ¼èí±•¸¡¥Ñ•µÌ¥ôÉÕ¹Ñ¥µ”‘¥ÍÁ½¹¥‰¥±¤ˆ€¬€¡˜ˆƒŠPí±•¸¡•ÉÉ½ÉÌ¥ôÁÉ½Ù¥‘•È¹½¸É…¥Õ¹¥‰¥±¤ˆ¥˜•ÉÉ½ÉÌ•±Í”€ˆˆ¤¤ìÍ•±˜¹±½œ ‰…Ñ…±½¼ÉÕ¹Ñ¥µ”…¥½É¹…Ñ¼¸ˆ¤ìÉ•ÑÕÉ¸…±Í”((€€€‘•˜¥¹ÍÑ…±±}Í•±•Ñ•‘}ÉÕ¹Ñ¥µ”¡Í•±˜°}ˆ¤è(€€€€€€€É½Ü€ôÍ•±˜¹…Ñ…±½}±¥ÍÐ¹•Ñ}Í•±•Ñ•‘}É½Ü ¤(€€€€€€€¥˜¹½ÐÉ½ÜèÉ•ÑÕÉ¸Í•±˜¹±½œ ‰M•±•é¥½¹„Õ¸ÉÕ¹Ñ¥µ”‘…°…Ñ…±½¼¸ˆ¤(€€€€€€€¥Ñ•´€ôÉ½Ü¹}ÉÕ¹¹•Èì‰…Í”€ôA…Ñ ¹¡½µ” ¤€¼€ˆ¹±½…°½Í¡…É”½ÁŒµ…µ”µµ…¹…•È½ÉÕ¹¹•ÉÌˆ(€€€€€€€‘•˜Ý½É­•È ¤è(€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€À€ô‘½Ý¹±½…‘}…¹‘}¥¹ÍÑ…±°¡¥Ñ•´°‰…Í”¤ì1¥ˆ¹¥‘±•}…‘¡Í•±˜¹±½œ°˜‰IÕ¹Ñ¥µ”¥¹ÍÑ…±±…Ñ¼èíÁôˆ¤ì1¥ˆ¹¥‘±•}…‘¡Í•±˜¹É•™É•Í¡}ÉÕ¹Ñ¥µ•}±½…°¤(€€€€€€€€€€€•á•ÁÐá•ÁÑ¥½¸…Ì•áŒè1¥ˆ¹¥‘±•}…‘¡Í•±˜¹±½œ°˜‰%¹ÍÑ…±±…é¥½¹”ÉÕ¹Ñ¥µ”™…±±¥Ñ„èí•áôˆ¤(€€€€€€€Ñ¡É•…‘¥¹œ¹Q¡É•…¡Ñ…É•ÐõÝ½É­•È°‘…•µ½¸õQÉÕ”¤¹ÍÑ…ÉÐ ¤((€€€‘•˜É•µ½Ù•}Í•±•Ñ•‘}ÉÕ¹Ñ¥µ”¡Í•±˜°}ˆ¤è(€€€€€€€É½Ü€ôÍ•±˜¹ÉÕ¹Ñ¥µ•}±¥ÍÐ¹•Ñ}Í•±•Ñ•‘}É½Ü ¤(€€€€€€€¥˜¹½ÐÉ½ÜèÉ•ÑÕÉ¸Í•±˜¹±½œ ‰M•±•é¥½¹„Õ¸ÉÕ¹Ñ¥µ”±½…±”¸ˆ¤(€€€€€€€¥Ñ•´€ôÉ½Ü¹}ÉÕ¹¹•È(€€€€€€€¥˜¥Ñ•´¹•Ð ‰­¥¹ˆ¤€ôô€‰Ý¥¹”µÍåÍÑ•´ˆ½È¥Ñ•´¹•Ð ‰Í½ÕÉ”ˆ¤€ôô€‰M¥ÍÑ•µ„ˆèÉ•ÑÕÉ¸Í•±˜¹±½œ ‰%°ÉÕ¹Ñ¥µ”‘¤Í¥ÍÑ•µ„¹½¸Á×È•ÍÍ•É”‘¥Í¥¹ÍÑ…±±…Ñ¼‘„ÅÕ¤¸ˆ¤(€€€€€€€À€ôA…Ñ ¡¥Ñ•µl‰Á…Ñ ‰t¤(€€€€€€€ÑÉäèÍ¡ÕÑ¥°¹ÉµÑÉ•”¡À¤ìÍ•±˜¹±½œ¡˜‰IÕ¹Ñ¥µ”É¥µ½ÍÍ¼èíÁôˆ¤ìÍ•±˜¹É•™É•Í¡}ÉÕ¹Ñ¥µ•}±½…° ¤(€€€€€€€•á•ÁÐá•ÁÑ¥½¸…Ì•áŒèÍ•±˜¹±½œ¡˜‰I¥µ½é¥½¹”ÉÕ¹Ñ¥µ”™…±±¥Ñ„èí•áôˆ¤((€€€‘•˜É•™É•Í¡}É•¹‘•É•É}±½…°¡Í•±˜°}ˆõ9½¹”¤è(€€€€€€€Í•±˜¹É•¹‘•É•É}±½…°€ô‘¥Í½Ù•É}É•¹‘•É•ÉÌ ¤(€€€€€€€™½È¡¥±¥¸Í•±˜¹}¡¥±‘É•¸¡Í•±˜¹É•¹‘•É•É}±½…±}±¥ÍÐ¤èÍ•±˜¹É•¹‘•É•É}±½…±}±¥ÍÐ¹É•µ½Ù”¡¡¥±¤(€€€€€€€™½È¥Ñ•´¥¸Í•±˜¹É•¹‘•É•É}±½…°è(€€€€€€€€€€€É½Ü€ôÑ¬¹1¥ÍÑ	½áI½Ü ¤ìÉ½Ü¹}É•¹‘•É•È€ô¥Ñ•´(€€€€€€€€€€€É½Ü¹Í•Ñ}¡¥±¡Ñ¬¹1…‰•°¡±…‰•°õ˜‰í¥Ñ•µl­¥¹t¹ÕÁÁ•È ¥ôí¥Ñ•µlÙ•ÉÍ¥½¸uõq¹í¥Ñ•µlÁ…Ñ uôˆ°á…±¥¸ôÀ°ÝÉ…ÀõQÉÕ”¤¤(€€€€€€€€€€€Í•±˜¹É•¹‘•É•É}±½…±}±¥ÍÐ¹…ÁÁ•¹¡É½Ü¤(€€€€€€€Í•±˜¹É•¹‘•É•É}±½…±}ÍÑ…ÑÕÌ¹Í•Ñ}Ñ•áÐ¡˜‰I•¹‘•É•È¥¹ÍÑ…±±…Ñ¤‘„A…µ”5…¹…•Èèí±•¸¡Í•±˜¹É•¹‘•É•É}±½…°¥ôˆ¤(€€€€€€€É•ÑÕÉ¸…±Í”((€€€‘•˜É•™É•Í¡}É•¹‘•É•É}…Ñ…±½œ¡Í•±˜°}ˆõ9½¹”¤è(€€€€€€€Í•±˜¹É•¹‘•É•É}…Ñ…±½}ÍÑ…ÑÕÌ¹Í•Ñ}Ñ•áÐ ‰¥½É¹…µ•¹Ñ¼…Ñ…±½¼É…™¥¿Š˜ˆ¤(€€€€€€€‘•˜Ý½É­•È ¤è(€€€€€€€€€€€¥Ñ•µÌ°•ÉÉ½ÉÌ€ôÉ•¹‘•É•É}…Ñ…±½}…±° ¤ì1¥ˆ¹¥‘±•}…‘¡Í•±˜¹Í¡½Ý}É•¹‘•É•É}…Ñ…±½œ°¥Ñ•µÌ°•ÉÉ½ÉÌ¤(€€€€€€€Ñ¡É•…‘¥¹œ¹Q¡É•…¡Ñ…É•ÐõÝ½É­•È°‘…•µ½¸õQÉÕ”¤¹ÍÑ…ÉÐ ¤((€€€‘•˜Í¡½Ý}É•¹‘•É•É}…Ñ…±½œ¡Í•±˜°¥Ñ•µÌ°•ÉÉ½ÉÌ¤è(€€€€€€€Í•±˜¹É•¹‘•É•É}…Ñ…±½œ€ô¥Ñ•µÌ(€€€€€€€™½È¡¥±¥¸Í•±˜¹}¡¥±‘É•¸¡Í•±˜¹É•¹‘•É•É}…Ñ…±½}±¥ÍÐ¤èÍ•±˜¹É•¹‘•É•É}…Ñ…±½}±¥ÍÐ¹É•µ½Ù”¡¡¥±¤(€€€€€€€™½È¥Ñ•´¥¸¥Ñ•µÌè(€€€€€€€€€€€É½Ü€ôÑ¬¹1¥ÍÑ	½áI½Ü ¤ìÉ½Ü¹}É•¹‘•É•È€ô¥Ñ•´(€€€€€€€€€€€É½Ü¹Í•Ñ}¡¥±¡Ñ¬¹1…‰•°¡±…‰•°õ˜‰í¥Ñ•´¹•Ð ­¥¹œ°œœ¤¹ÕÁÁ•È ¥ôƒ
Üí¥Ñ•´¹•Ð Ù•ÉÍ¥½¸œ°œœ¥ôƒ
ß
Üí¥Ñ•´¹•Ð Í½ÕÉ”œ°œœ¥õq¹í¥Ñ•´¹•Ð ™¥±•¹…µ”œ°œœ¥ôˆ°á…±¥¸ôÀ°ÝÉ…ÀõQÉÕ”¤¤(€€€€€€€€€€€Í•±˜¹É•¹‘•É•É}…Ñ…±½}±¥ÍÐ¹…ÁÁ•¹¡É½Ü¤(€€€€€€€ÍÕ™™¥à€ô˜ˆƒŠPí±•¸¡•ÉÉ½ÉÌ¥ô™½¹Ñ”½¤¹½¸É…¥Õ¹¥‰¥±¤ˆ¥˜•ÉÉ½ÉÌ•±Í”€ˆˆ(€€€€€€€Í•±˜¹É•¹‘•É•É}…Ñ…±½}ÍÑ…ÑÕÌ¹Í•Ñ}Ñ•áÐ¡˜‰…Ñ…±½¼É•¹‘•É•Èèí±•¸¡¥Ñ•µÌ¥ôÉ•±•…Í”‘¥ÍÁ½¹¥‰¥±¥íÍÕ™™¥áôˆ¤(€€€€€€€™½È•ÉÈ¥¸•ÉÉ½ÉÌèÍ•±˜¹±½œ¡˜‰…Ñ…±½¼É•¹‘•É•Èèí•ÉÉôˆ¤(€€€€€€€É•ÑÕÉ¸…±Í”((€€€‘•˜¥¹ÍÑ…±±}Í•±•Ñ•‘}É•¹‘•É•È¡Í•±˜°}ˆ¤è(€€€€€€€É½Ü€ôÍ•±˜¹É•¹‘•É•É}…Ñ…±½}±¥ÍÐ¹•Ñ}Í•±•Ñ•‘}É½Ü ¤(€€€€€€€¥˜¹½ÐÉ½ÜèÉ•ÑÕÉ¸Í•±˜¹±½œ ‰M•±•é¥½¹„Õ¸½µÁ½¹•¹Ñ”É…™¥¼‘…°…Ñ…±½¼¸ˆ¤(€€€€€€€¥Ñ•´€ôÉ½Ü¹}É•¹‘•É•È(€€€€€€€Í•±˜¹±½œ¡˜‰%¹ÍÑ…±±…é¥½¹”í¥Ñ•´¹•Ð ­¥¹œ°œœ¤¹ÕÁÁ•È ¥ôí¥Ñ•´¹•Ð Ù•ÉÍ¥½¸œ°œœ¥÷Š˜ˆ¤(€€€€€€€‘•˜Ý½É­•È ¤è(€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€Á…Ñ €ô¥¹ÍÑ…±±}É•¹‘•É•È¡¥Ñ•´¤(€€€€€€€€€€€€€€€1¥ˆ¹¥‘±•}…‘¡Í•±˜¹±½œ°˜‰I•¹‘•É•È¥¹ÍÑ…±±…Ñ¼èíÁ…Ñ¡ôˆ¤(€€€€€€€€€€€€€€€1¥ˆ¹¥‘±•}…‘¡Í•±˜¹É•™É•Í¡}É•¹‘•É•É}±½…°¤(€€€€€€€€€€€•á•ÁÐá•ÁÑ¥½¸…Ì•áŒè(€€€€€€€€€€€€€€€1¥ˆ¹¥‘±•}…‘¡Í•±˜¹±½œ°˜‰%¹ÍÑ…±±…é¥½¹”É•¹‘•É•È™…±±¥Ñ„èí•áôˆ¤(€€€€€€€Ñ¡É•…‘¥¹œ¹Q¡É•…¡Ñ…É•ÐõÝ½É­•È°‘…•µ½¸õQÉÕ”¤¹ÍÑ…ÉÐ ¤((€€€‘•˜ÕÍ•}Í•±•Ñ•‘}É•¹‘•É•È¡Í•±˜°}ˆ¤è(€€€€€€€É½Ü€ôÍ•±˜¹É•¹‘•É•É}±½…±}±¥ÍÐ¹•Ñ}Í•±•Ñ•‘}É½Ü ¤(€€€€€€€¥˜¹½ÐÉ½ÜèÉ•ÑÕÉ¸Í•±˜¹±½œ ‰M•±•é¥½¹„Õ¸É•¹‘•É•È±½…±”¸ˆ¤(€€€€€€€¥Ñ•´€ôÉ½Ü¹}É•¹‘•É•È(€€€€€€€­¥¹€ô¥Ñ•´¹•Ð ‰­¥¹ˆ¤(€€€€€€€¥˜­¥¹€ôô€‰Ù­Íˆè(€€€€€€€€€€€Í•±˜¹Ù­Í‘}•¹…‰±”¹Í•Ñ}…Ñ¥Ù”¡QÉÕ”¤ìÍ•±˜¹Ù­Í‘}Á…Ñ¡}•¹ÑÉä¹Í•Ñ}Ñ•áÐ¡¥Ñ•µl‰Á…Ñ ‰t¤(€€€€€€€•±¥˜­¥¹€ôô€‰‘áÙ¬µ¹Ù…Á¤ˆè(€€€€€€€€€€€Í•±˜¹¹Ù…Á¥}•¹…‰±”¹Í•Ñ}…Ñ¥Ù”¡QÉÕ”¤ìÍ•±˜¹¹Ù…Á¥}Á…Ñ¡}•¹ÑÉä¹Í•Ñ}Ñ•áÐ¡¥Ñ•µl‰Á…Ñ ‰t¤(€€€€€€€•±Í”è(€€€€€€€€€€€¥‘à€ôì‰‘áÙ¬ˆè€È°€‰ÝÙ¬ˆè€Ì°€‰‘Ù½½‘½¼ˆè€Ñô¹•Ð¡­¥¹°€Ä¤(€€€€€€€€€€€Í•±˜¹É•¹‘•É•É}½µ‰¼¹Í•Ñ}Í•±•Ñ•¡¥‘à¤ìÍ•±˜¹É•¹‘•É•É}Á…Ñ¡}•¹ÑÉä¹Í•Ñ}Ñ•áÐ¡¥Ñ•µl‰Á…Ñ ‰t¤(€€€€€€€€€€€Í•±˜¹É•¹‘•É•É}…ÕÑ½}ÍÑ…ÑÕÌ¹Í•Ñ}Ñ•áÐ¡˜‰í­¥¹¹ÕÁÁ•È ¥ôÍ•±•é¥½¹…Ñ¼èí¥Ñ•µlÙ•ÉÍ¥½¸uôˆ¤(€€€€€€€Í•±˜¹±½œ¡˜‰½µÁ½¹•¹Ñ”É…™¥¼Í•±•é¥½¹…Ñ¼èí­¥¹¹ÕÁÁ•È ¥ôí¥Ñ•µlÙ•ÉÍ¥½¸uôˆ¤(€€€€€€€¥˜Í•±˜¹ÕÉÉ•¹Ñ}ÁÉ½™¥±”èÍ•±˜¹Í…Ù•}ÁÉ½™¥±”¡9½¹”¤((€€€‘•˜É•µ½Ù•}Í•±•Ñ•‘}É•¹‘•É•È¡Í•±˜°}ˆ¤è(€€€€€€€É½Ü€ôÍ•±˜¹É•¹‘•É•É}±½…±}±¥ÍÐ¹•Ñ}Í•±•Ñ•‘}É½Ü ¤(€€€€€€€¥˜¹½ÐÉ½ÜèÉ•ÑÕÉ¸Í•±˜¹±½œ ‰M•±•é¥½¹„Õ¸É•¹‘•É•È±½…±”¸ˆ¤(€€€€€€€¥Ñ•´€ôÉ½Ü¹}É•¹‘•É•È(€€€€€€€ÑÉäè(€€€€€€€€€€€É•µ½Ù•}É•¹‘•É•È¡¥Ñ•µl‰Á…Ñ ‰t¤(€€€€€€€€€€€Í•±˜¹±½œ¡˜‰I•¹‘•É•ÈÉ¥µ½ÍÍ¼èí¥Ñ•µlÁ…Ñ uôˆ¤(€€€€€€€€€€€¥˜Í•±˜¹É•¹‘•É•É}Á…Ñ¡}•¹ÑÉä¹•Ñ}Ñ•áÐ ¤¹ÍÑÉ¥À ¤€ôô¥Ñ•µl‰Á…Ñ ‰tè(€€€€€€€€€€€€€€€Í•±˜¹É•¹‘•É•É}Á…Ñ¡}•¹ÑÉä¹Í•Ñ}Ñ•áÐ ˆˆ¤(€€€€€€€€€€€Í•±˜¹É•™É•Í¡}É•¹‘•É•É}±½…° ¤(€€€€€€€€€€€Í•±˜¹}É•¹‘•É•É}¡…¹• ¤(€€€€€€€•á•ÁÐá•ÁÑ¥½¸…Ì•áŒè(€€€€€€€€€€€Í•±˜¹±½œ¡˜‰I¥µ½é¥½¹”É•¹‘•É•È™…±±¥Ñ„èí•áôˆ¤((€€€‘•˜¥¹ÍÑ…±±}É•ÑÉ½}½‘•Œ¡Í•±˜°}ˆ¤è(€€€€€€€¥˜¹½ÐÍ•±˜¹ÕÉÉ•¹Ñ}ÁÉ½™¥±”èÉ•ÑÕÉ¸Í•±˜¹±½œ ‰M•±•é¥½¹„Õ¸ÁÉ½™¥±¼¸ˆ¤(€€€€€€€Ù•É‰Ì€ôl‰…±±½‘•Ìˆ°€‰¥½‘•Ìˆ°€‰¥¹•Á…¬ˆ°€‰°Í½‘•àˆ°€‰™™‘Í¡½Üˆ°€‰áÙ¥ˆ°€‰±…Ù™¥±Ñ•ÉÌˆ°€‰ÅÕ…ÉÑèˆ°€‰…µÍÑÉ•…´ˆ°€‰…Ù¥™¥°ÌÈˆ°€‰‰¥¹­ÜÌÈ‰t(€€€€€€€¥‘à€ôÍ•±˜¹É•ÑÉ½}½‘•}½µ‰¼¹•Ñ}Í•±•Ñ• ¤(€€€€€€€¥˜¥‘à€øô±•¸¡Ù•É‰Ì¤è¥‘à€ô€À(€€€€€€€Ù•Éˆ€ôÙ•É‰Ím¥‘át(€€€€€€€¡•±Á•È€ôÍ¡ÕÑ¥°¹Ý¡¥  ‰ÁŒµ…µ”µÍ…¹‘‰½àˆ¤½ÈÍÑÈ¡	M€¼€‰‰¥¸ˆ€¼€‰ÁŒµ…µ”µÍ…¹‘‰½àˆ¤(€€€€€€€Í•±˜¹±½œ¡˜‰%¹ÍÑ…±±…é¥½¹”½‘•ŒÉ•ÑÉ¼èíÙ•É‰ôˆ¤(€€€€€€€Í•±˜¹}ÍÁ…Ý¹}±½•¡m¡•±Á•È°€‰‘•ÁÌˆ°ÍÑÈ¡Í•±˜¹ÕÉÉ•¹Ñ}ÁÉ½™¥±”¤°Ù•É‰t°˜‰½‘•ŒéíÙ•É‰ôˆ¤((€€€‘•˜¥¹ÍÑ…±±}‘•Á•¹‘•¹¥•Ì¡Í•±˜°}ˆ¤è(€€€€€€€¥˜¹½ÐÍ•±˜¹ÕÉÉ•¹Ñ}ÁÉ½™¥±”èÉ•ÑÕÉ¸Í•±˜¹±½œ ‰M•±•é¥½¹„Õ¸ÁÉ½™¥±¼¸ˆ¤(€€€€€€€‘•ÁÌ€ôm•Ñ…ÑÑÈ¡É½Ü¹•Ñ}¡¥± ¤°€‰}‘•Á}­•äˆ°9½¹”¤™½ÈÉ½Ü¥¸Í•±˜¹‘•Á}±¥ÍÐ¥˜…±Í•t(€€€€€€€‘•ÁÌ€ômt(€€€€€€€É½Ü€ôÍ•±˜¹‘•Á}±¥ÍÐ¹•Ñ}™¥ÉÍÑ}¡¥± ¤(€€€€€€€Ý¡¥±”É½Üè(€€€€€€€€€€€ˆ€ôÉ½Ü¹•Ñ}¡¥± ¤(€€€€€€€€€€€¥˜¥Í¥¹ÍÑ…¹”¡ˆ°Ñ¬¹¡•­	ÕÑÑ½¸¤…¹ˆ¹•Ñ}…Ñ¥Ù” ¤è‘•ÁÌ¹…ÁÁ•¹¡•Ñ…ÑÑÈ¡ˆ°€‰}‘•Á}­•äˆ°€ˆˆ¤¤(€€€€€€€€€€€É½Ü€ôÉ½Ü¹•Ñ}¹•áÑ}Í¥‰±¥¹œ ¤(€€€€€€€ÕÍÑ½´€ôÍ•±˜¹‘•Á}¥¹ÁÕÐ¹•Ñ}Ñ•áÐ ¤¹ÍÑÉ¥À ¤(€€€€€€€¥˜ÕÍÑ½´è‘•ÁÌ¹•áÑ•¹¡ÕÍÑ½´¹ÍÁ±¥Ð ¤¤(€€€€€€€‘•ÁÌ€ômà™½Èà¥¸‘•ÁÌ¥˜át(€€€€€€€¥˜¹½Ð‘•ÁÌèÉ•ÑÕÉ¸Í•±˜¹±½œ ‰9•ÍÍÕ¹„‘¥Á•¹‘•¹é„Í•±•é¥½¹…Ñ„¸ˆ¤(€€€€€€€Í•±˜¹}ÍÁ…Ý¹}±½•¡l¡Í¡ÕÑ¥°¹Ý¡¥  ‰ÁŒµ…µ”µÍ…¹‘‰½àˆ¤½ÈÍÑÈ¡	M€¼€‰‰¥¸ˆ€¼€‰ÁŒµ…µ”µÍ…¹‘‰½àˆ¤¤°€‰‘•ÁÌˆ°ÍÑÈ¡Í•±˜¹ÕÉÉ•¹Ñ}ÁÉ½™¥±”¤°€©‘•ÁÍt°€‰‘•ÁÌˆ¤((€€€‘•˜É•‰Õ¥±‘}…¹‘}‘•ÁÌ¡Í•±˜°}ˆ¤è(€€€€€€€¥˜¹½ÐÍ•±˜¹ÕÉÉ•¹Ñ}ÁÉ½™¥±”èÉ•ÑÕÉ¸(€€€€€€€Í•±˜¹Í…Ù•}ÁÉ½™¥±”¡9½¹”¤(€€€€€€€¡•±Á•È€ôÍ¡ÕÑ¥°¹Ý¡¥  ‰ÁŒµ…µ”µÍ…¹‘‰½àˆ¤½ÈÍÑÈ¡	M€¼€‰‰¥¸ˆ€¼€‰ÁŒµ…µ”µÍ…¹‘‰½àˆ¤(€€€€€€€‘•ÁÌ€ô±¥ÍÐ¡Í•±˜¹ÁÉ½™¥±”¹•Ð ‰‘•Á•¹‘•¹¥•Ìˆ°mt¤¤(€€€€€€€‘•˜Ý½É­•È ¤è(€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€™½Èµ°Ñ…œ¥¸€ ¡m¡•±Á•È°€‰ÁÉ•™¥àµÉ•…Ñ”ˆ°ÍÑÈ¡Í•±˜¹ÕÉÉ•¹Ñ}ÁÉ½™¥±”¤°€ˆ´µÉ•‰Õ¥±‰t°€‰ÁÉ•™¥àˆ¤°¤è(€€€€€€€€€€€€€€€€€€€Í•±˜¹±½œ¡˜ˆìœ€œ¹©½¥¸¡µ¥ôˆ¤(€€€€€€€€€€€€€€€€€€€À€ôÍÕ‰ÁÉ½•ÍÌ¹A½Á•¸¡µ°ÍÑ‘½ÕÐõÍÕ‰ÁÉ½•ÍÌ¹A%A°ÍÑ‘•ÉÈõÍÕ‰ÁÉ½•ÍÌ¹MQ=UP°Ñ•áÐõQÉÕ”¤(€€€€€€€€€€€€€€€€€€€¥˜À¹ÍÑ‘½ÕÐè(€€€€€€€€€€€€€€€€€€€€€€€™½È±¥¹”¥¸À¹ÍÑ‘½ÕÐè1¥ˆ¹¥‘±•}…‘¡Í•±˜¹±½œ°˜‰míÑ…õtí±¥¹”¹ÉÍÑÉ¥À ¥ôˆ¤(€€€€€€€€€€€€€€€€€€€½‘”€ôÀ¹Ý…¥Ð ¤(€€€€€€€€€€€€€€€€€€€¥˜½‘”€„ô€Àè(€€€€€€€€€€€€€€€€€€€€€€€1¥ˆ¹¥‘±•}…‘¡Í•±˜¹±½œ°˜‰mÁÉ•™¥át•á¥Ðõí½‘•ôì‘¥Á•¹‘•¹é”¹½¸¥¹ÍÑ…±±…Ñ”ˆ¤(€€€€€€€€€€€€€€€€€€€€€€€É•ÑÕÉ¸(€€€€€€€€€€€€€€€¥˜‘•ÁÌè(€€€€€€€€€€€€€€€€€€€µ€ôm¡•±Á•È°€‰‘•ÁÌˆ°ÍÑÈ¡Í•±˜¹ÕÉÉ•¹Ñ}ÁÉ½™¥±”¤°€©‘•ÁÍtìÍ•±˜¹±½œ¡˜ˆìœ€œ¹©½¥¸¡µ¥ôˆ¤(€€€€€€€€€€€€€€€€€€€À€ôÍÕ‰ÁÉ½•ÍÌ¹A½Á•¸¡µ°ÍÑ‘½ÕÐõÍÕ‰ÁÉ½•ÍÌ¹A%A°ÍÑ‘•ÉÈõÍÕ‰ÁÉ½•ÍÌ¹MQ=UP°Ñ•áÐõQÉÕ”¤(€€€€€€€€€€€€€€€€€€€¥˜À¹ÍÑ‘½ÕÐè(€€€€€€€€€€€€€€€€€€€€€€€™½È±¥¹”¥¸À¹ÍÑ‘½ÕÐè1¥ˆ¹¥‘±•}…‘¡Í•±˜¹±½œ°˜‰m‘•ÁÍtí±¥¹”¹ÉÍÑÉ¥À ¥ôˆ¤(€€€€€€€€€€€€€€€€€€€1¥ˆ¹¥‘±•}…‘¡Í•±˜¹±½œ°˜‰m‘•ÁÍt•á¥ÐõíÀ¹Ý…¥Ð ¥ôˆ¤(€€€€€€€€€€€•á•ÁÐá•ÁÑ¥½¸…Ì•áŒè1¥ˆ¹¥‘±•}…‘¡Í•±˜¹±½œ°˜‰mÉ•‰Õ¥±‘tí•áôˆ¤(€€€€€€€Ñ¡É•…‘¥¹œ¹Q¡É•…¡Ñ…É•ÐõÝ½É­•È°‘…•µ½¸õQÉÕ”¤¹ÍÑ…ÉÐ ¤((€€€‘•˜É•™É•Í¡}‘•Á•¹‘•¹¥•Ì¡Í•±˜¤è(€€€€€€€Í•±•Ñ•€ôÍ•Ð¡Í•±˜¹ÁÉ½™¥±”¹•Ð ‰‘•Á•¹‘•¹¥•Ìˆ°mt¤¤ìÉ½Ü€ôÍ•±˜¹‘•Á}±¥ÍÐ¹•Ñ}™¥ÉÍÑ}¡¥± ¤(€€€€€€€Ý¡¥±”É½Üè(€€€€€€€€€€€ˆ€ôÉ½Ü¹•Ñ}¡¥± ¤(€€€€€€€€€€€¥˜¥Í¥¹ÍÑ…¹”¡ˆ°Ñ¬¹¡•­	ÕÑÑ½¸¤èˆ¹Í•Ñ}…Ñ¥Ù”¡•Ñ…ÑÑÈ¡ˆ°€‰}‘•Á}­•äˆ°€ˆˆ¤¥¸Í•±•Ñ•¤(€€€€€€€€€€€É½Ü€ôÉ½Ü¹•Ñ}¹•áÑ}Í¥‰±¥¹œ ¤((€€€‘•˜…‘‘}…•ÍÌ¡Í•±˜°É•…‘½¹±äè‰½½°°‘¥É•Ñ½Éäè‰½½°¤è(€€€€€€€ˆ€ô±…µ‰‘„°ÈèÍ•±˜¹}™¥¹¥Í¡}…•ÍÌ¡°È°É•…‘½¹±ä°‘¥É•Ñ½Éä¤(€€€€€€€€¡Ñ¬¹¥±•¥…±½œ ¤¹Í•±•Ñ}™½±‘•È¡Í•±˜°9½¹”°ˆ¤¥˜‘¥É•Ñ½Éä•±Í”Ñ¬¹¥±•¥…±½œ ¤¹½Á•¸¡Í•±˜°9½¹”°ˆ¤¤((€€€‘•˜}™¥¹¥Í¡}…•ÍÌ¡Í•±˜°°È°É•…‘½¹±ä°‘¥É•Ñ½Éä¤è(€€€€€€€ÑÉäè½‰¨€ô¹Í•±•Ñ}™½±‘•É}™¥¹¥Í ¡È¤¥˜‘¥É•Ñ½Éä•±Í”¹½Á•¹}™¥¹¥Í ¡È¤ìÁ…Ñ €ô½‰¨¹•Ñ}Á…Ñ  ¤(€€€€€€€•á•ÁÐ1¥ˆ¹ÉÉ½ÈèÉ•ÑÕÉ¸(€€€€€€€¥Ñ•´€ôì‰Á…Ñ ˆèÍÑÈ¡A…Ñ ¡Á…Ñ ¤¹É•Í½±Ù” ¤¤°€‰Ñ…É•Ðˆè€ˆ½¥¹ÍÑ…±°¼ˆ€¬Í…™•}¹…µ”¡A…Ñ ¡Á…Ñ ¤¹¹…µ”¤°€‰É•…‘½¹±äˆèÉ•…‘½¹±åôìÍ•±˜¹ÁÉ½™¥±”¹Í•Ñ‘•™…Õ±Ð ‰…±±½Ý•‘}Á…Ñ¡Ìˆ°mt¤¹…ÁÁ•¹¡¥Ñ•´¤ìÍ•±˜¹Í…Ù•}ÁÉ½™¥±”¡9½¹”¤ìÍ•±˜¹É•™É•Í¡}…•ÍÌ ¤((€€€‘•˜É•™É•Í¡}…•ÍÌ¡Í•±˜¤è(€€€€€€€™½ÈŒ¥¸Í•±˜¹}¡¥±‘É•¸¡Í•±˜¹…•ÍÍ}±¥ÍÐ¤èÍ•±˜¹…•ÍÍ}±¥ÍÐ¹É•µ½Ù”¡Œ¤(€€€€€€€™½È¥Ñ•´¥¸Í•±˜¹ÁÉ½™¥±”¹•Ð ‰…±±½Ý•‘}Á…Ñ¡Ìˆ°mt¤è(€€€€€€€€€€€É½Ü€ôÑ¬¹1¥ÍÑ	½áI½Ü ¤ìÉ½Ü¹}¥Ñ•´€ô¥Ñ•´ìÉ½Ü¹Í•Ñ}¡¥±¡Ñ¬¹1…‰•°¡±…‰•°õ˜‰mì€I<œ¥˜¥Ñ•´¹•Ð É•…‘½¹±äœ°QÉÕ”¤•±Í”€I\œõtí¥Ñ•´¹•Ð Á…Ñ œ¥ôƒŠHí¥Ñ•´¹•Ð Ñ…É•Ðœ¥ôˆ°á…±¥¸ôÀ°ÝÉ…ÀõQÉÕ”¤¤ìÍ•±˜¹…•ÍÍ}±¥ÍÐ¹…ÁÁ•¹¡É½Ü¤((€€€‘•˜É•µ½Ù•}…•ÍÌ¡Í•±˜°}ˆ¤è(€€€€€€€É½Ü€ôÍ•±˜¹…•ÍÍ}±¥ÍÐ¹•Ñ}Í•±•Ñ•‘}É½Ü ¤(€€€€€€€¥˜É½Üè(€€€€€€€€€€€Í•±˜¹ÁÉ½™¥±”¹•Ð ‰…±±½Ý•‘}Á…Ñ¡Ìˆ°mt¤¹É•µ½Ù”¡É½Ü¹}¥Ñ•´¤ìÍ•±˜¹Í…Ù•}ÁÉ½™¥±”¡9½¹”¤ìÍ•±˜¹É•™É•Í¡}…•ÍÌ ¤((€€€‘•˜…‘‘}‘¥ÍŒ¡Í•±˜°}ˆ¤è(€€€€€€€À€ôÍ•±˜¹‘¥Í}•¹ÑÉä¹•Ñ}Ñ•áÐ ¤¹ÍÑÉ¥À ¤(€€€€€€€¥˜¹½ÐÀèÉ•ÑÕÉ¸(€€€€€€€Í•±˜¹ÁÉ½™¥±”¹Í•Ñ‘•™…Õ±Ð ‰‘¥ÍÌˆ°mt¤¹…ÁÁ•¹¡ì‰¥µ…”ˆèÍÑÈ¡A…Ñ ¡À¤¹•áÁ…¹‘ÕÍ•È ¤¹É•Í½±Ù” ¤¥ô¤ìÍ•±˜¹Í…Ù•}ÁÉ½™¥±”¡9½¹”¤ìÍ•±˜¹É•™É•Í¡}‘¥ÍÌ ¤ìÍ•±˜¹‘¥Í}•¹ÑÉä¹Í•Ñ}Ñ•áÐ ˆˆ¤((€€€‘•˜É•™É•Í¡}‘¥ÍÌ¡Í•±˜¤è(€€€€€€€Í•±˜¹‘¥Í}ÍÑ…ÑÕÌ¹Í•Ñ}Ñ•áÐ ‰¥Í¡¤…ÍÍ½¥…Ñ¤è€ˆ€¬ÍÑÈ¡±•¸¡Í•±˜¹ÁÉ½™¥±”¹•Ð ‰‘¥ÍÌˆ°mt¤¤¤¤((€€€‘•˜ÉÕ¹}¡½ÍÑ}Ñ•ÍÑ}…Õ‘¥¼¡Í•±˜¤è(€€€€€€€¡•±Á•È€ôÍ¡ÕÑ¥°¹Ý¡¥  ‰ÁŒµ…µ”µÍ…¹‘‰½àˆ¤½ÈÍÑÈ¡	M€¼€‰‰¥¸ˆ€¼€‰ÁŒµ…µ”µÍ…¹‘‰½àˆ¤(€€€€€€€¥˜¹½ÐÍ•±˜¹ÕÉÉ•¹Ñ}ÁÉ½™¥±”èÉ•ÑÕÉ¸Í•±˜¹±½œ ‰%°Ñ•ÍÐ…Õ‘¥¼É¥¡¥•‘”Õ¸ÁÉ½™¥±¼É•…Ñ¼”Í•±•é¥½¹…Ñ¼¸ˆ¤(€€€€€€€Í•±˜¹…Õ‘¥½}¥¹™¼¹Í•Ñ}Ñ•áÐ ‰Q•ÍÐ…Õ‘¥¼…ÙÙ¥…Ñ¼ì½¹ÑÉ½±±„…¹¡”¥°Ñ…ˆ1½œ¸ˆ¤(€€€€€€€Í•±˜¹}ÍÁ…Ý¹}±½•¡m¡•±Á•È°€‰‘¥…œˆ°ÍÑÈ¡Í•±˜¹ÕÉÉ•¹Ñ}ÁÉ½™¥±”¤°€ˆ´´ˆ°€‰Í ˆ°€ˆµ±Œˆ°€‰Á…Ñ°¥¹™¼€˜˜Á…Á±…ä€½ÕÍÈ½Í¡…É”½Í½Õ¹‘Ì½™É••‘•Í­Ñ½À½ÍÑ•É•¼½½µÁ±•Ñ”¹½„‰t°€‰…Õ‘¥¼ˆ¤(()‘•˜I9%QI}%9`¡Ù…±Õ”èÍÑÈ¤€´ø¥¹Ðè(€€€ÑÉäèÉ•ÑÕÉ¸I9IIL¹¥¹‘•à¡Ù…±Õ”¤(€€€•á•ÁÐY…±Õ•ÉÉ½ÈèÉ•ÑÕÉ¸€Ä(()±…ÍÌ5…¹…•ÉÁÀ¡Ñ¬¹ÁÁ±¥…Ñ¥½¸¤è(€€€‘•˜}}¥¹¥Ñ}|¡Í•±˜¤èÍÕÁ•È ¤¹}}¥¹¥Ñ}|¡…ÁÁ±¥…Ñ¥½¹}¥õAA}%¤(€€€‘•˜‘½}…Ñ¥Ù…Ñ”¡Í•±˜¤è5…¹…•É]¥¹‘½Ü¡Í•±˜¤¹ÁÉ•Í•¹Ð ¤(()¥˜}}¹…µ•}|€ôô€‰}}µ…¥¹}|ˆè(€€€5…¹…•ÉÁÀ ¤¹ÉÕ¸¡ÍåÌ¹…ÉØ¤(