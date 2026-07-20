#!/usr/bin/env python3
"""Isolation test for the speak() pipeline — no OpenAI key needed.

Tests Edge TTS → ffmpeg decode → sounddevice.play() on the local CoreAudio output.
Falls back to `say` if Edge TTS fails.

Usage:
    ./venv/bin/python3 test_speak.py [--device N] [--text "..."] [--fallback]
"""
from __future__ import annotations
import argparse
import sys
import os

import importlib.util

# Daemon file has hyphens in name — use importlib to load it directly.
_daemon_path = os.path.join(os.path.dirname(__file__), "RealTimeTalk-daemon.py")
_spec = importlib.util.spec_from_file_location("rtd", _daemon_path)
d = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(d)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--device", type=int, default=None,
                        help="CoreAudio output device index (default: system default)")
    parser.add_argument("--text", default="Hello, this is Zeebot. The speak pipeline is working correctly.",
                        help="Text to speak")
    parser.add_argument("--zh", action="store_true",
                        help="Use a Chinese test phrase instead")
    parser.add_argument("--fallback", action="store_true",
                        help="Force say fallback (skip Edge TTS)")
    parser.add_argument("--volume", type=float, default=0.8,
                        help="Software volume 0.0-1.0 (default: 0.8)")
    args = parser.parse_args()

    if args.zh:
        args.text = "你好，我是Zeebot。语音合成流程已经正常工作了。"

    # List devices
    print("CoreAudio devices:")
    devs = d.sd.query_devices()
    for i, dev in enumerate(devs):
        marker = " ←" if i == args.device else ""
        direction = []
        if dev["max_input_channels"]:  direction.append("in")
        if dev["max_output_channels"]: direction.append("out")
        print(f"  [{i}] {dev['name']}  ({'/'.join(direction)}){marker}")

    # Set output device
    d._selected_output_device[0] = args.device
    d._cal_sw_volume = args.volume
    print(f"\nOutput device: {args.device!r} (None = system default)")
    print(f"Volume: {args.volume}")
    print(f"Text: {args.text!r}\n")

    if args.fallback:
        print("--- Force-testing say fallback ---")
        import tempfile
        import numpy as np
        aiff = tempfile.mktemp(suffix=".aiff")
        lang = "zh" if args.zh else "en"
        ok = d._say_fallback_to_aiff(args.text, lang, aiff)
        print(f"say → {aiff}: {'OK' if ok else 'FAILED'}")
        if ok:
            pcm = d._decode_to_pcm(aiff)
            print(f"ffmpeg decode: {pcm.size} samples ({pcm.size / d.TTS_SAMPLE_RATE:.1f}s)")
            if pcm.size:
                d.sd.play(pcm, samplerate=d.TTS_SAMPLE_RATE, device=args.device)
                d.sd.wait()
                print("Playback complete.")
            os.unlink(aiff)
        return

    print("--- Testing full speak() pipeline ---")
    # speak() reads _mic_level_current[0] for interrupt detection.
    # In test mode there's no mic stream, so the value stays 0 → no interrupt.
    d.speak(args.text, volume=args.volume)
    print("speak() returned.")

if __name__ == "__main__":
    main()
