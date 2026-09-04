# Architettura

PC Game Manager è composto da un manager GUI, un launcher sandboxato e un catalogo runtime.

GUI → profilo → launcher → bubblewrap → runtime → gioco

Il gioco non conosce il filesystem reale oltre alle allow-list del profilo. Il runtime selezionato viene montato in sola lettura. Wine e Proton sono trattati tramite adapter distinti; Proton usa UMU quando disponibile.

DOS/Windows 3.1 usa un profilo DOSBox Staging separato.
