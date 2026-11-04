# kick-sync: live beat sync from the kick drum. Listens to an audio input (a DJ player / mixer / controller used as
# a sound card), keeps a beat grid locked to the kick (tracker.py) and sends every beat over OSC, early by a "lead"
# so the visual lands when you hear the kick (audio input + render + screen delay).
#
#   kick-sync --list                       list audio inputs
#   kick-sync --device "XDJ"               run on the input whose name contains "XDJ" (default: system default input)
#   kick-sync --record mytrack.wav 60      record 60 s from the input (to test with tests/chain_test.py)
#
# Keys in its window (Windows):  [ / ] = beats 5 ms earlier / later   0 = lead back to 60 ms   r = resync now   q = quit
# The lead is remembered.
# OSC (default 127.0.0.1:7400):
#   /beat   int n, float bpm, float t     at each beat (n counts beats; t = sender clock, for measuring delays)
#   /state  float bpm, int locked         4 times a second (lets the receiver know the program is alive)
import argparse, os, sys, time, threading, queue, wave, socket, traceback
import numpy as np
import sounddevice as sd
from pythonosc.udp_client import SimpleUDPClient
from .tracker import KickTracker
from .sender import Sender, ClockSync, GapFill

HOME = os.path.join(os.path.expanduser('~'), '.kick-sync')
LEAD_FILE = os.path.join(HOME, 'lead_ms.txt')

def find_device(name):
	"""Input device whose name contains `name` (case-insensitive), preferring Windows WASAPI (shared: other apps can
	use the same input). None = the system default input. Returns (index, samplerate) or None if not found."""
	devs = sd.query_devices()
	if not name:
		i = sd.default.device[0]
		return (i, int(devs[i]['default_samplerate'])) if i is not None and i >= 0 else None
	hits = [i for i, d in enumerate(devs) if name.lower() in d['name'].lower() and d['max_input_channels'] > 0]
	if not hits:
		return None
	wasapi = [i for i in hits if 'wasapi' in sd.query_hostapis(devs[i]['hostapi'])['name'].lower()]
	i = (wasapi or hits)[0]
	return i, int(devs[i]['default_samplerate'])

def list_devices():
	for i, d in enumerate(sd.query_devices()):
		if d['max_input_channels'] > 0:
			print(f'{i:3d}  {d["name"]}  ({sd.query_hostapis(d["hostapi"])["name"]}, {int(d["default_samplerate"])} Hz)')

def wait_for(name):
	while True:
		found = find_device(name)
		if found:
			return found
		print(f'waiting for an input matching "{name}" (plug it in / turn it on)...', end='\r', flush=True)
		time.sleep(2)
		sd._terminate(); sd._initialize()            # re-scan the audio devices

def record(name, path, secs):
	dev, sr = wait_for(name)
	x = sd.rec(int(secs * sr), samplerate=sr, channels=2, device=dev, dtype='float32')
	sd.wait()
	with wave.open(path, 'wb') as w:
		w.setnchannels(2); w.setsampwidth(2); w.setframerate(sr)
		w.writeframes((np.clip(x, -1, 1) * 32767).astype('<i2').tobytes())
	print(f'saved {path} ({secs:.0f} s)')

