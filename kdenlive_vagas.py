#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
kdenlive_vagas.py - vágási JSON alkalmazása Kdenlive projektfájlon.

Bemenet : vágatlan .kdenlive + a szabálykönyv-prompt által adott vágási JSON
          ("cuts" = biztos vágások, "questions" = kérdéses részek)
Kimenet : vágott .kdenlive (a kivágott részek "ripple" módon kiesnek, nincs lyuk)

Használat:
    python kdenlive_vagas.py vagatlan.kdenlive vagas.json -o vagott.kdenlive

Kérdéses részeknél (questions) párbeszédablak jelenik meg, ahol visszajátszható
a rész, majd jóváhagyható ("Maradjon") vagy elvethető ("Kivágom").
Az ablakhoz:  pip install PySide6   (nélküle konzolos kérdezés + külső lejátszó).

Hasznos kapcsolók:
    --video FILE         a kérdéseknél lejátszandó videó (alapból a projektből)
    --answers MODE       ask (alap) | suggest | keep | cut
    --decisions FILE     korábbi döntések (id -> keep/cut) újrafelhasználása
    --context SEC        ennyi mp előzmény/utózmány a lejátszóban (alap: 5)
    --margin SEC         ennyi mp-et meghagy a vágások két szélén (alap: 0)
    --no-audio           hang nélküli lejátszás (ha nincs hangeszköz)
    --no-junction-guides ne tegyen jelölőt (guide) a vágási pontokra
    --dry-run            csak a terv, nem ír fájlt
