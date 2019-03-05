#!/usr/bin/env python3

# Multi-channel AM demodulator for airband, CB, etc.
#
# Requisites: - all channels must be within 80% of the raw I/Q bandwidth
#             - the computer must have enough CPU
#               (use less channels, or batch process the I/Q samples,
#               in case your computer can't demodulate in real-time)
#             - Center frequency, bandwidth and channels must be all
#               multiples of 100.

import struct, numpy, sys, math, wave, filters, time, datetime
import queue, threading

monitor_strength = "-e" in sys.argv

INPUT_RATE = int(sys.argv[2])

INGEST_SIZE = INPUT_RATE // 10

CENTER=int(sys.argv[1])

freqs = []

for i in range(3, len(sys.argv)):
	if sys.argv[i] == ".":
		break
	freqs.append(int(sys.argv[i]))

STEP = 100
IF_BANDWIDTH = 10000
IF_RATE = 10000
AUDIO_BANDWIDTH = 3400
AUDIO_RATE = 10000

# -45..-48 dbFS is the minimum, 6db = 1 bit of audio
THRESHOLD = -39

assert (INPUT_RATE // IF_RATE) == (INPUT_RATE / IF_RATE)
assert (IF_RATE // AUDIO_RATE) == (IF_RATE / AUDIO_RATE)

# Makes sure IF demodulation carrier will be a multiple of STEP Hz 
# and it is in bandwidth range (80% of INPUT_RATE)
assert (INPUT_RATE / STEP == INPUT_RATE // STEP)
for f in freqs:
	if_freq = abs(CENTER - f)
	assert(if_freq / STEP == if_freq // STEP)
	assert(if_freq < (0.4 * INPUT_RATE))

tau = 2 * math.pi

class Demodulator:
	def __init__(self, freq):
		self.freq = freq
		self.wav = wave.open("%d.wav" % freq, "w")
		self.wav.setnchannels(1)
		self.wav.setsampwidth(1)
		self.wav.setframerate(AUDIO_RATE)
		self.recording = False

		# Energy estimation
		self.energy = -48
		self.ecount = 0

		# IF
		self.if_freq = CENTER - freq
		# works because both if_freq and INPUT_RATE are multiples of STEP
		self.carrier_table = [ math.cos(t * tau * (self.if_freq / INPUT_RATE))
				for t in range(0, INGEST_SIZE * 2) ]
		self.carrier_table = numpy.array(self.carrier_table)
		self.if_period = INPUT_RATE // STEP
		self.if_phase = 0

		# IF filtering
		# complex samples, filter goes from -freq to +freq
		self.if_filter = filters.low_pass(INPUT_RATE, IF_BANDWIDTH / 2, 24)
		self.if_decimator = filters.decimator(INPUT_RATE // IF_RATE)

		# Audio filter
		self.audio_filter = filters.band_pass(IF_RATE, 300, AUDIO_BANDWIDTH, 18)
		self.audio_decimator = filters.decimator(IF_RATE // AUDIO_RATE)

		# Thread
		def worker():
			while True:
				iqsamples = self.queue.get()
				if iqsamples is None:
					break
				self._ingest(iqsamples)
				self.queue.task_done()

		self.queue = queue.Queue()
		self.thread = threading.Thread(target=worker)
		self.thread.start()

	def close_queue(self):
		self.queue.put(None)

	def drain_queue(self):
		self.thread.join()

	def ingest(self, iqsamples):
		self.queue.put(iqsamples)

	def _ingest(self, iqsamples):
		self.tmbase = time.time()

		# Center frequency of samples on desired frequency

		# Get a cosine table in correct phase
		carrier = self.carrier_table[self.if_phase:self.if_phase + len(iqsamples)]
		# Advance phase
		self.if_phase = (self.if_phase + len(iqsamples)) % self.if_period
		# Demodulate
		ifsamples = iqsamples * carrier
		# print("%s %f" % ('f demod', time.time() - self.tmbase))

		# Filter IF to radio bandwidth and decimate
		ifsamples = self.if_filter.feed(ifsamples)
		ifsamples = self.if_decimator.feed(ifsamples)
		# print("%s %f" % ('f filter', time.time() - self.tmbase))

		# Find amplitude of I/Q pairs = baseband signal
		asamples = numpy.absolute(ifsamples)

		# Average signal strengh
		energy = numpy.sum(asamples) \
			* math.sqrt(INPUT_RATE / IF_RATE) \
			/ len(ifsamples)
		db = 20 * math.log10(energy)
		self.energy = 0.5 * db + 0.5 * self.energy
		self.ecount = (self.ecount + 1) % 10

		if monitor_strength and self.ecount == 0:
			print("%f: signal %f dbFS" % (self.freq, self.energy))
		if not self.recording:
			if self.energy > THRESHOLD:
				print("%s %f: signal %f dbFS, recording" % \
					(str(datetime.datetime.now()), self.freq, self.energy))
				self.recording = True
		else:
			if self.energy < THRESHOLD:
				print("%s %f: signal %f dbFS, stopping tape" % \
					(str(datetime.datetime.now()), self.freq, self.energy))
				self.recording = False

		if not self.recording:
			return

		output_raw = asamples

		# Filter to audio bandwidth and decimate
		output_raw = self.audio_filter.feed(output_raw)
		output_raw = self.audio_decimator.feed(output_raw)

		# Scale to unsigned 8-bit int with offset (8-bit WAV)
		output_raw = numpy.clip(output_raw, -0.999, +0.999)
		output_raw = numpy.multiply(output_raw, 127) + 127
		output_raw = output_raw.astype(int)
	
		bits = struct.pack('%dB' % len(output_raw), *output_raw)
		self.wav.writeframes(bits)
		# print("%s %f" % ('f wav', time.time() - self.tmbase))

demodulators = {}
for freq in freqs:
	demodulators[freq] = Demodulator(freq)

remaining_data = b''

while True:
	# Ingest data
	data = sys.stdin.buffer.read(INGEST_SIZE * 2)
	if not data:
		break
	data = remaining_data + data

	tmbase = time.time()

	# Save odd byte
	if len(data) % 2 == 1:
		print("Odd byte, that's odd", file=sys.stderr)
		remaining_data = data[-1:]
		data = data[:-1]

	# Convert to complex numbers
	iqdata = numpy.frombuffer(data, dtype=numpy.uint8)
	iqdata = iqdata - 127.5
	iqdata = iqdata / 128.0
	iqdata = iqdata.view(complex)

	# Forward I/Q samples to all channels
	for k, d in demodulators.items():
		d.ingest(iqdata)

for k, d in demodulators.items():
	d.close_queue()

for k, d in demodulators.items():
	d.drain_queue()