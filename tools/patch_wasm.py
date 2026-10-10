"""Adds a per-frame "mailbox" poller to game.wasm so the page (via shared memory) can run engine functions on the engine thread.

  python patch_wasm.py <orig game.wasm> <out game.wasm> <out bridge.json>

How it works
  * a new function __gta_poll() is appended (new function index, nothing else is renumbered)
  * a new mutable i64 global holds the mailbox address (per instance; only the engine thread ever calls poll)
  * the first poll callocs the mailbox and tells the page its address through the existing import wasm_audio_publish_js
    (game.js forwards that magic call as a {gtaMail} message instead of an audio ring)
  * every poll bumps a heartbeat counter and executes any pending slots: slot = [cmd u32][flags u32][args 8 x 8 bytes @16][result @80]
    cmd is a 1-based index into the command table (bridge.json); the poller clears cmd when done unless flags&1 (sticky: runs every frame)
  * CTheScripts::Process() (called once per frame on the engine thread, where scripts normally run) gets `call __gta_poll` as its first instruction
"""
import json
import sys

from wasm_tools import Wasm, enc_u, enc_s, leb_u
from bridge_spec import COMMANDS

NSLOTS = 32
SLOT_BASE = 128
SLOT_SIZE = 128            # 1 << SLOT_SHIFT
SLOT_SHIFT = 7
ARG_BASE = 16
RESULT_OFF = 80
MBSIZE = 8192
STR_BASE = SLOT_BASE + NSLOTS * SLOT_SIZE
STR_SIZE = MBSIZE - STR_BASE
MAGIC = 0x4D415447         # 'GTAM'
PUBLISH_MAGIC = 0x47544D31 # 'GTM1'
HOOK_FUNC = 'CTheScripts::Process()'

# header byte the page owns: when non-zero the movie player is neutered (see build_guard)
GUARD_OFF = 20
# shared scratch area: natives that write through out-params read/write here (page passes mailbox+off as the pointer)
SCRATCH = {'vec': 32, 'vec2': 44, 'f1': 48, 'f2': 52, 'i1': 56, 'i2': 60}

# The TV (and every other in-game video) plays through rage::bwMovie. Changing channel re-loads a clip that
# this port does not ship -- TVPlaylists.xml is present but no video archive exists anywhere in the data tree --
# so the player walks into a null movie and traps. Gating SetMovie/Play/AsyncBink turns a channel change into a
# no-op: the TV stays dark instead of taking the tab down. Guard byte 0 restores normal playback.
MOVIE_GUARD_FUNCS = [
    ('rage::bwMovie::SetMovieSync(rage::bwMovie::bwMovieParams&)', 'i32'),
    ('rage::bwMovie::Play()', None),
    ('rage::bwMovie::AsyncBink(void*)', None),
]

# opcodes
BLOCK, LOOP, IF, END, BR, BR_IF, BR_TABLE, CALL = 0x02, 0x03, 0x04, 0x0B, 0x0C, 0x0D, 0x0E, 0x10
LOCAL_GET, LOCAL_SET, GLOBAL_GET, GLOBAL_SET = 0x20, 0x21, 0x23, 0x24
LOADS = {'i32': (0x28, 2), 'i64': (0x29, 3), 'f32': (0x2A, 2), 'f64': (0x2B, 3)}
STORES = {'i32': (0x36, 2), 'i64': (0x37, 3), 'f32': (0x38, 2), 'f64': (0x39, 3)}
I32_CONST, I64_CONST = 0x41, 0x42
I32_EQZ, I32_GE_U, I64_EQZ, I32_AND, RETURN = 0x45, 0x4F, 0x50, 0x71, 0x0F
I32_ADD, I32_SUB, I64_ADD, I64_SHL, I64_EXTEND_I32_U = 0x6A, 0x6B, 0x7C, 0x86, 0xAD
VOID = 0x40


def memarg(align, off):
    return enc_u(align) + enc_u(off)


def lget(i): return bytes([LOCAL_GET]) + enc_u(i)
def lset(i): return bytes([LOCAL_SET]) + enc_u(i)
def i32c(v): return bytes([I32_CONST]) + enc_s(v)
def i64c(v): return bytes([I64_CONST]) + enc_s(v)
def call(f): return bytes([CALL]) + enc_u(f)
def br(d): return bytes([BR]) + enc_u(d)
def br_if(d): return bytes([BR_IF]) + enc_u(d)
def load(t, off): op, al = LOADS[t]; return bytes([op]) + memarg(al, off)
def store(t, off): op, al = STORES[t]; return bytes([op]) + memarg(al, off)
def atomic_load32(off): return bytes([0xFE, 0x10]) + memarg(2, off)
def atomic_store32(off): return bytes([0xFE, 0x17]) + memarg(2, off)


