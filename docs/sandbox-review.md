# Sandbox review — 2.12

Review statica del backend `pc-game-sandbox` prima dei test DXVK.

## Garanzie attuali

- user, PID, UTS e cgroup namespace separati;
- rete separata per default;
- IPC separato in Wayland;
- `/run` e `/tmp` privati;
- nessun bind della HOME host;
- nessun D-Bus host;
- `/dev` ricreato da Bubblewrap; GPU esposta esplicitamente tramite `/dev/dri`;
- gamepad raw solo tramite allow-list `/dev/input/eventN` o `/dev/input/jsN`;
- runtime e componenti grafici montati in sola lettura;
- prefix, HOME sandbox e salvataggi sono i principali percorsi persistenti RW.

## Eccezioni di compatibilità

- XWayland non usa un IPC namespace separato perché MIT-SHM deve condividere gli ID SysV con il server X host. Questa eccezione è limitata al backend XWayland.
- La rete `host` non è una NAT sandbox: condivide la rete del sistema, quindi include Internet e LAN.
- I vecchi profili mantengono `game_root_readonly=false` per non introdurre regressioni; i nuovi profili partono RO.

## Correzioni 2.12

1. La directory di un installer/EXE esterno è montata RO invece che RW.
2. Gli input device vengono risolti e validati; non è più possibile attraversare `/dev/input/..`.
3. Gli `allowed_paths` possono essere montati soltanto sotto `/install`, impedendo di shadoware percorsi sensibili del namespace.
4. `USER` e `LOGNAME` dentro la sandbox sono sempre `game`, evitando di propagare il login host.
5. La GUI rende esplicito che `network=host` significa accesso a Internet **e** LAN.
6. CDEmu/UDisks2 sono disabilitati nella GUI finché non esiste il backend mediato reale.
7. I nuovi profili montano `/game` RO per default con opt-in RW per giochi legacy.
8. DXVK/D7VK selezionano un solo set di DLL coerente con la bitness PE dell'eseguibile, evitando collisioni x32/x64.

## Debito di hardening

- filtro seccomp dedicato;
- minimizzazione ulteriore di `/etc` e `/sys` dopo una matrice di test Wine/Proton/GPU;
- rete isolata con NAT/user-mode networking invece della condivisione della rete host;
- backend mediato per CDEmu/UDisks2, senza D-Bus host generale.
