"""Logging configuration for SQL Browser."""

import sys
from pathlib import Path
from loguru import logger

# Remove default handler
logger.remove()

# Create logs directory
LOGS_DIR = Path.home() / ".sql_browser" / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)

# Add file handler with rotation
logger.add(
    LOGS_DIR / "sql_browser_{time:YYYY-MM-DD}.log",
    rotation="1 day",
    retention="7 days",
    level="DEBUG",
    format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} - {message}",
    backtrace=True,
    diagnose=True,
)

# Add console handler for errors only (optional, can be disabled)
logger.add(
    sys.stderr,
    level="ERROR",
    format="<red>{level}</red>: {message}",
    colorize=True,
)


def get_logger(name: str):
    """Get a logger instance with the given name.
    
    Args:
        name: Name for the logger (typically __name__)
        
    Returns:
        Logger instance
    """
    return logger.bind(name=name)


# Export the configured logger
__all__ = ["logger", "get_logger", "LOGS_DIR"]

# Made with Bob
