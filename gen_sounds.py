import struct
import wave
import math
import os


def make_wav(filename, freqs, duration=0.3, sample_rate=44100, volume=0.5):
    n_samples = int(sample_rate * duration)
    samples = []
    for i in range(n_samples):
        t = i / sample_rate
        val = 0
        for freq, start, end in freqs:
            if start <= t <= end:
                seg_dur = end - start
                seg_t = t - start
                envelope = 1.0
                fade = 0.02
                if seg_t < fade:
                    envelope = seg_t / fade
                elif seg_t > seg_dur - fade:
                    envelope = (seg_dur - seg_t) / fade
                val += math.sin(2 * math.pi * freq * t) * envelope
        val = max(-1, min(1, val * volume))
        samples.append(int(val * 32767))

    path = os.path.join("sounds", filename)
    with wave.open(path, "w") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(struct.pack("<" + "h" * len(samples), *samples))
    print(f"Created {path} ({os.path.getsize(path)} bytes)")


# Chime - two ascending tones
make_wav("chime.wav", [(523, 0, 0.15), (659, 0.12, 0.3)], duration=0.3)

# Ding - single bright tone
make_wav("ding.wav", [(880, 0, 0.4)], duration=0.4)

# Double beep
make_wav("double_beep.wav", [(740, 0, 0.1), (740, 0.15, 0.25)], duration=0.3)

# Gentle - low soft tone
make_wav("gentle.wav", [(392, 0, 0.5), (523, 0.1, 0.5)],
         duration=0.5, volume=0.35)

print("Done!")
