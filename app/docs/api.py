from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi

from app.docs.schemas import (
    UserCreate, UserResponse, UserLogin, Token,
    FileMetadataResponse, FileUploadResponse, FileListResponse,
    TranslationRequest, TranslationResponse, TranslationStatus,
    ErrorResponse, RateLimitInfo
)

def custom_openapi(app: FastAPI):
    if app.openapi_schema:
        return app.openapi_schema

    openapi_schema = get_openapi(
        title="EPUBox API",
        version="1.0.0",
        description="""
        EPUBox is a powerful EPUB translation service that allows you to translate your EPUB files
        across multiple languages. This API provides endpoints for file management, translation,
        and user authentication.

        ## Features

        * User Authentication with JWT
        * File Upload and Management
        * EPUB Translation
        * Rate Limiting
        * File Metadata Tracking

        ## Rate Limiting

        All endpoints are rate-limited based on user roles:
        * Free: 100 requests/hour
        * Premium: 1000 requests/hour
        * Admin: Unlimited

        Rate limit headers are included in all responses:
        * X-RateLimit-Limit
        * X-RateLimit-Remaining
        * X-RateLimit-Reset

        ## Authentication

        To authenticate, send a POST request to `/api/v1/users/login` with your email and password.
        Use the returned JWT token in the Authorization header for subsequent requests:

        ```
        Authorization: Bearer your-token-here
        ```

        ## Error Handling

        The API uses standard HTTP status codes and returns error details in the response body:
        ```json
        {
            "detail": "Error message here"
        }
        ```
        """,
        routes=app.routes,
    )

    # Authentication examples
    openapi_schema["paths"]["/api/v1/users/register"]["post"].update(
        {
            "summary": "Register a new user",
            "description": "Create a new user account with email and password",
            "responses": {
                "201": {
                    "description": "User created successfully",
                    "content": {
                        "application/json": {
                            "example": {
                                "id": 1,
                                "email": "user@example.com",
                                "role": "free",
                                "created_at": "2023-01-01T00:00:00",
                                "updated_at": "2023-01-01T00:00:00"
                            }
                        }
                    }
                },
                "400": {
                    "description": "Invalid input",
                    "content": {
                        "application/json": {
                            "example": {
                                "detail": "Email already registered"
                            }
                        }
                    }
                }
            }
        }
    )

    # File upload examples
    openapi_schema["paths"]["/api/v1/files/upload"]["post"].update(
        {
            "summary": "Upload a file",
            "description": """
            Upload an EPUB file for translation.
            
            **Supported file types:** .epub
            **Maximum file size:** 100MB (free), 500MB (premium)
            
            The file will be validated and stored securely. File metadata will be tracked
            in the database.
            """,
            "responses": {
                "201": {
                    "description": "File uploaded successfully",
                    "content": {
                        "application/json": {
                            "example": {
                                "message": "File uploaded successfully",
                                "file_info": {
                                    "id": 1,
                                    "filename": "document.epub",
                                    "original_filename": "my_book.epub",
                                    "size": 1024576,
                                    "mime_type": "application/epub+zip",
                                    "status": "completed",
                                    "created_at": "2023-01-01T00:00:00",
                                    "updated_at": "2023-01-01T00:00:00"
                                }
                            }
                        }
                    }
                },
                "400": {
                    "description": "Invalid file",
                    "content": {
                        "application/json": {
                            "example": {
                                "detail": "Invalid file type. Allowed types: .epub"
                            }
                        }
                    }
                },
                "413": {
                    "description": "File too large",
                    "content": {
                        "application/json": {
                            "example": {
                                "detail": "File too large. Maximum size: 100MB"
                            }
                        }
                    }
                }
            }
        }
    )

    # Translation examples
    openapi_schema["paths"]["/api/v1/translation/translate"]["post"].update(
        {
            "summary": "Start file translation",
            "description": """
            Start translating an EPUB file. The translation is performed asynchronously.
            
            **Supported languages:**
            * Source: en, es, fr, de, it, pt, ru, zh-CN
            * Target: en, es, fr, de, it, pt, ru, zh-CN
            
            The translation process preserves the original formatting and structure of the EPUB file.
            """,
            "responses": {
                "202": {
                    "description": "Translation started",
                    "content": {
                        "application/json": {
                            "example": {
                                "message": "Translation started",
                                "task_id": "550e8400-e29b-41d4-a716-446655440000",
                                "status": "pending"
                            }
                        }
                    }
                },
                "404": {
                    "description": "File not found",
                    "content": {
                        "application/json": {
                            "example": {
                                "detail": "File not found"
                            }
                        }
                    }
                }
            }
        }
    )

    app.openapi_schema = openapi_schema
    return app.openapi_schema