def build_poll(cmds, G, F_CALLOC, F_PUBLISH):
    MB, SB, S, CMD = 0, 1, 2, 3
    c = bytearray()
    # first call: allocate + publish the mailbox
    c += bytes([GLOBAL_GET]) + enc_u(G) + lset(MB)
    c += lget(MB) + bytes([I64_EQZ, IF, VOID])
    c += i64c(1) + i64c(MBSIZE) + call(F_CALLOC) + lset(MB)
    c += lget(MB) + bytes([GLOBAL_SET]) + enc_u(G)
    c += lget(MB) + i32c(MAGIC) + store('i32', 0)
    c += lget(MB) + i32c(NSLOTS) + store('i32', 12)
    c += lget(MB) + i32c(len(cmds)) + store('i32', 16)
    c += lget(MB) + i32c(0) + store('i32', GUARD_OFF)
    c += lget(MB) + i32c(PUBLISH_MAGIC) + call(F_PUBLISH)
    c += bytes([END])
    # heartbeat
    c += lget(MB) + lget(MB) + load('i32', 8) + i32c(1) + bytes([I32_ADD]) + store('i32', 8)
    # slot loop
    c += i32c(0) + lset(S)
    c += bytes([BLOCK, VOID, LOOP, VOID])
    c += lget(S) + i32c(NSLOTS) + bytes([I32_GE_U]) + br_if(1)
    c += lget(MB) + i64c(SLOT_BASE) + bytes([I64_ADD]) + lget(S) + bytes([I64_EXTEND_I32_U]) + i64c(SLOT_SHIFT) + bytes([I64_SHL, I64_ADD]) + lset(SB)
    c += lget(SB) + atomic_load32(0) + lset(CMD)
    c += lget(CMD) + bytes([IF, VOID])
    c += dispatch(cmds, CMD, SB)
    # clear cmd unless sticky
    c += lget(SB) + load('i32', 4) + bytes([I32_EQZ, IF, VOID])
    c += lget(SB) + i32c(0) + atomic_store32(0)
    c += bytes([END])
    c += bytes([END])  # if cmd
    c += lget(S) + i32c(1) + bytes([I32_ADD]) + lset(S)
    c += br(0)
    c += bytes([END, END])  # loop, block
    body = enc_u(2) + enc_u(2) + bytes([0x7E]) + enc_u(2) + bytes([0x7F]) + bytes(c) + bytes([END])
    return body


def build_guard(G, nres=None):
    """prologue injected into the movie player: if (mailbox && *(i32*)(mailbox+GUARD_OFF)) return <zero>;
    mailbox==0 means the first poll has not run yet, so the call passes through untouched."""
    c = bytearray()
    c += bytes([GLOBAL_GET]) + enc_u(G) + bytes([I64_EQZ, I32_EQZ])
    c += bytes([GLOBAL_GET]) + enc_u(G) + load('i32', GUARD_OFF) + bytes([I32_EQZ, I32_EQZ])
    c += bytes([I32_AND, IF, VOID])
    if nres == 'i32':
        c += i32c(0)
    elif nres == 'i64':
        c += i64c(0)
    c += bytes([RETURN, END])
    return bytes(c)


def dispatch(cmds, CMD, SB):
    N = len(cmds)
    d = bytearray()
    d += bytes([BLOCK, VOID])      # done
    d += bytes([BLOCK, VOID])      # default
    for _ in range(N):             # case_N ... case_1 (case_1 innermost)
        d += bytes([BLOCK, VOID])
    d += lget(CMD) + i32c(1) + bytes([I32_SUB])
    d += bytes([BR_TABLE]) + enc_u(N) + b''.join(enc_u(i) for i in range(N)) + enc_u(N)
    for k in range(1, N + 1):
        d += bytes([END])
        d += case_code(cmds[k - 1], SB)
        d += br(N - k + 1)
    d += bytes([END, END])         # default, done
    return bytes(d)


def case_code(cmd, SB):
    key, name, fidx, params, results = cmd
    assert len(results) <= 1, name
    o = bytearray()
    if results:
        o += lget(SB)
    for i, p in enumerate(params):
        o += lget(SB) + load(p, ARG_BASE + 8 * i)
    o += call(fidx)
    if results:
        o += store(results[0], RESULT_OFF)
    return bytes(o)


def section_entries(b, sec):
    """(count, raw entries bytes) of a vec-style section."""
    _, ps, size, _ = sec
    n, q = leb_u(b, ps)
    return n, b[q:ps + size]


