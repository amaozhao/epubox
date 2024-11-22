from datetime import datetime, timedelta
import aioredis
from typing import Optional, Tuple
import json

from app.core.config import settings

class RateLimiter:
    def __init__(self):
        self.redis = aioredis.from_url(
            f"redis://{settings.REDIS_HOST}:{settings.REDIS_PORT}/{settings.REDIS_DB}"
        )

    async def _get_key(self, user_id: int, action: str) -> str:
        """Generate Redis key for rate limiting."""
        return f"rate_limit:{action}:{user_id}:{datetime.utcnow().strftime('%Y-%m-%d-%H')}"

    async def _get_upload_limits(self, user_role: str) -> Tuple[int, int, int]:
        """
        Get rate limits based on user role.
        Returns (requests_per_hour, max_file_size, daily_upload_quota)
        """
        limits = {
            "free": (5, 10 * 1024 * 1024, 50 * 1024 * 1024),  # 5 uploads/hour, 10MB per file, 50MB daily
            "premium": (20, 50 * 1024 * 1024, 500 * 1024 * 1024),  # 20 uploads/hour, 50MB per file, 500MB daily
            "admin": (100, 100 * 1024 * 1024, 1024 * 1024 * 1024),  # 100 uploads/hour, 100MB per file, 1GB daily
        }
        return limits.get(user_role, limits["free"])

    async def check_rate_limit(
        self,
        user_id: int,
        user_role: str,
        action: str = "upload",
        file_size: Optional[int] = None
    ) -> Tuple[bool, str]:
        """
        Check if the user has exceeded their rate limit.
        Returns (is_allowed, error_message)
        """
        hourly_limit, max_file_size, daily_quota = await self._get_upload_limits(user_role)
        
        # Check file size limit
        if file_size and file_size > max_file_size:
            return False, f"File size exceeds the limit of {max_file_size / (1024 * 1024):.1f}MB for {user_role} users"

        # Check hourly upload count
        hour_key = await self._get_key(user_id, action)
        daily_key = f"daily_upload:{user_id}:{datetime.utcnow().strftime('%Y-%m-%d')}"

        async with self.redis.pipeline() as pipe:
            try:
                # Get current hour count and daily upload size
                current_count = await self.redis.get(hour_key)
                current_count = int(current_count) if current_count else 0

                daily_usage = await self.redis.get(daily_key)
                daily_usage = int(daily_usage) if daily_usage else 0

                # Check hourly limit
                if current_count >= hourly_limit:
                    return False, f"Hourly {action} limit ({hourly_limit}) exceeded for {user_role} users"

                # Check daily quota
                if file_size and (daily_usage + file_size) > daily_quota:
                    return False, f"Daily upload quota ({daily_quota / (1024 * 1024):.1f}MB) exceeded for {user_role} users"

                return True, ""

            except Exception as e:
                return False, f"Error checking rate limit: {str(e)}"

    async def increment_counter(
        self,
        user_id: int,
        action: str = "upload",
        file_size: Optional[int] = None
    ):
        """Increment the rate limit counter and update usage statistics."""
        hour_key = await self._get_key(user_id, action)
        daily_key = f"daily_upload:{user_id}:{datetime.utcnow().strftime('%Y-%m-%d')}"

        async with self.redis.pipeline() as pipe:
            # Increment hourly counter
            await pipe.incr(hour_key)
            await pipe.expire(hour_key, 3600)  # Expire after 1 hour

            # Update daily usage
            if file_size:
                await pipe.incrby(daily_key, file_size)
                await pipe.expire(daily_key, 86400)  # Expire after 24 hours

            await pipe.execute()

    async def get_usage_stats(self, user_id: int) -> dict:
        """Get current usage statistics for a user."""
        hour_key = await self._get_key(user_id, "upload")
        daily_key = f"daily_upload:{user_id}:{datetime.utcnow().strftime('%Y-%m-%d')}"

        async with self.redis.pipeline() as pipe:
            hourly_count = await self.redis.get(hour_key)
            daily_usage = await self.redis.get(daily_key)

            return {
                "hourly_uploads": int(hourly_count) if hourly_count else 0,
                "daily_upload_size": int(daily_usage) if daily_usage else 0
            }
