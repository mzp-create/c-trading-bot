#!/usr/bin/env python3
"""
Logging module for the trading bot.
Structured logging with file rotation and console output.
"""

import os
import sys
import logging
import logging.handlers
from pathlib import Path
from datetime import datetime


class BotLogger:
    """Structured logging with file rotation and console output."""

    def __init__(self, config: dict):
        log_dir = Path(config.get('data', {}).get('log_file', 'logs/bot.log')).parent
        log_dir.mkdir(parents=True, exist_ok=True)

        log_file = log_dir / 'bot.log'
        error_file = log_dir / 'errors.log'
        trade_file = log_dir / 'trades.log'

        log_level = getattr(logging, config.get('monitoring', {}).get('log_level', 'INFO'))

        # Root logger config
        root_logger = logging.getLogger()
        root_logger.setLevel(log_level)

        # Clear any existing handlers
        root_logger.handlers = []

        # Format
        formatter = logging.Formatter(
            '%(asctime)s | %(levelname)-8s | %(name)-15s | %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )

        # Console handler
        console = logging.StreamHandler(sys.stdout)
        console.setLevel(log_level)
        console.setFormatter(formatter)
        root_logger.addHandler(console)

        # Rotating file handler (main log)
        file_handler = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=10*1024*1024, backupCount=5
        )
        file_handler.setLevel(log_level)
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)

        # Error file handler
        err_handler = logging.handlers.RotatingFileHandler(
            error_file, maxBytes=10*1024*1024, backupCount=3
        )
        err_handler.setLevel(logging.WARNING)
        err_handler.setFormatter(formatter)
        root_logger.addHandler(err_handler)

        # Trade-specific logger
        self.trade_logger = logging.getLogger('Trade')
        self.trade_logger.setLevel(logging.INFO)
        self.trade_logger.handlers = []
        trade_handler = logging.handlers.RotatingFileHandler(
            trade_file, maxBytes=10*1024*1024, backupCount=3
        )
        trade_handler.setFormatter(logging.Formatter(
            '%(asctime)s | %(message)s'
        ))
        self.trade_logger.addHandler(trade_handler)

        self.log = self.get_logger("Logger")
        self.log.info(f"Logging initialized — {log_file}")

    def get_logger(self, name: str) -> logging.Logger:
        """Get a named logger."""
        return logging.getLogger(name)

    def log_trade(self, trade_data: dict):
        """Log trade event."""
        self.trade_logger.info(str(trade_data))
