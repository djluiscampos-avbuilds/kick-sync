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
		return a + off - lead, a

	def mark(self, a):
		self.sent_a = a
		if self.tr.outbox and self.tr.outbox[0] == a:
			self.tr.outbox.pop(0)
