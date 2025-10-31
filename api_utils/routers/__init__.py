"""
Modular FastAPI routers for api_utils.
Each module defines focused endpoint handlers. This package aggregates them.
"""

# Re-export handlers for convenient imports
from .static import read_index, get_css, get_js
from .info import get_api_info
from .health import health_check, live_check, ready_check
from .models import list_models
from .chat import chat_completions
from .queue import cancel_request, get_queue_status
from .logs_ws import websocket_log_endpoint
from .api_keys import get_api_keys, add_api_key, test_api_key, delete_api_key

__all__ = [
    'read_index', 'get_css', 'get_js',
    'get_api_info',
    'health_check', 'ready_check', 'live_check',
    'ready_check',
    'live_check',
    'list_models',
    'chat_completions',
    'cancel_request', 'get_queue_status',
    'websocket_log_endpoint',
    'get_api_keys', 'add_api_key', 'test_api_key', 'delete_api_key'
]
