# EPUBox - EPUB Translation Service

EPUBox is a powerful web service that allows you to translate EPUB files across multiple languages while preserving their formatting and structure.

## Features

- User authentication with JWT tokens
- File upload and management
- EPUB translation with multiple language support
  - Multiple translation providers (Google, Mock, planned: OpenAI, Mistral)
  - HTML formatting preservation
  - Asynchronous processing with progress tracking
  - Task management (submit, status check, cancellation)
- Rate limiting based on user roles
- File metadata tracking
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

Get supported languages:

```bash
curl -X GET "http://localhost:8000/api/v1/translation/languages" \
     -H "Authorization: Bearer your-token-here"
```

Start translation:

```bash
curl -X POST "http://localhost:8000/api/v1/translation/translate" \
     -H "Authorization: Bearer your-token-here" \
     -H "Content-Type: application/json" \
     -d '{
       "file_id": 1,
       "source_lang": "en",
       "target_lang": "es",
       "provider": "google"
     }'
```

Check translation status:

```bash
curl -X GET "http://localhost:8000/api/v1/translation/status/task-id-here" \
     -H "Authorization: Bearer your-token-here"
```

Cancel translation:

```bash
curl -X DELETE "http://localhost:8000/api/v1/translation/cancel/task-id-here" \
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
- Complete authentication system
- File upload and management
- Translation API endpoints
  - Task submission
  - Status checking
  - Task cancellation
  - Language support
- Translation providers
  - Google Translate integration
  - Mock provider for testing
- Queue management
  - Async task processing
  - Progress tracking
  - Task cancellation
- Test coverage
  - API endpoint tests
  - Translation flow tests
  - Error handling tests

### In Progress
1. EPUB Translation Enhancement
   - Metadata translation (title, author, description)
   - Table of contents translation
   - Internal links preservation
   - Image alt text translation
   - Content validation
   - Progress reporting improvements

2. Translation Quality
   - Context-aware translation
   - Term consistency
   - Custom dictionaries support
   - Quality validation

3. Provider Integration
   - OpenAI integration
   - Mistral integration
   - Provider selection optimization

4. Testing & Validation
   - EPUB format tests
   - Large file handling
   - Performance benchmarks
   - Memory usage optimization

### Next Steps
1. Complete EPUB translation features
   - Full metadata support
   - Structure preservation
   - Navigation handling
2. Add new translation providers
   - OpenAI implementation
   - Mistral implementation
3. Enhance translation quality
   - Context awareness
   - Terminology consistency
4. Improve validation and testing
   - Format validation
   - Quality checks
   - Performance testing

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