"""
import argparse
import copy
import html
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET

log = logging.getLogger("vagas")


# --------------------------------------------------------------------------
# Idő-segédek
# --------------------------------------------------------------------------
def tc_to_frames(tc, fps):
    """'HH:MM:SS.mmm' (vagy sima frame-szám) -> frame."""
    tc = str(tc).strip()
    if re.fullmatch(r"-?\d+", tc):
        return int(tc)
    h, m, s = tc.split(":")
    return int(round((int(h) * 3600 + int(m) * 60 + float(s)) * fps))


def frames_to_tc(frames, fps):
    """frame -> 'HH:MM:SS.mmm' (ugyanúgy kerekítve, ahogy a Kdenlive írja)."""
    ms = int(round(frames * 1000.0 / fps))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return "%02d:%02d:%02d.%03d" % (h, m, s, ms)


def srt_to_seconds(ts):
    m = re.match(r"^\s*(\d+):(\d+):(\d+)[,.](\d+)\s*$", ts)
    if not m:
        raise ValueError("Érvénytelen időbélyeg: %r" % ts)
    h, mi, s, frac = m.groups()
    return int(h) * 3600 + int(mi) * 60 + int(s) + int(frac.ljust(3, "0")[:3]) / 1000.0


def fmt_short(seconds):
    seconds = max(0, int(round(seconds)))
    return "%02d:%02d:%02d" % (seconds // 3600, seconds % 3600 // 60, seconds % 60)


# --------------------------------------------------------------------------
# XML-segédek
# --------------------------------------------------------------------------
def prop(el, name):
    for p in el.findall("property"):
        if p.get("name") == name:
            return p
    return None


def prop_text(el, name):
    p = prop(el, name)
    return None if p is None else (p.text or "")


def set_prop(el, name, value):
    p = prop(el, name)
    if p is None:
        p = ET.SubElement(el, "property", {"name": name})
    p.text = str(value)


class Project:
    """A Kdenlive projekt (MLT XML) betöltése és a szekvencia megkeresése."""

    def __init__(self, path):
        self.path = path
        self.tree = ET.parse(path)
        self.root = self.tree.getroot()
        prof = self.root.find("profile")
        self.fps = float(prof.get("frame_rate_num")) / float(prof.get("frame_rate_den"))
        self.by_id = {e.get("id"): e for e in self.root if e.get("id")}
        self._find_sequence()

    def _find_sequence(self):
        proj = None
        for t in self.root.findall("tractor"):
            if prop_text(t, "kdenlive:projectTractor") == "1":
                proj = t
                break
        if proj is None:
            raise SystemExit("Hiba: nem találom a projekt-tractort (kdenlive:projectTractor).")
        self.proj_tractor = proj
        self.seq = self.by_id[proj.find("track").get("producer")]
        self.sub_tractors = []
        self.playlists = []
        for tr in self.seq.findall("track"):
            prod = self.by_id.get(tr.get("producer"))
            if prod is None:
                continue
            if prod.tag == "tractor":
                self.sub_tractors.append(prod)
                for t2 in prod.findall("track"):
                    pl = self.by_id.get(t2.get("producer"))
                    if pl is not None and pl.tag == "playlist":
                        self.playlists.append(pl)
            elif prod.tag == "playlist":
                self.playlists.append(prod)
        self.total = tc_to_frames(self.seq.get("out"), self.fps) + 1

    def video_for_review(self):
        """A lejátszáshoz használt média: az első hangsáv első klipje."""
        root_dir = self.root.get("root") or os.path.dirname(os.path.abspath(self.path))
        candidates = []
        for tr in self.sub_tractors:
            is_audio = prop_text(tr, "kdenlive:audio_track") == "1"
            for t2 in tr.findall("track"):
                pl = self.by_id.get(t2.get("producer"))
                if pl is None:
                    continue
                for en in pl.findall("entry"):
                    ch = self.by_id.get(en.get("producer"))
                    if ch is not None and prop_text(ch, "resource"):
                        candidates.append((0 if is_audio else 1, prop_text(ch, "resource")))
                        break
        candidates.sort(key=lambda c: c[0])
        for _, res in candidates:
            for base in (None, root_dir, os.path.dirname(os.path.abspath(self.path))):
                p = res if base is None else os.path.join(base, res)
                if os.path.isabs(p) and os.path.exists(p):
                    return p
                if base is not None and os.path.exists(p):
                    return os.path.abspath(p)
        return None


# --------------------------------------------------------------------------
# Vágási tartományok
# --------------------------------------------------------------------------
def make_ranges(items, fps, total, margin_s=0.0, merge_gap_s=0.0):
    """[{start,end,label}] (mp) -> összevont [(s,e,label)] frame-ben, e kizárólagos."""
    out = []
    mg = int(round(margin_s * fps))
    for it in items:
        s = int(round(srt_to_seconds(it["start"]) * fps)) + mg
        e = int(round(srt_to_seconds(it["end"]) * fps)) - mg
        s, e = max(0, s), min(total, e)
        if e > s:
            out.append([s, e, it.get("label", "")])
    out.sort()
    merged = []
    gap = int(round(merge_gap_s * fps))
    for s, e, lab in out:
        if merged and s <= merged[-1][1] + gap:
            if e > merged[-1][1]:
                merged[-1][1] = e
            if lab and lab not in merged[-1][2]:
                merged[-1][2] += " + " + lab
        else:
            merged.append([s, e, lab])
    return [tuple(m) for m in merged]


def complement(cuts, total):
    keeps, pos = [], 0
    for s, e, _ in cuts:
        if s > pos:
            keeps.append((pos, s))
        pos = max(pos, e)
    if pos < total:
        keeps.append((pos, total))
    return keeps


def removed_before(f, cuts):
    n = 0
    for s, e, _ in cuts:
        if f >= e:
            n += e - s
        elif f > s:
            n += f - s
    return n


# --------------------------------------------------------------------------
# Sáv-szerkesztés (ripple)
# --------------------------------------------------------------------------
class IdGen:
    def __init__(self, root):
        nums = [int(m.group(1)) for e in root.iter("filter")
                for m in [re.match(r"^filter(\d+)$", e.get("id") or "")] if m]
        self.n = max(nums) + 1 if nums else 0

    def next(self):
        v = "filter%d" % self.n
        self.n += 1
        return v


def parse_playlist(pl, fps):
    items, pos = [], 0
    for ch in list(pl):
        if ch.tag == "entry":
            i = tc_to_frames(ch.get("in"), fps)
            o = tc_to_frames(ch.get("out"), fps)
            items.append({"pos": pos, "len": o - i + 1, "in": i, "el": ch})
            pos += o - i + 1
        elif ch.tag == "blank":
            pos += tc_to_frames(ch.get("length"), fps)
    return items


def ripple_playlist(pl, keeps, new_starts, fps, ids):
    items = parse_playlist(pl, fps)
    if not items:
        return 0
    new = []
    for k, (ks, ke) in enumerate(keeps):
        for it in items:
            s, e = it["pos"], it["pos"] + it["len"]
            a, b = max(s, ks), min(e, ke)
            if b <= a:
                continue
            el = copy.deepcopy(it["el"])
            old_in_tc = el.get("in")
            n_in = it["in"] + (a - s)
            n_out = n_in + (b - a) - 1
            el.set("in", frames_to_tc(n_in, fps))
            el.set("out", frames_to_tc(n_out, fps))
            for p in list(el.findall("property")):
                if p.get("name") == "kdenlive:activeeffect":
                    el.remove(p)
            for f in el.iter("filter"):
                f.set("id", ids.next())
                for p in f.findall("property"):
                    # egyetlen kulcskockás érték, ami a klip kezdőpontjához kötött
                    if p.text and p.text.startswith(old_in_tc + "="):
                        p.text = el.get("in") + p.text[len(old_in_tc):]
            new.append((new_starts[k] + (a - ks), el))
    new.sort(key=lambda x: x[0])
    for ch in list(pl):
        if ch.tag in ("entry", "blank"):
            pl.remove(ch)
    cursor = 0
    for pos, el in new:
        if pos > cursor:
            pl.append(ET.Element("blank", {"length": frames_to_tc(pos - cursor, fps)}))
        pl.append(el)
        cursor = pos + tc_to_frames(el.get("out"), fps) - tc_to_frames(el.get("in"), fps) + 1
    return len(new)


# --------------------------------------------------------------------------
# Csoportok (groups) és guide-ok
# --------------------------------------------------------------------------
def _leaf_positions(node, acc):
    if node.get("leaf") == "clip" or "data" in node:
        acc.append(node["data"].split(":")[1])
    for c in node.get("children", []):
        _leaf_positions(c, acc)


def _set_leaf_positions(node, pos):
    if "data" in node:
        parts = node["data"].split(":")
        parts[1] = str(pos)
        node["data"] = ":".join(parts)
    for c in node.get("children", []):
        _set_leaf_positions(c, pos)


def rebuild_groups(seq, new_starts):
    p = prop(seq, "kdenlive:sequenceproperties.groups")
    if p is None or not (p.text or "").strip():
        return
    try:
        groups = json.loads(p.text)
    except ValueError:
        log.warning("A csoportok (groups) nem olvashatók, üresre állítom.")
        p.text = "[]"
        return
    positions = []
    for g in groups:
        _leaf_positions(g, positions)
    if len(groups) == 1 and len(set(positions)) == 1:
        out = []
        for st in new_starts:
            g = copy.deepcopy(groups[0])
            _set_leaf_positions(g, st)
            out.append(g)
        p.text = json.dumps(out, indent=4, ensure_ascii=False) + "\n"
        log.info("Csoportok újraépítve: %d db", len(out))
    else:
        log.warning("A csoportszerkezet nem egyszerű (%d csoport), ezért kiürítem.", len(groups))
        p.text = "[]"


def rebuild_guides(seq, cuts, new_total, junction_guides):
    p = prop(seq, "kdenlive:sequenceproperties.guides")
    guides = []
    if p is not None and (p.text or "").strip():
        try:
            guides = json.loads(p.text)
        except ValueError:
            log.warning("A guide-ok nem olvashatók, kihagyom.")
            guides = []
    out, dropped = [], 0
    for g in guides:
        pos = int(g["pos"])
        if any(s <= pos < e for s, e, _ in cuts):
            dropped += 1
            continue
        g = dict(g)
        g["pos"] = pos - removed_before(pos, cuts)
        out.append(g)
    added = 0
    if junction_guides:
        for s, e, label in cuts:
            npos = s - removed_before(s, cuts)
            if 0 < npos < new_total:
                out.append({"comment": ("✂ " + label)[:70], "duration": 0, "pos": npos, "type": 6})
                added += 1
    out.sort(key=lambda g: g["pos"])
    if p is None:
        p = ET.SubElement(seq, "property", {"name": "kdenlive:sequenceproperties.guides"})
    p.text = json.dumps(out, indent=4, ensure_ascii=False) + "\n"
    log.info("Guide-ok: %d megmaradt, %d kiesett a vágott részekkel, %d vágási jelölő",
             len(out) - added, dropped, added)


# --------------------------------------------------------------------------
# Kérdések: párbeszédablak
# --------------------------------------------------------------------------
def _find_player():
    for name, fn in (
        ("mpv", lambda v, s, d: ["mpv", "--start=%.2f" % s, "--length=%.2f" % d, "--force-window=yes", v]),
        ("vlc", lambda v, s, d: ["vlc", "--start-time=%.2f" % s, "--stop-time=%.2f" % (s + d), v]),
        ("ffplay", lambda v, s, d: ["ffplay", "-ss", "%.2f" % s, "-t", "%.2f" % d, "-autoexit", v]),
    ):
        if shutil.which(name):
            return fn
    return None


class ConsoleAsker:
    """Tartalék: konzolos kérdezés + külső lejátszó (mpv/vlc/ffplay), ha van."""

    def __init__(self, video, ctx):
        self.video, self.ctx = video, ctx
        self.player = _find_player()

    def ask(self, q, idx, total):
        s, e = srt_to_seconds(q["start"]), srt_to_seconds(q["end"])
        print("\n--- Kérdés %d/%d [%s] %s-%s ---" % (idx, total, q["id"], fmt_short(s), fmt_short(e)))
        print(q["question"])
        print("Javaslat: %s" % ("megtartani" if q.get("suggestion") == "keep" else "kivágni"))

        def play():
            if self.video and self.player:
                st = max(0, s - self.ctx)
                subprocess.Popen(self.player(self.video, st, e - st + self.ctx),
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                print("(nincs lejátszó/videó, nem tudom lejátszani)")
        play()
        while True:
            a = input("[m]aradjon / [k]ivágom / [l]ejátszás újra / [j]avaslat mindenre / [x] megszakít: ").strip().lower()
            if a in ("m", "maradjon", "keep"):
                return "keep"
            if a in ("k", "kivag", "cut"):
                return "cut"
            if a == "l":
                play()
            elif a == "j":
                return "suggest_all"
            elif a == "x":
                return "abort"


def make_qt_asker(video, ctx, autoplay=True, audio=True):
    """PySide6-os asker, ha elérhető; különben None."""
    try:
        from PySide6.QtCore import Qt, QUrl, QRect
        from PySide6.QtGui import QFont, QKeySequence, QPainter, QColor, QShortcut
        from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
        from PySide6.QtMultimediaWidgets import QVideoWidget
        from PySide6.QtWidgets import (QApplication, QDialog, QHBoxLayout, QLabel, QPushButton,
                                       QSizePolicy, QSlider, QStyle, QStyleOptionSlider, QVBoxLayout)
    except Exception as exc:  # noqa
        log.info("PySide6 nem érhető el (%s) - konzolos kérdezés.", exc.__class__.__name__)
        return None

    class RangeSlider(QSlider):
        """Csúszka, ami kattintásra odaugrik, és kiemeli a kérdéses szakaszt."""

        def __init__(self):
            super().__init__(Qt.Horizontal)
            self.hl = None

        def _groove(self):
            opt = QStyleOptionSlider()
            self.initStyleOption(opt)
            return opt, self.style().subControlRect(QStyle.CC_Slider, opt, QStyle.SC_SliderGroove, self)

        def mousePressEvent(self, ev):
            if ev.button() == Qt.LeftButton:
                _, g = self._groove()
                v = QStyle.sliderValueFromPosition(self.minimum(), self.maximum(),
                                                   int(ev.position().x()) - g.x(), max(1, g.width()))
                self.setValue(v)
                self.sliderMoved.emit(v)
            super().mousePressEvent(ev)

        def paintEvent(self, ev):
            super().paintEvent(ev)
            if self.hl and self.maximum() > self.minimum():
                _, g = self._groove()
                span = self.maximum() - self.minimum()
                x1 = g.x() + int((self.hl[0] - self.minimum()) / span * g.width())
                x2 = g.x() + int((self.hl[1] - self.minimum()) / span * g.width())
                p = QPainter(self)
                p.fillRect(QRect(x1, g.center().y() - 5, max(2, x2 - x1), 10), QColor(255, 140, 0, 120))
                p.end()

    class Dlg(QDialog):
        def __init__(self, q, idx, total):
            super().__init__()
            self.q, self.result_value, self._init, self._dragging = q, "default", False, False
            s, e = srt_to_seconds(q["start"]), srt_to_seconds(q["end"])
            self.qs, self.qe = int(s * 1000), int(e * 1000)
            self.ws, self.we = max(0, self.qs - int(ctx * 1000)), self.qe + int(ctx * 1000)
            self.setWindowTitle("Kérdés %d/%d (%s)" % (idx, total, q["id"]))
            self.setMinimumWidth(520)
            lay = QVBoxLayout(self)

            t = QLabel(q["question"])
            t.setTextFormat(Qt.PlainText)
            t.setWordWrap(True)
            f = QFont()
            f.setPointSize(13)
            f.setBold(True)
            t.setFont(f)
            lay.addWidget(t)

            sug = "megtartani" if q.get("suggestion") == "keep" else "kivágni"
            info = "Idő: %s – %s (%d mp) · Téma: %s · Javaslat: <b>%s</b>" % (
                fmt_short(s), fmt_short(e), int(e - s), html.escape(q.get("topic", "")), sug)
            il = QLabel(info)
            il.setTextFormat(Qt.RichText)
            il.setWordWrap(True)
            lay.addWidget(il)
            if q.get("first_words") or q.get("last_words"):
                wl = QLabel("Eleje: „%s…” · Vége: „…%s”" % (html.escape(q.get("first_words", "")),
                                                          html.escape(q.get("last_words", ""))))
                wl.setTextFormat(Qt.RichText)
                wl.setWordWrap(True)
                wl.setStyleSheet("color: gray;")
                lay.addWidget(wl)

            self.player = QMediaPlayer(self)
            self.audio = None
            if audio:  # hangkimenet nélküli gépen (pl. szerver) --no-audio
                self.audio = QAudioOutput(self)
                self.player.setAudioOutput(self.audio)
            if video:
                self.vw = QVideoWidget()
                self.vw.setMinimumSize(360, 420)
                self.vw.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
                lay.addWidget(self.vw, 1)
                self.player.setVideoOutput(self.vw)
                self.player.setSource(QUrl.fromLocalFile(video))
                self.player.mediaStatusChanged.connect(self._status)
                self.player.positionChanged.connect(self._pos)
                self.player.playbackStateChanged.connect(self._state)
                self.player.errorOccurred.connect(lambda *a: log.warning("Lejátszási hiba: %s", a[-1] if a else ""))

                self.slider = RangeSlider()
                self.slider.setRange(self.ws, self.we)
                self.slider.hl = (self.qs, self.qe)
                self.slider.sliderMoved.connect(self.player.setPosition)
                self.slider.sliderPressed.connect(lambda: setattr(self, "_dragging", True))
                self.slider.sliderReleased.connect(lambda: setattr(self, "_dragging", False))
                lay.addWidget(self.slider)
                self.tl = QLabel("")
                lay.addWidget(self.tl)

                row = QHBoxLayout()
                self.pb = self._btn("▶ Lejátszás", self.toggle, row)
                self._btn("⟲ A rész elejétől", lambda: self.seek(self.qs), row)
                self._btn("−5 mp", lambda: self.seek(self.player.position() - 5000), row)
                self._btn("+5 mp", lambda: self.seek(self.player.position() + 5000), row)
                lay.addLayout(row)
            else:
                lay.addWidget(QLabel("<i>(A videófájl nem található, a lejátszás nem elérhető. --video FILE)</i>"))

            row = QHBoxLayout()
            keep = QPushButton("✅ Maradjon (M)")
            cut = QPushButton("✂ Kivágom (K)")
            for b, val in ((keep, "keep"), (cut, "cut")):
                b.setMinimumHeight(40)
                b.clicked.connect(lambda _=False, v=val: self.finish(v))
                row.addWidget(b)
            (keep if q.get("suggestion") == "keep" else cut).setDefault(True)
            lay.addLayout(row)
            row = QHBoxLayout()
            self._btn("Mind a javaslat szerint", lambda: self.finish("suggest_all"), row, focus=True)
            self._btn("Megszakítás", lambda: self.finish("abort"), row, focus=True)
            lay.addLayout(row)

            QShortcut(QKeySequence("M"), self, activated=lambda: self.finish("keep"))
            QShortcut(QKeySequence("K"), self, activated=lambda: self.finish("cut"))
            QShortcut(QKeySequence("Space"), self, activated=self.toggle)

        def _btn(self, text, fn, row, focus=False):
            b = QPushButton(text)
            if not focus:
                b.setFocusPolicy(Qt.NoFocus)
            b.clicked.connect(lambda _=False: fn())
            row.addWidget(b)
            return b

        def _status(self, st):
            if not self._init and st in (QMediaPlayer.LoadedMedia, QMediaPlayer.BufferedMedia):
                self._init = True
                self.player.setPosition(max(0, self.qs - 2000))
                if autoplay:
                    self.player.play()
                else:
                    self.player.pause()

        def _state(self, st):
            self.pb.setText("⏸ Szünet" if st == QMediaPlayer.PlayingState else "▶ Lejátszás")

        def _pos(self, ms):
            if not self._dragging:
                self.slider.setValue(min(max(ms, self.ws), self.we))
            self.tl.setText("%s / szakasz: %s – %s  (kiemelt = kérdéses rész)" % (
                self._t(ms), self._t(self.ws), self._t(self.we)))
            if ms >= self.we and self.player.playbackState() == QMediaPlayer.PlayingState:
                self.player.pause()

        @staticmethod
        def _t(ms):
            s = int(ms // 1000)
            return "%02d:%02d:%02d" % (s // 3600, s % 3600 // 60, s % 60)

        def seek(self, ms):
            self.player.setPosition(int(min(max(ms, self.ws), self.we)))

        def toggle(self):
            if not video:
                return
            if self.player.playbackState() == QMediaPlayer.PlayingState:
                self.player.pause()
            else:
                if self.player.position() >= self.we - 200:
                    self.seek(self.qs)
                self.player.play()

        def finish(self, value):
            self.result_value = value
            self.player.stop()
            self.accept()

        def closeEvent(self, ev):
            self.player.stop()
            super().closeEvent(ev)

    class QtAsker:
        def __init__(self):
            self.app = QApplication.instance() or QApplication(sys.argv[:1])

        def ask(self, q, idx, total):
            d = Dlg(q, idx, total)
            d.show()
            d.raise_()
            d.activateWindow()
            d.exec()
            r = d.result_value
            d.player.setSource(QUrl())
            d.deleteLater()
            return r

    return QtAsker()


def has_display():
    if sys.platform.startswith("linux"):
        return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
                    or os.environ.get("QT_QPA_PLATFORM"))
    return True


def resolve_questions(questions, args, video):
    """Visszaad: {id: 'keep'|'cut'}"""
    decisions = {}
    prior = {}
    if args.decisions and os.path.exists(args.decisions):
        with open(args.decisions, encoding="utf-8") as f:
            prior = json.load(f)
        log.info("Korábbi döntések betöltve: %s", args.decisions)
    mode = args.answers
    asker = None
    auto_rest = None
    n = len(questions)
    for i, q in enumerate(questions, 1):
        qid = q["id"]
        sug = q.get("suggestion", q.get("default_if_unanswered", "keep"))
        if qid in prior:
            decisions[qid] = prior[qid]
            log.info("? %s -> %s (korábbi döntés)", qid, decisions[qid])
            continue
        if mode in ("suggest", "keep", "cut") or auto_rest:
            decisions[qid] = sug if (mode == "suggest" or auto_rest) else mode
            log.info("? %s -> %s (automatikus)", qid, decisions[qid])
            continue
        if asker is None:
            if has_display():
                asker = make_qt_asker(video, args.context, not args.no_autoplay, not args.no_audio)
            if asker is None:
                asker = ConsoleAsker(video, args.context)
        r = asker.ask(q, i, n)
        if r == "abort":
            log.error("Megszakítva a felhasználó által, nem írok kimenetet.")
            sys.exit(1)
        if r == "suggest_all":
            auto_rest = True
            r = sug
        if r == "default":
            r = q.get("default_if_unanswered", "keep")
            log.info("? %s -> %s (ablak bezárva, alapértelmezés)", qid, r)
        else:
            log.info("? %s -> %s (felhasználó)", qid, r)
        decisions[qid] = r
    return decisions


# --------------------------------------------------------------------------
# Fő folyamat
# --------------------------------------------------------------------------
def run(args):
    prj = Project(args.project)
    fps = prj.fps
    log.info("Projekt: %s | %.2f fps | hossz %s | %d sáv-playlist",
             os.path.basename(args.project), fps, frames_to_tc(prj.total, fps)[:-4], len(prj.playlists))

    with open(args.cuts, encoding="utf-8") as f:
        data = json.load(f)

    items = []
    for c in data.get("cuts", []):
        items.append({"start": c["start"], "end": c["end"],
                      "label": "%s %s" % (c.get("category", ""), c.get("reason", ""))})
    log.info("Biztos vágások: %d", len(data.get("cuts", [])))

    questions = data.get("questions", [])
    video = args.video or prj.video_for_review()
    if questions:
        if video:
            log.info("Kérdések: %d | lejátszott videó: %s", len(questions), video)
        else:
            log.warning("Kérdések: %d | a videófájl nem található (használd a --video kapcsolót)", len(questions))
        decisions = resolve_questions(questions, args, video)
        for q in questions:
            if decisions.get(q["id"]) == "cut":
                items.append({"start": q["start"], "end": q["end"],
                              "label": "%s kérdés: %s" % (q["id"], q.get("topic", ""))})
        if args.decisions_out:
            with open(args.decisions_out, "w", encoding="utf-8") as f:
                json.dump(decisions, f, ensure_ascii=False, indent=2)
            log.info("Döntések mentve: %s", args.decisions_out)

    cuts = make_ranges(items, fps, prj.total, args.margin, args.merge_gap)
    keeps = complement(cuts, prj.total)
    if not keeps:
        raise SystemExit("Hiba: a vágások után nem marad anyag.")
    for s, e, label in cuts:
        log.info("✂ %s–%s (%.1f mp) %s", fmt_short(s / fps), fmt_short(e / fps), (e - s) / fps, label[:60])

    new_starts, acc = [], 0
    for i, (ks, ke) in enumerate(keeps):
        new_starts.append(acc)
        acc += ke - ks
        if 0 < i < len(keeps) - 1 and (ke - ks) / fps < 8:
            log.warning("Rövid megmaradó szakasz (%.1f mp) itt: %s - érdemes ellenőrizni",
                        (ke - ks) / fps, fmt_short(ks / fps))
    new_total = acc
    log.info("Hossz: %s -> %s (megmarad: %.0f%%)", fmt_short(prj.total / fps), fmt_short(new_total / fps),
             100.0 * new_total / prj.total)

    if args.dry_run:
        log.info("--dry-run: nem írok fájlt.")
        return

    ids = IdGen(prj.root)
    for pl in prj.playlists:
        n = ripple_playlist(pl, keeps, new_starts, fps, ids)
        if n:
            log.info("Sáv %-10s: %d szakasz", pl.get("id"), n)

    out_tc = frames_to_tc(new_total - 1, fps)
    prj.seq.set("out", out_tc)
    for t in prj.sub_tractors:
        if t.get("out"):
            t.set("out", out_tc)
    prj.proj_tractor.set("out", out_tc)
    for tr in prj.proj_tractor.findall("track"):
        if tr.get("out"):
            tr.set("out", out_tc)
    for pl in prj.root.findall("playlist"):
        for en in pl.findall("entry"):
            if en.get("producer") == prj.seq.get("id"):
                en.set("out", out_tc)
    set_prop(prj.seq, "kdenlive:duration", frames_to_tc(new_total, fps))
    set_prop(prj.seq, "kdenlive:maxduration", new_total)
    set_prop(prj.seq, "kdenlive:sequenceproperties.position", 0)

    rebuild_groups(prj.seq, new_starts)
    rebuild_guides(prj.seq, cuts, new_total, not args.no_junction_guides)

    ET.indent(prj.tree, space=" ")
    xml = ET.tostring(prj.root, encoding="unicode")
    xml = re.sub(r'(?<=[\w"]) />', "/>", xml)  # ugyanaz a jelölés, mint a Kdenlive-fájlban
    with open(args.output, "w", encoding="utf-8", newline="\n") as f:
        f.write("<?xml version='1.0' encoding='utf-8'?>\n" + xml + "\n")
    log.info("Kész: %s", args.output)


def main():
    ap = argparse.ArgumentParser(description="Vágási JSON alkalmazása Kdenlive projekten.")
    ap.add_argument("project", help="vágatlan .kdenlive fájl")
    ap.add_argument("cuts", help="vágási JSON (cuts + questions)")
    ap.add_argument("-o", "--output", help="kimeneti .kdenlive (alap: <projekt>-vagott.kdenlive)")
    ap.add_argument("--video", help="a kérdésekhez lejátszandó videó (alap: a projekt hangsávi klipje)")
    ap.add_argument("--answers", choices=["ask", "suggest", "keep", "cut"], default="ask",
                    help="kérdések kezelése (alap: ask = párbeszédablak)")
    ap.add_argument("--decisions", help="korábbi döntések JSON-ja (id -> keep/cut)")
    ap.add_argument("--decisions-out", help="döntések mentése (alap: <kimenet>.decisions.json)")
    ap.add_argument("--context", type=float, default=5.0, help="előzmény/utózmány mp a lejátszóban (alap 5)")
    ap.add_argument("--margin", type=float, default=0.0, help="vágások két szélén meghagyott mp (alap 0)")
    ap.add_argument("--merge-gap", type=float, default=1.5,
                    help="ennél közelebbi vágásokat összevon, hogy ne maradjon pár mp-es szilánk (alap 1.5)")
    ap.add_argument("--no-audio", action="store_true", help="hang nélküli lejátszás (hangeszköz nélküli gépen)")
    ap.add_argument("--no-autoplay", action="store_true", help="ne induljon el automatikusan a lejátszás")
    ap.add_argument("--no-junction-guides", action="store_true", help="ne kerüljön jelölő a vágási pontokra")
    ap.add_argument("--dry-run", action="store_true", help="csak a terv, nem ír fájlt")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    if not args.output:
        base, ext = os.path.splitext(args.project)
        args.output = base + "-vagott" + (ext or ".kdenlive")
    if os.path.abspath(args.output) == os.path.abspath(args.project):
        sys.exit("Hiba: a kimenet nem egyezhet a bemenettel.")
    if not args.decisions_out:
        args.decisions_out = args.output + ".decisions.json"
    run(args)


if __name__ == "__main__":
    main()
