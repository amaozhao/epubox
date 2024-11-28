"""Constants for EPUB handling."""

# Resource types
RESOURCE_TYPE_STYLESHEET = "stylesheet"
RESOURCE_TYPE_IMAGE = "image"
RESOURCE_TYPE_FONT = "font"
RESOURCE_TYPE_SCRIPT = "script"
RESOURCE_TYPE_DOCUMENT = "document"
RESOURCE_TYPE_UNKNOWN = "unknown"

# MIME types mapping
MIME_TYPE_MAPPING = {
    "text/css": RESOURCE_TYPE_STYLESHEET,
    "text/javascript": RESOURCE_TYPE_SCRIPT,
    "application/javascript": RESOURCE_TYPE_SCRIPT,
    "image/jpeg": RESOURCE_TYPE_IMAGE,
    "image/png": RESOURCE_TYPE_IMAGE,
    "image/gif": RESOURCE_TYPE_IMAGE,
    "image/svg+xml": RESOURCE_TYPE_IMAGE,
    "application/x-font-ttf": RESOURCE_TYPE_FONT,
    "application/x-font-otf": RESOURCE_TYPE_FONT,
    "application/vnd.ms-opentype": RESOURCE_TYPE_FONT,
    "application/font-woff": RESOURCE_TYPE_FONT,
    "application/font-woff2": RESOURCE_TYPE_FONT,
    "text/html": RESOURCE_TYPE_DOCUMENT,
    "application/xhtml+xml": RESOURCE_TYPE_DOCUMENT,
}

# Content types for translation
TRANSLATABLE_CONTENT_TYPES = ["text", "heading"]
UNTRANSLATABLE_CONTENT_TYPES = ["code", "pre", "script", "style"]

# Status constants
STATUS_PENDING = "pending"
STATUS_IN_PROGRESS = "in_progress"
STATUS_COMPLETED = "completed"
STATUS_ERROR = "error"

# Selector types
SELECTOR_TYPE_CSS = "css"
SELECTOR_TYPE_XPATH = "xpath"

# File extensions
SUPPORTED_IMAGE_EXTENSIONS = [".jpg", ".jpeg", ".png", ".gif", ".svg"]
SUPPORTED_FONT_EXTENSIONS = [".ttf", ".otf", ".woff", ".woff2"]
SUPPORTED_STYLE_EXTENSIONS = [".css"]
SUPPORTED_SCRIPT_EXTENSIONS = [".js"]
