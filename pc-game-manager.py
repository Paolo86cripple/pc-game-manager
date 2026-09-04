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
        found.append({"mode": "pci", "pci": pci, "vendor_device": f"{vid.lower()}:{did.lower()}", "label": f"{desc} — {pci}", "model": desc})
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
            b = Gtk.Button(label="Scegli…"); row.append(b)
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
        self.home = self.entry(box, "HOME sandbox", str(DEFAULT_STORAGE / "nuovo" / "home"), folder=True)
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
        self.arch = Gtk.DropDown.new_from_strings(list(ARCHES)); self.arch.set_selected(0); self._row(box, "Architettura", self.arch)
        self.winver = Gtk.DropDown.new_from_strings([x[0] for x in WINDOWS]); self.winver.set_selected(1); self._row(box, "Versione Windows", self.winver)
        self.runner_combo = Gtk.DropDown.new_from_strings(["Wine di sistema"]); self._row(box, "Runtime", self.runner_combo)
        refresh = Gtk.Button(label="Aggiorna runtime locali"); refresh.connect("clicked", lambda _b: self.refresh_runners()); box.append(refresh)
        self.bootstrap = Gtk.CheckButton(label="Crea e inizializza automaticamente il prefix"); self.bootstrap.set_active(True); box.append(self.bootstrap)
        self.refresh_runners()

    def refresh_runners(self):
        self.runner_options = discover_runners()
        labels = ["Wine di sistema"] + [f"{x['name']} — {x['source']}" for x in self.runner_options if x.get("kind") != "wine-system"]
        self.runner_combo.set_model(Gtk.StringList.new(labels)); self.runner_combo.set_selected(0)

    def _row(self, box, label, widget):
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12); row.append(Gtk.Label(label=label, xalign=0)); widget.set_hexpand(True); row.append(widget); box.append(row)

    def page_graphics(self):
        box = self.page("Grafica")
        self.gpu = Gtk.DropDown.new_from_strings([x["label"] for x in self.parent_window.gpu_devices]); self.gpu.set_selected(0); self._row(box, "GPU", self.gpu)
        self.renderer = Gtk.DropDown.new_from_strings(["Auto", "WineD3D", "DXVK", "D7VK", "dgVoodoo2"]); self.renderer.set_selected(1); self._row(box, "Renderer", self.renderer)
        self.renderer_path = self.entry(box, "Directory runtime renderer", "", folder=True)
        self.display_backend = Gtk.DropDown.new_from_strings(["Auto (Wayland → XWayland)", "Wayland nativo", "XWayland"]); self.display_backend.set_selected(0); self._row(box, "Backend display/input", self.display_backend)
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
        test = Gtk.Button(label="Test audio host → sandbox"); test.connect("clicked", self._audio_test); box.append(test)
        self.audio_status = Gtk.Label(label="Non testato", xalign=0); box.append(self.audio_status)
        n = Gtk.Label(label="Dipendenze/codec usano temporaneamente la rete host. Il runtime Wine non riceve /dev/snd/seq.", xalign=0, wrap=True); n.add_css_class("dim-label"); box.append(n)

    def _check(self, box, text, active):
        cb = Gtk.CheckButton(label=text); cb.set_active(active); box.append(cb); return cb

    def page_deps(self):
        box = self.page("Dipendenze")
        self.deps: dict[str, Gtk.CheckButton] = {}
        labels = [("corefonts", "Microsoft Core Fonts"), ("vcrun2022", "Visual C++ 2015–2022"), ("d3dcompiler_47", "Direct3D compiler 47"), ("faudio", "FAudio / XAudio"), ("xact", "XACT audio runtime"), ("dotnet48", ".NET Framework 4.8")]
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
            f"Profilo: {self.name.get_text().strip()}", f"Gioco: {self.game_root.get_text().strip()}", f"EXE: {self.exe.get_text().strip()}",
            f"Prefix: {self.prefix.get_text().strip()}", f"HOME sandbox: {self.home.get_text().strip()}", f"Salvataggi: {self.saves.get_text().strip()}",
            f"Runtime: {runtime}", f"Architettura: {ARCHES[self.arch.get_selected()]}", f"Windows: {self.winver.get_selected_item().get_string() if self.winver.get_selected_item() else 'Windows 10'}",
            f"GPU: {gpu.get('label', 'Auto')}", f"Renderer: {RENDERERS[self.renderer.get_selected()]}", f"Display/input: {self.display_backend.get_selected() == 0 and 'Auto (Wayland → XWayland)' or self.display_backend.get_selected() == 1 and 'Wayland nativo' or 'XWayland'}",
            f"Audio: {'PipeWire/PulseAudio' if self.audio_backend.get_selected() == 0 else 'disabilitato'}",
            f"Dipendenze: {', '.join(deps) if deps else 'nessuna'}", f"Rete runtime: {'consentita' if self.network.get_selected() == 1 else 'negata'}",
            "\nCrea ambiente eseguirà bootstrap del prefix e poi installerà le dipendenze selezionate.",
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
        compat = Gtk.Label(label="Compatibilità Wine", xalign=0); compat.add_css_class("title-4"); b.append(compat)
        self.p_arch_combo = Gtk.DropDown.new_from_strings(["64 bit (WoW64)", "32 bit"]); self._row(b, "Architettura prefix", self.p_arch_combo)
        self.p_windows_combo = Gtk.DropDown.new_from_strings([x[0] for x in WINDOWS]); self._row(b, "Versione Windows", self.p_windows_combo)
        note = Gtk.Label(label="La versione Windows può essere applicata a un prefix esistente. Cambiare 64/32 bit richiede invece la ricreazione del prefix.", xalign=0, wrap=True); note.add_css_class("dim-label"); b.append(note)
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
        dg_note = Gtk.Label(label="dgVoodoo2 è disponibile come renderer/wrapper per DirectDraw, vecchi Direct3D e Glide. Può essere combinato con un backend D3D moderno in revisioni successive.", xalign=0, wrap=True); dg_note.add_css_class("dim-label"); b.append(dg_note)
        b.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
        display_title = Gtk.Label(label="Display e input finestra", xalign=0); display_title.add_css_class("title-4"); b.append(display_title)
        self.display_backend_combo = Gtk.DropDown.new_from_strings(["Auto (Wayland → XWayland)", "Wayland nativo", "XWayland"]); self.display_backend_combo.connect("notify::selected", self.changed_save); self._row(b, "Backend display/input", self.display_backend_combo)
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
        exposure = Gtk.Label(label="Interfacce esposte", xalign=0); exposure.add_css_class("title-4"); b.append(exposure)
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
        for key, label in (("corefonts", "Microsoft Core Fonts"), ("vcrun2022", "Visual C++ 2015–2022"), ("d3dcompiler_47", "Direct3D compiler 47"), ("faudio", "FAudio / XAudio"), ("xact", "XACT"), ("dotnet48", ".NET Framework 4.8")):
            cb = Gtk.CheckButton(label=label); cb._dep_key = key; self.dep_list.append(cb)
        r = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8); b.append(r); x = Gtk.Button(label="Installa/Ripara selezionate"); x.connect("clicked", self.install_dependencies); r.append(x); rb = Gtk.Button(label="Ricrea e reinstalla tutto"); rb.connect("clicked", self.rebuild_and_deps); r.append(rb)
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
            bt = Gtk.Button(label="Scegli…"); r.append(bt)
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
        self.launch_status.set_text(f"{self.p_name.get_text()} — {self.p_exe.get_text()}"); self.log(f"Profilo caricato: {row._path.name}")

    def _refresh_wayland_status(self):
        if not hasattr(self, "wayland_status_label"):
            return
        status = str(self.profile.get("wayland_input_status", "unknown")) if isinstance(self.profile, dict) else "unknown"
        reason = str(self.profile.get("wayland_input_reason", "")).strip() if isinstance(self.profile, dict) else ""
        labels = {"working": "funzionante", "broken": "NON funzionante", "unknown": "non verificato"}
        text = f"Stato input Wayland: {labels.get(status, status)}"
        if reason:
            text += f" — {reason}"
        if status == "broken":
            text += " — Auto userà XWayland"
        self.wayland_status_label.set_text(text)

    def _set_wayland_status(self, status: str, reason: str = ""):
        if not self.current_profile:
            return self.log("Seleziona un profilo.")
        self.profile["wayland_input_status"] = status
        self.profile["wayland_input_reason"] = reason
        self.current_profile.write_text(json.dumps(self.profile, indent=2), encoding="utf-8")
        self._refresh_wayland_status()
        self.log(f"Wayland input per {self.current_profile.name}: {status}" + (f" — {reason}" if reason else ""))

    def mark_wayland_broken(self, _b=None):
        self._set_wayland_status("broken", "input tastiera non disponibile con WineWayland per questo gioco")

    def mark_wayland_working(self, _b=None):
        self._set_wayland_status("working", "input tastiera verificato con WineWayland")

    def retry_wayland(self, _b=None):
        self._set_wayland_status("unknown", "")
        self.log("Wayland verrà ritentato al prossimo avvio in modalità Auto.")

    def debug_wayland_input(self, _b=None):
        if not self.current_profile:
            return self.log("Seleziona un profilo.")
        self.save_profile(None)
        helper = shutil.which("pc-game-sandbox") or str(BASE / "bin" / "pc-game-sandbox")
        self._spawn_logged([helper, "input-debug", str(self.current_profile)], "wayland-input")

    def _select_gpu(self, gpu):
        self.gpu_combo.set_selected(0)
        if gpu.get("mode") == "pci":
            for i, d in enumerate(self.gpu_devices):
                if d.get("pci") == gpu.get("pci") and d.get("vendor_device") == gpu.get("vendor_device"): self.gpu_combo.set_selected(i); break
        idx = self.gpu_combo.get_selected(); d = self.gpu_devices[idx] if idx < len(self.gpu_devices) else self.gpu_devices[0]; self.gpu_info.set_text(f"Modello: {d.get('model')}\nPCI: {d.get('pci', 'auto')}\nVendor:Device: {d.get('vendor_device', 'auto')}")

    def refresh_gpus(self):
        self.gpu_devices = detect_gpus(); self.gpu_combo.set_model(Gtk.StringList.new([d["label"] for d in self.gpu_devices])); self._select_gpu(self.profile.get("gpu", {"mode": "auto"})); self.log(f"GPU rilevate: {max(0, len(self.gpu_devices)-1)}")

    def _renderer_changed(self, *_args):
        if self._loading:
            return
        idx = self.renderer_combo.get_selected()
        renderer = RENDERERS[idx] if idx < len(RENDERERS) else "wined3d"
        if renderer in ("auto", "wined3d"):
            self.renderer_auto_status.set_text("Nessuna DLL esterna necessaria.")
            return
        path = self.renderer_path_entry.get_text().strip()
        if not path:
            detected = self.autodetect_renderer_path(log=False)
            if detected:
                self.renderer_path_entry.set_text(str(detected))
                path = str(detected)
        self.renderer_auto_status.set_text(f"{renderer.upper()}: {path if path else 'nessun componente locale rilevato'}")

    def autodetect_renderer_path(self, log=True):
        idx = self.renderer_combo.get_selected()
        renderer = RENDERERS[idx] if idx < len(RENDERERS) else "wined3d"
        if renderer in ("auto", "wined3d"):
            return None
        required = {"dxvk": {"d3d11.dll", "dxgi.dll"}, "d7vk": {"ddraw.dll"}, "dgvoodoo": {"ddraw.dll"}}.get(renderer, set())
        if not required:
            return None
        candidates = []
        home = Path.home()
        candidates += [home / ".local/share/pc-game-manager/dxvk", home / ".local/share/pc-game-manager/d7vk", Path("/usr/share/dxvk"), Path("/usr/share/d7vk"), Path("/opt/dxvk"), Path("/opt/d7vk")]
        pacman = shutil.which("pacman")
        if pacman:
            try:
                out = subprocess.check_output([pacman, "-Ql", "dxvk" if renderer == "dxvk" else "d7vk"], text=True, stderr=subprocess.DEVNULL) if renderer in ("dxvk", "d7vk") else ""
                candidates += [Path(line.split(None, 1)[1]) for line in out.splitlines() if " " in line and Path(line.split(None, 1)[1]).exists()]
            except Exception:
                pass
        for base in candidates:
            if base.is_file() and base.name.lower() in {x.lower() for x in required}:
                base = base.parent
            if base.is_dir():
                found = {p.name.lower() for p in base.rglob("*.dll") if p.is_file()}
                if required.issubset(found):
                    self.renderer_path_entry.set_text(str(base))
                    self.renderer_auto_status.set_text(f"{renderer.upper()} rilevato: {base}")
                    if log: self.log(f"Renderer {renderer} rilevato in {base}")
                    return base
        if log: self.log(f"Nessun componente {renderer.upper()} locale rilevato; puoi indicare manualmente la directory DLL.")
        self.renderer_auto_status.set_text(f"{renderer.upper()}: nessun componente locale rilevato")
        return None

    def _run_selected_file(self, mode: str):
        if not self.current_profile:
            return self.log("Crea/seleziona un profilo prima di eseguire un installer o EXE.")
        host = self.prefix_exe.get_text().strip()
        if not host:
            return self.log("Seleziona un EXE/installer.")
        helper = shutil.which("pc-game-sandbox") or str(BASE / "bin" / "pc-game-sandbox")
        self.save_profile(None)
        self._spawn_logged([helper, "run-file", str(self.current_profile), host], mode)

    def _profile_data_from_ui(self) -> dict:
        d = dict(self.profile)
        d.update({"name": self.p_name.get_text().strip(), "game_root": self.p_game.get_text().strip(), "executable": self.p_exe.get_text().strip(), "prefix_root": self.p_prefix.get_text().strip(), "sandbox_home": self.p_home.get_text().strip(), "save_root": self.p_saves.get_text().strip()})
        d["wine_arch"] = ARCHES[self.p_arch_combo.get_selected() if self.p_arch_combo.get_selected() < len(ARCHES) else 0]
        wi = self.p_windows_combo.get_selected() if self.p_windows_combo.get_selected() < len(WINDOWS) else 1
        d["windows_version"] = WINDOWS[wi][1]
        d["game_root_readonly"] = self.game_root_ro.get_active()
        i = self.gpu_combo.get_selected(); gpu = self.gpu_devices[i] if i < len(self.gpu_devices) else self.gpu_devices[0]; d["gpu"] = {k: gpu[k] for k in ("mode", "pci", "vendor_device", "model") if k in gpu}
        d["renderer"] = RENDERERS[self.renderer_combo.get_selected() if self.renderer_combo.get_selected() < len(RENDERERS) else 1]; d["renderer_path"] = self.renderer_path_entry.get_text().strip(); d["vkd3d_enabled"] = self.vkd3d_enable.get_active(); d["vkd3d_path"] = self.vkd3d_path_entry.get_text().strip(); d["nvapi_enabled"] = self.nvapi_enable.get_active(); d["nvapi_path"] = self.nvapi_path_entry.get_text().strip(); d["display_backend"] = ("auto", "wayland", "xwayland")[self.display_backend_combo.get_selected() if self.display_backend_combo.get_selected() < 3 else 0]; d["xwayland_fallback"] = self.xwayland_fallback_cb.get_active(); d.setdefault("wayland_input_status", self.profile.get("wayland_input_status", "unknown") if isinstance(self.profile, dict) else "unknown"); d.setdefault("wayland_input_reason", self.profile.get("wayland_input_reason", "") if isinstance(self.profile, dict) else ""); d["prefer_wayland"] = d["display_backend"] != "xwayland"; d["network"] = "host" if self.network_combo.get_selected() == 1 else "none"; d["audio_backend"] = "pulse" if self.audio_combo.get_selected() == 0 else "disabled"; d["capabilities"] = {k: cb.get_active() for k, cb in self.cap_checks.items()}; d["capabilities"]["audio"] = self.audio_cap.get_active()
        return d

    def apply_wine_profile_settings(self, _b):
        if not self.current_profile:
            return self.log("Seleziona un profilo.")
        old_arch = str(self.profile.get("wine_arch", "win64"))
        new_arch = ARCHES[self.p_arch_combo.get_selected() if self.p_arch_combo.get_selected() < len(ARCHES) else 0]
        self.save_profile(None)
        if old_arch != new_arch:
            self.log("Architettura prefix modificata: per passare tra win64 e win32 usa Rebuild. La modifica è stata salvata ma non applicata al prefix esistente.")
            return
        helper = shutil.which("pc-game-sandbox") or str(BASE / "bin" / "pc-game-sandbox")
        self._spawn_logged([helper, "prefix-create", str(self.current_profile)], "wine-settings")

    def disable_external_renderer(self, _b=None):
        self.renderer_combo.set_selected(1)
        self.renderer_path_entry.set_text("")
        self.renderer_auto_status.set_text("Nessun renderer esterno: WineD3D")
        if self.current_profile:
            self.save_profile(None)

    def clear_graphics_addons(self, _b=None):
        self.vkd3d_enable.set_active(False)
        self.vkd3d_path_entry.set_text("")
        self.nvapi_enable.set_active(False)
        self.nvapi_path_entry.set_text("")
        self.log("Componenti grafici opzionali disattivati e deselezionati.")
        if self.current_profile:
            self.save_profile(None)

    def _sandbox_policy_changed(self, *_args):
        self._refresh_sandbox_policy_info()

    def _refresh_sandbox_policy_info(self):
        if not hasattr(self, "sandbox_policy_info"):
            return
        lines = ["HOME host e D-Bus host: non esposti. Runtime e sistema: sola lettura."]
        if hasattr(self, "game_root_ro") and self.game_root_ro.get_active():
            lines.append("Directory gioco: sola lettura; prefix, HOME sandbox e salvataggi restano scrivibili.")
        else:
            lines.append("⚠ Directory gioco: scrivibile per compatibilità legacy.")
        if hasattr(self, "network_combo") and self.network_combo.get_selected() == 1:
            lines.append("⚠ Rete: namespace host condiviso; il gioco può raggiungere Internet e dispositivi LAN.")
        else:
            lines.append("Rete: namespace isolato, nessun accesso Internet/LAN.")
        if hasattr(self, "display_backend_combo"):
            backend = self.display_backend_combo.get_selected()
            status = str(self.profile.get("wayland_input_status", "unknown")) if isinstance(self.profile, dict) else "unknown"
            xwayland_effective = backend == 2 or (backend == 0 and status == "broken")
            if xwayland_effective:
                lines.append("⚠ XWayland: IPC host condiviso per compatibilità MIT-SHM; gli altri namespace restano isolati.")
            elif backend == 0:
                lines.append("Display Auto: Wayland usa IPC isolato; un fallback XWayland può condividere IPC se necessario.")
            else:
                lines.append("Wayland nativo: IPC isolato.")
        self.sandbox_policy_info.set_text("\n".join(lines))

    def save_profile(self, _b):
        if not self.current_profile: self.log("Seleziona un profilo."); return
        self.profile = self._profile_data_from_ui(); self.current_profile.write_text(json.dumps(self.profile, indent=2), encoding="utf-8"); self.log(f"Profilo salvato: {self.current_profile.name}")

    def changed_save(self, *_):
        if not self._loading: pass

    def delete_profile(self, _b):
        if not self.current_profile: return self.log("Seleziona un profilo.")
        p = self.current_profile; data = self.profile; win = Gtk.Window(title="Elimina profilo", transient_for=self, modal=True, default_width=520, default_height=220); box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12); box.set_margin_top(16); box.set_margin_bottom(16); box.set_margin_start(16); box.set_margin_end(16); win.set_child(box); box.append(Gtk.Label(label=f"Eliminare {p.stem}? Il gioco originale non verrà toccato.", wrap=True, xalign=0)); deldata = Gtk.CheckButton(label="Elimina anche prefix, HOME e salvataggi"); box.append(deldata); r = Gtk.Box(spacing=8); box.append(r); c = Gtk.Button(label="Annulla"); c.connect("clicked", lambda _b: win.close()); r.append(c); ok = Gtk.Button(label="Elimina"); ok.add_css_class("destructive-action"); r.append(ok)
        def do(_):
            try:
                if p.exists(): p.unlink()
                if deldata.get_active():
                    for key in ("prefix_root", "sandbox_home", "save_root"):
                        q = Path(str(data.get(key, ""))).expanduser()
                        if q.is_dir() and len(q.parts) >= 4 and q not in (Path.home(), Path("/")): shutil.rmtree(q)
                self.current_profile = None; self.profile = {}; win.close(); self.load_profiles(); self.log(f"Profilo eliminato: {p.name}")
            except Exception as exc: self.log(f"Eliminazione fallita: {exc}")
        ok.connect("clicked", do); win.present()

    def rebuild_profile(self, _b):
        if not self.current_profile: return self.log("Seleziona un profilo.")
        self.save_profile(None); self.bootstrap_and_deps(self.current_profile, self.profile.get("dependencies", []), True)

    def run_sandbox(self, args: list[str], tag: str, network=False):
        if not self.current_profile: return self.log("Seleziona un profilo.")
        helper = shutil.which("pc-game-sandbox") or str(BASE / "bin" / "pc-game-sandbox")
        self._spawn_logged([helper, *args, str(self.current_profile)], tag)

    def _spawn_logged(self, cmd: list[str], tag: str):
        self.log(f"$ {' '.join(cmd)}")
        def worker():
            try:
                p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                if p.stdout:
                    for line in p.stdout: GLib.idle_add(self.log, f"[{tag}] {line.rstrip()}")
                code = p.wait(); GLib.idle_add(self.log, f"[{tag}] exit={code}")
            except Exception as exc: GLib.idle_add(self.log, f"[{tag}] {exc}")
        threading.Thread(target=worker, daemon=True).start()

    def bootstrap_and_deps(self, path: Path, deps: list[str], bootstrap: bool):
        helper = shutil.which("pc-game-sandbox") or str(BASE / "bin" / "pc-game-sandbox")
        if bootstrap: self._spawn_logged([helper, "prefix-create", str(path)], "prefix")
        if deps: self._spawn_logged([helper, "deps", str(path), *deps], "deps")

    def run_profile_tool(self, payload: list[str], tag: str):
        helper = shutil.which("pc-game-sandbox") or str(BASE / "bin" / "pc-game-sandbox"); self._spawn_logged([helper, "wine", str(self.current_profile), "--", *payload], tag)

    def launch_game(self, _b):
        if not self.current_profile: return
        self.save_profile(None); self._spawn_logged([(shutil.which("pc-game-sandbox") or str(BASE / "bin" / "pc-game-sandbox")), "run", str(self.current_profile)], "game")

    def refresh_runtime_local(self, _b=None):
        self.runner_local = discover_runners();
        for child in self._children(self.runtime_list): self.runtime_list.remove(child)
        for item in self.runner_local:
            row = Gtk.ListBoxRow(); row._runner = item; row.set_child(Gtk.Label(label=f"{item['name']} — {item['kind']} — {item['source']}\n{item['path']}", xalign=0, wrap=True)); self.runtime_list.append(row)
        labels = ["Wine di sistema"] + [f"{x['name']} — {x['source']}" for x in self.runner_local if x.get('kind') != 'wine-system']
        self.runtime_choice.set_model(Gtk.StringList.new(labels)); self.runtime_choice.set_selected(0)
        self.runtime_status.set_text(f"Runtime locali rilevati: {len(self.runner_local)}")
        self._restore_runtime_choice()

    def _children(self, widget):
        out=[]; c=widget.get_first_child()
        while c: out.append(c); c=c.get_next_sibling()
        return out

    def _restore_runtime_choice(self):
        target = self.profile.get('selected_runner') if isinstance(self.profile, dict) else None
        if not isinstance(target, dict): self.runtime_choice.set_selected(0); return
        for i, item in enumerate([x for x in self.runner_local if x.get('kind') != 'wine-system'], start=1):
            if item.get('path') == target.get('path'):
                self.runtime_choice.set_selected(i); return
        self.runtime_choice.set_selected(0)

    def runtime_choice_changed(self, *_):
        if self._loading or not self.current_profile: return
        idx = self.runtime_choice.get_selected()
        candidates = [x for x in self.runner_local if x.get('kind') != 'wine-system']
        self.profile['selected_runner'] = None if idx == 0 or idx-1 >= len(candidates) else candidates[idx-1]
        self.save_profile(None)

    def refresh_catalog(self, _b=None):
        self.catalog_status.set_text("Aggiornamento catalogo remoto…")
        def worker():
            items, errors = catalog_all(); GLib.idle_add(self.show_catalog, items, errors)
        threading.Thread(target=worker, daemon=True).start()

    def show_catalog(self, items, errors):
        self.runner_catalog = items
        for child in list(self.catalog_list): self.catalog_list.remove(child)
        for item in items:
            row = Gtk.ListBoxRow(); row._runner = item; row.set_child(Gtk.Label(label=f"{item.get('name')} · {item.get('family', item.get('kind',''))} · {item.get('source','')}\n{item.get('filename', 'manifest remoto')}", xalign=0, wrap=True)); self.catalog_list.append(row)
        self.catalog_status.set_text(f"Catalogo remoto: {len(items)} runtime disponibili" + (f" — {len(errors)} provider non raggiungibili" if errors else "")); self.log("Catalogo runtime aggiornato."); return False

    def install_selected_runtime(self, _b):
        row = self.catalog_list.get_selected_row()
        if not row: return self.log("Seleziona un runtime dal catalogo.")
        item = row._runner; base = Path.home() / ".local/share/pc-game-manager/runners"
        def worker():
            try:
                p = download_and_install(item, base); GLib.idle_add(self.log, f"Runtime installato: {p}"); GLib.idle_add(self.refresh_runtime_local)
            except Exception as exc: GLib.idle_add(self.log, f"Installazione runtime fallita: {exc}")
        threading.Thread(target=worker, daemon=True).start()

    def remove_selected_runtime(self, _b):
        row = self.runtime_list.get_selected_row()
        if not row: return self.log("Seleziona un runtime locale.")
        item = row._runner
        if item.get("kind") == "wine-system" or item.get("source") == "Sistema": return self.log("Il runtime di sistema non può essere disinstallato da qui.")
        p = Path(item["path"])
        try: shutil.rmtree(p); self.log(f"Runtime rimosso: {p}"); self.refresh_runtime_local()
        except Exception as exc: self.log(f"Rimozione runtime fallita: {exc}")

    def refresh_renderer_local(self, _b=None):
        self.renderer_local = discover_renderers()
        for child in self._children(self.renderer_local_list): self.renderer_local_list.remove(child)
        for item in self.renderer_local:
            row = Gtk.ListBoxRow(); row._renderer = item
            row.set_child(Gtk.Label(label=f"{item['kind'].upper()} {item['version']}\n{item['path']}", xalign=0, wrap=True))
            self.renderer_local_list.append(row)
        self.renderer_local_status.set_text(f"Renderer installati da PC Game Manager: {len(self.renderer_local)}")
        return False

    def refresh_renderer_catalog(self, _b=None):
        self.renderer_catalog_status.set_text("Aggiornamento catalogo grafico…")
        def worker():
            items, errors = renderer_catalog_all(); GLib.idle_add(self.show_renderer_catalog, items, errors)
        threading.Thread(target=worker, daemon=True).start()

    def show_renderer_catalog(self, items, errors):
        self.renderer_catalog = items
        for child in self._children(self.renderer_catalog_list): self.renderer_catalog_list.remove(child)
        for item in items:
            row = Gtk.ListBoxRow(); row._renderer = item
            row.set_child(Gtk.Label(label=f"{item.get('kind','').upper()} · {item.get('version','')} · {item.get('source','')}\n{item.get('filename','')}", xalign=0, wrap=True))
            self.renderer_catalog_list.append(row)
        suffix = f" — {len(errors)} fonte/i non raggiungibili" if errors else ""
        self.renderer_catalog_status.set_text(f"Catalogo renderer: {len(items)} release disponibili{suffix}")
        for err in errors: self.log(f"Catalogo renderer: {err}")
        return False

    def install_selected_renderer(self, _b):
        row = self.renderer_catalog_list.get_selected_row()
        if not row: return self.log("Seleziona un componente grafico dal catalogo.")
        item = row._renderer
        self.log(f"Installazione {item.get('kind','').upper()} {item.get('version','')}…")
        def worker():
            try:
                path = install_renderer(item)
                GLib.idle_add(self.log, f"Renderer installato: {path}")
                GLib.idle_add(self.refresh_renderer_local)
            except Exception as exc:
                GLib.idle_add(self.log, f"Installazione renderer fallita: {exc}")
        threading.Thread(target=worker, daemon=True).start()

    def use_selected_renderer(self, _b):
        row = self.renderer_local_list.get_selected_row()
        if not row: return self.log("Seleziona un renderer locale.")
        item = row._renderer
        kind = item.get("kind")
        if kind == "vkd3d":
            self.vkd3d_enable.set_active(True); self.vkd3d_path_entry.set_text(item["path"])
        elif kind == "dxvk-nvapi":
            self.nvapi_enable.set_active(True); self.nvapi_path_entry.set_text(item["path"])
        else:
            idx = {"dxvk": 2, "d7vk": 3, "dgvoodoo": 4}.get(kind, 1)
            self.renderer_combo.set_selected(idx); self.renderer_path_entry.set_text(item["path"])
            self.renderer_auto_status.set_text(f"{kind.upper()} selezionato: {item['version']}")
        self.log(f"Componente grafico selezionato: {kind.upper()} {item['version']}")
        if self.current_profile: self.save_profile(None)

    def remove_selected_renderer(self, _b):
        row = self.renderer_local_list.get_selected_row()
        if not row: return self.log("Seleziona un renderer locale.")
        item = row._renderer
        try:
            remove_renderer(item["path"])
            self.log(f"Renderer rimosso: {item['path']}")
            if self.renderer_path_entry.get_text().strip() == item["path"]:
                self.renderer_path_entry.set_text("")
            self.refresh_renderer_local()
            self._renderer_changed()
        except Exception as exc:
            self.log(f"Rimozione renderer fallita: {exc}")

    def install_retro_codec(self, _b):
        if not self.current_profile: return self.log("Seleziona un profilo.")
        verbs = ["allcodecs", "icodecs", "cinepak", "l3codecx", "ffdshow", "xvid", "lavfilters", "quartz", "amstream", "avifil32", "binkw32"]
        idx = self.retro_codec_combo.get_selected()
        if idx >= len(verbs): idx = 0
        verb = verbs[idx]
        helper = shutil.which("pc-game-sandbox") or str(BASE / "bin" / "pc-game-sandbox")
        self.log(f"Installazione codec retro: {verb}")
        self._spawn_logged([helper, "deps", str(self.current_profile), verb], f"codec:{verb}")

    def install_dependencies(self, _b):
        if not self.current_profile: return self.log("Seleziona un profilo.")
        deps = [getattr(row.get_child(), "_dep_key", None) for row in self.dep_list if False]
        deps = []
        row = self.dep_list.get_first_child()
        while row:
            cb = row.get_child()
            if isinstance(cb, Gtk.CheckButton) and cb.get_active(): deps.append(getattr(cb, "_dep_key", ""))
            row = row.get_next_sibling()
        custom = self.dep_input.get_text().strip()
        if custom: deps.extend(custom.split())
        deps = [x for x in deps if x]
        if not deps: return self.log("Nessuna dipendenza selezionata.")
        self._spawn_logged([(shutil.which("pc-game-sandbox") or str(BASE / "bin" / "pc-game-sandbox")), "deps", str(self.current_profile), *deps], "deps")

    def rebuild_and_deps(self, _b):
        if not self.current_profile: return
        self.save_profile(None)
        helper = shutil.which("pc-game-sandbox") or str(BASE / "bin" / "pc-game-sandbox")
        deps = list(self.profile.get("dependencies", []))
        def worker():
            try:
                for cmd, tag in (([helper, "prefix-create", str(self.current_profile), "--rebuild"], "prefix"),):
                    self.log(f"$ {' '.join(cmd)}")
                    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                    if p.stdout:
                        for line in p.stdout: GLib.idle_add(self.log, f"[{tag}] {line.rstrip()}")
                    code = p.wait()
                    if code != 0:
                        GLib.idle_add(self.log, f"[prefix] exit={code}; dipendenze non installate")
                        return
                if deps:
                    cmd = [helper, "deps", str(self.current_profile), *deps]; self.log(f"$ {' '.join(cmd)}")
                    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                    if p.stdout:
                        for line in p.stdout: GLib.idle_add(self.log, f"[deps] {line.rstrip()}")
                    GLib.idle_add(self.log, f"[deps] exit={p.wait()}")
            except Exception as exc: GLib.idle_add(self.log, f"[rebuild] {exc}")
        threading.Thread(target=worker, daemon=True).start()

    def refresh_dependencies(self):
        selected = set(self.profile.get("dependencies", [])); row = self.dep_list.get_first_child()
        while row:
            cb = row.get_child()
            if isinstance(cb, Gtk.CheckButton): cb.set_active(getattr(cb, "_dep_key", "") in selected)
            row = row.get_next_sibling()

    def add_access(self, readonly: bool, directory: bool):
        cb = lambda d, r: self._finish_access(d, r, readonly, directory)
        (Gtk.FileDialog().select_folder(self, None, cb) if directory else Gtk.FileDialog().open(self, None, cb))

    def _finish_access(self, d, r, readonly, directory):
        try: obj = d.select_folder_finish(r) if directory else d.open_finish(r); path = obj.get_path()
        except GLib.Error: return
        item = {"path": str(Path(path).resolve()), "target": "/install/" + safe_name(Path(path).name), "readonly": readonly}; self.profile.setdefault("allowed_paths", []).append(item); self.save_profile(None); self.refresh_access()

    def refresh_access(self):
        for c in self._children(self.access_list): self.access_list.remove(c)
        for item in self.profile.get("allowed_paths", []):
            row = Gtk.ListBoxRow(); row._item = item; row.set_child(Gtk.Label(label=f"[{ 'RO' if item.get('readonly', True) else 'RW' }] {item.get('path')} → {item.get('target')}", xalign=0, wrap=True)); self.access_list.append(row)

    def remove_access(self, _b):
        row = self.access_list.get_selected_row()
        if row:
            self.profile.get("allowed_paths", []).remove(row._item); self.save_profile(None); self.refresh_access()

    def add_disc(self, _b):
        p = self.disc_entry.get_text().strip()
        if not p: return
        self.profile.setdefault("discs", []).append({"image": str(Path(p).expanduser().resolve())}); self.save_profile(None); self.refresh_discs(); self.disc_entry.set_text("")

    def remove_disc(self, _b):
        ds = self.profile.setdefault("discs", []); 
        if ds: ds.pop(); self.save_profile(None); self.refresh_discs()

    def refresh_discs(self):
        self.disc_status.set_text("Dischi associati: " + str(len(self.profile.get("discs", []))))

    def run_host_test_audio(self):
        helper = shutil.which("pc-game-sandbox") or str(BASE / "bin" / "pc-game-sandbox")
        if not self.current_profile: return self.log("Il test audio richiede un profilo creato e selezionato.")
        self.audio_info.set_text("Test audio avviato; controlla anche il tab Log.")
        self._spawn_logged([helper, "diag", str(self.current_profile), "--", "sh", "-lc", "pactl info && paplay /usr/share/sounds/freedesktop/stereo/complete.oga"], "audio")


def RENDITER_INDEX(value: str) -> int:
    try: return RENDERERS.index(value)
    except ValueError: return 1


class ManagerApp(Gtk.Application):
    def __init__(self): super().__init__(application_id=APP_ID)
    def do_activate(self): ManagerWindow(self).present()


if __name__ == "__main__":
    ManagerApp().run(sys.argv)
