"""Minimal wasm module reader: sections, types, imports, function/type mapping, code-body offsets, name section.
Only what the mod-bridge patcher needs. Works on the 63 MB game.wasm in about a second."""
import struct

VT = {0x7F: 'i32', 0x7E: 'i64', 0x7D: 'f32', 0x7C: 'f64', 0x7B: 'v128', 0x70: 'funcref', 0x6F: 'externref'}


def leb_u(b, p):
    r = 0
    s = 0
    while True:
        x = b[p]
        p += 1
        r |= (x & 0x7F) << s
        s += 7
        if not x & 0x80:
            return r, p


def enc_u(v):
    out = bytearray()
    while True:
        x = v & 0x7F
        v >>= 7
        if v:
            out.append(x | 0x80)
        else:
            out.append(x)
            return bytes(out)


def enc_s(v):
    out = bytearray()
    while True:
        x = v & 0x7F
        v >>= 7
        done = (v == 0 and not (x & 0x40)) or (v == -1 and (x & 0x40))
        if done:
            out.append(x)
            return bytes(out)
        out.append(x | 0x80)


class Wasm:
    def __init__(self, path):
        self.path = path
        self.b = open(path, 'rb').read()
        b = self.b
        assert b[:4] == b'\0asm' and b[4:8] == b'\x01\0\0\0', 'bad header'
        self.secs = []  # (id, payload_start, payload_size, header_start)
        p = 8
        while p < len(b):
            hs = p
            sid = b[p]
            p += 1
            size, p = leb_u(b, p)
            self.secs.append((sid, p, size, hs))
            p += size
        self._parse()

    def sec(self, sid):
        for s in self.secs:
            if s[0] == sid:
                return s
        return None

    def _valtypes(self, p):
        n, p = leb_u(self.b, p)
        out = []
        for _ in range(n):
            out.append(VT.get(self.b[p], hex(self.b[p])))
            p += 1
        return out, p

    def _limits(self, p):
        flag = self.b[p]
        p += 1
        mn, p = leb_u(self.b, p)
        mx = None
        if flag & 1:
            mx, p = leb_u(self.b, p)
        return {'flag': flag, 'min': mn, 'max': mx, 'shared': bool(flag & 2), 'is64': bool(flag & 4)}, p

    def _parse(self):
        b = self.b
        # types
        self.types = []
        s = self.sec(1)
        p = s[1]
        n, p = leb_u(b, p)
        self.type_count_pos = (s[1], n)
        for _ in range(n):
            assert b[p] == 0x60
            params, p = self._valtypes(p + 1)
            res, p = self._valtypes(p)
            self.types.append((tuple(params), tuple(res)))
        # imports
        self.imports = []
        self.func_imports = 0
        self.global_imports = 0
        self.mem = None
        s = self.sec(2)
        p = s[1]
        n, p = leb_u(b, p)
        for _ in range(n):
            l, p = leb_u(b, p)
            mod = b[p:p + l].decode()
            p += l
            l, p = leb_u(b, p)
            name = b[p:p + l].decode()
            p += l
            kind = b[p]
            p += 1
            info = None
            if kind == 0:
                info, p = leb_u(b, p)
                self.func_imports += 1
            elif kind == 1:
                p += 1
                info, p = self._limits(p)
            elif kind == 2:
                info, p = self._limits(p)
                self.mem = info
            elif kind == 3:
                p += 2
                self.global_imports += 1
            self.imports.append((mod, name, kind, info))
        # function section
        s = self.sec(3)
        p = s[1]
        n, p = leb_u(b, p)
        self.func_types = []
        for _ in range(n):
            t, p = leb_u(b, p)
            self.func_types.append(t)
        # global section count
        g = self.sec(6)
        self.global_count_defined = leb_u(b, g[1])[0] if g else 0
        # code section: body offsets
        c = self.sec(10)
        p = c[1]
        n, p = leb_u(b, p)
        self.code_count = n
        self.code_count_pos = c[1]
        self.bodies = []  # (size_field_start, body_start, body_size)
        for _ in range(n):
            st = p
            size, p = leb_u(b, p)
            self.bodies.append((st, p, size))
            p += size
        assert n == len(self.func_types), (n, len(self.func_types))
        # names
        self.names = {}
        for sid, ps, sz, hs in self.secs:
            if sid != 0:
                continue
            nl, q = leb_u(b, ps)
            if b[q:q + nl] != b'name':
                continue
            q += nl
            end = ps + sz
            while q < end:
                sub = b[q]
                q += 1
                ssz, q = leb_u(b, q)
                if sub == 1:
                    cnt, r = leb_u(b, q)
                    for _ in range(cnt):
                        idx, r = leb_u(b, r)
                        l, r = leb_u(b, r)
                        self.names[idx] = b[r:r + l].decode('utf8', 'replace')
                        r += l
                q += ssz
        self.by_name = {}
        for i, nm in self.names.items():
            self.by_name.setdefault(nm, i)

    # function index space helpers
    def func_type(self, idx):
        if idx < self.func_imports:
            k = 0
            for mod, name, kind, info in self.imports:
                if kind == 0:
                    if k == idx:
                        return self.types[info]
                    k += 1
        return self.types[self.func_types[idx - self.func_imports]]

    def import_func_index(self, name):
        k = 0
        for mod, nm, kind, info in self.imports:
            if kind == 0:
                if nm == name:
                    return k
                k += 1
        return None
