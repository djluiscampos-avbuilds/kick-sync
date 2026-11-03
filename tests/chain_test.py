# End-to-end test of the live chain on a recording: audio arrives in 256-sample blocks in (simulated) real time,
# the tracker runs, and the Sender decides what is sent and when - exactly the live code. Reports, for every time
# the music starts after silence and stops: when the beats sent begin and end, compared with the music itself.
#   python tests/chain_test.py my_recording.wav
import sys, os
import numpy as np
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
from kick_sync.tracker import KickTracker
from kick_sync.sender import Sender

raw = open(sys.argv[1], 'rb').read()[44:]
x = np.frombuffer(raw[:len(raw) // 4 * 4], '<i2').reshape(-1, 2) / 32768.0
sr, LEAD, LAT = 44100, 0.06, 0.012          # lead as live; audio arrives ~12 ms after it was played
tr = KickTracker(sr)
snd = Sender(tr)
sent = []                                    # (wall time sent, beat audio time)
pending = None
for i in range(0, len(x), 256):
	tr.feed(x[i:i + 256])
	wall_end = (i + 256) / sr + LAT          # wall time when this block has arrived
	off = LAT
	# the sender thread polls every ~2 ms until the next block arrives
	for now in np.arange(wall_end, wall_end + 256 / sr, 0.002):
		d = snd.due(now, off, LEAD)
		if d and d[0] <= now + 0.002:
			sent.append((max(now, d[0]), d[1]))
			snd.mark(d[1])
s = np.array([w for w, _ in sent]) - LAT     # back to audio time = when it hits the screen relative to the music
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
