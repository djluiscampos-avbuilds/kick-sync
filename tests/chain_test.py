# End-to-end test of the live chain on a recording: audio arrives in 256-sample blocks in (simulated) real time,
# the tracker runs, and the Sender decides what is sent and when - exactly the live code. Reports, for every time
# the music starts after silence and stops: when the beats sent begin and end, compared with the music itself.
#   python tests/chain_test.py NAME.wav [--pauses]   (record one with: kick-sync --record NAME.wav 120)   (--pauses: simulate audio stream pauses)
import sys, os
import numpy as np
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
from kick_sync.tracker import KickTracker
from kick_sync.sender import Sender, ClockSync

raw = open(sys.argv[1], 'rb').read()[44:]
x = np.frombuffer(raw[:len(raw) // 4 * 4], '<i2').reshape(-1, 2) / 32768.0
sr, LEAD, LAT = 44100, 0.06, 0.012          # lead as live; audio arrives ~12 ms after it was played
tr = KickTracker(sr)
snd = Sender(tr)
sync = ClockSync()
rng = np.random.default_rng(3)
# audio stream pauses (USB / driver hiccups): from these audio times on, the audio clock lags real time by this much
PAUSES = [(20.0, 4.0), (40.0, 9.0)] if '--pauses' in sys.argv else []
def lag(a):
	return sum(d for t0, d in PAUSES if a >= t0)
sent = []                                    # (wall time sent, beat audio time)
pending = None
early = []
for i in range(0, len(x), 256):
	tr.feed(x[i:i + 256])
	a_end = (i + 256) / sr
	wall_end = a_end + LAT + lag(a_end) + rng.uniform(0, 0.008)     # arrival time, with buffering jitter
	off = sync.update(wall_end, tr.t)
	# the sender thread polls every ~2 ms until the next block arrives
	for now in np.arange(wall_end, wall_end + 256 / sr, 0.002):
		d = snd.due(now, off, LEAD)
		if d and d[0] <= now + 0.002:
			sent.append((max(now, d[0]), d[1]))
			early.append((d[1], max(now, d[0]) - (d[1] + LAT + lag(d[1]))))     # sent this long before the kick played
			snd.mark(d[1])
s = np.array([a for _, a in sent])           # beat (audio time)
m = np.abs(x.mean(1))
idx = np.flatnonzero(m > 0.01)
gaps = np.flatnonzero(np.diff(idx) > sr)
starts = [idx[0] / sr] + [idx[g + 1] / sr for g in gaps]
stops = [idx[g] / sr for g in gaps] + [idx[-1] / sr]
print(f'{len(x) / sr:.0f} s, {len(sent)} beats sent')
for a, z in zip(starts, stops):
	inside = s[(s > a - 0.2) & (s < z + 2.0)]
	first = ', '.join(f'{v - a:+.3f}' for v in inside[:4])
	tail = inside[inside > z]
	print(f'music {a:7.3f}-{z:7.3f} s: first beats sent {first} | after the stop: {len(tail)} beat(s) {"(+%.2f s)" % (tail[0] - z) if len(tail) else ""}')

# timing: predicted beats should go out LEAD before the kick plays, whatever the audio stream did
E = np.array([(a, e) for a, e in early if not any(t0 <= a < t0 + 3.5 for t0, _ in PAUSES)])
if len(E):
	pred = E[:, 1] < -0.02                    # predicted beats (instant-start beats go out when heard)
	v = -E[pred, 1] * 1000
	print(f'predicted beats sent ahead of the kick: median {np.median(v):.1f} ms (lead {LEAD * 1000:.0f}), '
	      f'p10 {np.percentile(v, 10):.1f}, p90 {np.percentile(v, 90):.1f}; late ones (> 5 ms after the kick): '
	      f'{(E[:, 1] > 0.005).sum()} of {len(E)}' + (f'  [pauses {PAUSES}]' if PAUSES else ''))
