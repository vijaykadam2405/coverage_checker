"""Independent crawler coverage auditing package."""

from pathlib import Path

from dotenv import load_dotenv

# One operator-owned file, independent of working directory. Never overwrite
# process settings, interpolate secrets, or send them to frontend configuration.
load_dotenv(Path(__file__).resolve().parents[1] / '.env', override=False, interpolate=False)

__version__ = "0.1.0"
