# Exact test of the tracker at any tempo: builds tracks where every kick time is known (kick = 150->45 Hz pitch
# sweep, loud offbeat bassline, hi-hats, noise) and runs the tracker exactly as live. Each track has, in order:
#   30 s steady -> phase jump of 120 ms (a mix coming in off-grid) -> 30 s -> 8 s breakdown (no kick, bass + hats keep
#   going) -> 20 s -> tempo change of +6% (next track) -> 30 s
# and reports, per section: tempo found, uneven beats, and how far the beats land from the kicks.
#   python tests/synth_test.py [BPM ...]
import sys, os
import numpy as np
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
from kick_sync.tracker import KickTracker

SR = 44100
rng = np.random.default_rng(1)

def kick():
	t = np.arange(int(0.25 * SR)) / SR
	f = 45 + 105 * np.exp(-t / 0.03)
	return np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t / 0.12) * 0.9

def bass(f0, dur):
	t = np.arange(int(dur * SR)) / SR
	env = np.minimum(1.0, t / 0.01) * np.exp(-t / 0.15)
	return (np.sin(2 * np.pi * f0 * t) + 0.3 * np.sin(4 * np.pi * f0 * t)) * env * 0.7

def hat():
	t = np.arange(int(0.05 * SR)) / SR
	return rng.standard_normal(len(t)) * np.exp(-t / 0.01) * 0.15

def build(bpm):
	"""Returns audio and the true kick times."""
	P = 60.0 / bpm
	plan = [('steady', 30, bpm, 0.0, True), ('after jump', 30, bpm, 0.12, True), ('breakdown', 8, bpm, 0.0, False),
	        ('after break', 20, bpm, 0.0, True), ('new tempo', 30, bpm * 1.06, 0.0, True)]
	total = sum(p[1] for p in plan) + 2
	y = np.zeros(int(total * SR) + SR)
	K, kicks, hats, bas = kick(), [], hat(), None
	t, sections = 0.5, []
	for name, dur, b, jump, has_kick in plan:
		P = 60.0 / b
		t += jump
		start = t
		while t < start + dur:
			i = int(t * SR)
			if has_kick:
				y[i:i + len(K)] += K
				kicks.append(t)
			bl = bass(55 + 10 * (int(t / P) % 4 == 3), P / 2)
			j = int((t + P / 2) * SR)                            # offbeat bass, as loud as the kick
			y[j:j + len(bl)] += bl
			for h in (0.25, 0.5, 0.75):
				k = int((t + h * P) * SR)
				y[k:k + len(hats)] += hats * (1.5 if h == 0.5 else 0.6)
			t += P
		sections.append((name, start, t, b))
	y += rng.standard_normal(len(y)) * 0.01
	return np.stack([y, y], 1), np.array(kicks), sections

def run(bpm):
	y, kicks, sections = build(bpm)
	tr = KickTracker(SR)
	for i in range(0, len(y), 256):
		tr.feed(y[i:i + 256])
	b = np.array(tr.beats)
	print(f'--- {bpm:.0f} BPM track')
	for name, a, z, sb in sections:
		P = 60.0 / sb
		m = (b >= a + (3 if name != 'breakdown' else 0)) & (b < z)     # skip the first 3 s of each section (relock time)
		if m.sum() < 3:
			print(f'  {name:12s}: no beats'); continue
		bb = b[m]
		ref = kicks[(kicks >= a - P) & (kicks < z + P)]
		if name == 'breakdown':                                 # no kicks: the grid should just continue
			ref = a + np.arange(-1, int((z - a) / P) + 2) * P
		e = np.array([v - ref[np.argmin(np.abs(ref - v))] for v in bb]) * 1000
		d = np.diff(bb)
		print(f'  {name:12s}: {sb:6.1f} BPM | uneven {(np.abs(d - P) > 0.02).sum():2d}/{len(d):3d} | '
		      f'beat - kick: median {np.median(e):+5.1f} ms, worst {np.abs(e).max():5.1f} ms')
	print(f'  final tempo found: {tr.bpm:.2f}')

if __name__ == '__main__':
	for bpm in [float(v) for v in sys.argv[1:]] or [80, 95, 126, 140, 150, 175, 200]:
		run(bpm)
