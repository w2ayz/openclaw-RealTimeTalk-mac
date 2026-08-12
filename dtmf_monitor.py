#!/usr/bin/env python3
"""
Real-Time DTMF Monitor with training mode (Mac port). Works over any
registered radio interface — see radio_interfaces.py (AIOC only on Mac).

Normal:   python3 dtmf_monitor.py
Training: python3 dtmf_monitor.py --train
Retrain:  python3 dtmf_monitor.py --retrain
Profiles: ~/.openclaw/workspace/rtt_dtmf_profiles.json

Ported from the Pi build's dtmf_monitor.py. The Goertzel/FFT tone-detection
core and training-profile logic are unchanged (pure math over PCM samples,
no OS dependency) — only the audio capture source changes: Pi pipes raw
PCM from pacat/PipeWire, this captures directly via sounddevice.InputStream
on the AIOC's CoreAudio input device (confirmed native 48kHz, no resampling
needed). Pi's multimon-ng fallback path (used when no profiles are trained
yet) isn't ported — that's a `pacat | sox | multimon-ng` PipeWire pipeline
with no macOS equivalent; `--train` first is required here instead.
"""
from __future__ import annotations

import threading, time, sys, argparse, os, json
import numpy as np
import sounddevice as sd
import radio_interfaces as _radio
from radio_interfaces import RADIO_INTERFACES, SquelchTracker

# ── Config ─────────────────────────────────────────────────────────────────
parser = argparse.ArgumentParser()
parser.add_argument('--train',         action='store_true', help='Train all digits')
parser.add_argument('--retrain',       action='store_true', help='Show trained profiles, pick digits to retrain')
parser.add_argument('--digits',        default='1234567890*#', help='Digits to train (default all)')
parser.add_argument('--samples',       type=int, default=5,   help='Samples per digit in training (default 5)')
parser.add_argument('--wake',          default='123')
parser.add_argument('--sleep',         default='321')
parser.add_argument('--deepsleep',     default='987')
parser.add_argument('--monitor-on',    default='456')
parser.add_argument('--monitor-off',   default='654')
parser.add_argument('--wake-silent',   default='789')
parser.add_argument('--cos-threshold', type=int,   default=None,
                     help='Raw int16 COS threshold (default: whichever radio interface is '
                          'currently connected uses its own tuned value from radio_interfaces.py; '
                          'falls back to 200 if none detected)')
parser.add_argument('--cos-tail',      type=float, default=0.5,  help='COS hold-open seconds (default 0.5)')
parser.add_argument('--profiles',      default=os.path.expanduser('~/.openclaw/workspace/rtt_dtmf_profiles.json'))
args = parser.parse_args()

WAKE_SEQ         = args.wake
SLEEP_SEQ        = args.sleep
DEEPSLEEP_SEQ    = args.deepsleep
MONITOR_ON_SEQ   = args.monitor_on
MONITOR_OFF_SEQ  = args.monitor_off
WAKE_SILENT_SEQ  = args.wake_silent

_cos_found = _radio.find_radio_audio_devices()
if args.cos_threshold is not None:
    COS_THRESHOLD = args.cos_threshold
else:
    COS_THRESHOLD = _cos_found[0].cos_threshold if _cos_found else 200
COS_TAIL_S    = args.cos_tail
SEQ_TIMEOUT   = 8.0
DIGIT_COOLDOWN= 0.4
PROFILE_FILE  = args.profiles
RATE          = 48000                        # AIOC's confirmed native CoreAudio rate
BLOCK_SAMPLES = RATE * 50 // 1000             # 50ms int16 mono block

# Standard DTMF frequencies (used by training's FFT detector, not affected by capture source)
STD_ROWS = [697, 770, 852, 941]
STD_COLS = [1209, 1336, 1477, 1633]
STD_MAP  = {
    (697,1209):'1',(697,1336):'2',(697,1477):'3',(697,1633):'A',
    (770,1209):'4',(770,1336):'5',(770,1477):'6',(770,1633):'B',
    (852,1209):'7',(852,1336):'8',(852,1477):'9',(852,1633):'C',
    (941,1209):'*',(941,1336):'0',(941,1477):'#',(941,1633):'D',
}