def run(args):
	os.makedirs(HOME, exist_ok=True)
	lead = args.lead if args.lead is not None else (float(open(LEAD_FILE).read()) if os.path.exists(LEAD_FILE) else 60.0)
	# only one copy per OSC port: a second one just closes
	lock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
	try:
		lock.bind(('127.0.0.1', args.port + 9))
	except OSError:
		print('kick-sync is already running for this port - closing')
		time.sleep(3)
		return
	dev, sr = wait_for(args.device)
	tr = KickTracker(sr)
	osc = SimpleUDPClient(args.host, args.port)
	blocks = queue.Queue()
	clock = {'off': None}           # wall clock minus audio time (ClockSync: least-buffered value of the last 3 s)
	sync = ClockSync()
	gap = GapFill(sr)               # puts back audio lost on the way in (USB / Windows audio engine)
	state = {'lead': lead}

	def callback(indata, frames, t, status):
		blocks.put((time.perf_counter(), indata.copy()))

	def sender():
		n = 0
		snd = Sender(tr)
		while True:
			d = snd.due(time.perf_counter(), clock['off'], state['lead'] / 1000.0)
			if d is None:
				time.sleep(0.002)
				continue
			target, a = d
			if target - time.perf_counter() > 0.004:
				time.sleep(min(target - time.perf_counter() - 0.003, 0.02))
				continue
			while time.perf_counter() < target:
				pass
			n += 1
			osc.send_message('/beat', [n, round(tr.bpm, 2), time.perf_counter()])
			snd.mark(a)

	threading.Thread(target=sender, daemon=True).start()
	keys = None
	if sys.platform == 'win32':
		import msvcrt as keys
	print(f'kick-sync on "{sd.query_devices(dev)["name"]}" {sr} Hz -> OSC {args.host}:{args.port}, lead {lead:.0f} ms'
	      + ('  ([ ] adjust, 0 reset lead, r resync, q quit)' if keys else ''))
	last_state = 0.0
	# latency='high': a bigger buffer, so a busy moment doesn't lose audio (beats don't come later: ClockSync measures it)
	with sd.InputStream(device=dev, samplerate=sr, channels=2, dtype='float32', blocksize=256, latency='high', callback=callback):
		while True:
			arrived, b = blocks.get()
			fill = gap.check(arrived, tr.t + len(b) / sr, sync)
			if fill is not None:                           # audio was lost: put that much back first
				tr.feed(fill)
			tr.feed(b)
			gap.push(b)
			tr.kerr.clear()
			clock['off'] = sync.update(arrived, tr.t)      # right again within 3 s after an audio dropout
			now = time.perf_counter()
			if now - last_state > 0.25:
				last_state = now
				osc.send_message('/state', [round(tr.bpm, 2), int(tr.locked)])
				print(f'\r{tr.bpm:7.2f} BPM  {"locked " if tr.locked else "listening"}  kicks {len(tr.kicks):5d}  '
				      f'lead {state["lead"]:4.0f} ms ', end='', flush=True)
			while keys and keys.kbhit():
				c = keys.getwch()
				if c in '[]':
					state['lead'] += 5 if c == '[' else -5
					open(LEAD_FILE, 'w').write(str(state['lead']))
				elif c == '0':                        # lead back to the default
					state['lead'] = 60.0
					open(LEAD_FILE, 'w').write(str(state['lead']))
				elif c in 'rR':                       # resync: forget the grid, lock fresh on the next kicks
					tr.reset()
					tr.armed = True
					print(chr(10) + 'resync - locking on the next kicks')
				elif c == 'q':
					return

def main():
	p = argparse.ArgumentParser(prog='kick-sync', description='Live beat sync from the kick drum, sent over OSC.')
	p.add_argument('--device', default='', help='audio input whose name contains this text (default: system default)')
	p.add_argument('--host', default='127.0.0.1', help='OSC target host (default 127.0.0.1)')
	p.add_argument('--port', type=int, default=7400, help='OSC target port (default 7400)')
	p.add_argument('--lead', type=float, default=None, help='send beats this many ms early (default: last used, or 60)')
	p.add_argument('--list', action='store_true', help='list audio inputs and exit')
	p.add_argument('--record', nargs=2, metavar=('FILE', 'SECONDS'), help='record the input to a .wav and exit')
	args = p.parse_args()
	if args.list:
		return list_devices()
	if args.record:
		return record(args.device, args.record[0], float(args.record[1]))
	try:
		run(args)
	except KeyboardInterrupt:
		pass
	except Exception:
		# a crash must not vanish: write it down and keep the window open
		os.makedirs(HOME, exist_ok=True)
		err = traceback.format_exc()
		with open(os.path.join(HOME, 'error.log'), 'a') as f:
			f.write(time.strftime('%Y-%m-%d %H:%M:%S ') + err + '\n')
		print('\n' + err + f'\nkick-sync stopped - the error is saved in {os.path.join(HOME, "error.log")}')
		input('Press Enter to close')

if __name__ == '__main__':
	main()
