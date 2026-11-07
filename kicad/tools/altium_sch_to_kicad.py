#!/usr/bin/env python3
"""One-off converter: Altium .SchDoc (hierarchical project) -> KiCad .kicad_sch.

KiCad 10 has no CLI schematic importer, so the Altium binary records (OLE
"FileHeader" stream, pipe separated key=value) are read directly.

Usage (from the repository root):
  python3 kicad/tools/altium_sch_to_kicad.py <converted_symbol_lib.kicad_sym> <out_dir>

<converted_symbol_lib.kicad_sym> is airsense_lib.SchLib converted with
`kicad-cli sym upgrade -o ... altium/airsense_lib.SchLib`.
Writes the schematic sheets plus airsense_lib.kicad_sym (SchLib symbols + symbols
that Altium resolved from external libraries) and airsense_power.kicad_sym.
Requires: pip install olefile
"""
import math
import os
import re
import struct
import sys
import uuid

import olefile

U = 0.254  # Altium schematic unit (10 mil) in mm
FORCED_PIN_NETS = {("J1", "38"): "VCC3V3", ("J1", "39"): "GND", ("J1", "40"): "GND", ("U1", "8"): "VCC3V3"}
NS = uuid.UUID("6f0c1d5e-7a52-4f0e-9a52-a1a5e5e5e5e5")
PROJECT = "AIR_SENSE"
ALT = "altium"

SHEETS = [  # (altium file, kicad file, sheet name)
    ("[01] - CAPA.SchDoc", "01_CAPA.kicad_sch", "[01] - CAPA"),
    ("[02] - DIAGRAMA.SchDoc", "02_DIAGRAMA.kicad_sch", "[02] - DIAGRAMA"),
    ("[03] - CIRCUITO.SchDoc", "03_CIRCUITO.kicad_sch", "[03] - CIRCUITO"),
    ("[04] - MECANICO.SchDoc", "04_MECANICO.kicad_sch", "[04] - MECANICO"),
    ("[05] - HISTORICO REVISAO.SchDoc", "05_HISTORICO_REVISAO.kicad_sch", "[05] - HISTORICO REVISAO"),
]
# height is floored to a multiple of the 2.54 mm grid so Altium coordinates stay on the KiCad grid after the Y flip
PAPER = {"0": ("A4", 297.0, 208.28), "1": ("A3", 420.0, 294.64)}
ELEC = {"0": "input", "1": "bidirectional", "2": "output", "3": "open_collector",
        "4": "passive", "5": "tri_state", "6": "open_emitter", "7": "power_in"}
NET_RENAME = {"Vin": "VIN"}  # Altium nets are case-insensitive; the PCB net is VIN
DIRS = {0: (1, 0), 1: (0, 1), 2: (-1, 0), 3: (0, -1)}  # Altium rotation: 0 right, 1 up, 2 left, 3 down


def uid(*parts):
    return str(uuid.uuid5(NS, "/".join(str(p) for p in parts)))


def q(s):
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def f(v):
    s = ("%.4f" % v).rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def records(path):
    d = olefile.OleFileIO(path).openstream("FileHeader").read()
    i, out = 0, []
    while i + 4 <= len(d):
        n = struct.unpack("<I", d[i:i + 4])[0] & 0xFFFFFF
        b = d[i + 4:i + 4 + n]
        i += 4 + n
        r = {}
        for kv in b.rstrip(b"\0").decode("latin-1").split("|"):
            if "=" in kv:
                k, v = kv.split("=", 1)
                r[k] = v
        for k in list(r):  # UTF-8 text is stored in a %UTF8%TEXT twin
            if k.startswith("%UTF8%"):
                r[k[6:]] = r[k].encode("latin-1").decode("utf-8", "replace")
        out.append(r)
    return out


def num(r, key, default=0.0):
    if key not in r:
        return default
    return float(r[key]) + float(r.get(key + "_FRAC", 0)) / 100000.0


