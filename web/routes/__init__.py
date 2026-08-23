"""路由层：页面（HTML）+ API（JSON）。"""

from .api import router as api_router  # noqa: F401
from .pages import router as pages_router  # noqa: F401
from .sellers import router as sellers_router  # noqa: F401
