"""Mock modules for testing."""
from unittest.mock import MagicMock
import json
import gc

# Mock aioredis module
class MockRedis:
    def __init__(self, *args, **kwargs):
        self._data = {}

    async def get(self, key):
        return self._data.get(key)

    async def set(self, key, value, ex=None):
        self._data[key] = value
        return True

    async def incr(self, key):
        if key not in self._data:
            self._data[key] = 0
        self._data[key] += 1
        return self._data[key]

    async def close(self):
        pass

    def pipeline(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass

    async def execute(self):
        return [0, 0]  # Mock pipeline execution results

# Create mock redis module
mock_aioredis = MagicMock()
mock_aioredis.from_url = MockRedis
mock_aioredis.Redis = MockRedis

# Mock httpx module
class MockResponse:
    def __init__(self, status_code=200, content=None, headers=None):
        self.status_code = status_code
        self._content = content
        if self._content is None:
            self._content = json.dumps({
                "sentences": [{"trans": "Hello World"}]
            }).encode('utf-8')
        elif isinstance(self._content, dict):
            self._content = json.dumps(self._content).encode('utf-8')
        self.text = self._content.decode('utf-8') if isinstance(self._content, bytes) else str(self._content)
        self.headers = headers or {}

    def json(self):
        if isinstance(self._content, bytes):
            return json.loads(self._content.decode('utf-8'))
        if isinstance(self._content, dict):
            return self._content
        return json.loads(self.text)

    def read(self):
        return self._content

    @property
    def content(self):
        return self._content

class MockAsyncClient:
    """Mock httpx.AsyncClient for testing."""
    
    _instances = {}  # Class variable to store instances

    def __init__(self, *args, **kwargs):
        self.is_closed = False
        self._instance_id = id(self)
        self.failure_count = 0
        self._failure_pattern = []
        self._default_response = {
            "sentences": [{"trans": "Hello World"}]
        }
        # Store instance in class variable
        MockAsyncClient._instances[self._instance_id] = self

    @classmethod
    def set_failure_pattern(cls, instance_id, pattern):
        """Set a specific failure pattern for an instance.
        
        Args:
            instance_id: The ID of the client instance
            pattern: List of tuples (exception_type, status_code, headers)
                    where exception_type can be None for successful responses
        """
        if instance_id in cls._instances:
            cls._instances[instance_id]._failure_pattern = pattern
        elif instance_id == id(cls):
            # Handle case where class is used directly
            instance = cls()
            instance._failure_pattern = pattern
            cls._instances[instance_id] = instance

    def __call__(self, *args, **kwargs):
        """Support using the instance as a callable (like a class)."""
        return self

    async def __aenter__(self):
        # Make sure we're tracked when used as a context manager
        if self._instance_id not in MockAsyncClient._instances:
            MockAsyncClient._instances[self._instance_id] = self
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.aclose()
        # Remove instance from class variable
        if self._instance_id in MockAsyncClient._instances:
            del MockAsyncClient._instances[self._instance_id]

    async def post(self, url, headers=None, data=None, json=None, timeout=None):
        """Mock post request."""
        # Get the next failure/response from the pattern
        if self._failure_pattern and self.failure_count < len(self._failure_pattern):
            exc_type, status_code, resp_headers = self._failure_pattern[self.failure_count]
            self.failure_count += 1

            if exc_type:
                if isinstance(exc_type, type) and issubclass(exc_type, Exception):
                    raise exc_type("Simulated error")
                elif isinstance(exc_type, str):
                    exc_class = getattr(mock_httpx, exc_type)
                    raise exc_class("Simulated error")
                else:
                    raise exc_type

            # For successful responses, use the default response content
            return MockResponse(
                status_code=status_code or 200,
                content=self._default_response,
                headers=resp_headers or {}
            )

        # Default success response
        return MockResponse(
            status_code=200,
            content=self._default_response
        )

    async def get(self, *args, **kwargs):
        return await self.post(*args, **kwargs)

    async def close(self):
        self.is_closed = True

    async def aclose(self):
        """Match httpx.AsyncClient interface."""
        await self.close()

# Create mock httpx module
mock_httpx = MagicMock()
mock_httpx.AsyncClient = MockAsyncClient

# Define all necessary exception types
class HTTPError(Exception):
    pass

class NetworkError(HTTPError):
    pass

class ConnectError(NetworkError):
    pass

class TimeoutException(HTTPError):
    pass

mock_httpx.HTTPError = HTTPError
mock_httpx.NetworkError = NetworkError
mock_httpx.ConnectError = ConnectError
mock_httpx.TimeoutException = TimeoutException
mock_httpx.utils = MagicMock()
mock_httpx.utils.quote = lambda x: x