# ── Profile I/O ────────────────────────────────────────────────────────────
def load_profiles():
    try:
        with open(PROFILE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def save_profiles(profiles):
    os.makedirs(os.path.dirname(PROFILE_FILE), exist_ok=True)
    with open(PROFILE_FILE, 'w') as f:
        json.dump(profiles, f, indent=2)
    print(f"\n  Profiles saved → {PROFILE_FILE}")

# ── Goertzel ───────────────────────────────────────────────────────────────
def goertzel_energy(samples, freq, rate):
    n = len(samples)
    k = int(0.5 + n * freq / rate)
    w = 2 * np.pi * k / n
    c = 2 * np.cos(w)
    q1 = q2 = 0.0
    for s in samples:
        q0 = s + c*q1 - q2; q2=q1; q1=q0
    return q2*q2 + q1*q1 - c*q1*q2

def decode_with_profiles(frame, profiles):
    """Decode a DTMF digit using learned frequency profiles."""
    samples = frame.astype(np.float64).tolist()
    scores = {}
    for digit, prof in profiles.items():
        row_e = goertzel_energy(samples, prof['row_hz'], RATE)
        col_e = goertzel_energy(samples, prof['col_hz'], RATE)
        scores[digit] = row_e + col_e
    if not scores:
        return None
    best = max(scores, key=scores.get)
    best_e = scores[best]
    # Must be significantly above median
    median_e = sorted(scores.values())[len(scores)//2]
    if best_e < 1e6 or (median_e > 0 and best_e / median_e < 3.0):
        return None
    return best

def decode_frame_fft(frame):
    """Extract dominant row + col frequencies via FFT."""
    f = frame.astype(np.float64)
    fft = np.abs(np.fft.rfft(f))
    freqs = np.fft.rfftfreq(len(f), 1/RATE)
    # Row band 600-1000 Hz
    row_mask = (freqs >= 600) & (freqs <= 1000)
    col_mask = (freqs >= 1100) & (freqs <= 1700)
    if not row_mask.any() or not col_mask.any():
        return None, None
    row_freq = freqs[row_mask][np.argmax(fft[row_mask])]
    col_freq = freqs[col_mask][np.argmax(fft[col_mask])]
    row_e = np.max(fft[row_mask])
    col_e = np.max(fft[col_mask])
    if row_e < 100 or col_e < 100:
        return None, None
    return float(row_freq), float(col_freq)

# ── Shared state ───────────────────────────────────────────────────────────
state = {'cos':False,'level':0,'threshold':COS_THRESHOLD,'digits':[],'seq':'',
         'last_digit':None,'last_time':0.0,'actions':[]}
lock = threading.Lock()
raw_buf = []
raw_lock = threading.Lock()

# ── Raw audio capture (shared by COS + training) ──────────────────────────
def raw_capture_thread(in_idx=None):
    """Feeds `state`/`raw_buf` from the AIOC's CoreAudio input, mirroring
    Pi's pacat-based raw_capture_thread. Re-resolves the device on each
    reconnect if none was passed in; retries every couple seconds while
    the radio is unplugged."""
    squelch = SquelchTracker(COS_THRESHOLD, COS_TAIL_S)

    def _cb(indata, frames, time_info, status):
        frame = indata[:, 0]
        peak  = int(np.max(np.abs(frame))) if len(frame) else 0
        now   = time.time()
        with lock:
            state['level'] = peak
            state['cos'] = squelch.update(peak, now)
            state['threshold'] = squelch.threshold
        with raw_lock:
            raw_buf.append((now, frame.copy()))
            cutoff = now - 10
            while raw_buf and raw_buf[0][0] < cutoff:
                raw_buf.pop(0)

    while True:
        idx = in_idx
        if idx is None:
            found = _radio.find_radio_audio_devices()
            if not found or found[1] is None:
                time.sleep(2); continue
            idx = found[1]
        try:
            with sd.InputStream(device=idx, channels=1, samplerate=RATE,
                                 dtype='int16', blocksize=BLOCK_SAMPLES,
                                 callback=_cb):
                while True:
                    time.sleep(0.5)
        except Exception:
            pass
        time.sleep(1)

# ── DTMF detection thread ──────────────────────────────────────────────────
def dtmf_thread(profiles):
    # Custom Goertzel loop using learned profiles — this is the only decode
    # path on Mac (see module docstring re: dropped multimon-ng fallback).
    last_frame_time = [0.0]
    FRAME = RATE // 10  # 100ms analysis window
    prev_digit = [None]
    hold = [0]
    while True:
        time.sleep(0.025)
        if not state['cos']:
            prev_digit[0] = None; hold[0] = 0; continue
        with raw_lock:
            recent = [(t,f) for t,f in raw_buf if t > time.time()-0.15]
        if not recent: continue
        frames = np.concatenate([f for _,f in recent])
        if len(frames) < FRAME: continue
        digit = decode_with_profiles(frames[-FRAME:], profiles)
        if digit == prev_digit[0]:
            hold[0] += 1
        else:
            prev_digit[0] = digit; hold[0] = 1
        if digit and hold[0] == 3:  # stable for ~75ms
            _accept_digit(digit)

def _accept_digit(digit):
    now = time.time()
    with lock:
        if digit == state['last_digit'] and now-state['last_time'] < DIGIT_COOLDOWN: return
        if state['seq'] and now-state['last_time'] > SEQ_TIMEOUT: state['seq'] = ""
        state['last_digit'] = digit; state['last_time'] = now
        if not state['seq'] or state['seq'][-1] != digit:
            state['seq'] += digit
            state['digits'].append((now, digit))
        ml = max(len(WAKE_SEQ), len(SLEEP_SEQ), len(DEEPSLEEP_SEQ),
                 len(MONITOR_ON_SEQ), len(MONITOR_OFF_SEQ), len(WAKE_SILENT_SEQ))
        if len(state['seq']) > ml: state['seq'] = state['seq'][-ml:]
        if WAKE_SEQ in state['seq']:
            state['seq'] = ""
            msg = f"[{time.strftime('%H:%M:%S')}] *** WAKE '{WAKE_SEQ}' detected!"
            state['actions'].append(msg); print(f"\n\033[32m{msg}\033[0m")
        elif DEEPSLEEP_SEQ in state['seq']:
            state['seq'] = ""
            msg = f"[{time.strftime('%H:%M:%S')}] *** DEEP SLEEP '{DEEPSLEEP_SEQ}' detected!"
            state['actions'].append(msg); print(f"\n\033[31m{msg}\033[0m")
        elif SLEEP_SEQ in state['seq']:
            state['seq'] = ""
            msg = f"[{time.strftime('%H:%M:%S')}] *** SLEEP '{SLEEP_SEQ}' detected!"
            state['actions'].append(msg); print(f"\n\033[33m{msg}\033[0m")
        elif MONITOR_ON_SEQ in state['seq']:
            state['seq'] = ""
            msg = f"[{time.strftime('%H:%M:%S')}] *** MONITOR ON '{MONITOR_ON_SEQ}' detected!"
            state['actions'].append(msg); print(f"\n\033[36m{msg}\033[0m")
        elif MONITOR_OFF_SEQ in state['seq']:
            state['seq'] = ""
            msg = f"[{time.strftime('%H:%M:%S')}] *** MONITOR OFF '{MONITOR_OFF_SEQ}' detected!"
            state['actions'].append(msg); print(f"\n\033[36m{msg}\033[0m")
        elif WAKE_SILENT_SEQ in state['seq']:
            state['seq'] = ""
            msg = f"[{time.strftime('%H:%M:%S')}] *** WAKE SILENT '{WAKE_SILENT_SEQ}' detected!"
            state['actions'].append(msg); print(f"\n\033[94m{msg}\033[0m")

# ── Display thread ─────────────────────────────────────────────────────────
def display_thread(profiles):
    import shutil
    mode_lbl = "LEARNED"

    while True:
        with lock:
            cos   = state['cos']; level = state['level']; thr = state['threshold']
            digs  = [d for t,d in state['digits'] if time.time()-t<10]
            seq   = state['seq']

        cols  = shutil.get_terminal_size((80, 24)).columns
        bar_n = min(level*20//2000, 20)
        bar   = "█"*bar_n + "░"*(20-bar_n)
        dig_s = " ".join(digs[-8:]) if digs else "-"
        seq_s = " ".join(seq)        if seq  else "_"

        # Build plain line first so we know the exact visible width
        cos_p = "OPEN  " if cos else "CLOSED"
        plain = f"  COS:{cos_p}[{bar}]{level:6d}/{thr:<4d} | DTMF:{dig_s:<14}| Seq:{seq_s:<4} [{mode_lbl}]"
        if len(plain) > cols - 1:
            plain = plain[:cols-1]

        # Now inject ANSI colours at known positions in the plain string
        cos_col = "\033[32m" if cos else "\033[31m"
        mod_col = "\033[32m" if profiles else "\033[33m"
        line = (plain
                .replace(cos_p,    f"{cos_col}{cos_p}\033[0m", 1)
                .replace(mode_lbl, f"{mod_col}{mode_lbl}\033[0m", 1))

        sys.stdout.write(f"\r{line}\033[K")   # \033[K clears to end-of-line
        sys.stdout.flush()
        time.sleep(0.1)

# ══════════════════════════════════════════════════════════════════════════
# TRAINING MODE
# ══════════════════════════════════════════════════════════════════════════
def _ask(prompt, default='y'):
    """Single-keypress Y/N prompt. Returns True for yes."""
    import tty, termios, select as _sel
    sys.stdout.write(prompt); sys.stdout.flush()
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        while True:
            if _sel.select([sys.stdin], [], [], 0.05)[0]:
                ch = sys.stdin.read(1).lower()
                if ch in ('y', '\r', '\n', ' '):
                    sys.stdout.write("Y\n"); sys.stdout.flush(); return True
                if ch in ('n',):
                    sys.stdout.write("N\n"); sys.stdout.flush(); return False
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def run_training():
    found = _radio.find_radio_audio_devices()
    if not found or found[1] is None:
        print("No radio interface connected (looked for: " +
              ", ".join(i.name for i in RADIO_INTERFACES) + ")."); sys.exit(1)
    in_idx = found[1]

    profiles = load_profiles()
    digits   = list(args.digits)

    print(f"\n╔══════════════════════════════════════════════╗")
    print(f"║          DTMF TRAINING MODE                 ║")
    print(f"║  Digits : {args.digits:<35}║")
    print(f"║  Transmit each digit once — accept/reject   ║")
    print(f"║  1 accepted sample minimum, more = better   ║")
    print(f"╚══════════════════════════════════════════════╝\n")

    threading.Thread(target=raw_capture_thread, args=(in_idx,), daemon=True).start()
    time.sleep(1)

    for digit in digits:
        samples_row, samples_col = [], []
        print(f"\n── Digit \033[93m{digit}\033[0m {'─'*38}")
        if digit in profiles:
            p = profiles[digit]
            print(f"   Existing: row={p['row_hz']:.0f}Hz  col={p['col_hz']:.0f}Hz  "
                  f"(n={p['samples']})  — will update if you add more")

        want_more = True
        while want_more:
            print(f"   Transmit DTMF \033[93m{digit}\033[0m once (any duration) …")

            # Wait for a complete COS burst
            burst_active = False
            burst_frames = []
            last_cos = False
            got_burst = False

            while not got_burst:
                time.sleep(0.04)
                cos = state['cos']; lvl = state['level']
                bar_n = min(lvl*30//5000, 30)
                cos_s = "\033[32m●\033[0m" if cos else "○"
                sys.stdout.write(
                    f"\r   {cos_s} [{'█'*bar_n}{'░'*(30-bar_n)}] {lvl:6d}   ")
                sys.stdout.flush()

                if cos and not last_cos:
                    burst_active = True; burst_frames = []
                if burst_active and cos:
                    with raw_lock:
                        burst_frames += [f for t,f in raw_buf
                                         if t > time.time()-0.05]
                if not cos and last_cos and burst_active:
                    burst_active = False; got_burst = True
                last_cos = cos

            sys.stdout.write("\r" + " "*55 + "\r"); sys.stdout.flush()

            # Analyse burst
            row_hz = col_hz = None
            if burst_frames:
                full = np.concatenate(burst_frames)
                trim = max(len(full)//5, 1)
                mid  = full[trim:-trim] if len(full) > trim*3 else full
                if len(mid) > RATE//20:
                    row_hz, col_hz = decode_frame_fft(mid)

            if row_hz and col_hz:
                std = STD_MAP.get(
                    (min(STD_ROWS, key=lambda r: abs(r-row_hz)),
                     min(STD_COLS, key=lambda c: abs(c-col_hz))), '?')
                print(f"   Detected:  row=\033[96m{row_hz:.0f}\033[0mHz  "
                      f"col=\033[96m{col_hz:.0f}\033[0mHz  "
                      f"(std={std})  n={len(samples_row)+1}")
                if _ask("   Accept? [Y/n]: "):
                    samples_row.append(row_hz)
                    samples_col.append(col_hz)
                    print(f"   \033[32m✓ Accepted\033[0m  ({len(samples_row)} sample(s) so far)")
                else:
                    print(f"   \033[33m✗ Rejected — try again\033[0m")
            else:
                print(f"   \033[31m✗ Could not extract frequencies — try again\033[0m")

            if samples_row:
                want_more = _ask(f"   Transmit again for more confidence? [y/N]: " if len(samples_row)>=1 else "")
                if not want_more and len(samples_row) == 0:
                    want_more = True  # force at least one accepted sample
            # else loop back automatically

        # Save profile for this digit
        avg_row = float(np.median(samples_row))
        avg_col = float(np.median(samples_col))
        profiles[digit] = {
            'row_hz':  round(avg_row, 1),
            'col_hz':  round(avg_col, 1),
            'samples': len(samples_row),
        }
        std = STD_MAP.get((min(STD_ROWS, key=lambda r: abs(r-avg_row)),
                           min(STD_COLS, key=lambda c: abs(c-avg_col))), '?')
        print(f"   \033[32m✓ Digit {digit} saved\033[0m  "
              f"row={avg_row:.1f}Hz  col={avg_col:.1f}Hz  "
              f"std={std}  n={len(samples_row)}")

    save_profiles(profiles)
    print("\n\033[32mTraining complete!\033[0m  Run without --train to use profiles.\n")

# ══════════════════════════════════════════════════════════════════════════
# RETRAIN MODE — show profile table, pick digits to retrain
# ══════════════════════════════════════════════════════════════════════════
def run_retrain():
    import tty, termios, select as _sel

    ALL_DIGITS = list('1234567890*#ABCD')
    profiles   = load_profiles()

    def draw_table(selected):
        os.system('clear')
        SEP = "+-------+----------+----------+---------+------------------------+"
        HDR = "| Digit |  Row Hz  |  Col Hz  | Samples | Status                 |"
        print("\n  DTMF PROFILE TABLE — pick digits to retrain\n")
        print(f"  {SEP}")
        print(f"  {HDR}")
        print(f"  {SEP}")
        for d in ALL_DIGITS:
            mark = ">" if d in selected else " "
            if d in profiles:
                p = profiles[d]
                std_r = min(STD_ROWS, key=lambda r: abs(r-p['row_hz']))
                std_c = min(STD_COLS, key=lambda c: abs(c-p['col_hz']))
                std_d = STD_MAP.get((std_r, std_c), '?')
                drift = abs(p['row_hz']-std_r) + abs(p['col_hz']-std_c)
                if drift < 15:   qual_plain = "Good    "; qual_color = "\033[32mGood\033[0m    "
                elif drift < 50: qual_plain = "Offset  "; qual_color = "\033[33mOffset\033[0m  "
                else:            qual_plain = "Drift   "; qual_color = "\033[31mDrift\033[0m   "
                status_plain = f"{qual_plain} (std={std_d})"
                status_color = f"{qual_color} (std={std_d})"
                # Build plain line first to get correct width, then reinsert color
                line = (f"| {mark}{d:<2}  | {p['row_hz']:>7.1f}  |"
                        f" {p['col_hz']:>7.1f}  |   {p['samples']:>4}  |"
                        f" {status_plain:<22} |")
                # Replace plain qual with colored version for display
                line_display = line.replace(status_plain, status_color, 1)
                if d in selected:
                    line_display = "\033[93m" + line_display.replace(">","►",1) + "\033[0m"
            else:
                line_display = f"|  {d:<2}  | {'(not trained)':<8}  | {'':>7}  |   {'':>4}  | {'—':<22} |"
            print(f"  {line_display}")
        print(f"  {SEP}")
        sel_str = " ".join(sorted(selected, key=lambda x: ALL_DIGITS.index(x) if x in ALL_DIGITS else 99)) \
                  if selected else "(none)"
        print(f"\n  Selected: \033[93m{sel_str}\033[0m")
        print("\n  Keys:  digit/*/# = toggle  |  A = all trained  |  C = clear  |  Enter = start  |  Q = quit")

    selected = set()
    fd  = sys.stdin.fileno()
    old = termios.tcgetattr(fd)

    def read_key():
        """Read one keypress in raw mode, restore terminal after."""
        try:
            tty.setraw(fd)
            while True:
                if _sel.select([sys.stdin], [], [], 0.1)[0]:
                    return sys.stdin.read(1)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)

    while True:
        draw_table(selected)           # drawn in normal terminal mode → correct \r\n
        ch = read_key()                # brief raw mode for single keypress only
        if ch in ('\r', '\n'):
            break
        if ch.upper() == 'Q':
            print("\n"); return
        if ch.upper() == 'A':
            selected = set(d for d in ALL_DIGITS if d in profiles)
        elif ch.upper() == 'C':
            selected = set()
        elif ch.upper() in ALL_DIGITS or ch in ALL_DIGITS:
            selected ^= {ch.upper()}

    print()
    if not selected:
        print("No digits selected — nothing to retrain.")
        return

    # Override --digits with selection and run training
    args.digits = ''.join(sorted(selected, key=lambda d: ALL_DIGITS.index(d)
                                 if d in ALL_DIGITS else 99))
    run_training()


# ══════════════════════════════════════════════════════════════════════════
# NORMAL MONITOR MODE
# ══════════════════════════════════════════════════════════════════════════
def run_monitor():
    found = _radio.find_radio_audio_devices()
    if not found or found[1] is None:
        print("No radio interface connected (looked for: " +
              ", ".join(i.name for i in RADIO_INTERFACES) + ")."); sys.exit(1)
    _iface, in_idx, _ = found

    profiles = load_profiles()
    if not profiles:
        print("No DTMF profiles trained yet — run with --train first.")
        print("(Mac has no multimon-ng fallback; learned profiles are the only decode path.)")
        sys.exit(1)

    import shutil as _sh
    W = min(_sh.get_terminal_size((80,24)).columns, 72) - 2  # inner width
    def _row(text): return f"│ {text[:W-1]:<{W-1}} │"
    HR = "─" * W
    print(f"┌{HR}┐")
    print(_row(f"Radio DTMF Monitor   [ ESC / Q = quit ]"))
    print(f"├{HR}┤")
    try:
        _desc = sd.query_devices(in_idx)["name"]
    except Exception:
        _desc = _iface.name
    print(_row(f"Source   : {_desc}"))
    print(_row(f"Mode     : LEARNED PROFILES"))
    print(_row(f"Profiles : {len(profiles)} digits trained"))
    print(_row(f"Wake={WAKE_SEQ}  Silent={SLEEP_SEQ}  DeepSleep={DEEPSLEEP_SEQ}/{WAKE_SILENT_SEQ}  Mon={MONITOR_ON_SEQ}/{MONITOR_OFF_SEQ}"))
    print(f"├{HR}┤")
    print(_row("DTMF Command Reference:"))
    print(_row(f"  {WAKE_SEQ:<4} Wake         activate (cancels Sleep, goes fully active)"))
    print(_row(f"  {SLEEP_SEQ:<4} Sleep        go Silent (passive, 10-min idle disconnect)"))
    print(_row(f"  {DEEPSLEEP_SEQ:<4} Deep Sleep   disconnect immediately (skip 10-min wait)"))
    print(_row(f"  {WAKE_SILENT_SEQ:<4} Wake-Silent  wake from Deep Sleep into Silent (no routing)"))
    print(_row(f"  {MONITOR_ON_SEQ:<4} Monitor ON   start passive transcription monitoring"))
    print(_row(f"  {MONITOR_OFF_SEQ:<4} Monitor OFF  stop monitoring"))
    print(f"└{HR}┘")

    threading.Thread(target=raw_capture_thread, args=(in_idx,), daemon=True).start()
    threading.Thread(target=dtmf_thread,        args=(profiles,), daemon=True).start()
    threading.Thread(target=display_thread,     args=(profiles,), daemon=True).start()
    time.sleep(0.5)

    import tty, termios, select as _sel
    fd  = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        while True:
            if _sel.select([sys.stdin], [], [], 0.1)[0]:
                ch = sys.stdin.read(1)
                if ch in ('\x1b', 'q', 'Q'):
                    break
    except KeyboardInterrupt:
        pass
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        print("\n\nStopped.")

# ── Entry point ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if args.retrain:
        run_retrain()
    elif args.train:
        run_training()
    else:
        run_monitor()
