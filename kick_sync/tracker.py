# Kick-only beat tracker, streaming: feed it audio blocks, it keeps a beat grid on the kick drum and says, beat by
# beat, whether the kick is really there. Used by cli.py (live) and by the tests (offline), so what is tested is
# exactly what runs live.
#
# The rule:  kick plays -> beats.  kick stops -> beats stop.  kick returns -> straight back on the beat
# at the right tempo.  Whatever else plays (pads, voice, bass, hats) never makes a beat.
#
#   1. kick score (1 kHz): a kick's attack jumps at 100-200 Hz and fades fast as its pitch drops, then its body swells
#      at 30-60 Hz within ~100 ms. Score = attack rise x body swell x fast fade (pads, voice, bass and hats don't do
#      all three). Known 110 ms late; fine, beats are predicted.
#   2. the grid (tempo + position) comes only from kicks: locked at the start (instant start after silence: the first
#      three kicks; mid-music: the score's autocorrelation), then every real kick nudges it (phase-locked loop).
#   3. at each grid beat the score is checked in a +-45 ms window: is the kick there? A beat goes out only if the kick
#      was there on the beat before. No kick -> no beat, from the next beat on.
#   4. kick gone, then back: on the same grid -> beats continue from the next beat. Somewhere else (a new track, a new
#      tempo) -> three real kicks in a row give the new tempo and position, and the beats continue from there.
import math
import numpy as np

class Biquad:
	"""RBJ filter ('lp' low-pass or 'bp' band-pass), direct form I, sample by sample (state kept between blocks)."""
	def __init__(self, fs, f0, q=0.7071, kind='lp'):
		w = 2 * math.pi * f0 / fs
		a = math.sin(w) / (2 * q)
		c = math.cos(w)
		if kind == 'bp':
			b0, b1, b2 = a, 0.0, -a
		else:
			b0, b1, b2 = (1 - c) / 2, 1 - c, (1 - c) / 2
		a0, a1, a2 = 1 + a, -2 * c, 1 - a
		self.k = (b0 / a0, b1 / a0, b2 / a0, a1 / a0, a2 / a0)
		self.s = [0.0, 0.0, 0.0, 0.0]

	def run(self, x):
		b0, b1, b2, a1, a2 = self.k
		x1, x2, y1, y2 = self.s
		out = []
		for v in x:
			y = b0 * v + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2
			x2, x1, y2, y1 = x1, v, y1, y
			out.append(y)
		self.s = [x1, x2, y1, y2]
		return out


