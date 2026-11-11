# BMW iDrive 6 emulator

This project boots the native BMW F32 iDrive 6 Light interface in patched
QEMU. It does not include BMW firmware. You supply an extracted PSdZData
directory containing the verified NBTevo X20511I packages.

## Quick start

The verified release host is Apple silicon macOS with Nix flakes enabled.
Intel/AMD and ARM Linux, including WSL2 on Windows, build from the same flake;
see [Linux and Windows (WSL2)](#linux-and-windows-wsl2) for the differences and
what has been tested there. Clone the repository, copy the example config, and set `psdzdata` to the
absolute path of your extracted PSdZData directory:

```sh
cp idrive6.example.toml idrive6.toml
```

```toml
psdzdata = "/path/to/psdzdata"
modules = ["head-unit", "6wb"]
```

For the first run, check and stage the firmware:

```sh
nix run .#idrive6 -- --prepare-only
```

Then start the selected modules:

```sh
nix run .#idrive6
```

The default module selection opens the head unit, cluster, and HUD as
three windows. Use `modules = ["head-unit"]` or `modules = ["6wb"]` to
start one ECU. Closing a selected ECU's window ends the session.

Point `psdzdata` at the directory containing `swe/swfl`. The head unit
uses `swfl_00002719.bin.007_030_001`, `swfl_0000271a.bin.007_030_001`,
and `swfl_00002720.bin.007_030_001`. The 6WB uses the `.bin` and `.xml`
pairs for `00003e5b`, `00003e5c`, and `00003e5d` at `007_003_005`.
The launcher extracts and verifies the selected firmware, then reuses the
verified cache on later runs. By default the cache lives under
`$XDG_CACHE_HOME/idrive6` or `~/.cache/idrive6`.

The optional settings are `cacheDir`, `display` (`cocoa` or `none` on macOS;
`sdl`, `gtk` or `none` on Linux), `cpuCount` (`1` or `2`), and `pluginApps`
(`true` or `false`, default `false`). `pluginApps = true` codes `REMOTE_HMI`
active in the emulated head unit's coding defaults; without one of the
remoting features coded, the HMI skips its remoting bundle and never loads
plugin apps from `/opt/hmi/ID5/data/ro/Plugins`. Keep PSdZData and the cache outside the repository;
the launcher rejects paths inside the checkout so local firmware and cache
files never enter the Nix source or store. Both paths may contain spaces.
The launcher reads `./idrive6.toml` by default; pass `--config /path/to/file`
to either command to use another config.

## Linux and Windows (WSL2)

The flake also provides `x86_64-linux` and `aarch64-linux` packages. On
Windows, run them inside WSL2: install a WSL2 distribution (Ubuntu works),
install Nix with flakes enabled inside it, and keep the checkout, PSdZData and
the cache on the Linux filesystem (for example under `~`), not under `/mnt/c`.
The Linux filesystem is faster and case-sensitive, which the extracted QNX
root needs. WSLg, included with Windows 11 and current Windows 10 WSL
releases, shows the QEMU windows on the Windows desktop. The commands are the
same as on macOS:

```sh
nix run .#idrive6 -- --prepare-only
nix run .#idrive6
```

The first run compiles the two patched QEMU builds, which takes a while.

What differs from macOS:

- **Windows.** The head unit, cluster and HUD open as SDL windows. Set
  `display = "gtk"` to use QEMU's GTK window instead.
- **Graphics.** The host ANGLE bridge uses ANGLE's Vulkan backend instead of
  Metal. The launcher selects Mesa's software Vulkan driver (lavapipe) from
  Nixpkgs, so no GPU driver setup is needed and rendering runs on the CPU. To
  try another Vulkan driver, set `VK_DRIVER_FILES` to its ICD manifest before
  launching; the launcher leaves an existing `VK_DRIVER_FILES` or
  `VK_ICD_FILENAMES` untouched.
- **Speed.** Apple silicon and Intel/AMD hosts both run the ARMv7 head unit
  and the MIPS 6WB through QEMU's TCG translator, and Linux adds software
  rendering on top. Expect lower frame rates than the macOS release host.

Linux status: the flake evaluates for all three systems and the macOS QEMU
derivations are unchanged. On `x86_64-linux` both patched QEMU builds, the
ANGLE server and the launcher build, and the firmware-free unit and QEMU
device tests pass. The ANGLE host smoke test renders through lavapipe, and
every checked-in reconstructed shader compiles with ANGLE's Vulkan backend.
The OMAP5430 and Capricorn-M machines open SDL and GTK windows under X11, and
mouse, wheel and key input in those windows produces the QEMU trace events
that the CID touch and controller relay reads. Booting the BMW firmware end to
end has not been verified on Linux or under WSLg yet, because that needs a
PSdZData extraction; treat it as experimental and check the run logs.

## Controls and logs

Use the mouse for CID touch. The controller mapping uses the wheel or Page
Up/Page Down to turn, arrow keys to tilt, Return or Space to select, Escape or
Backspace for Back, M for Menu, and O for Options. C or R sends Media/Radio,
T sends Communication, N sends Navigation, and V sends Map. Click the QEMU
window first to give it keyboard focus.

Each run gets its own directory below the cache's `runs/` folder. It contains
the guest serial stream, QEMU debug log, ANGLE server log, and a manifest of
the source and firmware inputs. OTF recording, network packet dumps, ANGLE
shader/draw/resource captures, `pdebug`, and the web inspector remain off by
default. For an already staged cache, set `IDRIVE_CACHE_DIR` to the same path
as `cacheDir` (or its default) before using the developer profile runner. For
example, with the default cache location:

```sh
export IDRIVE_CACHE_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/idrive6"
nix develop -c python3 tools/run-hmi-profile.py \
  profiles/f32-x20511i-id6-light.json \
  --set IDRIVE_OTF_CAPTURE=1 \
  --set IDRIVE_OTF_CAPTURE_OUTPUT=/tmp/idrive6-otf.bin \
  --set IDRIVE_OTF_CAPTURE_SERVER_LOG=/tmp/idrive6-otf.log
```

## Current limits

The F32 home interface, text, controller input, and CID touch run through
BMW's native HmiMain3D and QNX Screen. QEMU supplies an OMAP5430 board model
and a software display path. The PowerVR SGX540 is not fully emulated; the
host ANGLE bridge implements the graphics calls used by the tested HMI path.
Other vehicle variants are not part of the verified release, and Linux hosts
are experimental as described above.

## Developer notes

- [QEMU patch workflow](docs/qemu-patches.md) explains how to update the four
  topic patches against the pinned QEMU in Nixpkgs.
- [Graphics contract](NATIVE_GRAPHICS_CONTRACT.md) documents the current
  native HMI display path and observed boundaries.
- [HMI startup trace](HMI_STARTUP_DEPENDENCY.md) records the measured service
  and startup dependencies.
- [Third-party notices](THIRD_PARTY_NOTICES.md) covers the QEMU patches and
  optional Vita3K-based shader diagnostics.
- `tools/` contains the retained firmware extraction, graphics, and QNX
  diagnostics. Enter `nix develop` for the corresponding build tools.

The exact PSdZData package SHA-256 values are pinned in
`tools/idrive6-launch.py`. A different firmware revision must be reviewed and
added as a distinct, verified profile before use. The checked-in shader data is
limited to the host-side ESSL reconstructions and hashes used by this profile;
the original BMW shader payloads are verified from PSdZData at runtime.

## Developer diagnostics

Run the firmware-free extractor, QNX bootstrap, and QEMU device tests with:

```sh
nix develop -c python3 -m unittest discover -s tests -v
```

The firmware staging diagnostics use the same verified input as the launcher;
`nix run .#idrive6 -- --prepare-only` checks and stages it without starting
QEMU. The device tests use QEMU directly and need no firmware or GUI.

## Experimental 6WB cluster and HUD

The Bosch BO03 6WB runs in a separate QEMU guest. Its cluster and HUD
open as SDL windows. The live fixture replays KSS startup and HUD
readiness in temporary guest RAM and uses the firmware's unwarped HUD
output.

The launcher prints a local simulator URL. Open it for manual speed and
RPM controls. External simulators can read `GET /api/v1/vehicle` or send
partial updates to `POST /api/v1/vehicle` at that URL. The API uses km/h
for `speed_kph` and RPM for `engine_rpm`.

### Standalone 6WB probe

Use this path to capture startup graphics or run the simulator separately.
Build the Capricorn-M machine and set `BO03_IMAGES` to the unpacked
`firmware/bo03-007_003_005` directory in the cache created by
`nix run .#idrive6 -- --prepare-only`.

```sh
nix build .#qemu-capricorn-m --out-link /tmp/idrive6-capricorn-qemu
kombi_run="$(mktemp -d /tmp/idrive6-kombi.XXXXXX)"
printf '6WB run directory %s\n' "$kombi_run"
nix develop -c python3 tools/kombi-gss-probe.py \
  "$BO03_IMAGES/swfl_00003e5b-bf400000.bin" \
  --config "$BO03_IMAGES/swfl_00003e5c-bec00000.bin" \
  --graphics-data "$BO03_IMAGES/swfl_00003e5d-bc000000.bin" \
  --qemu /tmp/idrive6-capricorn-qemu/bin/qemu-system-mips64el \
  --log "$kombi_run/gss.log" --kss-replay \
  --vehicle-ram "$kombi_run/vehicle.ram" \
  --display vnc=127.0.0.1:1,display=cluster
```

Connect a VNC client to `127.0.0.1:5901`. Use `display=hud` to view the
native HUD. For a one-shot cluster and HUD startup capture, replace the
live RAM and display options with `--capture "$kombi_run/frames"`.

In another terminal, set `kombi_run` to the printed run directory and start
the host-side Python simulator.

```sh
python3 tools/kombi-sim.py --vehicle-ram "$kombi_run/vehicle.ram"
```

Open `http://127.0.0.1:8765/` for controls and browser-rendered previews.
Add `--demo` for a looping feed. External simulators can read
`GET /api/v1/vehicle` and send partial updates. The API uses km/h for
`speed_kph` and RPM for `engine_rpm`. Both fields drive the stock QEMU
renderers through the shared RAM fixture.

```sh
curl --fail -H 'Content-Type: application/json' \
  --data '{"speed_kph":87,"engine_rpm":2300}' http://127.0.0.1:8765/api/v1/vehicle
```

To test head-unit MOST transport, also start the simulator with
`--sipc-port 8766` and launch the head unit with
`IDRIVE_MOST_SIPC_HOST_PORT=8766 nix run .#idrive6`. The native
`DSYSMOSTServer` runs inside the guest. QEMU forwards its upstream TCP SIPC
connection to the host Python process. `GET /api/v1/most?after=0` returns
link state and decoded telegrams. The vehicle-state feed and SIPC telegrams
are separate interfaces. Native MOST delivery to the Kombi still needs
network-master behavior and Kombi registration.
