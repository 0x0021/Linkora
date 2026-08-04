"""Regression guard for the web router <-> api circular-import risk (P2-11).

The FastAPI app in web/api.py mounts all sub-routers via include_router() at
module END (after every symbol is defined). Each sub-router imports the api
module as `import web.api as _api` (holding a module reference) rather than
pulling live symbols at top level. This ordering is what prevents a circular
import. If someone ever moves the include_router() calls above the symbol
definitions, or a router starts doing `from web.api import X` at module top
level, this import will raise ImportError and fail loudly.
"""

import importlib
import pkgutil

import web.api as api_module
from web import routers as router_pkg


def test_web_api_imports_without_circular_error():
    """Importing web.api must succeed and expose the FastAPI app."""
    assert hasattr(api_module, "app")
    # At least the base routes + all mounted sub-routers should be present.
    assert len(api_module.app.routes) > 20


def test_all_sub_routers_import_cleanly():
    """Every sub-router module must import without triggering a circular import."""
    for mod_info in pkgutil.iter_modules(router_pkg.__path__):
        if mod_info.name == "__init__":
            continue
        module = importlib.import_module(
            f"web.routers.{mod_info.name}"
        )
        # Each router module must expose a `router` (APIRouter) object.
        assert hasattr(module, "router"), (
            f"web.routers.{mod_info.name} does not expose a router object"
        )