class KickTracker:
	DEC = 8               # 44.1 kHz -> 5.5 kHz
	ORATE = 1000.0        # score history rate (Hz)
	HIST = 8.0            # seconds of history for the first lock
	D = 110               # score delay (ms): how far ahead the body swell is looked for
	BPM_LO, BPM_HI = 60.0, 220.0       # any tempo
	SILENCE = 0.0005      # RMS below this = stopped (a stopped player = exactly 0; music between kicks ~0.005)
	KP, KI, PCAP = 0.3, 0.05, 0.012    # phase-locked loop: phase gain, tempo gain, max move per kick (s)
	W = 0.045             # the kick is looked for within +-45 ms of each grid beat
	SWEEP = 1.4           # a kick's pitch drops: 0-25 ms after it starts vs 50-100 ms, at least 1.5x (measured on the
	                      # real club music: real kicks 1.7-2.2x; bass, pads, voice hold their pitch ~1.0x)
	FLOOR = 2.0           # absolute kick score below this is never a kick (noise / quiet pads)
	PRES = 0.2            # ...and counts as there when its absolute kick score is at least 20% of this track's typical
	                      # kick (measured on real club music: real kicks 19-31, a pads-only section 0-1.3)

	def __init__(self, sr):
		self.sr = sr
		self.fs = sr / self.DEC
		self.bp = Biquad(self.fs, 140.0, 1.0, 'bp')
		self.lp = [Biquad(self.fs, 60.0), Biquad(self.fs, 60.0)]
		self.zlp = [Biquad(self.fs, 250.0), Biquad(self.fs, 250.0)]   # 30-250 Hz: where the kick's pitch is followed
		self.zhp = Biquad(self.fs, 30.0)
		self.zb, self.zn = [], 0               # last ~2 s of that band, and how many samples so far
		self.eh = self.el = 0.0
		self.sh = self.sl = 1e-4               # slow levels: makes the score loudness-independent
		self.rem = np.zeros(0)
		self.t = 0.0                           # seconds of audio consumed
		self.H, self.L, self.C = [], [], [0.0]  # attack env, body env, running sum of body env (1 kHz)
		self.osig = []                         # kick score (1 kHz), last entry = time self.t - D ms
		self.asig = []                         # absolute kick score, same timing: for 'is the kick on this beat?'
		self.rsig = []                         # attack rise, same timing: for the kick's exact time
		self.ophase = 0.0
		self.thr = 1e9
		self.cand = None                       # (score, time) of the kick being detected
		self.period = 60.0 / 124.0
		self.base = self.period                # tempo at the last lock (the loop may move it +-1%)
		self.beats, self.kicks, self.kerr = [], [], []
		self.lastkick = -1.0
		self.quiet_s = 0.0                     # seconds of silence so far
		self.armed = False                     # instant start: music that starts after silence
		self.outbox = []                       # beats already heard (instant start) waiting to be sent
		self._since_est = 0.0
		self.relock = None
		self.clear()

	def clear(self):
		"""No grid: nothing is sent until the next kicks are locked."""
		self.locked = False
		self.next = self.onext = None          # the next grid beat (audio seconds); onext = the same, for the sender
		self.lastchk = -100.0                  # last grid beat that was checked for a kick
		self.streak = 0                        # grid beats in a row that had their kick (0 = the kick is gone)
		self.kstr = 0.0                        # typical absolute score of this track's real kick
		self.offk = []                         # real kicks heard off the grid while the kick was gone
		self.offs = []                         # much stronger kicks off the grid just after a return
		self.gpv = 0.0                         # level of the last hit on the grid
		self.qon, self.qlast, self.qmax = [], -1.0, 0.0
		self.relock = None
		self.mid = 0
		self.ret = None                        # time of the kick that came back on the grid after a gap
		self.tpend = None                      # (kick 2, its tempo): a new tempo waiting for kick 3 to confirm it
		self.pe = None                         # expected offset of the next kick (from the last sure one)
		self.wide = None                       # off-grid offset of a clear kick beside the last beat
		self.krise = 0.0                       # this track's typical kick attack rise
		self.ifired = -100.0                   # grid beat that got an instant beat
		self.idis = False                      # an instant beat that wasn't a kick: no more until a real kick
		self.lastpres, self.prevrun = -100.0, 0  # last grid beat that had its kick; length of the run before a miss
		self.prun2 = 0                         # and of the run before that one
		self.prevk = None                      # last real kick heard
		self.lastsure = 0
		self.instant = 0                       # beats fired instantly after pressing play
		self.osig.clear()
		self.asig.clear()
		self.rsig.clear()

	def reset(self):
		self.clear()

	# ---- feeding audio ----
	def feed(self, block):
		"""block: float array (n,) or (n, ch). Returns beat times (audio seconds) that fell inside this block."""
		x = block.mean(1) if block.ndim == 2 else block
		# music stopped (pure digital silence): stop the beats and forget the grid; the next start locks fresh
		if float(np.sqrt(np.mean(x * x))) < self.SILENCE:
			self.quiet_s += len(x) / self.sr
			if self.quiet_s > 0.15:
				if self.locked or self.qon:
					self.clear()
				self.armed = True
		else:
			self.quiet_s = 0.0
		x = np.concatenate([self.rem, x])
		n = len(x) // self.DEC * self.DEC
		self.rem = x[n:]
		d = x[:n].reshape(-1, self.DEC).mean(1).tolist()
		h = self.bp.run(d)
		l = self.lp[1].run(self.lp[0].run(d))
		zl = self.zlp[1].run(self.zlp[0].run(d))
		self.zb.extend(a - b for a, b in zip(zl, self.zhp.run(zl)))
		self.zn += len(zl)
		if len(self.zb) > 16000:
			del self.zb[:4000]
		return self._process(h, l)

	def _process(self, h, l):
		fired = []
		dt = 1.0 / self.fs
		step = self.fs / self.ORATE
		ah, rh = 1 - math.exp(-dt / 0.001), 1 - math.exp(-dt / 0.015)
		al, rl = 1 - math.exp(-dt / 0.004), 1 - math.exp(-dt / 0.04)
		ks = dt / 2.0
		for vh, vl in zip(h, l):
			vh, vl = abs(vh), abs(vl)
			self.eh += (vh - self.eh) * (ah if vh > self.eh else rh)
			self.el += (vl - self.el) * (al if vl > self.el else rl)
			self.sh += (self.eh - self.sh) * ks
			self.sl += (self.el - self.sl) * ks
			self.t += dt
			self.ophase += 1.0
			if self.ophase >= step:
				self.ophase -= step
				self._tick()
			if self.next is not None:
				while self.t >= self.next:
					fired.append(self.next)
					self.beats.append(self.next)
					self.next += self.period
				self.onext = self.next
		self._since_est += len(h) * dt
		return fired

	def _tick(self):
		H, L, C = self.H, self.L, self.C
		H.append(self.eh)
		L.append(self.el)
		C.append(C[-1] + self.el)
		n = len(H)
		j = n - 1 - self.D
		if j < 30:
			return
		# attack: rise over 12 ms around j; body: mean 60-110 ms after j minus mean 25-5 ms before j
		# absolute score (raw levels, not scaled by the recent loudness: a quiet breakdown can't be blown up into a
		# kick, and the first kick after silence isn't huge): attack rise x body swell
		a = max(0.0, H[j + 8] - H[j - 4]) * math.sqrt(max(0.0, (C[j + 110] - C[j + 60]) / 50.0 - (C[j - 5] - C[j - 25]) / 20.0)) * 1000.0
		self.asig.append(a)
		self.rsig.append(max(0.0, H[j + 8] - H[j - 4]))  # attack rise alone: when exactly the kick hits
		# for finding single kicks: a kick's pitch sweeps down, so its 100-200 Hz attack fades fast: 80 ms later it
		# is under ~50% of its peak (measured: real kicks 46%, offbeat bass / other hits 64-85%)
		s = a
		if s > 0.0:
			pk = max(H[j:j + 20]) + 1e-9
			s *= min(1.0, max(0.0, (0.75 - H[j + 80] / pk) / 0.3))
		self.osig.append(s)
		if n > 4000:                                       # envelopes: only the last few hundred ms are needed
			del H[:2000], L[:2000], C[:2000]
		if len(self.osig) > int(self.HIST * self.ORATE) + 2000:
			del self.osig[:2000]
			del self.asig[:2000]
			del self.rsig[:2000]
		if self.osig and len(self.osig) % 50 == 0:
			self.thr = max(self.FLOOR, 0.25 * max(self.osig[-2000:]))
		ts = self.t - self.D / self.ORATE                  # time of the newest score entry
		if self.locked:
			self._instant_return()
		if self.armed and not self.locked:
			self._quick_start()
		if self.locked:
			self._check_beat(ts)
		self._kick_detect(s, ts)

	# ---- is the kick there on this beat? ----
	def _check_beat(self, ts):
		P = self.period
		g = self.next - P * math.ceil((self.next - (ts - self.W)) / P)      # latest grid beat whose window is complete
		if g <= self.lastchk + 0.5 * P:
			return
		self.lastchk = g
		o = self.asig
		i0 = len(o) - 1 - int(round((ts - (g - self.W)) * self.ORATE))
		i1 = len(o) - 1 - int(round((ts - (g + self.W)) * self.ORATE))
		if i0 < 0 or i1 <= i0:
			self.streak = 0
			return
		w = o[i0:i1 + 1]
		k = int(np.argmax(w))
		pv = w[k] if 0 < k < len(w) - 1 else 0.0          # the peak must be inside the window (not something next to it)
		if self.kstr == 0 and self.streak == 0 and pv > 0:
			self.kstr = pv                                  # first check after a lock (made on kicks): learn the level
		r = self.rsig[i0:i1 + 1]
		k = max(0, k - 15) + int(np.argmax(r[max(0, k - 15):k + 6]))    # time it by its attack
		e = (g - self.W + k / self.ORATE) - g                               # the hit vs the grid beat
		sure = pv > 0 and pv >= 0.4 * self.kstr and self.sweep(g + e) >= 1.3      # a kick: strong and its pitch drops
		# a strong hit whose pitch drop wasn't clear (layers on top) counts, if one of the 2 beats before was sure
		maybe = not sure and self.lastsure > 0 and self.kstr > 0 and pv >= self.PRES * self.kstr
		self.lastsure = 2 if sure else max(0, self.lastsure - 1)
		if sure:
			self.idis = False
		elif abs(g - self.ifired) < 0.5 * P:
			self.idis = True                               # the instant beat's hit wasn't a sure kick
		strong = sure or (maybe and pv >= 0.4 * self.kstr)
		if strong and self.ret is not None and self.streak >= 1 and abs(g + e - self.ret - P) < 0.08 * P:
			I = g + e - self.ret                               # the kick after the one that came back: same tempo?
			self.ret = None
			if abs(I - P) > 0.025 * P:
				self._halfway(g + e, I)                        # maybe a new tempo: kick 3 decides (one early kick isn't one)
				return
		elif strong and self.tpend is not None and self._confirm(g + e):
			return
		back = (sure or maybe) and self.streak == 0
		if back:
			self.ret = g + e                                   # the kick is back (on this grid)
		if sure or maybe:
			if sure:                                           # only sure kicks move the grid
				# the kick sits at the same offset from the grid two beats running: the grid is off, jump onto it
				same = self.pe is not None and abs(e) > 0.012 and abs(e - self.pe) < 0.008
				n0, p0 = self.next, self.period
				self.next += e if (back or same) else max(-self.PCAP, min(self.PCAP, self.KP * e))   # back: straight onto it
				self.period += max(-0.002, min(0.002, self.KI * e))
				self.period = min(max(self.period, self.base * 0.97), self.base * 1.03)
				self.pe = e - (self.next - n0) - (self.period - p0)   # where this kick's next one should sit vs the moved grid
			else:
				self.pe = None
				self._wide_check(g, P, pv)                     # only an unclear hit on the beat: a clear kick beside it?
			self.kstr = pv if self.kstr == 0 else self.kstr + (pv - self.kstr) * 0.2
			self.gpv = pv
			if sure:
				rk = max(self.rsig[max(0, i0 + k - 5):i0 + k + 6])          # this kick's attack rise
				self.krise = rk if self.krise == 0 else self.krise + (rk - self.krise) * 0.2
			self.streak += 1
			self.lastpres = g
			# half-speed guard: real kicks also landing exactly halfway between beats -> the tempo is double
			m0 = len(o) - 1 - int(round((ts - (g - P / 2 - self.W)) * self.ORATE))
			m1 = len(o) - 1 - int(round((ts - (g - P / 2 + self.W)) * self.ORATE))
			if 0 <= m0 < m1:
				km = int(np.argmax(o[m0:m1 + 1]))
				pm = o[m0 + km]
				tm = g - P / 2 - self.W + km / self.ORATE
				midkick = pm >= 0.5 * self.kstr and self.sweep(tm) >= self.SWEEP
			else:
				midkick = False
			self.mid = self.mid + 1 if midkick else 0
			if self.mid >= 1 and P / 2 > 60.0 / self.BPM_HI:
				self.period = self.base = P / 2
				if self.next - P / 2 > self.t:
					self.next -= P / 2
				self.onext = self.next
				self.mid = 0
			self.offk = []
			self.kerr.append((g + e, e))
		else:
			if self.streak > 0:
				self.prun2, self.prevrun = self.prevrun, self.streak   # how long the kick had been playing
			self.streak = 0
			# not on the beat: is a clear kick just beside it (the grid jumped: next track, phase shift)?
			# the same off-grid spot two beats running -> move the grid there at once
			self._wide_check(g, P, pv)

	def kick_before(self, a):
		"""Send the beat at time a? If the kick was there on the beat before (checked by then: a beat's window is
		complete ~0.16 s after it, and beats are at least 0.27 s apart). One kick skipped in a steady pattern (a fill)
		doesn't stop the beat: two missing in a row do."""
		P = self.period
		if a - 4 * P < self.lastchk < a - 1.3 * P:
			return self.streak >= 1          # that beat's check isn't done yet (audio arriving late): go by the last one
		if abs(self.lastchk - (a - P)) >= 0.3 * P:
			return False
		if self.streak >= 1:
			return True
		return self.prevrun >= 4 and abs(self.lastpres - (a - 2 * P)) < 0.3 * P

	# ---- instant start: music begins after silence ----
	def _quick_start(self):
		"""Right after pressing play (DJs cue on the kick): the first two hits fire a beat the moment they are heard,
		until the grid is locked from the real kicks (which then continues from kick 3)."""
		H = self.H
		if self.instant >= 1 or len(H) < 80 or self.quiet_s > 0.0:
			return
		cur = H[-1]
		self.qmax = max(self.qmax * 0.9995, cur)
		if cur > 3.0 * min(H[-70:-10]) + 1e-6 and cur > 0.3 * self.qmax and self.t - self.qlast > 0.25:
			self.qlast = self.t
			self.instant += 1
			self.outbox.append(self.t)               # out right now (the sender empties the outbox)
			self.beats.append(self.t)

	def _lock(self, kick_t, P):
		"""New grid through a kick that has just been heard (its beat is already out): beats continue from the next."""
		self.period = self.base = P
		self.next = kick_t + P
		while self.next <= self.t:
			self.next += P
		self.onext = self.next
		self.lastchk = self.next - P                   # that kick's beat counts as checked...
		self.streak = 1                                # ...and present
		self.offk = []
		self.mid = 0
		self.lastsure = 2
		self.locked = True

	def _first_lock(self, k):
		"""No grid yet: lock from real (confirmed) kicks. After pressing play, two kicks are enough; starting in the
		middle of music, three evenly spaced ones. Beats then start on the next kick, always on the beat."""
		self.qon = (self.qon + [k])[-3:]
		q = self.qon
		lo, hi = 60.0 / self.BPM_HI, 60.0 / self.BPM_LO
		if self.armed and len(q) >= 2 and lo < q[-1] - q[-2] < hi:
			I = q[-1] - q[-2]
			# a kick missed in between? (then the two are two beats apart): look for it halfway
			pm, tm = self._peak(q[-2] + I / 2, self.W)
			if I / 2 > lo and (I > 0.6 or (pm >= 0.5 * self._peak(k, 0.01)[0] and self.sweep(tm) >= 1.3)):
				I /= 2                                 # under 100 BPM: two beats apart (dance music), one kick missed
			self._lock(k, I)
			self.armed = False
		elif len(q) == 3:
			i1, i2 = q[1] - q[0], q[2] - q[1]
			if lo < i1 < hi and abs(i1 - i2) < 0.03 * i1:
				self._lock(k, (i1 + i2) / 2.0)

	# ---- individual kicks (peaks of the score): used only while the kick is gone, to find where it came back ----
	def _kick_detect(self, s, t):
		if self.cand is None:
			if s > self.thr and t - self.lastkick > 0.25:
				self.cand = (s, t)
		else:
			if s > self.cand[0]:
				self.cand = (s, t)
			elif s < 0.5 * self.cand[0] or t - self.cand[1] > 0.03:
				k, ks = self.cand[1], self.cand[0]
				self.cand = None
				k = self._peak(k, 0.015)[1]                 # timed like the per-beat check: at the score's peak
				if self.sweep(k) < self.SWEEP:            # its pitch doesn't drop: bass, pads, voice... not a kick
					return
				self.lastkick = k
				self.kicks.append(k)
				if self.locked:
					self._locked_kick(k, t)
				else:
					self._first_lock(k)

	def _peak(self, t, w):
		"""(value, time) of the biggest absolute kick score within t +- w."""
		now = self.t - self.D / self.ORATE
		n = len(self.asig)
		i0 = max(0, n - 1 - int(round((now - (t - w)) * self.ORATE)))
		i1 = min(n - 1, n - 1 - int(round((now - (t + w)) * self.ORATE)))
		if i1 < i0:
			return 0.0, t
		seg = self.asig[i0:i1 + 1]
		k = int(np.argmax(seg))
		r = self.rsig[i0:i1 + 1]
		kr = max(0, k - 15) + int(np.argmax(r[max(0, k - 15):k + 6]))      # time it by its attack
		return seg[k], now - (n - 1 - (i0 + kr)) / self.ORATE

	def _freq(self, t0, t1):
		"""Pitch (Hz) of the 30-250 Hz band between audio times t0 and t1 (zero crossings)."""
		first = self.zn - len(self.zb)
		a, b = int(t0 * self.fs) - first, int(t1 * self.fs) - first
		if a < 0 or b > len(self.zb) or b - a < 4:
			return 0.0
		seg = self.zb[a:b]
		zc = [i for i in range(1, len(seg)) if (seg[i] < 0) != (seg[i - 1] < 0)]
		if len(zc) < 2:
			return 0.0
		return (len(zc) - 1) / 2.0 / ((zc[-1] - zc[0]) / self.fs)

	def sweep(self, k):
		"""How much the pitch drops after the hit at time k: a kick sweeps down ~2x, everything else ~1x."""
		late = self._freq(k + 0.05, k + 0.10)
		return self._freq(k, k + 0.025) / late if late > 0 else 0.0

	def _locked_kick(self, k, now):
		"""A real kick (its pitch drops) while there is a grid.
		 - kick back on the grid: _check_beat already restarted the beats. Its next kick tells if the tempo changed
		   (a new track): then the grid takes that tempo, from the 3rd kick on.
		 - kick back off the grid (a new track somewhere else): two kicks one beat apart (or three evenly spaced, any
		   tempo) give the new position and tempo; beats continue from the next kick.
		 - anything else is covered by the per-beat check."""
		i = len(self.asig) - 1 - int(round((now - k) * self.ORATE))
		a = max(self.asig[max(0, i - 10):i + 11]) if 0 <= i < len(self.asig) else 0.0
		if self.kstr > 0 and a < 0.4 * self.kstr:
			return                                     # too weak for this track's kick
		P = self.period
		pk, self.prevk = self.prevk, k
		if self.ret is not None:
			if abs(k - self.ret) < self.W:
				return                                 # that's the kick that came back
			r = pk if pk is not None and abs(pk - self.ret) < self.W else self.ret
			n = round((k - r) / P)                     # the kicks in between may not all have been heard
			if 1 <= n <= 4:
				I = (k - r) / n
				if 0.92 * P < I < 1.08 * P:
					self.ret = None
					if abs(I - P) > 0.025 * P:
						self._halfway(k, I)            # maybe a new tempo: kick 3 decides
					return
			if k - r > 4.5 * P:
				self.ret = None
		if self.tpend is not None and self._confirm(k):
			return
		if self.streak > 0:
			# just back, but on something weak: a steady kick a beat apart, off the grid and much stronger, is the real one
			if self.streak <= 4 and self.gpv > 0 and a >= 2.0 * self.gpv:
				e = (k - self.next + P / 2) % P - P / 2
				if abs(e) >= self.W:
					self.offs = [x for x in self.offs if k - x < 2.2 * P] + [k]
					I = self.offs[-1] - self.offs[-2] if len(self.offs) >= 2 else 0.0
					if 0.92 * P < I < 1.08 * P:
						self._rephase(k, P if abs(I - P) < 0.025 * P else I)
			return
		e = (k - self.next + P / 2) % P - P / 2
		if abs(e) < self.W:
			return                                     # on the grid: the per-beat check takes it
		self.offk = (self.offk + [k])[-3:]
		q = self.offk
		I = q[-1] - q[-2] if len(q) >= 2 else 0.0
		if 0.92 * P < I < 1.08 * P:
			self._rephase(k, I)
		elif len(q) == 3:
			i1 = q[1] - q[0]
			if 60.0 / self.BPM_HI < i1 < 60.0 / self.BPM_LO and abs(i1 - I) < 0.03 * i1:
				self._rephase(k, (i1 + I) / 2.0)

	def _instant_return(self):
		"""The kick is gone; it comes back on the grid: its beat can't be predicted (it wasn't there on the beat
		before), so fire it the moment its attack is heard (~60 ms later than a predicted beat, but on that kick)."""
		# a steady run before the gap (one stray hit in the gap doesn't count as a new run)
		if self.idis or self.streak > 0 or max(self.prevrun, self.prun2) < 4 or self.krise <= 0 or len(self.H) < 20:
			return
		P = self.period
		g = self.next - P if self.next - self.t > P / 2 else self.next   # nearest grid beat
		if abs(self.t - g) > 0.03 or abs(g - self.ifired) < 0.5 * P:
			return
		if self.kick_before(g):
			return                                     # its beat goes out anyway (one skipped kick)
		H = self.H
		rise = H[-1] - min(H[-15:-5])                  # attack rise over the last ~12 ms, like the kick score's
		if rise >= 0.8 * self.krise:
			self.ifired = g
			self.outbox.append(self.t)
			self.beats.append(self.t)

	def _wide_check(self, g, P, pin=0.0):
		o = self.asig
		now = self.t - self.D / self.ORATE
		n = len(o)
		best = None
		for lo, hi in ((g - 0.3 * P, g - self.W), (g + self.W, g + 0.3 * P)):
			i0 = n - 1 - int(round((now - lo) * self.ORATE)); i1 = n - 1 - int(round((now - hi) * self.ORATE))
			if i0 < 1 or i1 >= n - 1 or i1 <= i0:
				continue
			k = i0 + int(np.argmax(o[i0:i1 + 1]))
			if k in (i0, i1):
				continue                                   # peak must be inside the side window
			if best is None or o[k] > best[0]:
				best = (o[k], now - (n - 1 - k) / self.ORATE)
		if best is None or self.kstr <= 0 or best[0] < max(0.5 * self.kstr, 2.0 * pin):
			self.wide = None
			return
		pv, tp = self._peak(best[1], 0.01)
		if self.sweep(tp) < 1.3:
			self.wide = None
			return
		off = tp - g
		if self.wide is not None and abs(off - self.wide) < 0.01:
			self.wide = None
			self._rephase(tp, P)
			self.streak = 1                                # that kick was there: the next beat goes out
			self.lastsure = 2
			return
		self.wide = off

	def _halfway(self, k2, I):
		"""Kick 2 says another tempo, but one early/late kick says the same: aim kick 3 halfway between the two
		(either way it's off by half as much), and let kick 3 decide."""
		P = self.period
		nx = k2 + (P + I) / 2.0
		while nx <= self.t:
			nx += P
		self.next = self.onext = nx
		self.lastchk = max(self.lastchk, k2)
		self.streak += 1
		self.lastpres = k2
		self.pe = None
		self.tpend = (k2, I)

	def _confirm(self, k):
		"""Kick 3 after a return: the same new tempo again -> take it; the old tempo -> kick 1 was just early/late."""
		k2, I = self.tpend
		if abs(k - k2) < self.W:
			return False                               # kick 2 itself
		self.tpend = None
		I2 = k - k2
		if abs(I2 - I) < 0.025 * I and abs(I2 - self.period) > 0.025 * self.period:
			self._rephase(k, (I + I2) / 2.0)
			return True
		return False

	def _rephase(self, k, P):
		self.period = self.base = P
		self.next = k + P
		while self.next <= self.t:
			self.next += P
		self.onext = self.next
		self.lastchk = self.next - P if self.next - P > k + 0.5 * P else k
		self.streak = max(self.streak, 1)
		self.lastsure = 0                              # moved grid: only a sure kick keeps it going
		self.offk = []
		self.ret = None
		self.tpend = None
		self.offs = []
		self.mid = 0

	# ---- first lock when the program starts in the middle of music ----
	def _estimate(self):
		N = int(self.HIST * self.ORATE)
		o = np.asarray(self.osig[-N:], dtype=float)
		n = len(o)
		oc = o - o.mean()
		if n < 3000 or o.max() <= 0:
			return
		F = np.fft.rfft(oc, 2 * n)
		ac = np.fft.irfft(F * np.conj(F))[:n] / (n - np.arange(n))
		lo, hi = int(60.0 / self.BPM_HI * self.ORATE), int(60.0 / self.BPM_LO * self.ORATE)
		seg = ac[lo:hi + 1]
		peaks = [L for L in range(1, len(seg) - 1) if seg[L] > seg[L - 1] and seg[L] >= seg[L + 1] and seg[L] > 0]
		if not peaks:
			return
		top = max(seg[L] for L in peaks)
		Lb = lo + min(L for L in peaks if seg[L] >= 0.6 * top)     # the fastest pulse the kicks clearly repeat at
		m = 4 if 4 * Lb * 1.03 < n - 1000 else 2
		a, z = int(m * Lb * 0.97), int(m * Lb * 1.03) + 1
		if z + 2 >= n:
			return
		i = a + int(np.argmax(ac[a:z + 1]))
		den = ac[i - 1] - 2 * ac[i] + ac[i + 1]
		frac = 0.5 * (ac[i - 1] - ac[i + 1]) / den if den < 0 else 0.0
		period = (i + frac) / m / self.ORATE
		if not (60.0 / self.BPM_HI * 0.97 <= period <= 60.0 / self.BPM_LO * 1.03):
			return
		P = period * self.ORATE
		idx = (n - 1 - np.round(np.arange(int(P))[:, None] + np.arange(8)[None, :] * P)).astype(int)
		score = o[np.where(idx >= 0, idx, 0)].sum(1)
		last_beat = self.t - self.D / self.ORATE - int(np.argmax(score)) / self.ORATE
		prev = self.relock
		self.relock = (period, last_beat)                 # lock only when two estimates in a row agree
		if prev is None or abs(prev[0] - period) > 0.005 or \
				abs((last_beat - prev[1] + period / 2) % period - period / 2) > 0.015:
			return
		self.relock = None
		self.period = self.base = period
		self.next = last_beat + period
		while self.next <= self.t:
			self.next += period
		self.onext = self.next
		self.lastchk = self.next - period - 1.0          # nothing checked yet: beats start once a kick is confirmed
		self.streak = 0
		self.locked = True

	@property
	def bpm(self):
		return 60.0 / self.period
