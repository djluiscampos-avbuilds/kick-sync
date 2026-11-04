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
- **Kick stops, beats stop.** In a breakdown (pads, voice, bass, hats still playing) no beats go out: a beat is sent
  only when the kick was really there on the beat before. Kick back on the same grid: the first kick flashes the
  moment it's heard. A new track somewhere else or at another tempo: the right position and tempo within 2-3 kicks.
- **Latency compensation:** beats are sent early by an adjustable "lead", so the flash lands when you *hear* the kick.
- Light: about 2% of one CPU core.

## Install

Needs Python 3.9 or newer.

```bash
pip install git+https://github.com/djluiscampos-avbuilds/kick-sync
```

## Use

Your DJ player, mixer or controller must reach the computer as an audio input (most players' USB port does this, or
use a sound card input).

```bash
kick-sync --list                 # see your audio inputs
kick-sync --device "XDJ"         # run on the input whose name contains "XDJ"
```

Leave its window open during the set. It shows the tempo and whether it's locked. In that window (Windows):
`]` / `[` move all beats 5 ms later / earlier until the visuals hit on the kick for your setup (remembered), `0` puts
that back to 60 ms, `r` forgets the grid and locks fresh on the next kicks.

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
python tests/scenario_test.py           # test tracks with known kick times at 120-200 BPM: kick, breakdowns, returns on/off the grid, tempo change
python tests/chain_test.py my_set.wav   # your own recording through the whole live chain (add --pauses to simulate audio dropouts)
```

## Limits

- Built for music with a kick on (most) beats: house, techno, trance, psytrance, hardstyle, DnB rollers. Very
  syncopated breakbeats follow the kick pattern's steady pulse, not every hit.
- Not perfect. On recent real sessions (124-128 BPM, many stops) 98-99% of kicks get their beat, with no beats off
  the kick; on older, denser recordings (up to 195 BPM) 90-97%. The first kick after a stop gets its beat most of the
  time (41 of 43 in one session), sent the moment it's heard (so ~60 ms late on screen); otherwise from the next kick.
- A kick that comes back somewhere new (a track cued in another position) gets its beat from kick 3. Stray sounds
  just before a track starts, kicks faded in very quietly, and scrubbing a track can delay the first beats.
- Audio lost between the player and the computer is put back (see below), but long stretches of lost audio can still
  cost a few beats.

## What's new

**1.1.0**
- Kick-only rule: beats stop when the kick stops (1.0 kept beating through breakdowns) and come back with it.
- First kick after a break flashes at once; one guess per break, so other hits can't keep firing.
- After a break, a new tempo is only taken when a third kick confirms it (one early kick no longer throws the grid).
- A kick that drifts off the grid, or a clearly stronger kick pulse just after a return, pulls the grid onto it.
- Lost audio is put back: some USB players / Windows drop 15-40 ms chunks of audio without reporting it, which made
  the music seem to jump and run fast (beats skipping mid-track). Bigger input buffer as well.
- Two kicks almost a second apart at a track start lock at the double tempo (one kick missed), not at half speed.
- Clock recovers within 3 s after an audio dropout. New keys: `0` lead back to 60 ms, `r` resync now.

## License

MIT, see [LICENSE](LICENSE). (c) 2026 DJ Luis Campos.
