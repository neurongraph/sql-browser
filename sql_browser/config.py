"""Application settings, read from a `.env` file and/or the environment.

Every key is optional and has a sensible default, so the app runs with no
configuration at all. To override, copy `.env.example` to `.env` and edit it,
or export the equivalent `SQL_BROWSER_*` environment variable.

The data directory is where the web UI stores uploaded databases/workbooks. A
browser can't hand the server a real filesystem path, so uploading into a known
directory is how files get added to the UI's file selector. Files passed on the
command line are read in place and never copied here.
"""

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Extensions the file selector and the upload endpoint accept. Excel workbooks
# come from excel_loader.EXCEL_EXTENSIONS; these are the SQLite ones.
SQLITE_EXTENSIONS = {".db", ".sqlite", ".sqlite3"}


class Settings(BaseSettings):
    """Settings sourced from `.env` then the environment (env wins)."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="SQL_BROWSER_",
        extra="ignore",
    )

    data_dir: Path = Field(
        default=Path("data"),
        description="Directory where web UI uploads are stored",
    )
    default_model: str = Field(
        default="gemma4:31b-cloud",
        description="Ollama model used when none is given on the CLI or in the UI",
    )
    max_upload_mb: int = Field(
        default=256,
        gt=0,
        description="Reject uploads larger than this many megabytes",
    )

    def resolved_data_dir(self) -> Path:
        """Return the data directory as an absolute path, creating it if needed.

        Created lazily rather than at import time so merely importing the
        package never touches the filesystem.
        """
        path = self.data_dir.expanduser().resolve()
        path.mkdir(parents=True, exist_ok=True)
        return path


settings = Settings()

#: Convenience alias so callers don't each reach into `settings`.
DEFAULT_MODEL = settings.default_model