def rgb(v):
    v = int(v)
    return (v & 255, (v >> 8) & 255, (v >> 16) & 255)


def sexp_blocks(text):
    """{name: text} of the top-level symbols of a kicad_sym string."""
    out, depth, start, instr, i = {}, 0, None, False, 0
    while i < len(text):
        c = text[i]
        if instr:
            if c == "\\":
                i += 1
            elif c == '"':
                instr = False
        elif c == '"':
            instr = True
        elif c == "(":
            depth += 1
            if depth == 2:
                start = i
        elif c == ")":
            if depth == 2:
                blk = text[start:i + 1]
                m = re.match(r'\(symbol "([^"]+)"', blk)
                if m:
                    out[m.group(1)] = blk
            depth -= 1
        i += 1
    return out


def lib_pins(block):
    pins = {}
    for m in re.finditer(r'\(pin \w+ \w+\s*\(at ([-\d.]+) ([-\d.]+) [-\d.]+\).*?\(number "([^"]*)"', block, re.S):
        pins[m.group(3)] = (float(m.group(1)), float(m.group(2)))
    return pins


def effects(size=1.27, justify=None, hide=False):
    j = " (justify %s)" % justify if justify else ""
    return "(effects (font (size %s %s))%s%s)" % (f(size), f(size), j, " (hide yes)" if hide else "")


def prop(name, value, x, y, ang=0, size=1.27, justify=None, hide=False):
    return "(property %s %s (at %s %s %s) %s)" % (q(name), q(value), f(x), f(y), f(ang), effects(size, justify, hide))


def stroke(width_code):
    w = {"0": 0, "1": 0.1524, "2": 0.3, "3": 0.6}.get(str(width_code), 0.1524)
    return "(stroke (width %s) (type solid))" % f(w)


class Sheet:
    def __init__(self, path):
        self.recs = records(path)
        self.paper, self.w, self.h = PAPER.get(self.recs[1].get("SHEETSTYLE", "0"), PAPER["0"])

    def P(self, r, kx="LOCATION.X", ky="LOCATION.Y"):
        return (num(r, kx) * U, self.h - num(r, ky) * U)

    def font_mm(self, fontid):
        pt = float(self.recs[1].get("SIZE%s" % fontid, 10))
        return max(0.8, min(round(pt * 0.127, 2), 8))


class Component:
    pass


def collect_components(sh):
    comps, owner_map, cur = [], {}, None
    for i, r in enumerate(sh.recs):
        if r.get("RECORD") == "1":
            c = Component()
            c.rec, c.idx, c.children, c.model = r, i - 1, [], None
            c.loc = (num(r, "LOCATION.X"), num(r, "LOCATION.Y"))
            c.name = r["LIBREFERENCE"]
            comps.append(c)
            owner_map[c.idx] = c
    for i, r in enumerate(sh.recs):
        t = r.get("RECORD")
        if t == "1":
            cur = owner_map[i - 1]
        elif t == "45" and cur is not None and r.get("MODELTYPE") == "PCBLIB":
            cur.model = r.get("MODELNAME")
        elif "OWNERINDEX" in r and int(r["OWNERINDEX"]) in owner_map:
            owner_map[int(r["OWNERINDEX"])].children.append(r)
    for c in comps:
        c.params, c.designator = {}, None
        for r in c.children:
            if r.get("RECORD") == "34":
                c.designator = r
            elif r.get("RECORD") == "41" and r.get("NAME") not in (None, "PinUniqueId"):
                c.params[r["NAME"]] = r
    return comps


def rel(c, ax, ay):
    return ((ax - c.loc[0]) * U, (ay - c.loc[1]) * U)  # library space is y-up, like Altium


