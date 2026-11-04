# The acceptance test for the rule:  kick plays -> beats.  kick stops -> beats stop.  kick returns -> straight
# back on the beat at the right tempo, never confusing or off-beat.  Whatever else plays (pads, voice, bass, hats).
# Builds tracks where every kick time is known, runs tracker + sender exactly as live, and checks every sent beat.
#   python tests/scenario_test.py [BPM ...]
#
# Each track, in order:
#   A 20 s kick                                   (started from silence, like pressing play)
#   B  8 s NO kick: pads + voice + offbeat bass + hats keep playing
#   C 15 s kick returns, same grid                (the breakdown ends)
#   D  6 s NO kick
#   E 15 s kick returns 0.37 beat later           (a new track that isn't on the old grid): beats from kick 3
#   F  4 s NO kick
#   G 15 s kick returns at +5% tempo              (a new track at another tempo): right tempo from kick 3
import sys, os
import numpy as np
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
from kick_sync.tracker import KickTracker
from kick_sync.sender import Sender

SR = 44100
LEAD, LAT = 0.06, 0.012

def kick(rng, var):
	t = np.arange(int(0.25 * SR)) / SR
	f = 45 + (90 + 30 * var) * np.exp(-t / (0.025 + 0.01 * var))
	return np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t / 0.12) * 0.9

def tone(f0, dur, att, dec, amp, harm=(1.0, 0.3)):
	t = np.arange(int(dur * SR)) / SR
	env = np.minimum(1.0, t / att) * np.exp(-t / dec)
	return sum(a * np.sin(2 * np.pi * f0 * (i + 1) * t) for i, a in enumerate(harm)) * env * amp

def build(bpm, seed=1):
	rng = np.random.default_rng(seed)
	plan = [('A kick', 20, 1.0, 0.0, True), ('B no kick', 8, 1.0, 0.0, False), ('C returns same grid', 15, 1.0, 0.0, True),
	        ('D no kick', 6, 1.0, 0.0, False), ('E returns off-grid', 15, 1.0, 0.37, True), ('F no kick', 4, 1.0, 0.0, False),
	        ('G returns +5% tempo', 15, 1.05, 0.0, True)]
	total = sum(p[1] for p in plan) + 3
	y = np.zeros(int(total * SR) + SR)
	K = kick(rng, 0.5)
	hat = rng.standard_normal(int(0.04 * SR)) * np.exp(-np.arange(int(0.04 * SR)) / SR / 0.01) * 0.12
	t, kicks, sections = 1.0, [], []
	tempo = bpm
	for name, dur, tmul, shift, has_kick in plan:
		tempo = tempo * tmul
		P = 60.0 / tempo
		t += shift * P
		start = t
		while t < start + dur:
			i = int(t * SR)
			if has_kick:
				y[i:i + len(K)] += K
				kicks.append(t)
			b = tone(55 + 10 * (int(t / P) % 4 == 3), P / 2, 0.012, 0.35, 0.55)
			j = int((t + P / 2) * SR); y[j:j + len(b)] += b                      # offbeat bass note (held, no pitch drop)
			for h in (0.25, 0.5, 0.75):
				k = int((t + h * P) * SR); y[k:k + len(hat)] += hat * (1.4 if h == 0.5 else 0.6)
			t += P
		sections.append((name, start, t, P, has_kick))
	n = len(y)
	tt = np.arange(n) / SR
	# pads: slow chords across everything, plus plucked pad stabs (sharp attacks) at random times
	for f0, ph in ((110, 0), (138.6, 1), (164.8, 2), (220, 3)):
		y += 0.05 * np.sin(2 * np.pi * f0 * tt + ph) * (0.6 + 0.4 * np.sin(2 * np.pi * tt / 7.0 + ph))
	for _ in range(int(total / 1.3)):
		s = rng.uniform(1, total - 1)
		p = tone(rng.choice([110, 130.8, 146.8, 196]), 0.8, 0.012, 0.5, 0.35, harm=(1.0, 0.5, 0.25))
		i = int(s * SR); y[i:i + len(p)] += p[:max(0, n - i)]
	# voice: vowel-like formant buzz phrases
	for _ in range(int(total / 4)):
		s = rng.uniform(1, total - 2); d = rng.uniform(0.4, 1.5)
		f0 = rng.uniform(110, 220)
		v = tone(f0, d, 0.03, d, 0.25, harm=(1.0, 0.8, 0.6, 0.5, 0.4, 0.3))
		i = int(s * SR); y[i:i + len(v)] += v[:max(0, n - i)]
	y += rng.standard_normal(n) * 0.005
	y[:int(0.997 * SR)] = 0.0                                                     # silence, then play (from the cue, on the kick)
	return np.stack([y, y], 1) * 0.8, np.array(kicks), sections

