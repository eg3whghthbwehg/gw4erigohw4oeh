"""Idempotent edits for the mod bridge. Originals are kept in C:\\GTAbuild\\_logs\\backup.
  game.js   : wasm_audio_publish_js(ring, 0x47544D31) -> postMessage({gtaMail:{memory, addr}})  (the engine thread announces its mailbox)
  index.html: relay {gtaMail} / the input memory from the loader worker to the page as 'gta-mail' / 'gta-mem' events; load modbridge.js before mod.js
"""

MIRROR = r'C:\Users\tuncz\Downloads\GTA5Webport\playgta5-offline\mirror\playgta5.com'
GAMEJS = MIRROR + r'\b\8b0b5899ed\game.js'
INDEX = MIRROR + r'\index.html'

MAGIC_JS = 'if(capacity===1196314929){postMessage({gtaMail:{memory:wasmMemory,addr:Number(ring)}});return}'
FN_SIG = 'function wasm_audio_publish_js(ring,capacity){'


def patch_game_js():
    t = open(GAMEJS, 'r', encoding='utf-8').read()
    if MAGIC_JS in t:
        print('game.js: already patched')
        return
    assert t.count(FN_SIG) == 1, 'wasm_audio_publish_js not found exactly once'
    t = t.replace(FN_SIG, FN_SIG + MAGIC_JS)
    open(GAMEJS, 'w', encoding='utf-8', newline='').write(t)
    print('game.js: patched')


ANCHOR = "if (ev.data.audio) { audioPending = ev.data.audio; tryStartAudio(); }"
LINES = [
    ("'gta-mem'", "if (ev.data.input) { window.dispatchEvent(new CustomEvent('gta-mem', { detail: ev.data.input.memory })); }"),
    ("'gta-mail'", "if (ev.data.gtaMail) { window.dispatchEvent(new CustomEvent('gta-mail', { detail: ev.data.gtaMail })); }"),
]


def patch_index():
    t = open(INDEX, 'r', encoding='utf-8').read()
    changed = False
    assert t.count(ANCHOR) == 1, 'audio relay line not found exactly once'
    for marker, line in LINES:
        if marker not in t:
            t = t.replace(ANCHOR, line + '\n\t\t' + ANCHOR)
            changed = True
    if '/modbridge.js' not in t:
        assert t.count('<script src="/mod.js"></script>') == 1
        t = t.replace('<script src="/mod.js"></script>', '<script src="/modbridge.js"></script>\n<script src="/mod.js"></script>')
        changed = True
    if changed:
        open(INDEX, 'w', encoding='utf-8', newline='').write(t)
        print('index.html: patched')
    else:
        print('index.html: already patched')


if __name__ == '__main__':
    patch_game_js()
    patch_index()
