"""AUDIO persistence.

AUDIO is ``{"waveform": Tensor[B, C, T], "sample_rate": int}``. Encoding goes through PyAV using the same
stream setup ComfyUI's own ``AudioSaveHelper`` uses (``comfy_api/latest/_ui.py:260-370``), so the files we
write are byte-compatible with what a stock ``SaveAudio`` node produces.
"""

from __future__ import annotations

from typing import Any

import av
import torch

from .base import allocate, register, saved

#: Opus only accepts these rates; anything else must be resampled first.
_OPUS_RATES = (8000, 12000, 16000, 24000, 48000)

_BITRATES = {"64k": 64000, "96k": 96000, "128k": 128000, "192k": 192000, "320k": 320000}


def _opus_rate(sample_rate: int) -> int:
    if sample_rate > 48000:
        return 48000
    if sample_rate in _OPUS_RATES:
        return sample_rate
    return next((r for r in sorted(_OPUS_RATES) if r > sample_rate), 48000)


def _add_stream(container: av.container.OutputContainer, fmt: str, rate: int, layout: str, quality: str):
    if fmt == "opus":
        stream = container.add_stream("libopus", rate=rate, layout=layout)
        stream.bit_rate = _BITRATES.get(quality, 128000)
    elif fmt == "mp3":
        stream = container.add_stream("libmp3lame", rate=rate, layout=layout)
        if quality == "V0":
            stream.codec_context.qscale = 1
        else:
            stream.bit_rate = _BITRATES.get(quality, 192000)
    elif fmt == "wav":
        stream = container.add_stream("pcm_s16le", rate=rate, layout=layout)
    else:
        stream = container.add_stream("flac", rate=rate, layout=layout)
    return stream


class AudioHandler:
    kind = "audio"
    formats = ("flac", "wav", "mp3", "opus")
    default_format = "flac"

    def save(self, value, directory, subfolder, port_name, fmt, opts):
        if value is None:
            raise ValueError("WSAudioOutput received no audio")
        waveforms = value["waveform"].detach().cpu()
        source_rate = int(value["sample_rate"])
        quality = str(opts.get("quality", "192k"))

        files: list[dict[str, str]] = []
        duration = 0.0
        channels = 0
        rate = source_rate

        for waveform in waveforms:  # iterate the batch dimension
            channels = waveform.shape[0]
            rate = source_rate
            if fmt == "opus":
                rate = _opus_rate(source_rate)
                if rate != source_rate:
                    import torchaudio  # only needed on the resample path

                    waveform = torchaudio.functional.resample(waveform, source_rate, rate)

            layout = "mono" if channels == 1 else "stereo"
            if channels > 2:
                # Downmix rather than fail: an encoder configured for stereo cannot take 6 channels.
                waveform = waveform[:2]
                channels = 2
                layout = "stereo"

            path, filename, _ = allocate(directory, port_name, fmt)
            with av.open(path, mode="w", format=fmt) as container:
                stream = _add_stream(container, fmt, rate, layout, quality)
                frame = av.AudioFrame.from_ndarray(
                    waveform.movedim(0, 1).reshape(1, -1).float().numpy(),
                    format="flt",
                    layout=layout,
                )
                frame.sample_rate = rate
                frame.pts = 0
                container.mux(stream.encode(frame))
                container.mux(stream.encode(None))

            duration = waveform.shape[-1] / float(rate)
            files.append(saved(filename, subfolder))

        meta = {
            "count": len(files),
            "sample_rate": rate,
            "channels": channels,
            "duration": round(duration, 4),
            "format": fmt,
        }
        return files, meta

    def load(self, path: str, opts: dict[str, Any]):
        with av.open(path) as container:
            stream = next((s for s in container.streams if s.type == "audio"), None)
            if stream is None:
                raise ValueError(f"No audio stream in {path}")
            sample_rate = int(stream.codec_context.sample_rate or stream.rate or 44100)
            chunks = []
            for frame in container.decode(stream):
                array = frame.to_ndarray()  # (channels, samples) or (1, channels*samples) when packed
                tensor = torch.from_numpy(array).float()
                if tensor.shape[0] == 1 and frame.layout is not None and len(frame.layout.channels) > 1:
                    tensor = tensor.reshape(-1, len(frame.layout.channels)).movedim(1, 0)
                chunks.append(tensor)

        if not chunks:
            raise ValueError(f"Decoded no audio from {path}")
        waveform = torch.cat(chunks, dim=-1)
        # PyAV hands back int16 for pcm formats; normalise so downstream always sees float -1..1.
        if waveform.abs().max() > 1.5:
            waveform = waveform / 32768.0
        return {"waveform": waveform.unsqueeze(0), "sample_rate": sample_rate}


register(AudioHandler())
