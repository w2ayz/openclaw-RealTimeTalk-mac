"""Radio-interface registry for RealTimeTalk (Mac).

Identifies whether the AIOC (All-In-One-Cable) USB ham-radio dongle is
currently connected, and how to talk to it: its serial/PTT port VID:PID,
which serial line keys PTT, and how to find its CoreAudio input/output
devices. Imported by both RealTimeTalk-daemon.py and dtmf_monitor.py so
detection logic isn't duplicated.

Ported from the Pi build's radio_interfaces.py. AIOC-only — Digirig Mobile
support isn't included: its CM108 codec reports the generic product string
"C-Media Electronics Inc. USB PnP Sound Device", which collides with
unrelated USB mics (confirmed on this Mac's own desk mic) and needs
IOKit-based USB topology correlation to disambiguate, since the Pi
implementation's fix (`/proc/asound/cardN/usbid`) is ALSA/Linux-only with
no macOS equivalent. Add a second RadioInterface entry (with an
audio-resolution strategy for its generic name) if Digirig is ever needed.

AIOC's audio identification is simple by comparison: its firmware reports a
unique product string ("AIOC"/"All-In-One-Cable"), so a substring match
against sounddevice's device names is reliable — no ALSA/PipeWire-specific
correlation needed, confirmed via CoreAudio on this hardware.
"""

from __future__ import annotations

from dataclasses import dataclass
import struct

try:
    import hid
    _HAVE_HID = True
except ImportError:
    _HAVE_HID = False


@dataclass
class RadioInterface:
    name: str                    # "AIOC" — used in log lines and UI labels
    usb_vid: int
    usb_pid: int                 # VID:PID of the serial/PTT port
    ptt_line: str                # "dtr" or "rts"
    ptt_prekey_ms: int
    ptt_tail_ms: int
    audio_name_hints: list[str] | None = None
    cos_threshold: int = 200
    # Raw int16 peak threshold used by both the DTMF listener and EchoTest
    # listener to decide "a transmission is present" — there is no real
    # hardware squelch/COS signal exposed by AIOC in this setup, so this is
    # pure audio-level inference (see RADIO-INTERFACE.md on the Pi repo for
    # the full rationale — same reasoning applies here).


RADIO_INTERFACES = [
    RadioInterface(
        name="AIOC", usb_vid=0x1209, usb_pid=0x7388,
        ptt_line="dtr", ptt_prekey_ms=250, ptt_tail_ms=400,
        audio_name_hints=["AIOC", "All-In-One-Cable"],
    ),
]


def find_radio_port() -> tuple[RadioInterface, str] | None:
    """Return (interface, device_path) for whichever registered interface's
    serial/PTT port is currently plugged in, or None if none are."""
    try:
        from serial.tools import list_ports
    except ImportError:
        return None
    ports = list(list_ports.comports())
    for iface in RADIO_INTERFACES:
        for p in ports:
            if p.vid == iface.usb_vid and p.pid == iface.usb_pid:
                return iface, p.device
    return None


def find_radio_audio_devices(
    iface: RadioInterface | None = None,
) -> tuple[RadioInterface, int | None, int | None] | None:
    """Return (interface, input_index, output_index) for whichever registered
    interface's CoreAudio devices are currently visible via sounddevice, or
    None if none are. Either index may be None if that direction isn't
    exposed as a separate device (matches how AIOC currently splits into a
    input-only and an output-only sounddevice entry on this hardware)."""
    import sounddevice as sd
    try:
        devices = sd.query_devices()
    except Exception:
        return None
    for i in ([iface] if iface else RADIO_INTERFACES):
        if not i.audio_name_hints:
            continue
        in_idx = out_idx = None
        for idx, d in enumerate(devices):
            if not any(h in d["name"] for h in i.audio_name_hints):
                continue
            if d["max_input_channels"] > 0 and in_idx is None:
                in_idx = idx
            if d["max_output_channels"] > 0 and out_idx is None:
                out_idx = idx
        if in_idx is not None or out_idx is not None:
            return i, in_idx, out_idx
    return None