def pin_hot(c, r):
    """Connection point (library space), KiCad pin angle and length of an Altium pin."""
    rot = int(r.get("PINCONGLOMERATE", 0)) & 3
    ln = num(r, "PINLENGTH")
    dx, dy = DIRS[rot]
    x, y = rel(c, num(r, "LOCATION.X") + dx * ln, num(r, "LOCATION.Y") + dy * ln)
    return x, y, (rot * 90 + 180) % 360, ln * U


def pin_sexp(c, r):
    x, y, ang, ln = pin_hot(c, r)
    flags = int(r.get("PINCONGLOMERATE", 0))
    nm = (r.get("NAME") or "~").replace("\\", "")
    return "(pin %s line (at %s %s %s) (length %s) %s (name %s %s) (number %s %s))" % (
        ELEC.get(r.get("ELECTRICAL", "4"), "passive"), f(x), f(y), ang, f(ln),
        "(hide yes)" if flags & 4 else "", q(nm), effects(1.27, hide=not flags & 8),
        q(r.get("DESIGNATOR", "")), effects(1.27, hide=not flags & 16))


def inline_symbol(sh, c):
    body, pins = [], []
    for r in c.children:
        t = r.get("RECORD")
        fill = "(fill (type background))" if r.get("ISSOLID") == "T" else "(fill (type none))"
        st = stroke(r.get("LINEWIDTH", "1"))
        if t == "14":
            x1, y1 = rel(c, num(r, "LOCATION.X"), num(r, "LOCATION.Y"))
            x2, y2 = rel(c, num(r, "CORNER.X"), num(r, "CORNER.Y"))
            body.append("(rectangle (start %s %s) (end %s %s) %s %s)" % (f(x1), f(y1), f(x2), f(y2), st, fill))
        elif t in ("6", "7", "13"):
            if t == "13":
                pts = [(num(r, "LOCATION.X"), num(r, "LOCATION.Y")), (num(r, "CORNER.X"), num(r, "CORNER.Y"))]
            else:
                pts = [(num(r, "X%d" % k), num(r, "Y%d" % k)) for k in range(1, int(r["LOCATIONCOUNT"]) + 1)]
            if t == "7":
                pts.append(pts[0])
            xy = " ".join("(xy %s %s)" % tuple(map(f, rel(c, *p))) for p in pts)
            body.append("(polyline (pts %s) %s %s)" % (xy, st, fill if t == "7" else "(fill (type none))"))
        elif t == "8":
            x, y = rel(c, num(r, "LOCATION.X"), num(r, "LOCATION.Y"))
            body.append("(circle (center %s %s) (radius %s) %s %s)" % (f(x), f(y), f(num(r, "RADIUS") * U), st, fill))
        elif t == "12":
            cx, cy, rad = num(r, "LOCATION.X"), num(r, "LOCATION.Y"), num(r, "RADIUS")
            a1, a2 = float(r.get("STARTANGLE", 0)), float(r.get("ENDANGLE", 360))
            if a2 <= a1:
                a2 += 360
            pt = lambda a: rel(c, cx + rad * math.cos(math.radians(a)), cy + rad * math.sin(math.radians(a)))
            s, m, e = pt(a1), pt((a1 + a2) / 2), pt(a2)
            body.append("(arc (start %s %s) (mid %s %s) (end %s %s) %s (fill (type none)))" % (
                f(s[0]), f(s[1]), f(m[0]), f(m[1]), f(e[0]), f(e[1]), st))
        elif t == "4":
            x, y = rel(c, num(r, "LOCATION.X"), num(r, "LOCATION.Y"))
            body.append("(text %s (at %s %s %s) %s)" % (q(r["TEXT"]), f(x), f(y), int(r.get("ORIENTATION", 0)) * 90,
                                                         effects(sh.font_mm(r.get("FONTID", 1)), "left bottom")))
        elif t == "2":
            pins.append(pin_sexp(c, r))
    return body, pins


