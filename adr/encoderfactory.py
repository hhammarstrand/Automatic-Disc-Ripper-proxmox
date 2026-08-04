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
    """One line naming what will do the encoding, for the UI."""
    if config.encoder_backend == "vaapi":
        codec = config.vaapi_codec.upper()
        return f"ffmpeg on the GPU (VA-API, {codec}, quality {config.vaapi_quality})"
    return f"HandBrake, preset '{config.handbrake_preset}'"
