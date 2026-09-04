# Sicurezza

Default deny.

- rete gioco disabilitata e namespace di rete separato per default;
- quando la rete è abilitata viene condiviso il namespace di rete host: il processo può raggiungere Internet e LAN;
- IPC isolato in Wayland; XWayland condivide IPC host esclusivamente per la compatibilità MIT-SHM verificata nei test;
- Wayland preferito;
- XWayland disponibile come fallback;
- GPU esposta solo tramite DRM/Vulkan necessario;
- PipeWire/PulseAudio via socket, senza accesso al sequencer ALSA;
- input raw solo per device `eventN`/`jsN` selezionati e validati sotto `/dev/input`;
- filesystem host solo tramite bind espliciti; gli allow-list path sono confinati sotto `/install`;
- nuovi profili: directory del gioco in sola lettura per default; può essere resa RW per compatibilità legacy;
- installer/EXE esterni: directory sorgente montata in sola lettura sotto `/payload`;
- runtime e renderer in sola lettura;
- operazioni che richiedono rete (runtime e dipendenze) separate dall'esecuzione quotidiana.

## Limiti noti

- non è ancora applicato un filtro seccomp specifico del progetto; l'isolamento corrente si basa su namespace Bubblewrap, bind minimali e policy filesystem;
- `/usr`, `/etc` e parte di `/sys` sono visibili in sola lettura per compatibilità con Wine/driver; una riduzione ulteriore richiede test su più runtime e GPU;
- CDEmu e UDisks2 sono mostrati come pianificati ma restano disabilitati nella GUI finché il backend mediato non sarà implementato;
- la modalità XWayland è deliberatamente meno isolata sul solo namespace IPC rispetto a Wayland.
