"""Read-only settings for the connected profile's Ledger Desktop page."""
from pathlib import Path
from runpy import run_path

from fastapi import APIRouter, Request, Response
from hermes_cli.config import get_hermes_home
from hermes_cli.plugins_settings import plugin_settings_fields
from hermes_cli.web_server import _require_token
from hermes_cli.profiles import current_profile_name

router = APIRouter()
ROOT = Path(__file__).resolve().parents[1]
# Hermes imports dashboard APIs as standalone modules, including in profile workers.
# Load the sibling resolver once without registering an engine or opening SQLite.
effective_settings = run_path(str(ROOT / "config.py"))["effective_settings"]


@router.get("/settings")
def settings(request: Request, response: Response):
    _require_token(request)
    response.headers["Cache-Control"] = "no-store"
    resolved = effective_settings()
    fields = plugin_settings_fields("hermes-lcm-ledger", ROOT)
    profile_paths = {
        "database_path": "lcm.db",
        "extraction_output_path": "lcm-extractions",
        "large_output_externalization_path": "lcm-large-outputs",
    }
    for field in fields:
        key = field["key"]
        field["value"], field["source"] = resolved[key]
        field["integer"] = type(field.get("default")) is int
        if key in profile_paths and not field["value"]:
            field["automatic_path"] = str(get_hermes_home() / profile_paths[key])
    return {"profile": current_profile_name(default="custom"), "fields": fields}