def run(bpm):
	y, kicks, sections = build(bpm)
	tr = KickTracker(SR)
	snd = Sender(tr)
	sent = []
	for i in range(0, len(y), 256):
		tr.feed(y[i:i + 256])
		wall = (i + 256) / SR + LAT
		for now in np.arange(wall, wall + 256 / SR, 0.002):
			d = snd.due(now, LAT, LEAD)
			if d and d[0] <= now + 0.002:
				sent.append(d[1]); snd.mark(d[1])
	s = np.array(sent)
	ok = True
	lines = []
	for name, a, z, P, has_kick in sections:
		inside = s[(s >= a - 0.25 * P) & (s < z - 0.25 * P)]
		if has_kick:
			ks = kicks[(kicks >= a - 0.01) & (kicks < z - 0.01)]
			hit = [kk for kk in ks if np.any(np.abs(inside - kk) < 0.25 * P)]
			missed_first = next((i for i, kk in enumerate(ks) if np.any(np.abs(inside - kk) < 0.25 * P)), len(ks))
			err = np.array([v - ks[np.argmin(np.abs(ks - v))] for v in inside]) * 1000
			# off-beat = more than 25 ms from a kick (the beat right after a tempo jump: up to 6% of a beat, unknowable)
			lim = np.array([max(25.0, 60.0 * P) if 'tempo' in name and abs(v - ks[min(1, len(ks) - 1)]) < 0.25 * P else 25.0 for v in inside])
			false = int((np.abs(err) > lim).sum())
			# first beat allowed: from silence on kick 1; back on the same grid on kick 2; back somewhere new (off the
			# grid / another tempo) on kick 3: one hit can't tell where a new grid is, two kicks a beat apart can.
			# (a kick hidden by another sound now and then: one kick later is fine, a wrong beat never is)
			allow = 0 if 'returns' not in name else (1 if 'same grid' in name else 3)
			# at a new tempo the beat on kick 2 is still predicted from the old tempo (can't be known): up to 5% off
			skip = 1 if 'tempo' in name else 0
			after = np.array([e for v, e in zip(inside, err) if v > ks[min(missed_first + skip, len(ks) - 1)] + (0.5 * P if skip else 0.01)])
			worst = np.abs(after).max() if len(after) else 0.0
			good = false == 0 and missed_first <= allow and worst <= 15 and len(hit) >= len(ks) - missed_first - 1
			ok &= good
			lines.append(f'  {name:22s}: {"OK " if good else "BAD"} first beat on kick #{missed_first + 1}, '
			             f'kicks with a beat {len(hit)}/{len(ks)}, off-beat beats {false}, worst timing {worst:4.1f} ms')
		else:
			n_in = len(inside)
			good = n_in <= 2                     # 2: decided before the kick was known to be gone (one skip is a fill)
			ok &= good
			lines.append(f'  {name:22s}: {"OK " if good else "BAD"} beats sent with no kick: {n_in} (2 allowed: already on their way)')
	print(f'--- {bpm:.0f} BPM: {"PASS" if ok else "FAIL"}')
	print('\n'.join(lines))
	return ok

if __name__ == '__main__':
	res = [run(float(b)) for b in (sys.argv[1:] or [120, 128, 140, 150, 160, 174, 190, 200])]
	print('ALL PASS' if all(res) else f'{res.count(False)} of {len(res)} tempos FAIL')
