# EPUBox - EPUB Translation Service

EPUBox is a powerful web service that allows you to translate EPUB files across multiple languages while preserving their formatting and structure.

## Features

- User authentication with JWT tokens
- File upload and management
- EPUB translation with multiple language support
- Rate limiting based on user roles
- File metadata tracking
- Asynchronous translation processing
- Secure file handling

## API Documentation

The API documentation is available at `/docs` when running the service. It provides interactive documentation using Swagger UI.

### Authentication

Register a new user:

```bash
curl -X POST "http://localhost:8000/api/v1/users/register" \
     -H "Content-Type: application/json" \
     -d '{
       "email": "user@example.com",
       "password": "strongpassword123"
     }'
```

Login and get JWT token:

```bash
curl -X POST "http://localhost:8000/api/v1/users/login" \
     -H "Content-Type: application/json" \
     -d '{
       "email": "user@example.com",
       "password": "strongpassword123"
     }'
```

### File Management

Upload a file:

```bash
curl -X POST "http://localhost:8000/api/v1/files/upload" \
     -H "Authorization: Bearer your-token-here" \
     -F "file=@path/to/your/book.epub"
```

List your files:

```bash
curl -X GET "http://localhost:8000/api/v1/files/list" \
     -H "Authorization: Bearer your-token-here"
```

Get file information:

```bash
curl -X GET "http://localhost:8000/api/v1/files/info/1" \
     -H "Authorization: Bearer your-token-here"
```

Download a file:

```bash
curl -X GET "http://localhost:8000/api/v1/files/download/1" \
     -H "Authorization: Bearer your-token-here" \
     --output downloaded_book.epub
```

### Translation

Start translation:

```bash
curl -X POST "http://localhost:8000/api/v1/translation/translate" \
     -H "Authorization: Bearer your-token-here" \
     -H "Content-Type: application/json" \
     -d '{
       "file_id": 1,
       "source_language": "en",
       "target_language": "es"
     }'
```

Check translation status:

```bash
curl -X GET "http://localhost:8000/api/v1/translation/status/task-id-here" \
     -H "Authorization: Bearer your-token-here"
```

## Rate Limits

The API has rate limits based on user roles:

- Free users: 100 requests/hour
- Premium users: 1000 requests/hour
- Admin users: Unlimited

Rate limit information is included in response headers:
- `X-RateLimit-Limit`: Maximum requests per hour
- `X-RateLimit-Remaining`: Remaining requests
- `X-RateLimit-Reset`: Seconds until limit reset

## Error Handling

The API uses standard HTTP status codes and returns error details in the response body:

```json
{
    "detail": "Error message here"
}
```

Common status codes:
- 200: Success
- 201: Created
- 202: Accepted (for async operations)
- 400: Bad Request
- 401: Unauthorized
- 403: Forbidden
- 404: Not Found
- 413: Payload Too Large
- 429: Too Many Requests
- 500: Internal Server Error

## Development Status

### Implemented Features
- Basic file upload structure
- Authentication framework
- Database structure
- API endpoints structure
- Basic HTML attribute preservation
- Initial configuration setup

### In Progress
1. Translation Service
   - Translation queue management
   - Mistral API integration
   - Progress tracking for translation jobs

2. Authentication & Authorization
   - Role-based access control
   - Rate limiting implementation
   - User management endpoints

3. EPUB Processing
   - EPUB content extraction
   - HTML attribute preservation during translation
   - EPUB reassembly after translation

4. File Management
   - File cleanup/deletion
   - Storage optimization
   - File versioning

5. Testing
   - Integration tests
   - Performance tests
   - Edge case handling

6. System Reliability
   - Comprehensive error handling
   - Detailed logging system
   - Error recovery mechanisms

### Next Steps
1. Complete translation service implementation
2. Implement comprehensive testing
3. Add rate limiting and role-based access
4. Enhance error handling and logging
5. Implement file management features
6. Add monitoring and optimization

## Development Setup

1. Clone the repository:
```bash
git clone https://github.com/yourusername/epubox.git
cd epubox
```

2. Create a virtual environment:
```bash
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
```

3. Install dependencies:
```bash
pip install -r requirements.txt
```

4. Set up environment variables:
```bash
cp .env.example .env
# Edit .env with your configuration
```

5. Run the development server:
```bash
uvicorn app.main:app --reload
```

The API will be available at `http://localhost:8000`

## License

MIT License - see LICENSE file for details
