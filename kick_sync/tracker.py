# Kick-only beat tracker, streaming: feed it audio blocks, it keeps a beat grid locked to the kick drum.
# Used by the live program (cli.py: audio in -> OSC out) and by the tests (offline, on recordings),
# so what is tested is exactly what runs live.
#
# What a kick looks like (measured on real club music): its attack jumps up at 100-200 Hz (the pitch starts high),
# then within ~100 ms its body swells at 30-60 Hz (the pitch drops). Offbeat bass and hats don't do both, so:
#   1. two bands from the audio averaged down 8x (5.5 kHz): band-pass 140 Hz (attack) and low-pass 60 Hz (body)
#   2. kick score (1 kHz): attack rise x body swell in the next 60-110 ms (so it is known 110 ms late; fine,
#      the beats come from the predicted grid, kicks only correct it)
#   3. tempo: autocorrelation of the score over the last 8 s (4-beat lag, 100-160 BPM, parabolic peak)
#   4. phase: comb over the last 8 beats; then every detected kick near a predicted beat nudges the grid
#      (phase-locked loop), so it follows to the millisecond and coasts through breakdowns
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
	HIST = 8.0            # seconds of history for tempo / phase
	D = 110               # score delay (ms): how far ahead the body swell is looked for
	BPM_LO, BPM_HI = 60.0, 220.0       # any tempo
	SILENCE = 0.0005                    # RMS below this = stopped (a stopped player = exactly 0; music between kicks ~0.005)
	KP, KI, PCAP = 0.3, 0.003, 0.012    # phase-locked loop: phase gain, tempo gain, max phase move per kick (s)

	def __init__(self, sr):
		self.sr = sr
		self.fs = sr / self.DEC
		self.bp = Biquad(self.fs, 140.0, 1.0, 'bp')
		self.lp = [Biquad(self.fs, 60.0), Biquad(self.fs, 60.0)]
		self.eh = self.el = 0.0
		self.sh = self.sl = 1e-4               # slow levels: makes the score loudness-independent
		self.rem = np.zeros(0)
		self.t = 0.0                           # seconds of audio consumed
		self.H, self.L, self.C = [], [], [0.0]  # attack env, body env, running sum of body env (1 kHz)
		self.osig = []                         # kick score (1 kHz), last entry = time self.t - D ms
		self.ophase = 0.0
		self.thr = 1e9
		self.cand = None                       # (score, time) of the kick being detected
		self.period = 60.0 / 124.0
		self.next = None                       # internal grid: predicted time of the next beat (audio seconds)
		self.onext = None                      # output clock: time of the next beat that goes out
		self.base = self.period                # tempo at the last lock
		self.locked = False
		self.beats, self.kicks = [], []
		self.kerr = []
		self.kstr = 0.0                        # typical strength of the on-grid kicks
		self.quiet_s = 0.0                     # seconds of silence so far
		self.qon, self.qlast, self.qmax = [], -1.0, 0.0   # quick start: kicks heard before the grid exists
		self.armed = False                     # instant start only for music that starts after silence
		self.outbox = []                       # beats already heard (instant start) waiting to be sent
		self.snap = False                      # set on a relock: the output clock jumps to the new grid
		self.miss = 0
		self.misses = []
		self.relock = None
		self.lastkick = -1.0
		self._since_est = 0.0

	def reset(self):
		"""Music stopped: no beats until the next track is locked."""
		self.locked = False
		self.next = self.onext = None
		self.relock = None
		self.miss, self.misses, self.snap = 0, [], False
		self.osig.clear()
		self.qon, self.qlast, self.qmax = [], -1.0, 0.0

	# ---- feeding audio ----
	def feed(self, block):
		"""block: float array (n,) or (n, ch). Returns beat times (audio seconds) that fell inside this block."""
		x = block.mean(1) if block.ndim == 2 else block
		# music stopped (a stopped player sends pure digital silence): stop the beats and forget the grid, so the looks
		# rest and the next track locks fresh. Breakdowns are never this quiet.
		if float(np.sqrt(np.mean(x * x))) < self.SILENCE:
			self.quiet_s += len(x) / self.sr
			if self.quiet_s > 0.15:
				if self.locked or self.onext is not None:
					self.reset()
				self.armed = True                     # silence seen: the next music start gets the instant start
		else:
			self.quiet_s = 0.0
		x = np.concatenate([self.rem, x])
		n = len(x) // self.DEC * self.DEC
		self.rem = x[n:]
		d = x[:n].reshape(-1, self.DEC).mean(1).tolist()
		h = self.bp.run(d)
		l = self.lp[1].run(self.lp[0].run(d))
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
				# output clock: the beats that go out. It follows the internal grid but moves at most 10 ms per
				# beat, so whatever the grid does, the output never skips, doubles or jumps
				if self.onext is None:
					self.onext = self.next
				if self.t >= self.onext:
					fired.append(self.onext)
					self.beats.append(self.onext)
					nxt = self.onext + self.period
					diff = (self.next + self.period - nxt + self.period / 2) % self.period - self.period / 2
					if self.snap:                      # a relock on strong evidence: go straight to the new grid
						self.snap = False
						self.onext = nxt + diff if diff > -0.4 * self.period else nxt + diff + self.period
					else:
						self.onext = nxt + max(-0.01, min(0.01, diff))
				while self.t >= self.next:
					self.next += self.period
		self._since_est += len(h) * dt
		if self._since_est >= 0.25 and len(self.osig) >= int(4.0 * self.ORATE):
			self._since_est = 0.0
			self._estimate()
		return fired

	def _tick(self):
		H, L, C = self.H, self.L, self.C
		H.append(self.eh)
		L.append(self.el)
		C.append(C[-1] + self.el)
		n = len(H)
		if not self.locked:
			self._quick_start()
		j = n - 1 - self.D
		if j < 30:
			return
		# attack: rise over 12 ms around j; body: mean 60-110 ms after j minus mean 25-5 ms before j
		ra = max(0.0, H[j + 8] - H[j - 4]) / (self.sh + 1e-6)
		rb = max(0.0, (C[j + 110] - C[j + 60]) / 50.0 - (C[j - 5] - C[j - 25]) / 20.0) / (self.sl + 1e-6)
		s = ra * math.sqrt(rb)
		# a kick's pitch sweeps down, so its 100-200 Hz attack fades fast: 80 ms later it is under ~50% of its peak
		# (measured: real kicks 46%, offbeat bass / other hits 64-85%). Sustained hits are not kicks.
		if s > 0.0:
			pk = max(H[j:j + 20]) + 1e-9
			s *= min(1.0, max(0.0, (0.75 - H[j + 80] / pk) / 0.3))
		self.osig.append(s)
		if n > 4000:                                       # envelopes: only the last few hundred ms are needed
			del H[:2000], L[:2000], C[:2000]
		if len(self.osig) > int(self.HIST * self.ORATE) + 2000:
			del self.osig[:2000]
		if self.osig and len(self.osig) % 50 == 0:
			w = self.osig[-2000:]
			self.thr = max(0.05, 0.35 * max(w))
		self._kick_detect(s, self.t - self.D / self.ORATE)

	def _quick_start(self):
		"""Before the grid exists (start of the program, music just started): fire a beat on every kick the moment it
		is heard, and lock the grid from the first three kicks, so the visuals start with the music."""
		H = self.H
		if not self.armed or len(H) < 80 or self.quiet_s > 0.0:
			return
		cur = H[-1]
		self.qmax = max(self.qmax * 0.9995, cur)
		if cur > 3.0 * min(H[-70:-10]) + 1e-6 and cur > 0.3 * self.qmax and self.t - self.qlast > 0.25:
			self.qlast = self.t
			self.outbox.append(self.t)               # out right now (the sender empties the outbox)
			self.beats.append(self.t)
			self.qon.append(self.t)
			q = self.qon[-3:]
			if len(q) == 3:
				i1, i2 = q[1] - q[0], q[2] - q[1]
				if 60.0 / self.BPM_HI < i1 < 60.0 / self.BPM_LO and abs(i1 - i2) < 0.03 * i1:
					P = (i1 + i2) / 2.0
					self.period = self.base = P
					self.next = q[2] + P                  # (the beat for this kick is still on its way out)
					self.locked = True
					self.armed = False

	# ---- individual kicks: peaks of the score ----
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
				self.lastkick = k
				self.kicks.append(k)
				self._correct(k, ks)

	def _correct(self, k, ks=1.0):
		"""A kick near a predicted beat pulls the grid onto it (phase-locked loop)."""
		if self.next is None or not self.locked:
			return
		m = round((self.next - k) / self.period)
		e = k - (self.next - m * self.period)
		self.kerr.append((k, e))                       # for the live log: how far each kick is from the grid
		if abs(e) < 0.12 * self.period:
			# small, capped nudges: one odd kick can't throw the grid
			self.next += max(-self.PCAP, min(self.PCAP, self.KP * e))
			self.period += max(-0.0005, min(0.0005, self.KI * e))
			# the tempo can't wander away from where it locked (a DJ set doesn't jump tempo without a relock)
			self.period = min(max(self.period, self.base * 0.99), self.base * 1.01)
			self.kstr += (ks - self.kstr) * 0.2        # how strong the real (on-grid) kicks are
			self.miss = 0
			self.misses = []
		elif ks > 0.6 * self.kstr:
			# a strong kick off the grid: evidence the beat moved. Weak off-grid hits (offbeat bass in a breakdown,
			# percussion) are ignored completely, so they can never drag the grid
			self.miss += 1
			self.misses.append(e)

	# ---- tempo + phase from the score history ----
	def _estimate(self):
		N = int(self.HIST * self.ORATE)
		raw = self.osig[-N:]
		o = np.asarray(raw, dtype=float)
		n = len(o)
		oc = o - o.mean()
		if n < 3000 or o.max() <= 0:
			return
		# autocorrelation of the kick score (FFT), any tempo BPM_LO..BPM_HI, no favourite tempo
		F = np.fft.rfft(oc, 2 * n)
		ac = np.fft.irfft(F * np.conj(F))[:n] / (n - np.arange(n))
		lo, hi = int(60.0 / self.BPM_HI * self.ORATE), int(60.0 / self.BPM_LO * self.ORATE)
		seg = ac[lo:hi + 1]
		peaks = [L for L in range(1, len(seg) - 1) if seg[L] > seg[L - 1] and seg[L] >= seg[L + 1] and seg[L] > 0]
		if not peaks:
			return
		top = max(seg[L] for L in peaks)
		# the beat = the fastest pulse the kicks clearly repeat at (so a fast track is never taken at half speed)
		Lb = lo + min(L for L in peaks if seg[L] >= 0.6 * top)
		# precision: refine over 4 beats (or 2 when 4 don't fit in the history), parabolic peak
		m = 4 if 4 * Lb * 1.03 < n - 1000 else 2
		a, z = int(m * Lb * 0.97), int(m * Lb * 1.03) + 1
		if z + 2 >= n:
			return
		i = a + int(np.argmax(ac[a:z + 1]))
		den = ac[i - 1] - 2 * ac[i] + ac[i + 1]
		frac = 0.5 * (ac[i - 1] - ac[i + 1]) / den if den < 0 else 0.0
		period = (i + frac) / m / self.ORATE
		if not (60.0 / self.BPM_HI * 0.97 <= period <= 60.0 / self.BPM_LO * 1.03):     # nonsense estimate: ignore
			return
		# phase: comb over the last 8 beats, 1 ms steps
		P = period * self.ORATE
		idx = (n - 1 - np.round(np.arange(int(P))[:, None] + np.arange(8)[None, :] * P)).astype(int)
		idx = np.where(idx >= 0, idx, 0)
		score = o[idx].sum(1)
		bph = int(np.argmax(score))
		tend = self.t - self.D / self.ORATE                # time of the newest score entry
		last_beat = tend - bph / self.ORATE
		if not self.locked:
			# first lock: only when two estimates in a row agree (no wrong guess at the start)
			prev = self.relock
			self.relock = (period, last_beat)
			if prev is None or abs(prev[0] - period) > 0.005 or \
					abs((last_beat - prev[1] + period / 2) % period - period / 2) > 0.015:
				return
			self.relock = None
			self.period = period
			self.base = period
			self.next = last_beat + period
			while self.next <= self.t:
				self.next += period
			self.locked = True
			return
		# running: no clear kicks (breakdown, blend) -> coast, change nothing
		recent = [k for k in self.kicks if k > self.t - 4.0]
		if len(recent) < 0.6 * 4.0 / self.period:      # fewer than ~60% of the beats had a kick: coast
			return
		# relock only on strong evidence: 8 kicks in a row off the grid by the same amount (a beatmatched mix keeps
		# the grid, so this is rare; stray percussion off the beat must never move it)
		# an estimate at half / double / 1.5x the tempo is the same groove heard differently (rolling bass, sparse
		# kicks), never a reason to relock; only a genuinely different tempo is
		r = period / self.period
		same_groove = any(abs(r - m) < 0.04 * m for m in (0.5, 2.0, 1.5, 2.0 / 3.0))
		wrong_tempo = abs(period - self.period) > 0.04 * self.period and not same_groove
		# a jump of about half a beat is nearly always the offbeat bass fooling us, almost never a real mix: needs 16
		mo = abs(float(np.median(self.misses[-6:]))) if self.misses else 0.0
		need = 16 if abs(mo - self.period / 2) < 0.12 * self.period else 6
		if (self.miss >= need and np.std(self.misses[-need:]) < 0.02) or wrong_tempo:
			# the kicks have moved (new track / tempo): relock, but only if two estimates in a row agree
			prev = self.relock
			self.relock = (period, last_beat)
			if prev is not None and abs(prev[0] - period) < 0.005 and \
					abs((last_beat - prev[1] + period / 2) % period - period / 2) < 0.015:
				self.period = period
				self.base = period
				self.snap = True
				self.next = last_beat + period
				while self.next <= self.t:
					self.next += period
				self.miss = 0
				self.misses = []
				self.relock = None
			return
		self.relock = None
		# locked and kicks agree: the tempo comes only from the kicks themselves (_correct), far more precise than
		# this estimate, which only serves to lock and to relock

	@property
	def bpm(self):
		return 60.0 / self.period