def main():
    src, out_wasm, out_json = sys.argv[1:4]
    w = Wasm(src)
    b = w.b
    assert w.mem and w.mem['is64'] and w.mem['shared'], 'expected shared memory64'

    # resolve commands
    cmds = []
    missing = []
    seen_keys = {}
    for key, name in COMMANDS:
        if key in seen_keys:
            # a repeated key would collapse in the json dict and silently send callers to the wrong command id
            print('DUPLICATE KEY %r (already bound to %r)' % (key, seen_keys[key]))
            sys.exit(1)
        seen_keys[key] = name
        idx = w.by_name.get(name)
        if idx is None:
            missing.append((key, name))
            continue
        params, results = w.func_type(idx)
        cmds.append((key, name, idx, list(params), list(results)))
    if missing:
        for k, n in missing:
            print('MISSING', k, n)
        sys.exit(1)

    hook = w.by_name[HOOK_FUNC]
    assert w.func_type(hook) == ((), ())
    F_CALLOC = w.by_name['emscripten_builtin_calloc']
    F_PUBLISH = w.import_func_index('wasm_audio_publish_js')
    assert F_PUBLISH is not None
    assert w.func_type(F_PUBLISH) == (('i64', 'i32'), ())
    assert w.func_type(F_CALLOC) == (('i64', 'i64'), ('i64',))
    unit_type = w.types.index(((), ()))

    poll_idx = w.func_imports + len(w.func_types)
    G = w.global_imports + w.global_count_defined
    poll_body = build_poll(cmds, G, F_CALLOC, F_PUBLISH)

    # ---- function section: + one entry
    fsec = w.sec(3)
    n, raw = section_entries(b, fsec)
    new_func_payload = enc_u(n + 1) + raw + enc_u(unit_type)
    # ---- global section: + one mutable i64 = 0
    gsec = w.sec(6)
    n, raw = section_entries(b, gsec)
    new_global_payload = enc_u(n + 1) + raw + bytes([0x7E, 0x01, 0x42, 0x00, 0x0B])
    # ---- code section: patch the hook, gate the movie player, append the poller
    csec = w.sec(10)
    n, q = leb_u(b, csec[1])
    edits = {}
    edits[hook - w.func_imports] = call(poll_idx)
    gated = []
    for nm, nres in MOVIE_GUARD_FUNCS:
        idx = w.by_name.get(nm)
        assert idx is not None, 'missing ' + nm
        params, results = w.func_type(idx)
        assert len(results) <= 1, nm
        edits[idx - w.func_imports] = build_guard(G, results[0] if results else None)
        gated.append(nm)
    parts = [enc_u(n + 1)]
    for di, (st, bs, size) in enumerate(w.bodies):
        if di in edits:
            ng, p = leb_u(b, bs)
            for _ in range(ng):
                _, p = leb_u(b, p)
                p += 1
            locals_end = p
            body = b[bs:locals_end] + edits[di] + b[locals_end:bs + size]
        else:
            body = b[bs:bs + size]
        parts.append(enc_u(len(body)) + body)
    parts.append(enc_u(len(poll_body)) + poll_body)
    new_code_payload = b''.join(parts)

    # ---- reassemble
    repl = {3: new_func_payload, 6: new_global_payload, 10: new_code_payload}
    out = bytearray(b[:8])
    for sid, ps, size, hs in w.secs:
        if sid in repl:
            pl = repl[sid]
            out += bytes([sid]) + enc_u(len(pl)) + pl
        else:
            out += b[hs:ps + size]
    open(out_wasm, 'wb').write(out)

    meta = {
        'version': 1,
        'magic': MAGIC, 'publishMagic': PUBLISH_MAGIC,
        'nslots': NSLOTS, 'slotBase': SLOT_BASE, 'slotSize': SLOT_SIZE,
        'argBase': ARG_BASE, 'resultOff': RESULT_OFF,
        'strBase': STR_BASE, 'strSize': STR_SIZE, 'mbSize': MBSIZE,
        'guard': GUARD_OFF, 'scratch': SCRATCH,
        'movieGuard': gated,
        'hdr': {'magic': 0, 'version': 4, 'heartbeat': 8, 'slots': 12, 'ncmds': 16, 'guard': GUARD_OFF},
        'hook': HOOK_FUNC,
        'commands': {k: {'id': i + 1, 'params': p, 'result': (r[0] if r else None), 'name': nm}
                     for i, (k, nm, f, p, r) in enumerate(cmds)},
    }
    json.dump(meta, open(out_json, 'w'), indent=1)
    print('commands:', len(cmds), 'poll func idx', poll_idx, 'global idx', G, 'hook', hook)
    print('orig', len(b), '->', len(out), 'bytes; poll body', len(poll_body), 'bytes')


if __name__ == '__main__':
    main()
