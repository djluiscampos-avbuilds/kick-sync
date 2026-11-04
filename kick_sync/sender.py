# Decides when each beat goes out (used live by cli.py and offline by tests/chain_test.py, so the test checks the
# real thing). Every beat the tracker produces (tr.onext, audio time) is sent exactly once: predicted beats early by
# the lead, beats of an instant start (already heard) at once.
class Sender:
	def __init__(self, tr):
		self.tr = tr
		self.sent_a = None              # audio time of the last beat sent

	def due(self, now, off, lead):
		"""now / off in wall seconds (off = wall clock minus audio time). Returns (target wall time, beat audio time)
		of the next beat to send, or None when there is nothing new to send."""
		if off is None:
			return None
		if self.tr.outbox:                                         # instant-start beats: send at once, in order
			a = self.tr.outbox[0]
			return a + off - lead, a
		a = self.tr.onext
		if a is None:
			return None
		gap = 0.6 * self.tr.period if self.tr.locked else 0.2
		if self.sent_a is not None and a - self.sent_a < gap:     # this beat was already sent
			return None
		target = a + off - lead
		if now < target - 0.006:                                  # not due yet (decided at sending time)
			return target, a
		if not self.tr.kick_before(a):                            # no kick on the previous beat: no beat
			self.sent_a = a                                       # (skip this one; the next is checked again)
			return None
		return target, a

	def mark(self, a):
		self.sent_a = a
		if self.tr.outbox and self.tr.outbox[0] == a:
			self.tr.outbox.pop(0)


class ClockSync:
	"""Wall clock minus audio time, i.e. when a moment of the music actually played. Each audio block gives
	(arrival time - audio time) = that plus how long the block waited in buffers; the smallest value over the last
	few seconds is the least-buffered one. A moving window (not an all-time minimum) means that after a pause in the
	audio stream (USB / driver hiccup: the audio clock falls behind) it is right again within WINDOW seconds,
	instead of beats going out late from then on."""
	WINDOW = 3.0

	def __init__(self):
		self.hist = []                  # (arrival time, offset)
		self.off = None

	def update(self, arrived, audio_t):
		o = arrived - audio_t
		self.hist.append((arrived, o))
		while self.hist and self.hist[0][0] < arrived - self.WINDOW:
			self.hist.pop(0)
		self.off = min(v for _, v in self.hist)
		return self.off


class GapFill:
	"""Audio lost between the player and the computer (USB / Windows audio engine: chunks of 15-40 ms, not reported as
	an error) makes the music seem to jump early and run fast. It shows as blocks arriving later than their sample
	count says: for WIN seconds every block arrives at least MIN later than the clock offset (ClockSync) expects. Then
	that much audio is put back (a copy of the last few ms, so no silence step looks like a hit), and the kick times
	line up with what is heard again. Short stalls (blocks late, then catching up) aren't filled: they don't last WIN."""
	WIN, MIN, MAX = 0.25, 0.008, 0.10

	def __init__(self, sr, ch=2):
		import numpy as np
		self.np, self.sr = np, sr
		self.ring = np.zeros((int(0.2 * sr), ch), np.float32)
		self.recent = []                # (arrival time, offset) of the last WIN seconds
		self.filled = 0.0               # total seconds put back

	def check(self, arrived, audio_end, sync):
		"""Before feeding a block: arrived = its arrival (wall) time, audio_end = audio time at its end, sync = the
		ClockSync. Returns the samples to feed first (lost audio), or None."""
		o = arrived - audio_end
		self.recent.append((arrived, o))
		while self.recent and self.recent[0][0] < arrived - self.WIN - 0.05:
			self.recent.pop(0)
		if sync.off is None or not sync.hist or arrived - sync.hist[0][0] < 1.0 or self.recent[0][0] > arrived - self.WIN:
			return None
		lost = min(v for _, v in self.recent) - sync.off
		if not self.MIN < lost <= self.MAX:
			return None
		n = int(round(lost * self.sr))
		self.recent = [(t, v - n / self.sr) for t, v in self.recent]
		self.filled += n / self.sr
		return self.ring[-n:].copy()

	def push(self, b):
		"""After feeding: keep the last 0.2 s of audio (b: (n, ch) block)."""
		n = len(b)
		if n >= len(self.ring):
			self.ring[:] = b[-len(self.ring):]
		else:
			self.ring[:-n] = self.ring[n:]
			self.ring[-n:] = b