def symbol_block(name, prefix, body, pins, description):
    return ("(symbol %s (exclude_from_sim no) (in_bom yes) (on_board yes) (in_pos_files yes) "
            "(duplicate_pin_numbers_are_jumpers no) %s %s %s %s %s (symbol %s %s) (symbol %s %s))") % (
        q(name), prop("Reference", prefix, 0, 0), prop("Value", name, 0, -2.54, hide=True),
        prop("Footprint", "", 0, 0, hide=True), prop("Datasheet", "", 0, 0, hide=True),
        prop("Description", description, 0, 0, hide=True),
        q(name + "_0_1"), " ".join(body), q(name + "_1_1"), " ".join(pins))


def power_symbol(name):
    pin = '(pin power_in line (at 0 0 90) (length 0) (hide yes) (name %s %s) (number "1" %s))' % (
        q(name), effects(1.27), effects(1.27))
    body = ("(polyline (pts (xy 0 0) (xy 0 1.905)) (stroke (width 0.254) (type default)) (fill (type none))) "
            "(polyline (pts (xy -1.27 1.905) (xy 1.27 1.905)) (stroke (width 0.254) (type default)) (fill (type none)))")
    return ("(symbol %s (power global) (pin_names (offset 0) (hide yes)) (exclude_from_sim no) (in_bom no) (on_board yes) "
            "(in_pos_files no) (duplicate_pin_numbers_are_jumpers no) %s %s %s %s %s (symbol %s %s) (symbol %s %s))") % (
        q(name), prop("Reference", "#PWR", 0, -3.81, hide=True), prop("Value", name, 0, 3.81),
        prop("Footprint", "", 0, 0, hide=True), prop("Datasheet", "", 0, 0, hide=True),
        prop("Description", "Power symbol %s" % name, 0, 0, hide=True),
        q(name + "_0_1"), body, q(name + "_1_1"), pin)


def rename_symbol(block, old, new):
    return block.replace('(symbol "%s"' % old, '(symbol "%s"' % new, 1)


