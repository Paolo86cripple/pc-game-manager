# PC Game Manager

Manager e launcher sandboxato per giochi PC di tutte le epoche. Il progetto gestisce profili, prefix, runtime, grafica, audio, accessi filesystem, dipendenze e permessi sandbox in un'unica applicazione.

## Runtime

Il manager supporta Wine di sistema, runtime Wine aggiuntivi, runtime Proton tramite UMU, Proton-GE, Proton-CachyOS e le famiglie Soda/ProtoSoda del registro runtime. I runtime installati sono montati in sola lettura nei giochi.

## Epoche supportate

- Windows 95 → versioni Windows moderne tramite Wine/runner compatibili.
- DOS e Windows 3.1 sono previsti come ambiente separato con DOSBox Staging.

## Sicurezza

Rete negata per il gioco per impostazione predefinita. PipeWire/PulseAudio viene esposto tramite socket; non viene concesso il sequencer MIDI. I percorsi host sono allow-listati e possono essere RO o RW.

## CLI

`pc-game-sandbox run PROFILE`

`pc-game-sandbox wine PROFILE -- winecfg`

`pc-game-sandbox prefix-create PROFILE`

`pc-game-sandbox deps PROFILE vcrun2022 faudio`

## 2.10 — fallback Wayland/XWayland per profilo

Il backend `Auto` ora usa uno stato persistente per profilo: `unknown`, `working` o `broken`. Se un gioco è marcato `broken`, `Auto` evita WineWayland e avvia direttamente XWayland. La scheda Grafica offre i pulsanti **Segna Wayland non funzionante**, **Riprova Wayland**, **Segna Wayland funzionante** e **Diagnostica input Wayland**. Questo evita fallback basati soltanto sull'exit code, che non possono rilevare giochi avviati correttamente ma privi di tastiera.


### Gestione DXVK / D7VK

La scheda Grafica include un catalogo GitHub per DXVK e D7VK, installazione e rimozione centralizzate e selezione per profilo. I pacchetti gestiti sono salvati sotto `~/.local/share/pc-game-manager/dxvk/` e `~/.local/share/pc-game-manager/d7vk/`. D7VK viene rilevato tramite `ddraw.dll`, come previsto dal deployment moderno del progetto.

## Grafica 2.11

PC Game Manager gestisce ora DXVK, D7VK, VKD3D-Proton, DXVK-NVAPI e dgVoodoo2 da cataloghi upstream. I codec video retro possono essere installati nel prefix tramite Winetricks (allcodecs, Indeo, Cinepak, MP3 DirectShow, ffdshow, Xvid, LAV Filters, Quartz, AMStream, AVIFile32 e Bink). Snapshot/rollback/import-export non fanno parte della roadmap; il blocco corrispondente è sostituito dal gestore dgVoodoo2.

## 2.12 — review sandbox e riordino GUI

- Runtime usa due sottoschede: **Wine / Proton** e **Componenti grafici**.
- Grafica contiene solo le scelte del profilo; cataloghi/installazione sono stati spostati fuori dalla pagina.
- Codec retro sono nella scheda **Dipendenze**.
- Audio è stato assorbito nella scheda **Sandbox**.
- Profilo espone architettura Wine e versione Windows; le impostazioni Wine possono essere applicate a un prefix esistente.
- È possibile disattivare esplicitamente renderer esterni, VKD3D-Proton e DXVK-NVAPI.
- La GUI mostra la policy sandbox effettiva e segnala rete host/XWayland come eccezioni di isolamento.
- Nuovi profili montano la directory del gioco in sola lettura per default.
- DXVK/D7VK scelgono le DLL 32/64 bit in base al PE del gioco.

La review completa è in `docs/sandbox-review.md`.