# AIOC HID feature-report config interface (firmware >= 1.3.0, shipped starting
# with hardware v1.2). Report format "<BBBL>" = [report_id, command, address,
# value] — mirrors github.com/hrafnkelle/aioc-util, itself derived from
# skuep/AIOC's own config protocol. Reading register 0x00 (MAGIC) back as
# "AIOC" is just a liveness check: older AIOC units (hardware v1.0) run
# firmware that predates this interface entirely and don't respond to it at
# all, which is the actual signal used below — not the register value itself.
# On macOS this needs the `hid` package's native hidapi library discoverable
# at runtime — see README for the `brew install hidapi` + DYLD_LIBRARY_PATH
# setup this depends on.
_AIOC_HID_FMT      = struct.Struct("<BBBL")
_AIOC_MAGIC_REG    = 0x00
_AIOC_MAGIC_VALUE  = 0x434F4941  # "AIOC" packed little-endian

_hw_variant_cache: dict[str, str] = {}  # USB serial number -> display label


def detect_hw_variant(iface: RadioInterface) -> str:
    """Best-effort hardware-revision label for `iface`, for display only —
    never use this for detection logic (that stays on iface.name/audio hints).
    Returns iface.name unchanged unless this is an AIOC that answers the HID
    config interface, in which case it returns "AIOC v1.2+". Result is cached
    per USB serial number so repeated dashboard refreshes don't re-open the
    HID handle every time."""
    if not _HAVE_HID or iface.name != "AIOC":
        return iface.name
    try:
        devices = hid.enumerate(iface.usb_vid, iface.usb_pid)
    except Exception:
        return iface.name
    if not devices:
        return iface.name
    serial = devices[0].get("serial_number") or ""
    cached = _hw_variant_cache.get(serial)
    if cached:
        return cached
    label = iface.name
    try:
        dev = hid.Device(iface.usb_vid, iface.usb_pid)
        try:
            dev.send_feature_report(_AIOC_HID_FMT.pack(0, 0x00, _AIOC_MAGIC_REG, 0))
            data = dev.get_feature_report(0, 7)
            _, _, _, value = _AIOC_HID_FMT.unpack(bytes(data))
            if value == _AIOC_MAGIC_VALUE:
                label = f"{iface.name} v1.2+"
        finally:
            dev.close()
    except Exception:
        pass
    if serial:
        _hw_variant_cache[serial] = label
    return label


class SquelchTracker:
    """Adaptive squelch/COS state from raw audio peak samples. `base_threshold`
    (RadioInterface.cos_threshold) is a floor, never lowered below — hand-tune
    against the interface's worst-case measured idle noise. The *effective*
    threshold can rise above that floor via an EMA of the ambient peak level,
    tracked only from ticks that are already below the current threshold
    (never learns from a tick that looks like a real signal — same principle
    as SPEAK_COUPLING_EMA in the daemon's speak() self-interrupt tracking),
    so a noisier environment / AGC drift raises the bar instead of causing
    false opens, and it settles back down once things quiet down.

    One instance per listener/reconnect — no shared/cross-thread state."""

    def __init__(self, base_threshold: int, tail_s: float,
                 margin: float = 1.5, ema_alpha: float = 0.02):
        self.base_threshold = base_threshold
        self.tail_s = tail_s
        self.margin = margin
        self.ema_alpha = ema_alpha
        self.ambient_ema = base_threshold / margin
        self._cos_until = 0.0

    @property
    def threshold(self) -> int:
        return max(self.base_threshold, int(self.ambient_ema * self.margin))

    def update(self, peak: int, now: float) -> bool:
        """Feed one peak sample, return whether squelch/COS is open now."""
        thr = self.threshold
        if peak > thr:
            self._cos_until = now + self.tail_s
        else:
            self.ambient_ema = (self.ambient_ema * (1 - self.ema_alpha) +
                                 peak * self.ema_alpha)
        return now < self._cos_until
