# kick-sync

**Live beat sync from the kick drum, for visuals.** It listens to your DJ audio and sends every beat over OSC, so
TouchDesigner (or Resolume, Max, Ableton, anything with OSC) hits exactly on the kick.

Made by **DJ Luis Campos** for his own live AV sets, after every other approach drifted, lagged or locked onto the
offbeat bass.

## Why

Many DJ setups give your visuals no beat at all: a standalone player with no Pro DJ Link, no MIDI clock, no Ableton
Link. All you have is the audio. Generic audio analysis inside a visual tool is tied to its frame rate, smears the
timing and is easily fooled by basslines. kick-sync runs outside your visual software and listens only for the kick:

- **Any tempo**, 60-220 BPM, house to drum & bass to hardstyle.
- **Kick only.** A kick is recognised by its shape (a sharp 100-200 Hz attack that fades as the pitch drops into a
  30-60 Hz body). Offbeat bass, hats and percussion can't fool it.
- **Locked within a few ms.** A phase-locked loop follows every kick; on test tracks with known kick times the beats
  land 2-6 ms from the kick at every tempo.
- **Starts and stops with the music.** Press play after silence and the first beat fires on the first kick; stop and
  the beats stop within 0.15 s.
- **Holds through breakdowns**, and relocks on its own when a new track comes in (only on strong evidence, so a
  mix that's slightly off or a stray hit can't throw it).
- **Steady output:** beats never skip, double or jump; corrections are smoothed.
- **Latency compensation:** beats are sent early by an adjustable "lead", so the flash lands when you *hear* the kick.
- Light: about 2% of one CPU core.

## Install

Needs Python 3.9 or newer.

```bash
pip install git+https://github.com/luiscamposnyc/kick-sync
```

## Use

Your DJ player, mixer or controller must reach the computer as an audio input (most players' USB port does this, or
use a sound card input).

```bash
kick-sync --list                 # see your audio inputs
kick-sync --device "XDJ"         # run on the input whose name contains "XDJ"
```

Leave its window open during the set. It shows the tempo and whether it's locked. In that window (Windows):
`]` / `[` move all beats 5 ms later / earlier until the visuals hit on the kick for your setup; the setting is
remembered.

Options: `--host` / `--port` (OSC target, default `127.0.0.1:7400`), `--lead` (ms), `--record FILE SECONDS`
(record your input, to test with your own music).

## OSC messages

| Address  | Arguments                                   | When                                  |
|----------|---------------------------------------------|---------------------------------------|
| `/beat`  | `int n` (beat count), `float bpm`, `float t` | on every beat                         |
| `/state` | `float bpm`, `int locked`                   | 4 times a second (program is alive)   |

## TouchDesigner

Run [`touchdesigner/kick_sync_receiver.py`](touchdesigner/kick_sync_receiver.py) once in the Textport:

```python
exec(open(r'C:/path/to/kick-sync/touchdesigner/kick_sync_receiver.py').read())
```

It creates `/project1/kick_sync`. Link anything to its CHOP:

- `op('/project1/kick_sync/beat')['pulse']`: 1 on each beat, falling to 0. One knob sets the pulse length for
  everything: `op('/project1/kick_sync').store('decay', 5.0)`
- also `beat` (count), `bar` (0-3), `bpm`, `locked`
- in shaders: pass `op('/project1/kick_sync').fetch('age')` (seconds since the beat) and use `exp(-age * k)`

Tip: start it automatically when your project opens (an Execute DAT `onStart` that launches `kick-sync` with
`subprocess.Popen`). Remove TouchDesigner's `PYTHON*` variables from the environment you pass, or the system Python
will load TouchDesigner's libraries and crash.

## Tests

```bash
python tests/synth_test.py              # test tracks with known kick times at 80-200 BPM: phase jump, breakdown, tempo change
python tests/chain_test.py my_set.wav   # your own recording through the whole live chain: first beats after each start, beats after each stop
```

## Limits

- Built for music with a kick on (most) beats: house, techno, trance, psytrance, hardstyle, DnB rollers. Very
  syncopated breakbeats follow the kick pattern's steady pulse, not every hit.
- A very deep offbeat bassline through a breakdown at ~200 BPM can occasionally end up half a beat off until the kick
  returns.

## License

MIT, see [LICENSE](LICENSE). (c) 2026 DJ Luis Campos.
