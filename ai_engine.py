#!/usr/bin/env python3
"""
AI Response Engine
Generates AI-powered responses to TikTok live comments using OpenAI API.
Supports different personalities: coder, salesperson, and default assistant.
"""

import os
import json
import time
import threading
import logging
from typing import Optional, List, Dict, Any, Callable
from collections import deque
from datetime import datetime

try:
    from openai import OpenAI
    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False

logger = logging.getLogger("AIEngine")

class AIResponseEngine:
    """
    AI-powered comment response engine.
    Uses OpenAI GPT models to generate responses to live comments.
    Supports multiple personas and response caching.
    """

    MAX_RESPONSES_CACHE = 50
    DEFAULT_TEMPERATURE = 0.7
    DEFAULT_MAX_TOKENS = 150

    LIVE_CONTEXT_PROMPT = """Bạn đang tham gia vào một buổi Live Stream TikTok. Người dùng sẽ gửi bình luận và bạn cần trả lời ngắn gọn, tự nhiên, tạo sự tương tác.
- Luôn nhớ bạn đang trong môi trường live stream, trò chuyện tức thời với người xem
- Trả lời dưới 250 ký tự, ngắn gọn và dễ đọc
- Khuyến khích người xem tiếp tục tương tác bằng cách đặt câu hỏi"""

    PERSONAS = {
        "coder": {
            "name": "coder",
            "system_prompt": """Bạn là một AI lập trình viên đang live stream code. Hãy trả lời ngắn gọn, thân thiện, phù hợp với người chơi mới và chuyên gia.
- Giải thích code ngắn gọn nhưng rõ ràng
- Đưa ra câu hỏi khuyến khích tương tác
- Sử dụng emoji code 💻 🚀 
- Trả lời dưới 200 ký tự""",
            "default_responses": [
                "Đang code live đây mọi người ơi 💻",
                "Câu hỏi tuyệt vời! Mình sẽ giải thích ngay",
                "Code này dùng để xử lý gì vậy các bạn?",
                "Thấy comment nhiều rồi, mình sẽ giải thích chi tiết",
            ]
        },
        "salesperson": {
            "name": "salesperson",
            "system_prompt": """Bạn là một AI chuyên gia bán hàng đang live stream bán hàng. Hãy thú vị, thuyết phục, và tạo cảm giác khẩn cấp.
- Nhấn mạnh lợi ích sản phẩm
- Tạo cảm giác giới hạn thời gian
- Khuyến khích đặt hàng ngay
- Trả lời dưới 200 ký tự
- Dùng emoji 🎁 🔥 💰""",
            "default_responses": [
                "Sản phẩm hot số lượng có hạn đây mọi người ơi 🔥",
                "Giá tốt sốc được không các bạn?",
                "Chỉ hôm nay giảm 20% đặc biệt cho AE nha!",
                "Còn ít hàng, comment ngay để mình ghi tên bạn!",
            ]
        },
        "assistant": {
            "name": "assistant",
            "system_prompt": """Bạn là một AI trợ lý thông minh đang live stream, trò chuyện với người xem. Hãy tự nhiên, thân thiện, và tạo sự tương tác.
- Trò chuyện tự nhiên như bạn bè
- Đặt câu hỏi gắn kết
- Nhớ tên người comment
- Trả lời dưới 200 ký tự""",
            "default_responses": [
                "Chào mọi người đến với stream livestream!",
                "Mình vui lắm khi có mọi người xem đây",
                "Câu hỏi hay, mình sẽ trả lời nhé!",
                "Cảm ơn mọi người đã đến xem stream",
            ]
        }
    }

    def __init__(self):
        self.client: Optional[OpenAI] = None
        self.api_key: Optional[str] = None
        self.model: str = "gpt-4o-mini"
        self.base_url: str = None
        self.persona: str = "assistant"
        self.custom_system_prompt: str = None
        self.enabled: bool = False
        self.last_response_time: float = 0
        self.response_cooldown: float = 2.0  # Minimum seconds between responses
        self.max_comments_per_response: int = 3  # Process up to 3 comments at once
        
        # Response cache
        self.response_cache: deque = deque(maxlen=self.MAX_RESPONSES_CACHE)
        self.total_responses: int = 0
        
        # Callback for when a response is generated
        self.on_response_callbacks: List[Callable] = []
        
        # Recent context for conversation
        self.conversation_history: deque = deque(maxlen=20)
        
        # Thread safety
        self._lock = threading.Lock()

    def is_available(self) -> bool:
        """Check if OpenAI library is installed."""
        return OPENAI_AVAILABLE

    def configure(self, api_key: str, model: str = None, persona: str = "assistant", base_url: str = None, custom_system_prompt: str = None):
        """Configure the AI engine with API key and settings."""
        self.api_key = api_key
        if model:
            self.model = model
        if persona:
            self.persona = persona
        if base_url:
            self.base_url = base_url
        if custom_system_prompt is not None:
            self.custom_system_prompt = custom_system_prompt or None
        
        if OPENAI_AVAILABLE and api_key:
            try:
                if base_url:
                    self.client = OpenAI(api_key=api_key, base_url=base_url)
                else:
                    self.client = OpenAI(api_key=api_key)
                self.enabled = True
                logger.info(f"AI Engine configured with model: {self.model}, persona: {self.persona}" + (f", base_url: {base_url}" if base_url else ""))
            except Exception as e:
                logger.error(f"Failed to initialize OpenAI client: {e}")
                self.enabled = False
                self.last_error = str(e)
        else:
            if not OPENAI_AVAILABLE:
                logger.warning("OpenAI library not installed. Install with: pip install openai")
            self.enabled = False

    def set_persona(self, persona: str):
        """Change the AI persona."""
        if persona in self.PERSONAS:
            with self._lock:
                self.persona = persona
            logger.info(f"Persona changed to: {persona}")
        else:
            logger.warning(f"Unknown persona: {persona}")

    def set_enabled(self, enabled: bool):
        """Enable or disable the AI engine."""
        with self._lock:
            self.enabled = enabled and self.client is not None
        logger.info(f"AI Engine {'enabled' if self.enabled else 'disabled'}")

    def _get_persona_config(self) -> Dict[str, Any]:
        """Get the current persona configuration."""
        return self.PERSONAS.get(self.persona, self.PERSONAS["assistant"])

    def _build_system_prompt(self) -> str:
        """Build the system prompt combining live context + persona + custom."""
        persona_config = self._get_persona_config()
        parts = [self.LIVE_CONTEXT_PROMPT, persona_config["system_prompt"]]
        if self.custom_system_prompt:
            parts.append(self.custom_system_prompt)
        return "\n\n---\n\n".join(parts)

    def generate_response(self, comment: str, username: str = " Viewer", context: Optional[List[Dict]] = None) -> Optional[str]:
        """
        Generate an AI response to a comment.
        Returns None if AI is disabled or on error.
        """
        if not self.enabled or not self.client:
            return self._get_default_response()

        current_time = time.time()
        if current_time - self.last_response_time < self.response_cooldown:
            return None  # Still in cooldown

        with self._lock:
            self.last_response_time = current_time

        # Build conversation context
        messages = [
            {"role": "system", "content": self._build_system_prompt()},
        ]
        
        # Add recent history
        if context:
            messages.extend(context)
        else:
            messages.extend(list(self.conversation_history))
        
        # Add current comment
        messages.append({
            "role": "user", 
            "content": f"[{username}]: {comment} (Trả lời tự nhiên, ngắn gọn)"
        })

        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=self.DEFAULT_TEMPERATURE,
                max_tokens=self.DEFAULT_MAX_TOKENS,
                top_p=0.9,
            )
            
            if response and response.choices and response.choices[0].message:
                ai_reply = response.choices[0].message.content.strip()
                
                # Update conversation history
                with self._lock:
                    self.conversation_history.append({"role": "user", "content": f"{username}: {comment}"})
                    self.conversation_history.append({"role": "assistant", "content": ai_reply})
                    self.total_responses += 1
                    
                    # Cache response
                    self.response_cache.append({
                        "timestamp": datetime.now().strftime("%H:%M:%S"),
                        "comment": comment,
                        "username": username,
                        "response": ai_reply,
                        "persona": self.persona,
                        "model": self.model,
                    })
                
                # Fire callbacks
                for cb in self.on_response_callbacks:
                    try:
                        cb({
                            "comment": comment,
                            "username": username,
                            "response": ai_reply,
                            "timestamp": datetime.now().strftime("%H:%M:%S")
                        })
                    except Exception as e:
                        logger.error(f"Error in response callback: {e}")
                
                return ai_reply

        except Exception as e:
            logger.error(f"AI response error: {e}")
            return self._get_default_response()

        return None

    def _get_default_response(self) -> Optional[str]:
        """Get a random default response based on persona."""
        import random
        persona_config = self._get_persona_config()
        defaults = persona_config.get("default_responses", [])
        if defaults:
            return random.choice(defaults)
        return None

    def process_comments(self, comments: List[Dict[str, Any]]) -> Optional[str]:
        """
        Process a batch of comments and generate a response.
        Returns the best response for display.
        """
        if not comments:
            return None

        # Pick the most recent comments to process
        recent = comments[-self.max_comments_per_response:] if len(comments) > self.max_comments_per_response else comments
        
        best_comment = recent[-1]  # Most recent comment
        comment_text = best_comment.get("comment", "")
        username = best_comment.get("user", "Viewer")
        
        return self.generate_response(comment_text, username)

    def get_cached_responses(self, count: int = 10) -> List[Dict[str, Any]]:
        """Get recent AI responses from cache."""
        with self._lock:
            return list(self.response_cache)[-count:]

    def clear_cache(self):
        """Clear response cache."""
        with self._lock:
            self.response_cache.clear()
            self.conversation_history.clear()
        logger.info("AI response cache cleared")

    def get_telemetry(self) -> Dict[str, Any]:
        """Get AI engine telemetry."""
        with self._lock:
            return {
                "enabled": self.enabled,
                "available": self.is_available(),
                "api_key_configured": bool(self.api_key),
                "model": self.model,
                "base_url": self.base_url,
                "persona": self.persona,
                "custom_system_prompt": self.custom_system_prompt,
                "total_responses": self.total_responses,
                "available_personas": list(self.PERSONAS.keys()),
                "response_cooldown_seconds": self.response_cooldown,
                "cached_responses": len(self.response_cache),
            }


# Global instance
ai_engine = AIResponseEngine()
