# kick-sync receiver for TouchDesigner. Creates /project1/kick_sync: an OSC In DAT that receives kick-sync's beats
# and a CHOP you can link to anything.
#
# Run once in the Textport (Alt+T), then save your project:
#   exec(open(r'C:/path/to/kick-sync/touchdesigner/kick_sync_receiver.py').read())
#
# Use it (all on /project1/kick_sync):
#   CHOP  op('/project1/kick_sync/beat')   channels:
#       pulse    1 on each beat, falling to 0 (shape set by 'decay')      e.g. scale = 1 + op('/project1/kick_sync/beat')['pulse'] * 0.3
#       beat     beat count (1, 2, 3 ...)            bar   beat in the bar (0-3)
#       bpm      current tempo                       locked  1 when locked to the kick
#   Python  op('/project1/kick_sync').fetch('age')   seconds since the last beat (for your own shapes: exp(-age * k))
#   Pulse length for everything:  op('/project1/kick_sync').store('decay', 5.0)    bigger = shorter, snappier
#   OSC port:  op('/project1/kick_sync/osc').par.port = 7400   (must match kick-sync --port)

PORT = 7400
root = op('/project1')
ks = root.op('kick_sync') or root.create(baseCOMP, 'kick_sync')
for c in list(ks.children):
	c.destroy()
if ks.fetch('decay', None) is None:
	ks.store('decay', 5.0)
ks.store('last', -100.0)
ks.store('n', 0)

cb = ks.create(textDAT, 'osc_callbacks')
cb.text = '''
def onReceiveOSC(dat, rowIndex, message, byteData, timeStamp, address, args, peer):
	k = parent()
	now = absTime.seconds
	if address == '/beat':
		k.store('last', now)
		k.store('n', k.fetch('n', 0) + 1)
		if len(args) > 1 and args[1] > 0:
			k.store('bpm', float(args[1]))
	elif address == '/state':
		k.store('seen', now)
		if args and args[0] > 0:
			k.store('bpm', float(args[0]))
		k.store('locked', int(args[1]) if len(args) > 1 else 1)
	return
'''
osc = ks.create(oscinDAT, 'osc')
osc.nodeX = 200
osc.par.port = PORT
osc.par.callbacks = cb.path

clock = ks.create(executeDAT, 'clock')
clock.nodeY = -200
clock.text = '''
import math

def onFrameStart(frame):
	k = parent()
	age = max(0.0, absTime.seconds - k.fetch('last', -100.0))
	k.store('age', age)
	k.store('pulse', math.exp(-age * k.fetch('decay', 5.0)))
	if absTime.seconds - k.fetch('seen', -100.0) > 1.0:      # kick-sync not running
		k.store('locked', 0)
	return
'''
clock.par.framestart = True

beat = ks.create(constantCHOP, 'beat')
beat.nodeX, beat.nodeY = 200, -200
chans = (('pulse', "parent().fetch('pulse', 0.0)"), ('beat', "parent().fetch('n', 0)"),
         ('bar', "parent().fetch('n', 0) % 4"), ('bpm', "parent().fetch('bpm', 0.0)"),
         ('locked', "parent().fetch('locked', 0)"))
for i, (name, expr) in enumerate(chans):
	getattr(beat.par, f'name{i}').val = name
	getattr(beat.par, f'value{i}').expr = expr

print('kick-sync receiver ready on port', PORT, '- link op("/project1/kick_sync/beat")["pulse"] to anything.',
      'errors:', [(c.name, c.errors()) for c in ks.children if c.errors()] or 'none')
