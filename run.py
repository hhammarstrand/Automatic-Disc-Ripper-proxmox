#!/usr/bin/env python3
"""Automatic Disc Ripper – Entry point.

Starts the disc watcher, pipeline manager, and Flask web UI.

Usage:
    python run.py                  # Normal start
    python run.py --port 9090      # Custom port
    python run.py --config path    # Custom config file
"""

import argparse
import logging
import signal
import sys

from adr.config import Config
from adr.models import init_db
from adr.pipeline import PipelineManager
from adr.utils import get_lan_ip, setup_logging
from web.app import create_app

logger = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(description="Automatic Disc Ripper — automatic optical disc ripping & transcoding")
    parser.add_argument("--config", type=str, default=None, help="Path to adr.yaml config file")
    parser.add_argument("--port", type=int, default=None, help="Override web UI port")
    parser.add_argument("--host", type=str, default=None, help="Override web UI host")
    args = parser.parse_args()

    # Load config
    config = Config(args.config)
    setup_logging(config.log_level)

    logger.info("=" * 60)
    # From the package, not a literal: a startup banner that disagrees with
    # what is installed makes every "which version is this?" question worse.
    from adr import __version__
    logger.info("Automatic Disc Ripper v%s starting", __version__)
    logger.info("=" * 60)
    logger.info("Config: %s", config)
    logger.info("MakeMKV: %s", config.makemkv_path)
    logger.info("HandBrake: %s", config.handbrake_path)
    logger.info("Raw path: %s", config.raw_path)
    logger.info("Completed path: %s", config.completed_path)

    # Initialise database
    init_db()
    logger.info("Database initialised")

    # Start pipeline manager (disc watcher + encoder workers)
    pipeline = PipelineManager(config)
    pipeline.start()

    # Create Flask app
    host = args.host or config.web_host
    port = args.port or config.web_port
    app = create_app(config, pipeline_manager=pipeline)

    # Graceful shutdown
    def shutdown(signum, frame):
        logger.info("Shutting down...")
        pipeline.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    # Start web server
    lan_ip = get_lan_ip()
    logger.info("Web UI (local):  http://localhost:%d", port)
    logger.info("Web UI (LAN):    http://%s:%d", lan_ip, port)
    if config.watch_path:
        logger.info("Watch folder:    %s", config.watch_path)
    logger.info("Waiting for disc insertion...")
    logger.info("-" * 60)

    try:
        app.run(host=host, port=port, debug=False, use_reloader=False)
    except KeyboardInterrupt:
        pass
    finally:
        pipeline.stop()
        logger.info("Automatic Disc Ripper stopped")


if __name__ == "__main__":
    main()
