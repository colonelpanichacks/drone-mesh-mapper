#!/usr/bin/env python3
"""
Mesh-Detect C5 -- Firmware Flasher (XIAO ESP32-C5)

Drop your .bin in the firmware/ folder (or pass a path), plug in your
XIAO ESP32-C5, and run:

    python flash.py

Supports batch flashing -- after each board finishes, swap it out and
press Enter to flash the next one. Great for production runs.

Works on macOS, Linux, and Windows.

Requirements:  pip install esptool pyserial
"""

import glob
import io
import os
import sys
import time
import subprocess
import serial.tools.list_ports

# Force UTF-8 on Windows so box-drawing / special chars don't garble
if sys.platform == "win32":
    os.system("")  # enable ANSI/VT100 on Win10+ terminals
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    else:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# -- Config ----------------------------------------------------------------
# XIAO ESP32-C5: RISC-V, dual-band WiFi 6, different flash settings from S3
APP_OFFSET    = "0x10000"
BAUD          = "115200"
CHIP          = "esp32c5"
FIRMWARE_DIR  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "firmware")

# Known USB VID:PID pairs for ESP32-S3 / common UART bridges
ESP_VIDS = {
    "303A",  # Espressif USB JTAG/serial
    "1A86",  # CH340/CH341
    "10C4",  # CP210x
    "0403",  # FTDI
}

BANNER = """
  +==========================================+
  |   Mesh-Detect -- C5 Flasher              |
  +==========================================+"""


def find_port():
    """Auto-detect the ESP32 serial port (macOS, Linux, Windows)."""
    ports = serial.tools.list_ports.comports()
    candidates = []
    for p in ports:
        vid = f"{p.vid:04X}" if p.vid else ""
        if vid in ESP_VIDS:
            candidates.append(p)
        elif "esp" in (p.description or "").lower():
            candidates.append(p)
        # macOS
        elif "usbmodem" in (p.device or "").lower():
            candidates.append(p)
        elif "usbserial" in (p.device or "").lower():
            candidates.append(p)
        # Linux
        elif "ttyACM" in (p.device or ""):
            candidates.append(p)
        elif "ttyUSB" in (p.device or ""):
            candidates.append(p)
        # Windows — COM ports with a real description (skip built-in COM1)
        elif sys.platform == "win32" and (p.device or "").upper().startswith("COM"):
            port_num = p.device.upper().replace("COM", "")
            if port_num.isdigit() and int(port_num) > 1:
                candidates.append(p)

    if len(candidates) == 1:
        return candidates[0].device
    if len(candidates) > 1:
        print("\n  Multiple serial ports found:\n")
        for i, p in enumerate(candidates):
            desc = p.description or "unknown"
            vid = f"{p.vid:04X}" if p.vid else "----"
            print(f"    [{i + 1}] {p.device}  ({desc})  VID:{vid}")
        print()
        while True:
            try:
                choice = input("  Pick a port [1]: ").strip()
                idx = int(choice) - 1 if choice else 0
                if 0 <= idx < len(candidates):
                    return candidates[idx].device
            except (ValueError, IndexError):
                pass
            print("  Invalid choice, try again.")
    return None


def wait_for_port(timeout=30):
    """Wait for an ESP32 to appear on USB. Returns port or None."""
    print(f"\n  Waiting for ESP32 (plug in a board)...", end="", flush=True)
    start = time.time()
    last_dot = start
    while time.time() - start < timeout:
        port = find_port()
        if port:
            print(f" found!")
            return port
        if time.time() - last_dot >= 2:
            print(".", end="", flush=True)
            last_dot = time.time()
        time.sleep(0.5)
    print(" timeout.")
    return None


def find_firmware(path_arg=None):
    """Locate the .bin file to flash."""
    # Explicit path from CLI
    if path_arg:
        if os.path.isfile(path_arg):
            return os.path.abspath(path_arg)
        print(f"\n  File not found: {path_arg}")
        sys.exit(1)

    # Look in firmware/ folder
    if os.path.isdir(FIRMWARE_DIR):
        bins = sorted(glob.glob(os.path.join(FIRMWARE_DIR, "*.bin")), key=os.path.getmtime, reverse=True)
        if len(bins) == 1:
            return bins[0]
        if len(bins) > 1:
            print("\n  Multiple .bin files found:\n")
            for i, b in enumerate(bins):
                size = os.path.getsize(b) / 1024
                print(f"    [{i + 1}] {os.path.basename(b)}  ({size:.0f} KB)")
            print()
            while True:
                try:
                    choice = input("  Pick a firmware [1]: ").strip()
                    idx = int(choice) - 1 if choice else 0
                    if 0 <= idx < len(bins):
                        return bins[idx]
                except (ValueError, IndexError):
                    pass
                print("  Invalid choice, try again.")

    # Check current directory
    bins = sorted(glob.glob("*.bin"), key=os.path.getmtime, reverse=True)
    if bins:
        return os.path.abspath(bins[0])

    return None


