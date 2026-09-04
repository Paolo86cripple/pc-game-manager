# Sicurezza

Default deny.

- rete gioco disabilitata;
- IPC isolato;
- Wayland preferito;
- XWayland disponibile come fallback;
- GPU esposta solo tramite DRM/Vulkan necessario;
- PipeWire/PulseAudio via socket, senza accesso al sequencer ALSA;
- input solo per device selezionati;
- filesystem host solo tramite bind espliciti;
- runtime e renderer in sola lettura;
- operazioni che richiedono rete (runtime e dipendenze) separate dall'esecuzione quotidiana.
