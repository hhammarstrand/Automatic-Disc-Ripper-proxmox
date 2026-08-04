"""Which encoder the pipeline holds.

Two backends now do the same job — HandBrake with its presets, and ffmpeg
driving the GPU through VA-API — and every place that builds one has to make
the same choice. One function, so the pipeline and the web app cannot end up
disagreeing about which encoder a job will run on.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def build_encoder(config):
    """The encoder this configuration asks for."""
    if config.encoder_backend == "vaapi":
        from adr.vaapi import VaapiEncoder

        logger.info("Encoding with ffmpeg on the GPU (VA-API)")
        return VaapiEncoder(config)

    from adr.encoder import HandBrakeEncoder

    return HandBrakeEncoder(config)


def describe_backend(config) -> str:
    """One line naming what will do the encoding, and how, for the UI.

    Both halves matter: which program runs, and what the shared settings will
    make it do. Naming only the program is how someone ends up believing a
    HandBrake preset governs an ffmpeg encode.
    """
    from adr.encodingsettings import describe

    if config.encoder_backend == "vaapi":
        codec = (getattr(config, "vaapi_codec", "") or "h264").upper()
        return f"ffmpeg on the GPU (VA-API, {codec}) — {describe(config)}"
    return f"HandBrake, preset '{config.handbrake_preset}' — {describe(config)}"
