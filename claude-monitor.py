#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CLAUDE MONITOR v2 — monitor en vivo de sesiones de Claude Code.
Reimplementación con UX/UI renovada (layout en tarjetas, alineación real,
orden y filtrado mejorados). Solo stdlib.

Uso:
    claude-monitor-v2.py [--interval SEG] [--once] [--json] [--status]
Teclas:
    ↑↓/jk  navegar      Enter  ir a la sesión (tmux/wmctrl)
    x      matar (y/n)  s/S    ordenar (campo / invertir)
    /      filtrar      g      agrupar por proyecto
    C      compacto     c      limpiar muertas
    m      mute         d      ver/ocultar muertas       q  salir
"""
import os, sys, re, json, time, glob, signal, select, subprocess
from collections import deque, defaultdict

CFG        = os.environ.get("CLAUDE_CONFIG_DIR", os.path.expanduser("~/.claude"))
SESS_DIR   = os.path.join(CFG, "sessions")
PROJ_DIR   = os.path.join(CFG, "projects")
QUOTA_FILE = os.path.join(CFG, "vscode-claude-status-cache.json")
EVENT_LOG  = os.path.join(CFG, "monitor-events.log")
HOME       = os.path.expanduser("~")

# ───────────────────────── color / theme ──────────────────────────────────
def fg(r, g, b): return f"\x1b[38;2;{r};{g};{b}m"
def bg(r, g, b): return f"\x1b[48;2;{r};{g};{b}m"
RST, B, DIM, ITAL = "\x1b[0m", "\x1b[1m", "\x1b[2m", "\x1b[3m"

class T:  # paleta activa (poblada por apply_theme)
    pass

# temas: cada rol como rgb. coral/coral_l/coral_d = acentos marca; cream = texto ppal.
THEMES = {
    "oscuro":   dict(coral=(217,119,87), coral_l=(244,168,132), coral_d=(168,86,60),
                     cream=(242,233,216), purple=(170,132,232), teal=(94,204,194),
                     green=(122,206,122), yellow=(246,206,92), red=(236,106,106),
                     gray=(120,122,134), white=(235,235,238),
                     sel_bg=(58,46,70), panel_bg=(30,28,38), band_bg=(38,26,30), zebra=(30,29,36)),
    "claro":    dict(coral=(190,90,55), coral_l=(150,70,45), coral_d=(120,55,35),
                     cream=(40,38,44), purple=(120,70,200), teal=(20,120,110),
                     green=(40,140,60), yellow=(150,110,10), red=(190,50,50),
                     gray=(110,110,120), white=(20,20,24),
                     sel_bg=(214,205,232), panel_bg=(236,233,226), band_bg=(244,231,224), zebra=(234,231,224)),
    "contraste":dict(coral=(255,160,110), coral_l=(255,205,170), coral_d=(255,140,80),
                     cream=(255,255,255), purple=(215,175,255), teal=(110,255,240),
                     green=(120,255,120), yellow=(255,240,80), red=(255,90,90),
                     gray=(200,200,210), white=(255,255,255),
                     sel_bg=(90,70,130), panel_bg=(0,0,0), band_bg=(18,18,22), zebra=(24,24,30)),
}
THEME_NAMES = list(THEMES)

STATUS = {
    "waiting": dict(label="ESPERA PERMISO", icon="◔", fgc=(20,20,20),    bgc=(246,206,92),  rank=0, accent=""),
    "blocked": dict(label="bloqueado",      icon="■", fgc=(255,235,235), bgc=(150,50,50),   rank=1, accent=""),
    "busy":    dict(label="trabajando",     icon="◐", fgc=(220,252,250), bgc=(26,86,96),    rank=2, accent=""),
    "idle":    dict(label="en reposo",      icon="●", fgc=(225,248,225), bgc=(34,78,40),    rank=3, accent=""),
    "dead":    dict(label="terminada",      icon="✝", fgc=(150,150,150), bgc=(40,40,44),    rank=9, accent=""),
}

def apply_theme(name):
    p = THEMES.get(name, THEMES["oscuro"])
    for role in ("coral","coral_l","coral_d","cream","purple","teal","green","yellow","red","gray","white"):
        setattr(T, role, fg(*p[role]))
    for role in ("sel_bg","panel_bg","band_bg","zebra"):
        setattr(T, role, bg(*p[role]))
    for st, role in (("waiting","yellow"),("blocked","red"),("busy","teal"),("idle","green"),("dead","gray")):
        STATUS[st]["accent"] = getattr(T, role)

apply_theme("oscuro")
MASCOT = {
    "busy":    "✳✴❉❈❊✺✷✸", "waiting": "◆◇◆◇", "idle": "✳·✳·",
    "blocked": "■□", "dead": "✝✝",
}

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

def disp_width(s):
    """ancho visible (ignora ANSI, cuenta CJK/emoji como 2)."""
    s = ANSI_RE.sub("", s); w = 0
    for ch in s:
        o = ord(ch)
        if o == 0: continue
        if (0x1100 <= o <= 0x115f or o in (0x2329, 0x232a)
            or (0x2e80 <= o <= 0xa4cf and o != 0x303f)
            or 0xac00 <= o <= 0xd7a3 or 0xf900 <= o <= 0xfaff
            or 0xfe30 <= o <= 0xfe4f or 0xff00 <= o <= 0xff60
            or 0xffe0 <= o <= 0xffe6 or 0x1f300 <= o <= 0x1faff
            or 0x20000 <= o <= 0x3fffd):
            w += 2
        else:
            w += 1
    return w

def pad(s, width, align="<"):
    """pad/trunca a `width` columnas visibles, con elipsis."""
    w = disp_width(s)
    if w > width:
        # truncar respetando ancho (sin romper ANSI: asumimos texto plano aquí)
        out, cur = "", 0
        for ch in ANSI_RE.sub("", s):
            cw = disp_width(ch)
            if cur + cw > width - 1:
                out += "…"; cur += 1; break
            out += ch; cur += cw
        s = out; w = disp_width(s)
    filln = max(0, width - w)
    if align == "<":  return s + " " * filln
    if align == ">":  return " " * filln + s
    l = filln // 2;   return " " * l + s + " " * (filln - l)

def short_path(p):
    return p.replace(HOME, "~", 1) if p.startswith(HOME) else p

def abbrev_path(p, width):
    """acorta a `width` cols dejando lo significativo: ~/…/parent/leaf"""
    p = short_path(p)
    if disp_width(p) <= width:
        return p
    parts = [x for x in p.split("/") if x]
    if not parts:
        return p
    leaf = parts[-1]
    cand = "…/" + leaf
    if len(parts) >= 2:
        c2 = "…/" + parts[-2] + "/" + leaf
        if disp_width(c2) <= width:
            cand = c2
    return cand

def tcell(text, width, fgcode, rb):
    """celda de texto plano: bg continuo (rb) + color de texto, padeada al ancho."""
    return f"{rb}{fgcode}{pad(text, width)}"

def ccell(colored, width, rb):
    """celda ya coloreada (pill, sparkline, mascota): recorta o rellena al ancho."""
    vis = disp_width(colored)
    if vis > width:
        return f"{rb}{clip(colored, width)}"
    return f"{rb}{colored}{rb}{' ' * (width - vis)}"

def fmt_tokens(n):
    if not n:
        return "—"
    if n >= 1000:
        return f"{n/1000:.0f}k"
    return str(n)

def fmt_age(ms):
    if not ms: return "—"
    d = max(0, int(time.time()*1000 - ms) // 1000)
    if d < 60:   return f"{d}s"
    if d < 3600: return f"{d//60}m"
    return f"{d//3600}h{(d%3600)//60}m"

def gradient(text, off, pal):
    out = []
    for i, ch in enumerate(text):
        r, g, b = pal[(i + off) % len(pal)]
        out.append(f"{fg(r,g,b)}{B}{ch}")
    return "".join(out) + RST

GRAD_PAL = [(217,119,87),(236,146,102),(246,178,140),(250,214,180),(246,178,140),(236,146,102)]

# ───────────────────────── data sources ───────────────────────────────────
class Cache:
    def __init__(self):
        self.act = {}; self.act_mt = {}
        self.usage = {}; self.usage_mt = {}
        self.title = {}; self.title_mt = {}
        self.model = {}
        self.git = {}; self.git_t = {}
        self.hist = defaultdict(lambda: deque(maxlen=24))
        self.prev = {}
        self.changed = {}      # pid -> tick del último cambio de estado (flash)

CA = Cache()

def jsonl_for(sid):
    if not sid: return None
    hits = glob.glob(os.path.join(PROJ_DIR, "*", f"{sid}.jsonl"))
    return hits[0] if hits else None

def tail_lines(path, n=60):
    try:
        with open(path, "rb") as f:
            f.seek(0, 2); size = f.tell()
            block = min(size, 65536); f.seek(size - block)
            data = f.read().decode("utf-8", "replace")
        return data.splitlines()[-n:]
    except Exception:
        return []

def last_activity(sid, waiting_for, status):
    if status == "waiting":
        return waiting_for or "permiso pendiente"
    jf = jsonl_for(sid)
    if not jf: return ""
    try: mt = os.path.getmtime(jf)
    except OSError: return ""
    if CA.act_mt.get(sid) == mt: return CA.act.get(sid, "")
    res = ""
    for line in reversed(tail_lines(jf, 60)):
        try: o = json.loads(line)
        except Exception: continue
        if o.get("type") != "assistant": continue
        for c in (o.get("message", {}).get("content") or []):
            if c.get("type") == "tool_use":
                arg = (c.get("input", {}) or {})
                detail = arg.get("file_path") or arg.get("command") or arg.get("pattern") or arg.get("path") or ""
                detail = " ".join(str(detail).split())
                res = f"{c.get('name','tool')} {detail}".strip(); break
            if c.get("type") == "text" and c.get("text"):
                res = " ".join(c["text"].split()); break
        if res: break
    res = res[:80]
    CA.act[sid] = res; CA.act_mt[sid] = mt
    return res

def context_tokens(sid):
    """tokens de contexto del último turno (input + caché). cacheado por mtime."""
    if not sid:
        return 0
    jf = jsonl_for(sid)
    if not jf:
        return 0
    try: mt = os.path.getmtime(jf)
    except OSError: return 0
    if CA.usage_mt.get(sid) == mt:
        return CA.usage.get(sid, 0)
    ctx = 0
    for line in reversed(tail_lines(jf, 60)):
        try: o = json.loads(line)
        except Exception: continue
        u = o.get("message", {}).get("usage")
        if u:
            ctx = (u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0)
                   + u.get("cache_creation_input_tokens", 0))
            break
    CA.usage[sid] = ctx; CA.usage_mt[sid] = mt
    return ctx

def title_of(sid):
    """nombre/título auto-generado de la sesión (aiTitle). cacheado por mtime."""
    if not sid:
        return ""
    jf = jsonl_for(sid)
    if not jf:
        return ""
    try: mt = os.path.getmtime(jf)
    except OSError: return ""
    if CA.title_mt.get(sid) == mt:
        return CA.title.get(sid, "")
    t = ""
    for line in reversed(tail_lines(jf, 400)):
        try: o = json.loads(line)
        except Exception: continue
        if o.get("type") == "ai-title" and o.get("aiTitle"):
            t = o["aiTitle"]; break
    CA.title[sid] = t; CA.title_mt[sid] = mt
    return t

def model_of(sid):
    if not sid: return "?"
    if sid in CA.model: return CA.model[sid]
    jf = jsonl_for(sid); m = "?"
    if jf:
        for line in reversed(tail_lines(jf, 200)):
            try: o = json.loads(line)
            except Exception: continue
            mm = o.get("message", {}).get("model")
            if mm: m = mm; break
    m = m.replace("claude-", ""); CA.model[sid] = m
    return m

def git_info(cwd, tick):
    t10 = tick // 10
    if CA.git_t.get(cwd) == t10 and cwd in CA.git: return CA.git[cwd]
    info = "—"
    try:
        br = subprocess.run(["git","-C",cwd,"rev-parse","--abbrev-ref","HEAD"],
                            capture_output=True, text=True, timeout=2)
        if br.returncode == 0 and br.stdout.strip():
            branch = br.stdout.strip()
            st = subprocess.run(["git","-C",cwd,"status","--porcelain"],
                                capture_output=True, text=True, timeout=2)
            dirty = len([l for l in st.stdout.splitlines() if l])
            info = f"{branch} ✎{dirty}" if dirty else branch
    except Exception: pass
    CA.git[cwd] = info; CA.git_t[cwd] = t10
    return info

def ps_info(pid):
    try:
        r = subprocess.run(["ps","-o","%cpu=,rss=","-p",str(pid)],
                           capture_output=True, text=True, timeout=2)
        parts = r.stdout.split()
        if len(parts) >= 2:
            return f"{parts[0]}%  {int(parts[1])//1024}MB"
    except Exception: pass
    return "— —"

def pc_stats(pids):
    """agrega CPU/RAM de todas las sesiones + uso de RAM y carga del sistema."""
    cpu, rss_kb = 0.0, 0
    if pids:
        try:
            r = subprocess.run(["ps","-o","%cpu=,rss=","-p",",".join(map(str,pids))],
                               capture_output=True, text=True, timeout=2)
            for ln in r.stdout.splitlines():
                p = ln.split()
                if len(p) >= 2:
                    try: cpu += float(p[0]); rss_kb += int(p[1])
                    except ValueError: pass
        except Exception: pass
    mem_pct = None
    try:
        mt = ma = 0
        with open("/proc/meminfo") as f:
            for ln in f:
                if ln.startswith("MemTotal:"):     mt = int(ln.split()[1])
                elif ln.startswith("MemAvailable:"): ma = int(ln.split()[1])
        if mt: mem_pct = 100 * (mt - ma) / mt
    except Exception: pass
    try: load = os.getloadavg()[0]
    except Exception: load = None
    return cpu, rss_kb // 1024, mem_pct, load

def fmt_mb(mb):
    return f"{mb/1024:.1f}GB" if mb >= 1024 else f"{mb}MB"

def read_quota():
    try:
        with open(QUOTA_FILE) as f: u = json.load(f).get("usageData", {})
        return float(u.get("utilization5h", 0)), float(u.get("utilization7d", 0))
    except Exception:
        return None, None

def alive(pid):
    try: os.kill(pid, 0); return True
    except OSError: return False

def log_event(pid, a, b, cwd):
    try:
        with open(EVENT_LOG, "a") as f:
            f.write(f"{time.strftime('%F %T')}\tPID {pid}\t{a} -> {b}\t{short_path(cwd)}\n")
    except Exception: pass

CLAUDE_LOGO = os.path.join(CFG, "claude-logo.png")
def notify(title, body, urgency="normal", tag=None, timeout=None, category=None):
    """Notificación enriquecida. `tag` reemplaza la anterior de la misma sesión
    (no se apilan); `timeout` en ms (0 = no expira)."""
    icon = CLAUDE_LOGO if os.path.exists(CLAUDE_LOGO) else "dialog-information"
    args = ["notify-send", "-a", "Claude Monitor", "-u", urgency, "-i", icon]
    if timeout is not None:
        args += ["-t", str(timeout)]
    if category:
        args += ["-c", category]
    if tag:
        # reemplazo in-place en distintos daemons (dunst / GNOME / KDE)
        args += ["-h", f"string:x-dunst-stack-tag:{tag}",
                 "-h", f"string:x-canonical-private-synchronous:{tag}"]
    args += [title, body]
    try:
        subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception: pass

# ───────── sonidos chiptune 8-bit (sintetizados, ondas cuadradas) ─────────
import wave, math, struct
SND_DIR = os.path.join(CFG, "monitor-sounds")
SR = 22050               # sample rate
DUTY = 0.5               # ciclo de trabajo de la onda cuadrada (0.5 = clásico)

def _note(freq):         # nombre/num -> ya recibimos Hz directo
    return freq

# jingles: lista de (frecuencia_Hz, duración_seg). freq 0 = silencio.
def _scale(base, semis):  # transpone una lista de notas N semitonos
    r = 2 ** (semis / 12.0)
    return [(0 if f == 0 else f * r, d) for f, d in base]

C5,D5,E5,F5,G5,A5,B5,C6,E6,G6 = 523,587,659,698,784,880,988,1047,1319,1568
JINGLES = {
    "start":   [(C5,.07),(E5,.07),(G5,.07),(C6,.16)],            # power-on ascendente
    "quit":    [(C6,.07),(G5,.07),(E5,.07),(C5,.16)],            # power-off descendente
    "done":    [(E5,.08),(G5,.08),(C6,.20)],                     # "ta-da" nivel completado
    "blocked": [(330,.12),(294,.12),(247,.28)],                  # error grave descendente
    "perm":    [(B5,.08),(0,.03),(E6,.08),(0,.03),(B5,.06),(E6,.18)],  # alerta tipo "coin"
    "nav":     [(A5,.025)],                                      # blip corto al navegar
}

def _square_bytes(notes, amp=0.32):
    out = bytearray()
    for freq, dur in notes:
        n = int(SR * dur)
        period = SR / freq if freq > 0 else 1
        for i in range(n):
            env = 1.0 - (i / max(1, n)) * 0.35           # decaimiento leve (evita clicks)
            if freq <= 0:
                v = 0.0
            else:
                v = 1.0 if (i % period) < period * DUTY else -1.0
            out.append(max(0, min(255, int(128 + v * env * amp * 127))))
    return bytes(out)

def _write_wav(path, notes):
    with wave.open(path, "wb") as w:
        w.setnchannels(1); w.setsampwidth(1); w.setframerate(SR)
        w.writeframes(_square_bytes(notes))

def ensure_sounds():
    try: os.makedirs(SND_DIR, exist_ok=True)
    except Exception: return
    for k in ("start", "quit", "done", "blocked", "nav"):
        p = os.path.join(SND_DIR, f"{k}.wav")
        if not os.path.exists(p):
            try: _write_wav(p, JINGLES[k])
            except Exception: pass
    for i in range(6):                                   # 6 variantes de tono para permisos
        p = os.path.join(SND_DIR, f"perm{i}.wav")
        if not os.path.exists(p):
            try: _write_wav(p, _scale(JINGLES["perm"], i * 2))  # +0,+2,+4… semitonos
            except Exception: pass

_HAS = {}
def has_cmd(c):
    if c not in _HAS:
        _HAS[c] = subprocess.run(["which", c], capture_output=True).returncode == 0
    return _HAS[c]

def play_sound(kind, pid=None):
    if kind == "permission":
        fn = f"perm{(pid or 0) % 6}.wav"
    else:
        fn = f"{kind}.wav"
    path = os.path.join(SND_DIR, fn)
    if os.path.exists(path):
        for player in ("paplay", "pw-play", "aplay", "ffplay"):
            if has_cmd(player):
                cmd = [player, path] if player != "ffplay" else ["ffplay","-nodisp","-autoexit","-loglevel","quiet",path]
                try:
                    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); return
                except Exception: pass
    sys.stdout.write("\a"); sys.stdout.flush()           # último recurso: beep

# ───────────────────────── session model ──────────────────────────────────
class Session:
    __slots__=("pid","status","kind","cwd","upd","sid","wf","ver","name","started","is_alive","activity")
    def __init__(self, d):
        self.pid = int(d.get("pid", 0)); self.kind = d.get("kind","?")
        self.cwd = d.get("cwd",""); self.upd = d.get("statusUpdatedAt") or d.get("updatedAt") or 0
        self.sid = d.get("sessionId",""); self.wf = d.get("waitingFor","")
        self.ver = d.get("version",""); self.name = d.get("name","")
        self.started = d.get("startedAt") or 0
        self.is_alive = alive(self.pid)
        self.status = d.get("status","?") if self.is_alive else "dead"
        self.activity = ""
    @property
    def age(self): return max(0, int(time.time()*1000 - self.upd)//1000) if self.upd else 0
    @property
    def uptime(self): return fmt_age(self.started) if self.started else "—"
    @property
    def meta(self): return STATUS.get(self.status, STATUS["idle"])
    @property
    def display_name(self):
        # nombre manual (--name) > título auto-generado > carpeta
        return self.name or title_of(self.sid) or (short_path(self.cwd).split("/")[-1] or "—")

def collect(app):
    sessions, seen = [], set()
    for fpath in glob.glob(os.path.join(SESS_DIR, "*.json")):
        try:
            with open(fpath) as f: d = json.load(f)
        except Exception: continue
        s = Session(d); seen.add(s.pid)
        if not s.is_alive and not app.show_dead:
            continue
        sessions.append(s)
    # transiciones -> alertas + log + historial
    for s in sessions:
        s.activity = last_activity(s.sid, s.wf, s.status)
        pv = CA.prev.get(s.pid)
        if pv and pv != s.status:
            log_event(s.pid, pv, s.status, s.cwd)
            CA.changed[s.pid] = app.tick           # para el flash de la fila
            if app.mode == "tui":
                snd = app.sound_on; ntf = app.notif_on
                nm = s.display_name; loc = short_path(s.cwd); tag = f"claude-{s.pid}"
                if s.status == "waiting":
                    if snd: play_sound("permission", s.pid)
                    if ntf: notify(f"🔐 {nm} · espera permiso",
                                   f"Pide aprobar: {s.wf or 'una acción'}\n📁 {loc}   ·   PID {s.pid}",
                                   urgency="critical", tag=tag, timeout=0, category="im.received")
                elif s.status == "blocked":
                    if snd: play_sound("blocked")
                    if ntf: notify(f"🚫 {nm} · bloqueado",
                                   f"La sesión quedó bloqueada (¿límite de uso?)\n📁 {loc}   ·   PID {s.pid}",
                                   urgency="critical", tag=tag, timeout=0)
                elif pv == "busy" and s.status == "idle":
                    if snd: play_sound("done")
                    if ntf: notify(f"✅ {nm} · tarea terminada",
                                   f"{s.activity or 'Listo.'}\n📁 {loc}   ·   PID {s.pid}",
                                   urgency="normal", tag=tag, timeout=6000, category="transfer.complete")
        CA.prev[s.pid] = s.status
        lvl = {"busy":6,"waiting":3,"blocked":1,"idle":1}.get(s.status, 0)
        CA.hist[s.pid].append(lvl)
    for pid in list(CA.prev):
        if pid not in seen:
            CA.prev.pop(pid, None); CA.hist.pop(pid, None); CA.changed.pop(pid, None)
    # filtro
    if app.filter:
        q = app.filter.lower()
        sessions = [s for s in sessions if q in f"{s.pid} {s.status} {s.cwd} {s.activity}".lower()]
    return sessions

SORT_FIELDS = [("estado", lambda s:(s.meta["rank"], s.pid)),
               ("actividad", lambda s:-s.upd),
               ("pid", lambda s:s.pid),
               ("directorio", lambda s:s.cwd.lower())]

def sort_sessions(sessions, idx, rev):
    key = SORT_FIELDS[idx][1]
    return sorted(sessions, key=key, reverse=rev)

# ───────────────────────── widgets ────────────────────────────────────────
def spark_header(tick):
    f = tick % 4
    o, c, cl = T.coral, T.cream, T.coral_l
    if f == 0:   lines = [f"   {o}|{RST}   ", f" {o}──{c}{B}✳{RST}{o}──{RST} ", f"   {o}|{RST}   "]
    elif f == 2: lines = [f" {o}\\   {o}/{RST} ", f"   {c}{B}✺{RST}   ", f" {o}/   {o}\\{RST} "]
    else:        lines = [f" {o}\\ {o}| {o}/{RST} ", f" {o}──{cl}{B}✸{RST}{o}──{RST} ", f" {o}/ {o}| {o}\\{RST} "]
    return [pad(l, 7) for l in lines]   # ancho fijo -> sin jitter

def face(status, tick):
    accent = STATUS.get(status, STATUS["idle"])["accent"]
    blink = (tick % 6 == 0)
    eyes, mouths = "- -", ["‿‿","‿‿"]
    if status == "busy":    eyes, mouths = "◕ ◕", ["◜◝","◟◞"]
    elif status == "waiting": eyes, mouths = "◉ ◉", ["!!","..","!!",".."]
    elif status == "idle":    eyes, mouths = "- -", ["‿‿","zz"]
    elif status == "blocked": eyes, mouths = "× ×", ["⌢⌢","⌢⌢"]
    elif status == "dead":    eyes, mouths = "x x", ["__","__"]
    if blink and status != "idle": eyes = "- -"
    m = mouths[tick % len(mouths)]
    # cada línea: ancho visible exacto = 9, esquinas alineadas con pad() (consciente del ancho)
    return [f"{accent} ╭─────╮ {RST}",
            f"{accent} │ {pad(eyes,3,'^')} │ {RST}",
            f"{accent} ╰ {pad(m,3,'^')} ╯ {RST}"]

SPARK_CHARS = "▁▂▃▄▅▆▇"
def sparkline(pid, width=11):
    h = CA.hist.get(pid)
    if not h: return ""
    vals = list(h)[-width:]
    return T.teal + "".join(SPARK_CHARS[min(v, 6)] for v in vals) + RST

def mascot(status, pid, tick):
    fr = MASCOT.get(status, "✳✳")
    accent = STATUS.get(status, STATUS["idle"])["accent"]
    return f"{B}{accent}{fr[(tick + pid) % len(fr)]}{RST}"

def gauge(frac, width):
    if frac is None: return ""
    filled = max(0, min(width, round(frac * width)))
    col = T.red if frac >= .9 else T.yellow if frac >= .7 else T.green
    return col + "▰"*filled + DIM + "▱"*(width-filled) + RST

def pill(status, big=False):
    m = STATUS.get(status, STATUS["idle"])
    r,g,b = m["bgc"]; fr,fgg,fb = m["fgc"]
    txt = f"{m['icon']} {m['label']}"
    return f"{bg(r,g,b)}{fg(fr,fgg,fb)}{B} {txt} {RST}"

# ───────────────────────── application ────────────────────────────────────
class App:
    def __init__(self, interval, mode):
        self.interval = interval; self.mode = mode
        self.tick = 0; self.sel = 0; self.scroll = 0
        self.show_dead = True; self.compact = False
        self.sound_on = True; self.notif_on = True          # toggles independientes
        self.group = False; self.filter = ""; self.sort_i = 0; self.sort_rev = False
        self.confirm = None; self.msg = ""; self.theme_i = 0
        self.rowpids = []

    # ---- rendering ----
    def render(self):
        cols, rows = os.get_terminal_size()
        cols = max(cols, 76)
        sessions = collect(self)
        sessions = sort_sessions(sessions, self.sort_i, self.sort_rev)
        counts = defaultdict(int)
        for s in sessions: counts[s.status] += 1
        n_dead = sum(1 for s in sessions if s.status == "dead")

        L = []
        inner = cols - 2
        sp = spark_header(self.tick)
        title = gradient("CLAUDE  MONITOR", self.tick, GRAD_PAL)
        clock = time.strftime("%a %d %b · %H:%M:%S")
        u5, u7 = read_quota()
        q = ""
        if u5 is not None:
            q = (f"{T.gray}5h{RST} {gauge(u5,8)} {T.cream}{int(u5*100):>2}%{RST}   "
                 f"{T.gray}7d{RST} {gauge(u7,8)} {T.cream}{int(u7*100):>2}%{RST}")

        # ----- header band -----
        L.append(f"{T.coral}╭{'─'*inner}╮{RST}")
        L.append(self._band(sp[0] + "  " + title, inner))
        L.append(self._band(sp[1] + "  " + f"{T.cream}{clock}{RST}    {q}", inner))
        # chips
        chips = []
        if counts["waiting"]: chips.append(pill("waiting"))
        chips.append(pill("busy")); chips.append(pill("idle"))
        if counts["blocked"]: chips.append(pill("blocked"))
        info = []
        info.append(f"{T.gray}orden:{SORT_FIELDS[self.sort_i][0]}{'↓' if self.sort_rev else '↑'}{RST}")
        info.append(f"{T.gray}tema:{THEME_NAMES[self.theme_i]}{RST}")
        if self.group:        info.append(f"{T.purple}⊞ grupos{RST}")
        if not self.sound_on: info.append(f"{T.yellow}🔇 sonido{RST}")
        if not self.notif_on: info.append(f"{T.yellow}🔕 notif{RST}")
        if self.filter:       info.append(f"{T.yellow}/{self.filter}{RST}")
        chip_line = "  ".join(chips) + "   " + "  ".join(info)
        L.append(self._band(sp[2] + "  " + chip_line, inner))
        L.append(f"{T.coral}╰{'─'*inner}╯{RST}")

        # ----- column header (anchos balanceados) -----
        W_MARK,W_PID,W_AGE,W_ST,W_HIS,W_CTX = 4,7,6,19,7,6
        nsep = 9  # un espacio por columna que agrega _cols
        fixed = W_MARK+W_PID+W_AGE+W_ST+W_HIS+W_CTX + nsep + 2
        leftover = max(30, cols - fixed)
        W_NAME = min(34, max(12, int(leftover * 0.32)))
        W_HAC  = min(44, max(14, int(leftover * 0.40)))
        W_DIR  = max(10, leftover - W_NAME - W_HAC)
        L.append("  " + T.coral_d + B + self._cols(
            ("", W_MARK), ("PID", W_PID), ("NOMBRE", W_NAME), ("EDAD", W_AGE),
            ("ESTADO", W_ST), ("USO", W_HIS), ("CTX", W_CTX),
            ("HACIENDO", W_HAC), ("DIRECTORIO", W_DIR)) + RST)
        L.append("  " + DIM + "─"*inner + RST)

        # ----- rows (group + scroll window) -----
        panel_h = 0 if self.compact else 7
        avail = max(1, rows - len(L) - panel_h - 2)   # 2 = stats + footer

        # build display list: items are ('h', text) or ('s', session, sel_index)
        items = []; self.rowpids = []; last_proj = None
        for s in sessions:
            if self.group:
                proj = "~" if s.cwd == HOME else os.path.basename(s.cwd) or s.cwd
                if proj != last_proj:
                    items.append(("h", short_path(s.cwd))); last_proj = proj
            items.append(("s", s, len(self.rowpids)))
            self.rowpids.append(s.pid)

        count = len(self.rowpids)
        self.sel = 0 if count == 0 else max(0, min(self.sel, count-1))

        # line index of selected session
        nlines = len(items)
        indic = nlines > avail
        if indic: avail = max(1, avail - 1)
        tgt = next((i for i,it in enumerate(items) if it[0]=="s" and it[2]==self.sel), 0)
        if tgt < self.scroll: self.scroll = tgt
        if tgt >= self.scroll + avail: self.scroll = tgt - avail + 1
        self.scroll = max(0, min(self.scroll, max(0, nlines - avail)))

        if indic:
            up = " ▲" if self.scroll > 0 else "  "
            dn = " ▼" if self.scroll + avail < nlines else "  "
            L.append(f"  {T.gray}[{self.sel+1}/{count}]{up}{dn}{RST}")

        drawn = 0; vis = 0
        for it in items[self.scroll:]:
            if drawn >= avail: break
            if it[0] == "h":
                L.append(f"  {T.purple}{DIM}▾ {pad(short_path(it[1]), inner-4)}{RST}"); drawn += 1; continue
            _, s, si = it
            selected = (si == self.sel)
            flashing = (self.tick - CA.changed.get(s.pid, -99)) in (0, 1, 2)
            if flashing and not selected:
                fb = STATUS.get(s.status, STATUS["idle"])["bgc"]
                rb = bg(*(min(255, c + 55) for c in fb))     # destello del color de estado
            else:
                rb = T.sel_bg if selected else (T.zebra if vis % 2 else "")
            vis += 1
            accent = STATUS.get(s.status, STATUS["idle"])["accent"]
            mk = f"{T.coral_l}{B}▸{RST}" if selected else " "
            mark_cell = f"{accent}▌{RST}{mk} {mascot(s.status,s.pid,self.tick)}"
            agecol = T.red+B if (s.status=="waiting" and s.age>30) else T.cream
            warn = f" {T.red}⚠{RST}" if (s.status=="busy" and s.age>180) else ""
            ctx = context_tokens(s.sid)
            ctxcol = T.red if ctx>160000 else T.yellow if ctx>120000 else T.gray
            name = s.display_name
            cells = [
                ccell(mark_cell, W_MARK, rb),
                tcell(str(s.pid),        W_PID,  T.coral_l, rb),
                tcell(name,              W_NAME, T.white,    rb),
                tcell(fmt_age(s.upd),    W_AGE,  agecol,     rb),
                ccell(pill(s.status),    W_ST,   rb),
                ccell(sparkline(s.pid, W_HIS), W_HIS, rb),
                tcell(fmt_tokens(ctx),   W_CTX,  ctxcol,     rb),
                tcell(s.activity,        W_HAC,  T.cream,    rb),
                tcell(abbrev_path(s.cwd, W_DIR), W_DIR, T.purple, rb),
            ]
            row = f"{rb}  " + f"{rb} ".join(cells)
            tail = max(0, (cols - 1) - disp_width(row))
            row += f"{rb}{' '*tail}{RST}{warn}"
            L.append(row); drawn += 1

        # ----- detail panel -----
        if not self.compact:
            L.append("  " + T.coral_d + "─"*inner + RST)
            if count:
                s = next((x for x in sessions if x.pid == self.rowpids[self.sel]), None)
            else:
                s = None
            if s:
                fc = face(s.status, self.tick)
                g = git_info(s.cwd, self.tick); ps = ps_info(s.pid); mdl = model_of(s.sid)
                ctx = context_tokens(s.sid)
                name = s.display_name
                L.append("  " + fc[0] + "   " +
                         f"{T.coral_l}{B}PID {s.pid}{RST} {T.gray}{s.kind}{RST}   "
                         f"{T.cream}{B}❝ {pad(name, inner-34)}{B}❞{RST}")
                L.append("  " + fc[1] + "   " +
                         f"{T.teal}git{RST} {pad(g,20)} {T.teal}modelo{RST} {mdl} {T.gray}v{s.ver}{RST}   "
                         f"{T.teal}cpu/ram{RST} {pad(ps,14)} {T.teal}activa hace{RST} {s.uptime}")
                L.append("  " + fc[2] + "   " +
                         f"{T.teal}ctx{RST} {pad(fmt_tokens(ctx),6)} {T.teal}en estado{RST} hace {pad(fmt_age(s.upd),6)} "
                         f"{T.teal}→{RST} {pill(s.status, big=True)}")
                L.append("  " + f"{T.teal}dir{RST} {short_path(s.cwd)}")
                L.append("  " + f"{T.teal}haciendo{RST} {pad(s.activity, inner-12)}")
                L.append("  " + f"{T.teal}uso{RST}      {sparkline(s.pid, 24)}")
            else:
                L.append(f"  {T.gray}(sin sesiones que mostrar){RST}")
                for _ in range(5): L.append("")

        # ----- pad + línea de stats del sistema + footer -----
        while len(L) < rows - 2: L.append("")
        L = L[:rows-2]
        L.append("  " + self._statsline([s.pid for s in sessions if s.is_alive]))
        L.append("  " + self._footer())
        return L

    def _statsline(self, pids):
        cpu, rss, mem, load = pc_stats(pids)
        seg = (f"{T.coral_d}🖥 PC{RST}  "
               f"{T.gray}Claude:{RST} {T.teal}{cpu:.0f}% cpu{RST} {T.gray}·{RST} {T.teal}{fmt_mb(rss)}{RST}   "
               f"{T.gray}Sistema:{RST} ")
        if mem is not None:
            mc = T.red if mem >= 85 else T.yellow if mem >= 65 else T.green
            seg += f"{mc}RAM {mem:.0f}%{RST} {T.gray}·{RST} "
        if load is not None:
            lc = T.red if load >= os.cpu_count() else T.yellow if load >= os.cpu_count()/2 else T.green
            seg += f"{lc}load {load:.2f}{RST}"
        return seg

    def _band(self, content, inner):
        return f"{T.coral}│{RST} " + pad(content, inner-1) + f"{T.coral}│{RST}"

    def _cols(self, *pairs):
        return "".join(pad(t, w) + " " for t, w in pairs)

    def _footer(self):
        if self.confirm is not None:
            return (f"{bg(150,50,50)}{fg(255,235,235)}{B}"
                    f"  ¿Matar PID {self.confirm}?   (y) sí    (n) no  {RST}")
        keys = [("↵","ir"),("↑↓","nav"),("x","matar"),("s/S","orden"),("/","filtro"),
                ("g","grupos"),("C","compacto"),("c","limpiar"),("t","tema"),
                ("m","sonido"),("n","notif"),("d","muertas"),("q","salir")]
        f = " ".join(f"{T.coral}{k}{RST}{DIM}{v}{RST}" for k, v in keys)
        if self.msg:
            return f"{T.yellow}{B}{self.msg}{RST}    " + f
        return f

    # ---- actions ----
    def jump(self):
        if not self.rowpids: return
        pid = self.rowpids[self.sel]
        s_cwd = CA_cwd.get(pid, "")
        # tmux
        if which("tmux") and run_ok(["tmux","info"]):
            try:
                out = subprocess.run(["tmux","list-panes","-a","-F","#{pane_pid} #{session_name}:#{window_index}.#{pane_index}"],
                                     capture_output=True, text=True).stdout
                panes = {}
                for ln in out.splitlines():
                    p = ln.split()
                    if len(p) == 2: panes[p[0]] = p[1]
                cur, guard = str(pid), 0
                while cur and cur != "0" and guard < 40:
                    if cur in panes:
                        tgt = panes[cur]
                        subprocess.run(["tmux","select-window","-t",tgt]); subprocess.run(["tmux","select-pane","-t",tgt])
                        subprocess.run(["tmux","switch-client","-t",tgt.split(":")[0]])
                        self.msg = f"↪ tmux {tgt}"; return
                    cur = ppid_of(cur); guard += 1
            except Exception: pass
        # wmctrl
        if which("wmctrl") and os.environ.get("DISPLAY"):
            proj = os.path.basename(s_cwd) or s_cwd
            try:
                out = subprocess.run(["wmctrl","-l"], capture_output=True, text=True).stdout
                for ln in out.splitlines():
                    if proj.lower() in ln.lower():
                        subprocess.run(["wmctrl","-i","-a", ln.split()[0]])
                        self.msg = f"↪ ventana '{proj}'"; return
            except Exception: pass
        self.msg = "no pude saltar (usá tmux o instalá wmctrl)"

    def ask_kill(self):
        if self.rowpids: self.confirm = self.rowpids[self.sel]
    def do_kill(self, yes):
        pid = self.confirm; self.confirm = None
        if not yes or pid is None: self.msg = "cancelado"; return
        try: os.kill(pid, signal.SIGTERM); self.msg = f"✖ cierre enviado a PID {pid}"
        except OSError: self.msg = f"PID {pid} ya no existe"
    def clean_dead(self):
        c = 0
        for f in glob.glob(os.path.join(SESS_DIR, "*.json")):
            try:
                with open(f) as fh: pid = int(json.load(fh).get("pid",0))
            except Exception: continue
            if not alive(pid):
                try: os.remove(f); c += 1
                except OSError: pass
        self.msg = f"🧹 limpiadas {c} muertas"

CA_cwd = {}  # pid -> cwd, para jump (poblado en collect via render)

def which(cmd):
    return subprocess.run(["which",cmd], capture_output=True).returncode == 0
def run_ok(args):
    try: return subprocess.run(args, capture_output=True).returncode == 0
    except Exception: return False
def ppid_of(pid):
    try:
        with open(f"/proc/{pid}/stat") as f: return f.read().split()[3]
    except Exception: return "0"

# poblar CA_cwd dentro de collect
_orig_collect = collect
def collect(app):  # noqa: F811
    ss = _orig_collect(app)
    for s in ss: CA_cwd[s.pid] = s.cwd
    return ss

# ───────────────────────── input / loop ───────────────────────────────────
def clip(s, width):
    """recorta a `width` columnas visibles sin cortar secuencias ANSI."""
    out, w, i, n = [], 0, 0, len(s)
    while i < n:
        if s[i] == "\x1b":
            j = s.find("m", i)
            if j == -1: break
            out.append(s[i:j+1]); i = j + 1; continue
        cw = disp_width(s[i])
        if w + cw > width: break
        out.append(s[i]); w += cw; i += 1
    return "".join(out) + RST

def draw(lines):
    cols = os.get_terminal_size()[0]
    sys.stdout.write("\x1b[H")
    sys.stdout.write("\r\n".join(clip(l, cols) + "\x1b[K" for l in lines))
    sys.stdout.write("\x1b[J")
    sys.stdout.flush()

def run_tui(app):
    import termios, tty
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    sys.stdout.write("\x1b[?1049h\x1b[?25l")  # alt screen + hide cursor
    ensure_sounds()
    if app.sound_on: play_sound("start")
    try:
        tty.setraw(fd)
        while True:
            draw(app.render())
            app.tick += 1; app.msg = ""
            r,_,_ = select.select([fd], [], [], app.interval)
            if not r: continue
            ch = os.read(fd, 1).decode("utf-8","replace")
            if ch in ("\x03", "\x04"):  # Ctrl+C / Ctrl+D -> salir
                break
            if app.confirm is not None:
                app.do_kill(ch in ("y","Y")); continue
            def nav(delta):
                new = max(0, app.sel + delta)
                if new != app.sel and app.sound_on: play_sound("nav")
                app.sel = new
            if ch == "\x1b":
                seq = os.read(fd, 2).decode("utf-8","replace")
                if seq == "[A": nav(-1)
                elif seq == "[B": nav(1)
                continue
            if ch in ("\r","\n"): app.jump()
            elif ch in ("k","K"): nav(-1)
            elif ch in ("j","J"): nav(1)
            elif ch in ("x","X"): app.ask_kill()
            elif ch == "s": app.sort_i = (app.sort_i+1) % len(SORT_FIELDS)
            elif ch == "S": app.sort_rev = not app.sort_rev
            elif ch in ("g","G"): app.group = not app.group
            elif ch == "C": app.compact = not app.compact
            elif ch == "c": app.clean_dead()
            elif ch in ("m","M"): app.sound_on = not app.sound_on
            elif ch in ("n","N"): app.notif_on = not app.notif_on
            elif ch in ("t","T"):
                app.theme_i = (app.theme_i + 1) % len(THEME_NAMES)
                apply_theme(THEME_NAMES[app.theme_i])
            elif ch in ("d","D"): app.show_dead = not app.show_dead
            elif ch == "/":
                termios.tcsetattr(fd, termios.TCSADRAIN, old)
                sys.stdout.write("\x1b[?25h"); sys.stdout.write(f"\x1b[{os.get_terminal_size()[1]};1H\x1b[K{T.yellow}/ filtro: {RST}")
                sys.stdout.flush()
                try: app.filter = sys.stdin.readline().strip()
                except Exception: pass
                tty.setraw(fd); sys.stdout.write("\x1b[?25l")
            elif ch in ("q","Q"): break
    finally:
        if app.sound_on: play_sound("quit")
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        sys.stdout.write("\x1b[?25h\x1b[?1049l"); sys.stdout.flush()

# ───────────────────────── non-interactive ────────────────────────────────
def emit_json(app):
    out = []
    for s in collect(app):
        out.append(dict(pid=s.pid, status=s.status, kind=s.kind, cwd=s.cwd,
                        statusUpdatedAt=s.upd, activity=s.activity,
                        git=git_info(s.cwd, 0), model=model_of(s.sid), version=s.ver))
    print(json.dumps(out, ensure_ascii=False))

def emit_status(app):
    c = defaultdict(int)
    for s in collect(app): c[s.status] += 1
    parts = []
    if c["waiting"]: parts.append(f"◔{c['waiting']}")
    parts += [f"◐{c['busy']}", f"●{c['idle']}"]
    if c["blocked"]: parts.append(f"■{c['blocked']}")
    print(" ".join(parts))

def emit_once(app):
    app.tick = 3
    print("\n".join(ANSI_RE.sub("", l) if not sys.stdout.isatty() else l for l in app.render()))

# ───────────────────────── main ───────────────────────────────────────────
def main():
    args = sys.argv[1:]
    mode, interval = "tui", 1.0
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--once": mode = "once"
        elif a == "--json": mode = "json"
        elif a == "--status": mode = "status"
        elif a == "--interval": i += 1; interval = float(args[i])
        elif a.replace(".","",1).isdigit(): interval = float(a)
        i += 1
    app = App(interval, mode)
    if mode == "json":   emit_json(app);  return
    if mode == "status": emit_status(app); return
    if mode == "once":   emit_once(app);  return
    try: run_tui(app)
    except KeyboardInterrupt: pass

if __name__ == "__main__":
    main()
