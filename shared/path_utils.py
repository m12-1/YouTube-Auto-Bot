"""
shared/path_utils.py

Secure filesystem operations to prevent path traversal and enforce
that all file access happens within the project boundary.
"""

from __future__ import annotations

import os
import re
import uuid
from pathlib import Path

# The absolute base directory of the project.
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Regex to allow only safe alphanumeric characters, dashes, and underscores.
SAFE_NAME_REGEX = re.compile(r"^[A-Za-z0-9_.-]+$")


class PathSecurityError(ValueError):
    """Raised when a path is outside the project root or a name is invalid."""


def sanitize_filename(name: str) -> str:
    """
    Sanitize a string to be safely used as a filename, cache key, or document ID.
    Replaces spaces with underscores and removes any other unsafe characters.
    """
    if not name:
        raise PathSecurityError("Filename cannot be empty")
        
    sanitized = re.sub(r"\s+", "_", name)
    sanitized = re.sub(r"[^A-Za-z0-9_.-]", "_", sanitized)
    sanitized = re.sub(r"_+", "_", sanitized)
    
    if not sanitized or sanitized == "." or sanitized == "..":
        raise PathSecurityError(f"Invalid filename after sanitization: '{name}'")
        
    return sanitized


def safe_path(base_dir: Path | str, *parts: str) -> Path:
    """
    Safely join and resolve a path, ensuring it stays within the project root.
    
    Args:
        base_dir: The base directory to start from (e.g., PROJECT_ROOT / "db").
        parts: Additional path segments. These should be clean (no path separators).
        
    Returns:
        A resolved pathlib.Path object.
        
    Raises:
        PathSecurityError: If the resulting path escapes PROJECT_ROOT.
    """
    base_path = Path(base_dir).resolve()
    
    # Ensure base is within project
    if not base_path.is_relative_to(PROJECT_ROOT):
        raise PathSecurityError(f"Base directory {base_dir} is outside project root.")
        
    # Build target path
    target = base_path
    for part in parts:
        # Prevent manual injection of separators or traversal in parts
        if "/" in part or "\\" in part:
            raise PathSecurityError(f"Invalid characters in path segment: {part}")
        if part == "..":
             raise PathSecurityError(f"Path traversal detected in segment: {part}")
        target = target / part
        
    target = target.resolve()
    
    # Final safety check
    if not target.is_relative_to(PROJECT_ROOT):
        raise PathSecurityError(f"Target path {target} escapes project root.")
        
    return target


def generate_secure_uuid() -> str:
    """Generate a random UUID suitable for safe filenames."""
    return str(uuid.uuid4())