def convert(lib_text, outdir):
    lib_blocks = sexp_blocks(lib_text)
    extras, power_nets = {}, {}
    root_uuid = uid("root")
    sheet_uuids = {k: uid("sheet", k) for _, k, _ in SHEETS}
    summary = []

    for alt, kfile, sname in SHEETS:
        sh = Sheet(os.path.join(ALT, alt))
        items, libsyms = [], {}
        comps = collect_components(sh)
        recs = sh.recs
        path_ = "/%s/%s" % (root_uuid, sheet_uuids[kfile])

        for c in comps:
            r = c.rec
            pin_recs = [x for x in c.children if x.get("RECORD") == "2"]
            blk = lib_blocks.get(c.name) if r.get("SOURCELIBRARYNAME") == "airsense_lib.SchLib" else None
            if blk:
                lp = lib_pins(blk)
                ok = len(lp) == len(pin_recs) and all(
                    p.get("DESIGNATOR") in lp
                    and abs(lp[p["DESIGNATOR"]][0] - pin_hot(c, p)[0]) < 0.01
                    and abs(lp[p["DESIGNATOR"]][1] - pin_hot(c, p)[1]) < 0.01 for p in pin_recs)
                if not ok:
                    print("WARN: library symbol pins differ, using sheet graphics:", c.name, file=sys.stderr)
                    blk = None
            if blk:
                key = c.name
                libsyms["airsense_lib:" + key] = rename_symbol(blk, c.name, "airsense_lib:" + key)
            else:
                body, pins = inline_symbol(sh, c)
                prefix = re.sub(r"\d+$", "", (c.designator or {}).get("TEXT", "U"))
                desc = r.get("COMPONENTDESCRIPTION", "")
                key, n = c.name, 1
                while True:
                    cand = symbol_block(key, prefix, body, pins, desc)
                    if key not in extras or extras[key] == cand:
                        break
                    n += 1
                    key = "%s_%d" % (c.name, n)
                extras[key] = cand
                libsyms["airsense_lib:" + key] = rename_symbol(cand, key, "airsense_lib:" + key)
            lib_id = "airsense_lib:" + key

            ref = (c.designator or {}).get("TEXT", "U?")
            x, y = sh.P(r)
            val_rec, com_rec = c.params.get("Value"), c.params.get("Comment")
            vis = next((p for p in (val_rec, com_rec) if p is not None and p.get("ISHIDDEN") != "T"), None)
            val = None
            if vis is not None:
                t = vis.get("TEXT", "")
                val = val_rec.get("TEXT") if (t.startswith("=") and val_rec) else t
            elif val_rec and val_rec.get("TEXT") not in (None, "", "*"):
                val = val_rec["TEXT"]
            val = val or c.name
            fp = ("airsense_lib:" + c.model) if c.model else ""
            in_bom = "no" if ref.startswith(("FID", "PCB")) else "yes"
            props = []
            if c.designator:
                dx, dy = sh.P(c.designator)
                props.append(prop("Reference", ref, dx, dy, int(c.designator.get("ORIENTATION", 0)) * 90,
                                  sh.font_mm(c.designator.get("FONTID", 1)), "left bottom"))
            else:
                props.append(prop("Reference", ref, x, y, hide=True))
            if vis is not None:
                vx, vy = sh.P(vis)
                props.append(prop("Value", val, vx, vy, int(vis.get("ORIENTATION", 0)) * 90,
                                  sh.font_mm(vis.get("FONTID", 1)), "left bottom"))
            else:
                props.append(prop("Value", val, x, y, hide=True))
            props.append(prop("Footprint", fp, x, y, hide=True))
            props.append(prop("Datasheet", (c.params.get("Datasheet") or {}).get("TEXT", ""), x, y, hide=True))
            props.append(prop("Description", r.get("COMPONENTDESCRIPTION", ""), x, y, hide=True))
            for pname, pr in c.params.items():
                if pname in ("Value", "Comment", "Datasheet") or not pr.get("TEXT") or pr["TEXT"].startswith("="):
                    continue
                props.append(prop(pname, pr["TEXT"], x, y, hide=True))
            pins_s = " ".join("(pin %s (uuid %s))" % (q(p["DESIGNATOR"]), q(uid(kfile, "pin", c.idx, p["DESIGNATOR"])))
                              for p in pin_recs)
            items.append(
                "(symbol (lib_id %s) (at %s %s 0) (unit 1) (body_style 1) (exclude_from_sim no) (in_bom %s) "
                "(on_board %s) (dnp no) (uuid %s) %s %s (instances (project %s (path %s (reference %s) (unit 1)))))" % (
                    q(lib_id), f(x), f(y), in_bom, "yes" if c.model else "no", q(uid(kfile, "sym", ref)),
                    " ".join(props), pins_s, q(PROJECT), q(path_), q(ref)))
            c.ref = ref

        segs, juncs, pin_pts = [], set(), set()
        for c in comps:
            for p in (x for x in c.children if x.get("RECORD") == "2"):
                hx, hy, _, _ = pin_hot(c, p)
                pin_pts.add((round(c.loc[0] * U + hx, 3), round(sh.h - (c.loc[1] * U + hy), 3)))
        for i, r in enumerate(recs):
            t = r.get("RECORD")
            if t == "27":
                pts = [sh.P(r, "X%d" % k, "Y%d" % k) for k in range(1, int(r["LOCATIONCOUNT"]) + 1)]
                for a, b in zip(pts, pts[1:]):
                    if a != b:
                        segs.append((round(a[0], 3), round(a[1], 3), round(b[0], 3), round(b[1], 3)))
            elif t == "29":
                x, y = sh.P(r)
                juncs.add((round(x, 3), round(y, 3)))

        def interior(px, py, s):
            x1, y1, x2, y2 = s
            if (px, py) in ((x1, y1), (x2, y2)):
                return False
            if abs((x2 - x1) * (py - y1) - (y2 - y1) * (px - x1)) > 0.01:
                return False
            return min(x1, x2) - .005 <= px <= max(x1, x2) + .005 and min(y1, y2) - .005 <= py <= max(y1, y2) + .005

        ends = {}
        for s in segs:
            for p in ((s[0], s[1]), (s[2], s[3])):
                ends[p] = ends.get(p, 0) + 1
        for p, n in ends.items():
            if n + 2 * sum(interior(p[0], p[1], s) for s in segs) + (p in pin_pts) >= 3:
                juncs.add(p)
        for s in dict.fromkeys(segs):
            items.append("(wire (pts (xy %s %s) (xy %s %s)) (stroke (width 0) (type default)) (uuid %s))" % (
                f(s[0]), f(s[1]), f(s[2]), f(s[3]), q(uid(kfile, "wire", s))))
        for j in sorted(juncs):
            items.append("(junction (at %s %s) (diameter 0) (color 0 0 0 0) (uuid %s))" % (
                f(j[0]), f(j[1]), q(uid(kfile, "j", j))))

        forced = []
        sheet_power = {NET_RENAME.get(r["TEXT"], r["TEXT"]) for r in recs if r.get("RECORD") == "17"}
        label_pts = {(round(sh.P(r)[0], 3), round(sh.P(r)[1], 3)) for r in recs if r.get("RECORD") in ("17", "25")}
        for c in comps:  # unused pins get a no-connect flag (net-neutral, keeps ERC meaningful)
            for p in (x for x in c.children if x.get("RECORD") == "2"):
                hx, hy, _, _ = pin_hot(c, p)
                pt = (round(c.loc[0] * U + hx, 3), round(sh.h - (c.loc[1] * U + hy), 3))
                fnet = FORCED_PIN_NETS.get((c.designator["TEXT"], p["DESIGNATOR"]))
                if fnet:  # hidden power pins that Altium ties to a net implicitly (confirmed by the PCB netlist)
                    forced.append((pt, fnet))
                    continue
                if pt in ends or pt in label_pts or any(interior(pt[0], pt[1], sg) for sg in segs):
                    continue
                if any(x.get("RECORD") == "22" and (round(sh.P(x)[0], 3), round(sh.P(x)[1], 3)) == pt for x in recs):
                    continue
                items.append("(no_connect (at %s %s) (uuid %s))" % (f(pt[0]), f(pt[1]), q(uid(kfile, "ncpin", c.idx, p["DESIGNATOR"]))))
        nlabels = nports = 0
        for k, (pt, fnet) in enumerate(forced):
            nports += 1
            lib_id = "airsense_power:" + fnet
            if lib_id not in libsyms:
                libsyms[lib_id] = rename_symbol(power_symbol(fnet), fnet, lib_id)
            power_nets[fnet] = 1
            ref = "#PWR%03d" % (900 + k)
            items.append(
                "(symbol (lib_id %s) (at %s %s 0) (unit 1) (body_style 1) (exclude_from_sim no) (in_bom no) "
                "(on_board yes) (dnp no) (uuid %s) %s %s (pin \"1\" (uuid %s)) "
                "(instances (project %s (path %s (reference %s) (unit 1)))))" % (
                    q(lib_id), f(pt[0]), f(pt[1]), q(uid(kfile, "fpwr", k)),
                    prop("Reference", ref, pt[0], pt[1], hide=True),
                    prop("Value", fnet, pt[0] + 2.54, pt[1], 0, 1.27, "left"),
                    q(uid(kfile, "fpwrpin", k)), q(PROJECT), q(path_), q(ref)))
        for i, r in enumerate(recs):
            t = r.get("RECORD")
            if t == "25" and NET_RENAME.get(r["TEXT"], r["TEXT"]) not in sheet_power:
                x, y = sh.P(r)
                items.append("(label %s (at %s %s %d) %s (uuid %s))" % (
                    q(NET_RENAME.get(r["TEXT"], r["TEXT"])), f(x), f(y), int(r.get("ORIENTATION", 0)) * 90,
                    effects(1.27, "left bottom"), q(uid(kfile, "label", i))))
                nlabels += 1
            elif t == "17" or t == "25":
                x, y = sh.P(r)
                o = int(r.get("ORIENTATION", 0)) if t == "17" else (3 if r["TEXT"] == "GND" else 1)
                name = NET_RENAME.get(r["TEXT"], r["TEXT"])
                power_nets[name] = 1
                dx, dy = DIRS[o]
                nports += 1
                lib_id = "airsense_power:" + name
                if lib_id not in libsyms:
                    libsyms[lib_id] = rename_symbol(power_symbol(name), name, lib_id)
                ref = "#PWR%03d" % nports
                items.append(
                    "(symbol (lib_id %s) (at %s %s %d) (unit 1) (body_style 1) (exclude_from_sim no) (in_bom no) "
                    "(on_board yes) (dnp no) (uuid %s) %s %s (pin \"1\" (uuid %s)) "
                    "(instances (project %s (path %s (reference %s) (unit 1)))))" % (
                        q(lib_id), f(x), f(y), (o - 1) * 90 % 360, q(uid(kfile, "pwr", i)),
                        prop("Reference", ref, x, y, hide=True),
                        prop("Value", name, x + dx * 2.54, y - dy * 2.54, 0, 1.27,
                             {0: "left", 1: "bottom", 2: "right", 3: "top"}[o]),
                        q(uid(kfile, "pwrpin", i)), q(PROJECT), q(path_), q(ref)))
            elif t == "22":
                x, y = sh.P(r)
                items.append("(no_connect (at %s %s) (uuid %s))" % (f(x), f(y), q(uid(kfile, "nc", i))))

        for i, r in enumerate(recs):  # sheet-level drawing graphics (components and template are skipped)
            t = r.get("RECORD")
            if "OWNERINDEX" in r:
                continue
            if t == "14":
                x1, y1 = sh.P(r)
                x2, y2 = sh.P(r, "CORNER.X", "CORNER.Y")
                fill = ("(fill (type color) (color %d %d %d 1))" % rgb(r.get("AREACOLOR", 16777215))
                        if r.get("ISSOLID") == "T" else "(fill (type none))")
                items.append("(rectangle (start %s %s) (end %s %s) %s %s (uuid %s))" % (
                    f(x1), f(y1), f(x2), f(y2), stroke(r.get("LINEWIDTH", "1")), fill, q(uid(kfile, "rect", i))))
            elif t == "6":
                pts = [sh.P(r, "X%d" % k, "Y%d" % k) for k in range(1, int(r["LOCATIONCOUNT"]) + 1)]
                items.append("(polyline (pts %s) %s (fill (type none)) (uuid %s))" % (
                    " ".join("(xy %s %s)" % (f(p[0]), f(p[1])) for p in pts), stroke(r.get("LINEWIDTH", "1")),
                    q(uid(kfile, "poly", i))))
            elif t == "4":
                x, y = sh.P(r)
                items.append("(text %s (exclude_from_sim no) (at %s %s %d) %s (uuid %s))" % (
                    q(r["TEXT"].replace("~1", "\n")), f(x), f(y), int(r.get("ORIENTATION", 0)) * 90,
                    effects(sh.font_mm(r.get("FONTID", 1)), "left bottom"), q(uid(kfile, "text", i))))
            elif t == "28":
                x1, y1 = sh.P(r)
                x2, y2 = sh.P(r, "CORNER.X", "CORNER.Y")
                items.append(
                    "(text_box %s (exclude_from_sim no) (at %s %s 0) (size %s %s) (margins 0.9525 0.9525 0.9525 0.9525) "
                    "(stroke (width 0) (type default)) (fill (type none)) %s (uuid %s))" % (
                        q(r["TEXT"].replace("~1", "\n")), f(min(x1, x2)), f(min(y1, y2)), f(abs(x2 - x1)), f(abs(y2 - y1)),
                        effects(sh.font_mm(r.get("FONTID", 1)), "left top"), q(uid(kfile, "tbox", i))))

        sch = ['(kicad_sch (version 20250114) (generator "eeschema") (generator_version "9.0") (uuid %s)' % q(sheet_uuids[kfile]),
               "(paper %s)" % q(sh.paper),
               '(title_block (title %s) (rev "V1I1") (company "Embedded Things") (comment 1 %s))' % (
                   q(sname), q("Converted from Altium: " + alt)),
               "(lib_symbols %s)" % " ".join(libsyms.values())]
        sch += items
        sch += ['(sheet_instances (path "/" (page "1")))', "(embedded_fonts no))"]
        write(os.path.join(outdir, kfile), "\n".join(sch))
        summary.append((alt, len(comps), len(set(segs)), nlabels, nports, len(juncs)))

    sh = Sheet(os.path.join(ALT, "AIR_SENSE.SchDoc"))
    by_file = {a: (k, n) for a, k, n in SHEETS}
    items, page = [], 2
    for i, r in enumerate(sh.recs):
        if r.get("RECORD") == "15":
            fname = next(x["TEXT"] for x in sh.recs if x.get("RECORD") == "33" and int(x["OWNERINDEX"]) == i - 1)
            kfile, sname = by_file[fname]
            x, y = sh.P(r)
            w, h = num(r, "XSIZE") * U, num(r, "YSIZE") * U
            items.append(
                "(sheet (at %s %s) (size %s %s) (exclude_from_sim no) (in_bom yes) (on_board yes) (dnp no) "
                "(fields_autoplaced yes) (stroke (width 0.1524) (type solid)) (fill (color 0 0 0 0.0)) (uuid %s) %s %s "
                "(instances (project %s (path %s (page %s)))))" % (
                    f(x), f(y), f(w), f(h), q(sheet_uuids[kfile]),
                    prop("Sheetname", sname, x, y - 1, 0, 1.27, "left bottom"),
                    prop("Sheetfile", kfile, x, y + h + 1, 0, 1.27, "left top"),
                    q(PROJECT), q("/" + root_uuid), q(str(page))))
            page += 1
    root = ['(kicad_sch (version 20250114) (generator "eeschema") (generator_version "9.0") (uuid %s)' % q(root_uuid),
            '(paper "A3")',
            '(title_block (title "AIR SENSE - Hardware") (rev "V1I1") (company "Embedded Things") '
            '(comment 1 "Converted from Altium: AIR_SENSE.SchDoc"))',
            "(lib_symbols)"] + items + ['(sheet_instances (path "/" (page "1")))', "(embedded_fonts no))"]
    write(os.path.join(outdir, "AIR_SENSE.kicad_sch"), "\n".join(root))

    base = lib_text.rstrip()
    assert base.endswith(")")
    write(os.path.join(outdir, "airsense_lib.kicad_sym"), base[:-1] + "\n".join("\t" + b for b in extras.values()) + "\n)\n")
    write(os.path.join(outdir, "airsense_power.kicad_sym"),
          '(kicad_symbol_lib (version 20251024) (generator "kicad_symbol_editor") (generator_version "10.0")\n'
          + "\n".join(power_symbol(n) for n in sorted(power_nets)) + "\n)\n")
    for row in summary:
        print("%-34s comps=%-3d wires=%-3d labels=%-3d power=%-3d junctions=%d" % row)
    print("extra symbols appended to airsense_lib:", sorted(extras))
    print("power nets:", sorted(power_nets))


def write(path, text):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text if text.endswith("\n") else text + "\n")


if __name__ == "__main__":
    convert(open(sys.argv[1], encoding="utf-8").read(), sys.argv[2])
