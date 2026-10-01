"""Lightweight i18n for the Toastovač desktop UI.

The app speaks Czech first. Every user-facing string goes through ``tr(key)``
which resolves the current UI language (config ``ui_language``) against the
table below, falling back to English then the key itself.

Supported languages: en, cs, sk, vi.

To add a language: extend the ``_STRINGS`` table. To add a string: give it a
stable key and translate it everywhere.
"""

from __future__ import annotations

from typing import Optional

_current_language: Optional[str] = None

#: key -> {lang -> text}. English is the reference; Czech is the primary UI
#: language (the product is Czech-first).
_STRINGS: dict[str, dict[str, str]] = {
    # Toaster widget.
    "push_to_talk": {
        "en": "Push-to-Talk",
        "cs": "Stiskni a mluv",
        "sk": "Stlač a hovor",
        "vi": "Nhấn để nói",
    },
    "continuous": {
        "en": "Continuous",
        "cs": "Nepřetržitě",
        "sk": "Nepretržite",
        "vi": "Liên tục",
    },
    "push_to_talk_tooltip": {
        "en": "Voice PE push-to-talk: wake words off; the centre button opens the voice session.",
        "cs": "Voice PE stiskni-a-mluv: wake slova vypnutá; prostřední tlačítko otevře relaci.",
        "sk": "Voice PE stlač-a-hovor: wake slová vypnuté; stredné tlačidlo otvorí reláciu.",
        "vi": "Voice PE nhấn-để-nói: tắt từ đánh thức; nút giữa mở phiên.",
    },
    "continuous_tooltip": {
        "en": "Voice PE continuous: wake words on; the mic reopens after each reply during the conversation window.",
        "cs": "Voice PE nepřetržitě: wake slova zapnutá; mikrofon se po každé odpovědi znovu otevře.",
        "sk": "Voice PE nepretržite: wake slová zapnuté; mikrofón sa po každej odpovedi znovu otvorí.",
        "vi": "Voice PE liên tục: bật từ đánh thức; micro mở lại sau mỗi câu trả lời.",
    },
    # --- Settings: SearXNG search provider ---
    "settings.searxng_enabled.label": {
        "en": "SearXNG (local instance)",
        "cs": "SearXNG (lokální instance)",
        "sk": "SearXNG (lokálna inštancia)",
        "vi": "SearXNG (bản địa)",
    },
    "settings.searxng_enabled.desc": {
        "en": "Use your local SearXNG meta-search as the primary search fallback (instead of Brave). SearXNG runs on your own machine, aggregates many engines, and needs no API key. Defaults to http://127.0.0.1:8080.",
        "cs": "Používat lokální SearXNG jako hlavní záložní vyhledávání (místo Brave). SearXNG běží na vašem počítači, agreguje mnoho enginů a nepotřebuje API klíč. Výchozí http://127.0.0.1:8080.",
        "sk": "Používať lokálny SearXNG ako hlavné záložné vyhľadávanie (namiesto Brave). SearXNG beží na vašom počítači, agreguje mnoho enginov a nepotrebuje API kľúč. Predvolené http://127.0.0.1:8080.",
        "vi": "Dùng SearXNG cục bộ làm tìm kiếm dự phòng chính (thay Brave). SearXNG chạy trên máy bạn, tổng hợp nhiều engine, không cần API key. Mặc định http://127.0.0.1:8080.",
    },
    "settings.searxng_base_url.label": {
        "en": "SearXNG URL",
        "cs": "URL SearXNG",
        "sk": "URL SearXNG",
        "vi": "URL SearXNG",
    },
    "settings.searxng_base_url.desc": {
        "en": "Base URL of your local SearXNG instance. The instance must have its JSON output format enabled (Settings → Search → Formats → JSON). Leave empty for http://127.0.0.1:8080.",
        "cs": "Základní URL lokální instance SearXNG. Instance musí mít povolený JSON výstup (Nastavení → Vyhledávání → Formáty → JSON). Prázdné = http://127.0.0.1:8080.",
        "sk": "Základná URL lokálnej inštancie SearXNG. Inštancia musí mať povolený JSON výstup (Nastavenia → Vyhľadávanie → Formáty → JSON). Prázdne = http://127.0.0.1:8080.",
        "vi": "URL cơ sở của SearXNG cục bộ. Instance phải bật định dạng JSON (Settings → Search → Formats → JSON). Để trống = http://127.0.0.1:8080.",
    },
    # --- Settings: Voice PE LED ring ---
    "settings.voice_pe_led_brightness.label": {
        "en": "LED Ring Brightness",
        "cs": "Jas LED kruhu",
        "sk": "Jas LED kruhu",
        "vi": "Độ sáng vòng LED",
    },
    "settings.voice_pe_led_brightness.desc": {
        "en": "Brightness of the public led_ring light (the voice animations come from the standard assistant events). Applied to paired satellites live.",
        "cs": "Jas veřejného světla led_ring (hlasové animace pocházejí ze standardních událostí asistenta). Aplikováno na spárované satelity okamžitě.",
        "sk": "Jas verejného svetla led_ring (hlasové animácie pochádzajú zo štandardných udalostí asistenta). Aplikuje sa na spárované satelity okamžite.",
        "vi": "Độ sáng của đèn led_ring công khai (hoạt ảnh giọng nói đến từ sự kiện trợ lý chuẩn). Áp dụng trực tiếp cho vệ tinh đã ghép.",
    },
    "settings.voice_pe_led_rgb.label": {
        "en": "LED Ring Colour",
        "cs": "Barva LED kruhu",
        "sk": "Farba LED kruhu",
        "vi": "Màu vòng LED",
    },
    "settings.voice_pe_led_rgb.desc": {
        "en": "Accent colour of the led_ring light: pick a colour or enter hex ('8c00ff') or '0.55,0,1' RGB text. The stock firmware drives the internal pixel effects itself. Applied to paired satellites live.",
        "cs": "Akcentová barva světla led_ring: vyberte barvu nebo zadejte hex ('8c00ff') či '0.55,0,1'. Firmware řídí vnitřní efekty pixelů sám. Aplikováno na satelity okamžitě.",
        "sk": "Akcentová farba svetla led_ring: vyberte farbu alebo zadajte hex ('8c00ff') či '0.55,0,1'. Firmware riadi vnútorné efekty pixelov sám. Aplikuje sa na satelity okamžite.",
        "vi": "Màu nhấn của đèn led_ring: chọn màu hoặc nhập hex ('8c00ff') hay '0.55,0,1'. Firmware tự điều khiển hiệu ứng pixel. Áp dụng trực tiếp cho vệ tinh.",
    },
    # --- Settings: Windows Virtual Microphone page ---
    "vm.clean_mic_title": {
        "en": "🎛️ Echo / Music Cancellation (Clean Audio)",
        "cs": "🎛️ Potlačení ozvěny / hudby (čisté audio)",
        "sk": "🎛️ Potlačenie ozveny / hudby (čisté audio)",
        "vi": "🎛️ Khử tiếng vọng / nhạc (âm thanh sạch)",
    },
    "vm.clean_mic_info": {
        "en": "The native AEC3 lane continuously cleans the microphone signal (a Voice PE satellite or the local USB microphone) against the system audio that is currently playing — this is the enhanced echo / music cancellation. It runs in-process as part of the voice pipeline and needs NO virtual microphone driver.\n\nThe cleaned stream is then available to Toustovač itself and — when the Windows virtual microphone below is installed — to every other application as a normal Windows microphone.",
        "cs": "Nativní AEC3 linka průběžně čistí signál mikrofonu (satelit Voice PE nebo lokální USB mikrofon) proti právě přehrávanému systémovému zvuku — to je vylepšené potlačení ozvěny / hudby. Běží v procesu jako součást hlasové linky a nepotřebuje ŽÁDNÝ ovladač virtuálního mikrofonu.\n\nVyčištěný proud je pak k dispozici samotnému Toustovači a — když je nainstalován virtuální mikrofon níže — i všem ostatním aplikacím jako běžný Windows mikrofon.",
        "sk": "Natívna AEC3 linka priebežne čistí signál mikrofónu (satelit Voice PE alebo lokálny USB mikrofón) proti práve prehrávanému systémovému zvuku — to je vylepšené potlačenie ozveny / hudby. Beží v procese ako súčasť hlasovej linky a nepotrebuje ŽIADNY ovládač virtuálneho mikrofónu.\n\nVyčistený prúd je potom k dispozícii samotnému Toustovaču a — keď je nainštalovaný virtuálny mikrofón nižšie — aj všetkým ostatným aplikáciám ako bežný Windows mikrofón.",
        "vi": "Luồng AEC3 gốc liên tục làm sạch tín hiệu micro (vệ tinh Voice PE hoặc micro USB cục bộ) so với âm thanh hệ thống đang phát — đây là khử tiếng vọng / nhạc nâng cao. Chạy trong tiến trình như một phần của pipeline giọng nói và KHÔNG cần driver micro ảo.\n\nLuồng đã sạch có sẵn cho Toustovač và — khi micro ảo bên dưới được cài — cho mọi ứng dụng khác như micro Windows thường.",
    },
    "vm.virtual_mic_title": {
        "en": "🎙️ Windows Virtual Microphone (Toustovač Clean Microphone)",
        "cs": "🎙️ Windows virtuální mikrofon (Toustovač Clean Microphone)",
        "sk": "🎙️ Windows virtuálny mikrofón (Toustovač Clean Microphone)",
        "vi": "🎙️ Micro ảo Windows (Toustovač Clean Microphone)",
    },
    "vm.virtual_mic_info": {
        "en": "The kernel driver + broker publish the cleaned stream as a Windows capture device named \"Toustovač Clean Microphone\". Only applications that select that device in Windows Sound Settings receive the cleaned audio — browsers, meetings, recorders and so on. Without the driver the stream stays internal to Toustovač.",
        "cs": "Ovladač + broker publikují vyčištěný proud jako Windows záznamové zařízení „Toustovač Clean Microphone“. Vyčištěné audio přijímají jen aplikace, které toto zařízení vyberou v Nastavení zvuku Windows — prohlížeče, schůzky, rekordéry atd. Bez ovladače zůstává proud uvnitř Toustovače.",
        "sk": "Ovládač + broker publikujú vyčistený prúd ako Windows nahrávacie zariadenie „Toustovač Clean Microphone“. Vyčistené audio prijímajú len aplikácie, ktoré toto zariadenie vyberú v Nastaveniach zvuku Windows — prehliadače, stretnutia, rekordéry atď. Bez ovládača zostáva prúd vnútri Toustovača.",
        "vi": "Driver + broker xuất luồng đã sạch thành thiết bị thu Windows tên \"Toustovač Clean Microphone\". Chỉ ứng dụng chọn thiết bị đó trong Sound Settings nhận âm thanh đã sạch — trình duyệt, cuộc họp, máy ghi... Không có driver, luồng chỉ nội bộ Toustovač.",
    },
    # --- Settings: Voice PE WebAudio bridge page ---
    "vpb.title": {
        "en": "🌐 Voice PE WebAudio Bridge (v271-webaudio/1)",
        "cs": "🌐 Voice PE WebAudio bridge (v271-webaudio/1)",
        "sk": "🌐 Voice PE WebAudio bridge (v271-webaudio/1)",
        "vi": "🌐 Voice PE WebAudio bridge (v271-webaudio/1)",
    },
    "vpb.info": {
        "en": "Streams the Voice PE satellite microphone to local applications over ws://127.0.0.1:<port>/voice-pe/v1 (framed PCM16LE, 16 kHz). Loopback only — nothing on your LAN can reach it.\n\nThe protocol is NOT limited to the V271 PWA: it is a generic WebSocket stream. Any app running on this machine — a local website (e.g. http://localhost:5173), a desktop app (Electron, Tauri, Python), a script — can consume it when it sends the bearer token and its Origin is allowed below. The V271 web composer is simply the first client. A third-party website in a browser cannot reach 127.0.0.1 from the public internet; only software on this computer can connect.",
        "cs": "Streamuje mikrofon satelitu Voice PE do lokálních aplikací přes ws://127.0.0.1:<port>/voice-pe/v1 (framed PCM16LE, 16 kHz). Pouze loopback — nic ve vaší LAN se k němu nedostane.\n\nProtokol NENÍ omezen na V271 PWA: je to generický WebSocket stream. Jakákoli aplikace běžící na tomto počítači — lokální web (např. http://localhost:5173), desktopová aplikace (Electron, Tauri, Python), skript — ho může konzumovat, když pošle bearer token a její Origin je povolen níže. V271 webový kompozitor je prostě první klient. Web třetí strany v prohlížeči se k 127.0.0.1 z veřejného internetu nedostane; připojit se může jen software na tomto počítači.",
        "sk": "Streamuje mikrofón satelitu Voice PE do lokálnych aplikácií cez ws://127.0.0.1:<port>/voice-pe/v1 (framed PCM16LE, 16 kHz). Iba loopback — nič vo vašej LAN sa k nemu nedostane.\n\nProtokol NIE je obmedzený na V271 PWA: je to generický WebSocket stream. Akákoľvek aplikácia bežiaca na tomto počítači — lokálny web (napr. http://localhost:5173), desktopová aplikácia (Electron, Tauri, Python), skript — ho môže konzumovať, keď pošle bearer token a jej Origin je povolený nižšie. V271 webový kompozitor je jednoducho prvý klient. Web tretej strany v prehliadači sa k 127.0.0.1 z verejného internetu nedostane; pripojiť sa môže len softvér na tomto počítači.",
        "vi": "Phát micro của vệ tinh Voice PE tới ứng dụng cục bộ qua ws://127.0.0.1:<port>/voice-pe/v1 (PCM16LE framed, 16 kHz). Chỉ loopback — không gì trên LAN chạm tới được.\n\nGiao thức KHÔNG giới hạn ở PWA V271: nó là WebSocket stream chung. Mọi ứng dụng chạy trên máy này — website cục bộ (vd http://localhost:5173), app desktop (Electron, Tauri, Python), script — đều dùng được khi gửi bearer token và Origin nằm trong danh sách cho phép bên dưới. Composer web V271 chỉ là client đầu tiên. Website bên thứ ba trong trình duyệt không thể với tới 127.0.0.1 từ internet công cộng; chỉ phần mềm trên máy này kết nối được.",
    },
    "settings.voice_pe_bridge_enabled.label": {
        "en": "WebAudio Bridge enabled",
        "cs": "WebAudio bridge povolen",
        "sk": "WebAudio bridge povolený",
        "vi": "Bật WebAudio Bridge",
    },
    "settings.voice_pe_bridge_enabled.desc": {
        "en": "Streams the satellite microphone to local apps over ws://127.0.0.1:<port>/voice-pe/v1 (protocol v271-webaudio/1). Loopback only — nothing on the LAN can reach it. The protocol is generic: any local app, local website or desktop client with the token and an allowed Origin can consume the stream; the V271 PWA composer is simply the first client.",
        "cs": "Streamuje mikrofon satelitu do lokálních aplikací přes ws://127.0.0.1:<port>/voice-pe/v1 (protokol v271-webaudio/1). Pouze loopback — nic v LAN se k němu nedostane. Protokol je generický: jakákoli lokální aplikace, web nebo desktop klient s tokenem a povoleným Originem může stream konzumovat; V271 PWA kompozitor je prostě první klient.",
        "sk": "Streamuje mikrofón satelitu do lokálnych aplikácií cez ws://127.0.0.1:<port>/voice-pe/v1 (protokol v271-webaudio/1). Iba loopback — nič v LAN sa k nemu nedostane. Protokol je generický: akákoľvek lokálna aplikácia, web alebo desktop klient s tokenom a povoleným Originom môže stream konzumovať; V271 PWA kompozitor je jednoducho prvý klient.",
        "vi": "Phát micro vệ tinh tới ứng dụng cục bộ qua ws://127.0.0.1:<port>/voice-pe/v1 (giao thức v271-webaudio/1). Chỉ loopback — không gì trên LAN chạm tới. Giao thức chung: mọi app cục bộ, website hay client desktop có token và Origin được phép đều dùng được; composer PWA V271 chỉ là client đầu tiên.",
    },
    "settings.voice_pe_bridge_token.label": {
        "en": "Bridge Token",
        "cs": "Token bridge",
        "sk": "Token bridge",
        "vi": "Token Bridge",
    },
    "settings.voice_pe_bridge_token.desc": {
        "en": "Bearer token clients must send (Authorization: Bearer <token>). Leave empty to use the JARVIS_VOICE_PE_BRIDGE_TOKEN environment variable. Regenerating it kicks existing clients out on reconnect.",
        "cs": "Bearer token, který musí klienti posílat (Authorization: Bearer <token>). Prázdné = použije se proměnná prostředí JARVIS_VOICE_PE_BRIDGE_TOKEN. Regenerace odpojí stávající klienty.",
        "sk": "Bearer token, ktorý musia klienti posielať (Authorization: Bearer <token>). Prázdne = použije sa premenná prostredia JARVIS_VOICE_PE_BRIDGE_TOKEN. Regenerácia odpojí existujúcich klientov.",
        "vi": "Bearer token client phải gửi (Authorization: Bearer <token>). Để trống = dùng biến môi trường JARVIS_VOICE_PE_BRIDGE_TOKEN. Đổi token sẽ đá client cũ ra.",
    },
    "settings.voice_pe_bridge_port.label": {
        "en": "Bridge Port",
        "cs": "Port bridge",
        "sk": "Port bridge",
        "vi": "Cổng Bridge",
    },
    "settings.voice_pe_bridge_port.desc": {
        "en": "Loopback TCP port of the bridge WebSocket (default 27123).",
        "cs": "Loopback TCP port bridge WebSocketu (výchozí 27123).",
        "sk": "Loopback TCP port bridge WebSocketu (predvolený 27123).",
        "vi": "Cổng TCP loopback của WebSocket bridge (mặc định 27123).",
    },
    "settings.voice_pe_bridge_allowed_origins.label": {
        "en": "Allowed Origins",
        "cs": "Povolené Origins",
        "sk": "Povolené Origins",
        "vi": "Origins được phép",
    },
    "settings.voice_pe_bridge_allowed_origins.desc": {
        "en": "Web Origins that may connect. https://v271.cz (the V271 composer) is the default. Add your own local web app, e.g. http://localhost:5173. \"*\" allows any origin — safe only because the server binds loopback, so only software on this machine can ever reach it.",
        "cs": "Webové Origins, které se smí připojit. Výchozí je https://v271.cz (V271 kompozitor). Přidejte vlastní lokální web, např. http://localhost:5173. „*“ povolí jakýkoli origin — bezpečné jen proto, že server běží na loopbacku a dosáhne na něj jen software na tomto počítači.",
        "sk": "Webové Origins, ktoré sa smú pripojiť. Predvolený je https://v271.cz (V271 kompozitor). Pridajte vlastný lokálny web, napr. http://localhost:5173. „*“ povolí akýkoľvek origin — bezpečné len preto, že server beží na loopbacku a dosiahne naň len softvér na tomto počítači.",
        "vi": "Web Origins được phép kết nối. Mặc định https://v271.cz (composer V271). Thêm web cục bộ của bạn, vd http://localhost:5173. \"*\" cho phép mọi origin — an toàn chỉ vì server chạy loopback, chỉ phần mềm trên máy này với tới.",
    },
    "settings.voice_pe_bridge_max_clients.label": {
        "en": "Max Clients",
        "cs": "Max klientů",
        "sk": "Max klientov",
        "vi": "Số client tối đa",
    },
    "settings.voice_pe_bridge_max_clients.desc": {
        "en": "Concurrent WebSocket clients (default 1 — the composer). Raise it to feed several local apps at once.",
        "cs": "Souběžní WebSocket klienti (výchozí 1 — kompozitor). Zvyšte pro více lokálních aplikací najednou.",
        "sk": "Súbežní WebSocket klienti (predvolený 1 — kompozitor). Zvýšte pre viac lokálnych aplikácií naraz.",
        "vi": "Số client WebSocket đồng thời (mặc định 1 — composer). Tăng lên để nhiều app cục bộ cùng dùng.",
    },
    "settings.voice_pe_bridge_buffer_frames.label": {
        "en": "Buffer Frames",
        "cs": "Rámce bufferu",
        "sk": "Rámce bufferu",
        "vi": "Số frame buffer",
    },
    "settings.voice_pe_bridge_buffer_frames.desc": {
        "en": "Bounded outbound frame queue per client (default 200 frames ≈ 6.4 s at 512 samples / 32 ms). Oldest frames drop first.",
        "cs": "Ohraničená fronta odchozích rámců na klienta (výchozí 200 rámců ≈ 6,4 s při 512 vzorcích / 32 ms). Nejstarší rámce se zahazují.",
        "sk": "Ohraničená fronta odchádzajúcich rámcov na klienta (predvolených 200 rámcov ≈ 6,4 s pri 512 vzorkách / 32 ms). Najstaršie rámce sa zahadzujú.",
        "vi": "Hàng đợi frame gửi đi giới hạn mỗi client (mặc định 200 frame ≈ 6,4 s ở 512 mẫu / 32 ms). Frame cũ nhất bị bỏ trước.",
    },
    "settings.voice_pe_bridge_device.label": {
        "en": "Satellite",
        "cs": "Satelit",
        "sk": "Satelit",
        "vi": "Vệ tinh",
    },
    "settings.voice_pe_bridge_device.desc": {
        "en": "Which satellite to stream (MAC, node name or host). Empty = the single attached satellite.",
        "cs": "Který satelit streamovat (MAC, název uzlu nebo host). Prázdné = jediný připojený satelit.",
        "sk": "Ktorý satelit streamovať (MAC, názov uzla alebo host). Prázdne = jediný pripojený satelit.",
        "vi": "Vệ tinh nào được stream (MAC, tên node hoặc host). Để trống = vệ tinh duy nhất đã ghép.",
    },
    # --- MCP catalogue (new entries) ---
    "mcp.browsermcp.name": {
        "en": "🌐 Browser MCP",
        "cs": "🌐 Browser MCP",
        "sk": "🌐 Browser MCP",
        "vi": "🌐 Browser MCP",
    },
    "mcp.browsermcp.desc": {
        "en": "Drive your real Chrome / Edge (including the BrowserOS Neo Chromium profile) — navigate, click, fill forms, read pages, take screenshots. Needs the free Browser MCP extension (browsermcp.io).",
        "cs": "Ovládejte svůj skutečný Chrome / Edge (včetně profilu Chromium z BrowserOS Neo) — navigace, klikání, formuláře, čtení stránek, snímky obrazovky. Vyžaduje bezplatné rozšíření Browser MCP (browsermcp.io).",
        "sk": "Ovládajte svoj skutočný Chrome / Edge (vrátane profilu Chromium z BrowserOS Neo) — navigácia, klikanie, formuláre, čítanie stránok, snímky obrazovky. Vyžaduje bezplatné rozšírenie Browser MCP (browsermcp.io).",
        "vi": "Điều khiển Chrome / Edge thật (gồm profile Chromium của BrowserOS Neo) — điều hướng, bấm, điền form, đọc trang, chụp màn hình. Cần extension Browser MCP miễn phí (browsermcp.io).",
    },
    "mcp.vscode.name": {
        "en": "🧩 VS Code",
        "cs": "🧩 VS Code",
        "sk": "🧩 VS Code",
        "vi": "🧩 VS Code",
    },
    "mcp.vscode.desc": {
        "en": "Control Visual Studio Code — open files, run commands, read diagnostics, search the workspace. Companion of Kilo Code / Zoo Code style IDE agents.",
        "cs": "Ovládejte Visual Studio Code — otevírání souborů, příkazy, diagnostika, hledání v pracovním prostoru. Společník IDE agentů typu Kilo Code / Zoo Code.",
        "sk": "Ovládajte Visual Studio Code — otváranie súborov, príkazy, diagnostika, hľadanie v pracovnom priestore. Spoločník IDE agentov typu Kilo Code / Zoo Code.",
        "vi": "Điều khiển Visual Studio Code — mở file, chạy lệnh, đọc diagnostics, tìm workspace. Bạn đồng hành của agent IDE kiểu Kilo Code / Zoo Code.",
    },
    "mcp.docker.name": {
        "en": "🐳 Docker",
        "cs": "🐳 Docker",
        "sk": "🐳 Docker",
        "vi": "🐳 Docker",
    },
    "mcp.docker.desc": {
        "en": "Manage containers, images, volumes and logs on your local Docker daemon — status, start/stop, compose and cleanup from the chat (docker-mcp, MarkPhelps).",
        "cs": "Spravujte kontejnery, obrazy, svazky a logy na lokálním Docker daemonu — stav, start/stop, compose a úklid z chatu (docker-mcp, MarkPhelps).",
        "sk": "Spravujte kontajnery, obrazy, zväzky a logy na lokálnom Docker daemone — stav, start/stop, compose a upratovanie z chatu (docker-mcp, MarkPhelps).",
        "vi": "Quản lý container, image, volume và log trên Docker daemon cục bộ — trạng thái, start/stop, compose và dọn dẹp từ chat (docker-mcp, MarkPhelps).",
    },
    "mcp.excel.name": {
        "en": "📊 Excel",
        "cs": "📊 Excel",
        "sk": "📊 Excel",
        "vi": "📊 Excel",
    },
    "mcp.excel.desc": {
        "en": "Read, edit and format Excel workbooks on this Windows machine via COM — sheets, cells, formulas and charts without leaving the chat.",
        "cs": "Čtěte, upravujte a formátujte excelové sešity na tomto Windows počítači přes COM — listy, buňky, vzorce a grafy bez opuštění chatu.",
        "sk": "Čítajte, upravujte a formátujte excelové zošity na tomto Windows počítači cez COM — listy, bunky, vzorce a grafy bez opustenia chatu.",
        "vi": "Đọc, sửa và định dạng workbook Excel trên máy Windows này qua COM — sheet, ô, công thức và biểu đồ ngay trong chat.",
    },
    "mcp.outlook.name": {
        "en": "📅 Outlook / Microsoft 365",
        "cs": "📅 Outlook / Microsoft 365",
        "sk": "📅 Outlook / Microsoft 365",
        "vi": "📅 Outlook / Microsoft 365",
    },
    "mcp.outlook.desc": {
        "en": "Mail, calendar and meetings through Microsoft Graph — read and send e-mail, create appointments, join and manage meetings. Requires an Azure app registration.",
        "cs": "Pošta, kalendář a schůzky přes Microsoft Graph — čtení a odesílání e-mailů, tvorba schůzek a jejich správa. Vyžaduje registraci aplikace v Azure.",
        "sk": "Pošta, kalendár a stretnutia cez Microsoft Graph — čítanie a odosielanie e-mailov, tvorba stretnutí a ich správa. Vyžaduje registráciu aplikácie v Azure.",
        "vi": "Mail, lịch và cuộc họp qua Microsoft Graph — đọc/gửi email, tạo lịch hẹn, quản lý cuộc họp. Cần đăng ký ứng dụng Azure.",
    },
    "mcp.email.name": {
        "en": "✉️ Email (IMAP/SMTP)",
        "cs": "✉️ E-mail (IMAP/SMTP)",
        "sk": "✉️ E-mail (IMAP/SMTP)",
        "vi": "✉️ Email (IMAP/SMTP)",
    },
    "mcp.email.desc": {
        "en": "Universal e-mail via IMAP + SMTP — read, search, draft and send from any provider (Gmail, Outlook, Exchange, own domain). No cloud API keys; use app passwords.",
        "cs": "Univerzální e-mail přes IMAP + SMTP — čtení, hledání, návrhy a odesílání z libovolného poskytovatele (Gmail, Outlook, Exchange, vlastní doména). Bez cloudových API klíčů; stačí hesla aplikací.",
        "sk": "Univerzálny e-mail cez IMAP + SMTP — čítanie, hľadanie, návrhy a odosielanie z ľubovoľného poskytovateľa (Gmail, Outlook, Exchange, vlastná doména). Bez cloudových API kľúčov; stačia heslá aplikácií.",
        "vi": "Email phổ quát qua IMAP + SMTP — đọc, tìm, soạn và gửi từ mọi nhà cung cấp (Gmail, Outlook, Exchange, domain riêng). Không cần cloud API key; dùng app password.",
    },
    "mcp.youtube.name": {
        "en": "📺 YouTube",
        "cs": "📺 YouTube",
        "sk": "📺 YouTube",
        "vi": "📺 YouTube",
    },
    "mcp.youtube.desc": {
        "en": "YouTube data and management — search videos and channels, read statistics and comments, upload and manage playlists with a Google API key.",
        "cs": "YouTube data a správa — vyhledávání videí a kanálů, statistiky, komentáře, nahrávání a správa playlistů s Google API klíčem.",
        "sk": "YouTube dáta a správa — vyhľadávanie videí a kanálov, štatistiky, komentáre, nahrávanie a správa playlistov s Google API kľúčom.",
        "vi": "Dữ liệu và quản lý YouTube — tìm video/kênh, đọc thống kê và bình luận, tải lên và quản lý playlist với Google API key.",
    },
}


def set_language(language: str) -> None:
    """Set the UI language (en, cs, sk, vi). Defaults to Czech (cs)."""
    global _current_language
    lang = str(language or "cs").strip().lower()
    _current_language = lang if lang in ("en", "cs", "sk", "vi") else "cs"


def current_language() -> str:
    """The active UI language, resolved from config on first use."""
    global _current_language
    if _current_language is None:
        try:
            from jarvis.config import _load_json, default_config_path
            data = _load_json(default_config_path())
            set_language(data.get("ui_language") or data.get("whisper_language")
                         or "cs")
        except Exception:
            _current_language = "cs"
    return _current_language


def tr(key: str) -> str:
    """Translate a string key into the current UI language."""
    lang = current_language()
    entry = _STRINGS.get(key)
    if entry is None:
        return key
    return entry.get(lang) or entry.get("en") or key


def available_languages() -> list[tuple[str, str]]:
    """(code, native name) pairs for the settings dropdown."""
    return [("en", "English"), ("cs", "Čeština"), ("sk", "Slovenčina"),
            ("vi", "Tiếng Việt")]