def flash_one(port, firmware, do_erase=False, board_num=None):
    """Flash a single board. Returns True on success."""
    size_kb = os.path.getsize(firmware) / 1024
    label = f"  Board #{board_num}" if board_num else "  Target"
    print(f"""
{label}
  Port:       {port}
  Firmware:   {os.path.basename(firmware)}  ({size_kb:.0f} KB)
  Chip:       {CHIP}
  Offset:     {APP_OFFSET}
  Baud:       {BAUD}
""")

    if do_erase:
        erase(port)

    print("  Flashing...\n")

    cmd = [
        sys.executable, "-m", "esptool",
        "--chip", CHIP,
        "--port", port,
        "--baud", BAUD,
        "--before", "default_reset",
        "--after", "hard_reset",
        "write_flash",
        "-z",
        "--flash_mode", "dio",
        "--flash_freq", "80m",
        "--flash_size", "detect",
        APP_OFFSET, firmware,
    ]

    try:
        result = subprocess.run(cmd)
        if result.returncode == 0:
            print("\n  Done! Device will reboot into the new firmware.")
            return True
        else:
            print(f"\n  esptool exited with code {result.returncode}")
            return False
    except FileNotFoundError:
        print("  esptool not found. Install it:\n")
        print("    pip install esptool\n")
        sys.exit(1)


def erase(port):
    """Full flash erase."""
    print(f"  Erasing flash on {port}...\n")
    cmd = [
        sys.executable, "-m", "esptool",
        "--chip", CHIP,
        "--port", port,
        "--baud", BAUD,
        "erase_flash",
    ]
    subprocess.run(cmd)
    print()


def batch_mode(firmware, do_erase=False):
    """Flash multiple boards one after another."""
    print(BANNER)
    size_kb = os.path.getsize(firmware) / 1024
    print(f"""
  BATCH MODE -- flash boards one after another
  Firmware:   {os.path.basename(firmware)}  ({size_kb:.0f} KB)
  Erase:      {"YES" if do_erase else "no"}
  
  Plug in a board, flash, swap, repeat.
  Press Ctrl+C when done.
""")

    board_num = 0
    success_count = 0
    fail_count = 0

    while True:
        board_num += 1

        # Wait for a board to appear
        port = wait_for_port(timeout=300)  # 5 min timeout
        if not port:
            print("\n  No board detected. Still waiting? Plug one in and try again.")
            try:
                input("  Press Enter to retry, Ctrl+C to quit: ")
            except KeyboardInterrupt:
                break
            continue

        # Give the port a moment to stabilize (Windows especially needs this)
        time.sleep(1)

        ok = flash_one(port, firmware, do_erase=do_erase, board_num=board_num)
        if ok:
            success_count += 1
        else:
            fail_count += 1

        print(f"\n  -- Score: {success_count} flashed, {fail_count} failed --")

        try:
            resp = input("\n  Swap board and press Enter for next (q to quit): ").strip().lower()
            if resp in ("q", "quit", "exit"):
                break
        except (KeyboardInterrupt, EOFError):
            break

        # Wait for the old port to disappear and new one to appear
        print("  Waiting for board swap...", end="", flush=True)
        time.sleep(2)  # give time for USB disconnect
        print(" ready.")

    print(f"""
  +==========================================+
  |   Batch complete                         |
  +==========================================+
  |   Flashed:  {success_count:<5}                        |
  |   Failed:   {fail_count:<5}                        |
  +==========================================+
""")


def main():
    # Parse args
    args = sys.argv[1:]
    do_erase = "--erase" in args
    do_batch = "--batch" in args
    bin_path = None

    for a in args:
        if a in ("--erase", "--batch"):
            continue
        if a in ("-h", "--help"):
            print("""
  Usage:  python flash.py [firmware.bin] [--erase] [--batch]

  Options:
    firmware.bin   Path to .bin file (auto-detects from firmware/ folder)
    --erase        Erase entire flash before writing
    --batch        Batch mode: flash multiple boards one after another

  Single board:
    python flash.py

  Batch flash (production run):
    python flash.py --batch
    python flash.py --batch --erase

  Setup:
    pip install esptool pyserial
    mkdir firmware
    # drop your .bin in firmware/
    python flash.py
""")
            sys.exit(0)
        bin_path = a

    # Check esptool is installed
    try:
        subprocess.run([sys.executable, "-m", "esptool", "version"],
                       capture_output=True, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("\n  esptool not found. Install it:\n")
        print("    pip install esptool\n")
        sys.exit(1)

    # Find firmware first (same for all boards)
    firmware = find_firmware(bin_path)
    if not firmware:
        print(f"\n  No .bin file found.")
        print(f"  Drop your firmware in:  {FIRMWARE_DIR}/")
        print(f"  Or pass it directly:    python flash.py my_firmware.bin\n")
        sys.exit(1)

    # Batch mode
    if do_batch:
        batch_mode(firmware, do_erase=do_erase)
        return

    # Single mode — find port
    print(BANNER)
    port = find_port()
    if not port:
        print("\n  No ESP32 detected. Is the board plugged in?")
        print("  Make sure drivers are installed (CH340/CP210x).\n")
        sys.exit(1)

    print(f"  Found: {port}")

    confirm = input("\n  Flash? [Y/n]: ").strip().lower()
    if confirm and confirm != "y":
        print("  Aborted.")
        sys.exit(0)

    ok = flash_one(port, firmware, do_erase=do_erase)
    if not ok:
        sys.exit(1)

    # Offer to flash another
    try:
        resp = input("\n  Flash another board? [y/N]: ").strip().lower()
        if resp == "y":
            batch_mode(firmware, do_erase=do_erase)
    except (KeyboardInterrupt, EOFError):
        pass

    print()


if __name__ == "__main__":
    main()
