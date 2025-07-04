"""
Enhanced Memory Module with Intelligent Indexing

This module implements advanced memory management features including:
- Multi-dimensional indexing (tags, type, time, quality score)
- Intelligent memory recall algorithms
- Memory decay and quality scoring
- Cross-layer memory search and filtering
- Token-aware memory recall with 25000 token limit
- Structured recall order: type → index → specified files
"""

import json
import logging
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from .conversation_memory import (
    ENABLE_ENHANCED_MEMORY,
    MEMORY_STORAGE_PATH,
    _load_memory_layer,
    _save_memory_layer,
)
from .conversation_memory import (
    save_memory as base_save_memory,
)
from .token_utils import estimate_tokens

logger = logging.getLogger(__name__)

# Enhanced memory configuration
MEMORY_INDEX_VERSION = "1.0"
MEMORY_INDEX_FILE = "memory_index.json"
MEMORY_DECAY_ENABLED = os.getenv("MEMORY_DECAY_ENABLED", "true").lower() == "true"
MEMORY_DECAY_DAYS = int(os.getenv("MEMORY_DECAY_DAYS", "30"))
MEMORY_QUALITY_THRESHOLD = float(os.getenv("MEMORY_QUALITY_THRESHOLD", "0.3"))

# Token limit for memory recall
MEMORY_TOKEN_LIMIT = int(os.getenv("MEMORY_TOKEN_LIMIT", "20000"))

# Recall order configuration
RECALL_ORDER = ["type", "index", "files"]  # 类型 → 索引 → 指定文件


# Index structure for fast lookups
class MemoryIndex:
    """
    Multi-dimensional index for memory entries.

    Provides fast lookups by:
    - Tags: Set of descriptive tags
    - Type: Category of memory (bug, feature, decision, etc.)
    - Time: Temporal indexing with bucketing
    - Quality: Score-based filtering
    - Layer: Cross-layer search capability
    """

    def __init__(self):
        self.version = MEMORY_INDEX_VERSION
        self.last_updated = datetime.now(timezone.utc).isoformat()

        # Multi-dimensional indexes
        self.tag_index: dict[str, set[str]] = defaultdict(set)  # tag -> set of memory keys
        self.type_index: dict[str, set[str]] = defaultdict(set)  # type -> set of memory keys
        self.time_index: dict[str, set[str]] = defaultdict(set)  # time bucket -> set of memory keys
        self.quality_index: dict[float, set[str]] = defaultdict(set)  # quality score -> set of memory keys
        self.layer_index: dict[str, set[str]] = defaultdict(set)  # layer -> set of memory keys

        # Reverse lookup
        self.memory_metadata: dict[str, dict[str, Any]] = {}  # key -> metadata

    def add_memory(self, key: str, layer: str, metadata: dict[str, Any], timestamp: str):
        """Add a memory entry to all relevant indexes."""
        # Layer index
        self.layer_index[layer].add(key)

        # Tag index
        tags = metadata.get("tags", [])
        if isinstance(tags, str):
            tags = [tags]
        for tag in tags:
            self.tag_index[tag.lower()].add(key)

        # Type index
        mem_type = metadata.get("type", "general")
        self.type_index[mem_type].add(key)

        # Time index (bucket by day)
        try:
            dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            time_bucket = dt.strftime("%Y-%m-%d")
            self.time_index[time_bucket].add(key)
        except Exception as e:
            logger.debug(f"Failed to parse timestamp {timestamp}: {e}")

        # Quality index (initialize with 1.0)
        quality = metadata.get("quality_score", 1.0)
        quality_bucket = round(quality, 1)  # Round to nearest 0.1
        self.quality_index[quality_bucket].add(key)

        # Store metadata for reverse lookup
        self.memory_metadata[key] = {
            "layer": layer,
            "tags": tags,
            "type": mem_type,
            "timestamp": timestamp,
            "quality_score": quality,
        }

    def remove_memory(self, key: str):
        """Remove a memory entry from all indexes."""
        metadata = self.memory_metadata.get(key)
        if not metadata:
            return

        # Remove from all indexes
        self.layer_index[metadata["layer"]].discard(key)

        for tag in metadata.get("tags", []):
            self.tag_index[tag.lower()].discard(key)

        self.type_index[metadata["type"]].discard(key)

        # Remove from time index
        try:
            dt = datetime.fromisoformat(metadata["timestamp"].replace("Z", "+00:00"))
            time_bucket = dt.strftime("%Y-%m-%d")
            self.time_index[time_bucket].discard(key)
        except Exception:
            pass

        # Remove from quality index
        quality_bucket = round(metadata.get("quality_score", 1.0), 1)
        self.quality_index[quality_bucket].discard(key)

        # Remove metadata
        del self.memory_metadata[key]

    def search_by_tags(self, tags: list[str], match_all: bool = False) -> set[str]:
        """Search memories by tags."""
        if not tags:
            return set()

        tag_sets = [self.tag_index[tag.lower()] for tag in tags]

        if match_all:
            # Intersection - must have all tags
            result = tag_sets[0].copy()
            for tag_set in tag_sets[1:]:
                result &= tag_set
            return result
        else:
            # Union - must have at least one tag
            result = set()
            for tag_set in tag_sets:
                result |= tag_set
            return result

    def search_by_type(self, mem_type: str) -> set[str]:
        """Search memories by type."""
        return self.type_index.get(mem_type, set()).copy()

    def search_by_time_range(
        self, start_date: Optional[datetime] = None, end_date: Optional[datetime] = None
    ) -> set[str]:
        """Search memories within a time range."""
        result = set()

        # Default to last 30 days if no range specified
        if not start_date and not end_date:
            end_date = datetime.now(timezone.utc)
            start_date = end_date - timedelta(days=30)
        elif not start_date:
            start_date = datetime.min.replace(tzinfo=timezone.utc)
        elif not end_date:
            end_date = datetime.now(timezone.utc)

        # Iterate through time buckets
        current_date = start_date.date()
        end_date_only = end_date.date()

        while current_date <= end_date_only:
            bucket = current_date.strftime("%Y-%m-%d")
            result |= self.time_index.get(bucket, set())
            current_date += timedelta(days=1)

        return result

    def search_by_quality(self, min_score: float = 0.0, max_score: float = 1.0) -> set[str]:
        """Search memories by quality score range."""
        result = set()

        # 处理 None 值，使用默认值
        min_score = min_score if min_score is not None else 0.0
        max_score = max_score if max_score is not None else 1.0

        # Iterate through quality buckets
        for score_bucket in range(int(min_score * 10), int(max_score * 10) + 1):
            score = score_bucket / 10.0
            result |= self.quality_index.get(score, set())

        return result

    def search_by_layer(self, layer: str) -> set[str]:
        """Search memories by layer."""
        return self.layer_index.get(layer, set()).copy()

    def get_memory_metadata(self, key: str) -> Optional[dict[str, Any]]:
        """Get metadata for a memory key."""
        return self.memory_metadata.get(key)

    def to_dict(self) -> dict[str, Any]:
        """Serialize index to dictionary."""
        return {
            "version": self.version,
            "last_updated": self.last_updated,
            "tag_index": {tag: list(keys) for tag, keys in self.tag_index.items()},
            "type_index": {typ: list(keys) for typ, keys in self.type_index.items()},
            "time_index": {time: list(keys) for time, keys in self.time_index.items()},
            "quality_index": {str(score): list(keys) for score, keys in self.quality_index.items()},
            "layer_index": {layer: list(keys) for layer, keys in self.layer_index.items()},
            "memory_metadata": self.memory_metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MemoryIndex":
        """Deserialize index from dictionary."""
        index = cls()
        index.version = data.get("version", MEMORY_INDEX_VERSION)
        index.last_updated = data.get("last_updated", datetime.now(timezone.utc).isoformat())

        # Rebuild indexes
        for tag, keys in data.get("tag_index", {}).items():
            index.tag_index[tag] = set(keys)

        for typ, keys in data.get("type_index", {}).items():
            index.type_index[typ] = set(keys)

        for time, keys in data.get("time_index", {}).items():
            index.time_index[time] = set(keys)

        for score_str, keys in data.get("quality_index", {}).items():
            score = float(score_str)
            index.quality_index[score] = set(keys)

        for layer, keys in data.get("layer_index", {}).items():
            index.layer_index[layer] = set(keys)

        index.memory_metadata = data.get("memory_metadata", {})

        return index


# Global memory index instance
_memory_index: Optional[MemoryIndex] = None


def get_memory_index() -> MemoryIndex:
    """Get or create the global memory index."""
    global _memory_index

    if _memory_index is None:
        _memory_index = load_memory_index()

    return _memory_index


def load_memory_index() -> MemoryIndex:
    """Load memory index from disk."""
    index_path = MEMORY_STORAGE_PATH / MEMORY_INDEX_FILE

    if index_path.exists():
        try:
            with open(index_path, encoding="utf-8") as f:
                data = json.load(f)
                return MemoryIndex.from_dict(data)
        except Exception as e:
            logger.warning(f"Failed to load memory index: {e}")

    # Create new index if loading failed
    return MemoryIndex()


def save_memory_index(index: Optional[MemoryIndex] = None) -> bool:
    """Save memory index to disk."""
    if index is None:
        index = get_memory_index()

    try:
        MEMORY_STORAGE_PATH.mkdir(exist_ok=True)
        index_path = MEMORY_STORAGE_PATH / MEMORY_INDEX_FILE

        with open(index_path, "w", encoding="utf-8") as f:
            json.dump(index.to_dict(), f, indent=2, ensure_ascii=False)

        logger.debug(f"Memory index saved to {index_path}")
        return True
    except Exception as e:
        logger.error(f"Failed to save memory index: {e}")
        return False


def rebuild_memory_index() -> MemoryIndex:
    """Rebuild the entire memory index from stored memories."""
    logger.info("Rebuilding memory index...")
    index = MemoryIndex()

    # Process each memory layer
    for layer in ["global", "project"]:
        layer_data = _load_memory_layer(layer)

        for key, memory in layer_data.items():
            metadata = memory.get("metadata", {})
            timestamp = memory.get("timestamp", datetime.now(timezone.utc).isoformat())
            index.add_memory(key, layer, metadata, timestamp)

    # Save the rebuilt index
    save_memory_index(index)

    logger.info(f"Memory index rebuilt with {len(index.memory_metadata)} entries")
    return index


def calculate_memory_quality(memory: dict[str, Any]) -> float:
    """
    Calculate quality score for a memory based on various factors.

    Factors considered:
    - Age (newer is better)
    - Access frequency
    - Content length and structure
    - Metadata completeness
    - Relevance indicators
    """
    score = 1.0

    # Age factor (decay over time)
    if MEMORY_DECAY_ENABLED:
        try:
            timestamp = memory.get("timestamp", "")
            dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            age_days = (datetime.now(timezone.utc) - dt).days

            if age_days > MEMORY_DECAY_DAYS:
                # Linear decay after threshold
                decay_factor = max(0.3, 1.0 - (age_days - MEMORY_DECAY_DAYS) / MEMORY_DECAY_DAYS)
                score *= decay_factor
        except Exception:
            pass

    # Content quality factor
    content = memory.get("content", "")
    if isinstance(content, str):
        # Longer, structured content is generally better
        if len(content) > 500:
            score *= 1.1
        elif len(content) < 50:
            score *= 0.8

    # Metadata completeness factor
    metadata = memory.get("metadata", {})
    if metadata.get("tags"):
        score *= 1.05
    if metadata.get("type") and metadata.get("type") != "general":
        score *= 1.05

    # Access frequency factor (if tracked)
    access_count = metadata.get("access_count", 0)
    if access_count > 10:
        score *= 1.2
    elif access_count > 5:
        score *= 1.1

    # Importance indicators
    if metadata.get("importance") == "high":
        score *= 1.3
    elif metadata.get("importance") == "low":
        score *= 0.7

    # Normalize to 0-1 range
    return min(1.0, max(0.0, score))


def enhanced_save_memory(
    content: Any,
    layer: str = "session",
    metadata: Optional[dict[str, Any]] = None,
    key: Optional[str] = None,
    tags: Optional[list[str]] = None,
    mem_type: Optional[str] = None,
    importance: Optional[str] = None,
) -> str:
    """
    Enhanced memory save with intelligent indexing.

    Args:
        content: The content to save
        layer: Memory layer ("global", "project", "session")
        metadata: Additional metadata
        key: Optional key (auto-generated if not provided)
        tags: List of tags for indexing
        mem_type: Type of memory (bug, feature, decision, etc.)
        importance: Importance level (high, medium, low)

    Returns:
        str: The key used to store the memory
    """
    if not ENABLE_ENHANCED_MEMORY:
        logger.debug("Enhanced memory is disabled")
        return ""

    # Prepare enhanced metadata
    enhanced_metadata = metadata or {}

    if tags:
        enhanced_metadata["tags"] = tags

    if mem_type:
        enhanced_metadata["type"] = mem_type

    if importance:
        enhanced_metadata["importance"] = importance

    # Auto-tagging based on content analysis
    if not tags and isinstance(content, str):
        auto_tags = extract_auto_tags(content)
        if auto_tags:
            enhanced_metadata["tags"] = enhanced_metadata.get("tags", []) + auto_tags

    # Save using base function
    key = base_save_memory(content, layer, enhanced_metadata, key)

    if key:
        # Update index
        index = get_memory_index()
        timestamp = datetime.now(timezone.utc).isoformat()

        # Calculate initial quality score
        memory_entry = {
            "content": content,
            "metadata": enhanced_metadata,
            "timestamp": timestamp,
            "layer": layer,
        }
        quality_score = calculate_memory_quality(memory_entry)
        enhanced_metadata["quality_score"] = quality_score

        # Add to index
        index.add_memory(key, layer, enhanced_metadata, timestamp)
        save_memory_index(index)

        logger.debug(
            f"Enhanced memory saved with key: {key}, tags: {enhanced_metadata.get('tags', [])}, quality: {quality_score:.2f}"
        )

    return key


def extract_auto_tags(content: str) -> list[str]:
    """Extract tags automatically from content."""
    tags = []
    content_lower = content.lower()

    # Programming language detection
    languages = ["python", "javascript", "typescript", "java", "cpp", "rust", "go"]
    for lang in languages:
        if lang in content_lower:
            tags.append(lang)

    # Topic detection
    topics = {
        "bug": ["bug", "error", "issue", "problem", "fix"],
        "feature": ["feature", "implement", "add", "new"],
        "refactor": ["refactor", "cleanup", "improve", "optimize"],
        "test": ["test", "testing", "unit test", "integration"],
        "doc": ["document", "documentation", "readme", "comment"],
        "security": ["security", "vulnerability", "auth", "permission"],
        "performance": ["performance", "optimize", "speed", "memory"],
        "architecture": ["architecture", "design", "pattern", "structure"],
    }

    for tag, keywords in topics.items():
        if any(keyword in content_lower for keyword in keywords):
            tags.append(tag)

    return list(set(tags))  # Remove duplicates


def intelligent_recall_memory(
    query: Optional[str] = None,
    tags: Optional[list[str]] = None,
    mem_type: Optional[str] = None,
    layer: Optional[str] = None,
    time_range: Optional[tuple[datetime, datetime]] = None,
    min_quality: float = MEMORY_QUALITY_THRESHOLD,
    match_mode: str = "any",  # "any" or "all" for tag matching
    limit: int = 10,
    include_metadata: bool = True,
) -> list[dict[str, Any]]:
    """
    Intelligent memory recall with multi-dimensional filtering.

    Args:
        query: Text search query
        tags: Tags to filter by
        mem_type: Memory type filter
        layer: Specific layer to search
        time_range: Tuple of (start_date, end_date)
        min_quality: Minimum quality score
        match_mode: "any" or "all" for tag matching
        limit: Maximum results
        include_metadata: Include full metadata in results

    Returns:
        List of matching memories with relevance scoring
    """
    if not ENABLE_ENHANCED_MEMORY:
        return []

    index = get_memory_index()

    # Start with all memories
    candidate_keys = set(index.memory_metadata.keys())

    # Apply filters progressively
    if tags:
        tag_matches = index.search_by_tags(tags, match_all=(match_mode == "all"))
        candidate_keys &= tag_matches

    if mem_type:
        type_matches = index.search_by_type(mem_type)
        candidate_keys &= type_matches

    if layer:
        layer_matches = index.search_by_layer(layer)
        candidate_keys &= layer_matches

    if time_range:
        time_matches = index.search_by_time_range(time_range[0], time_range[1])
        candidate_keys &= time_matches

    # Apply quality filter
    quality_matches = index.search_by_quality(min_quality, 1.0)
    candidate_keys &= quality_matches

    # Load and score memories
    results = []
    layers_to_search = [layer] if layer else ["global", "project"]

    for search_layer in layers_to_search:
        layer_data = _load_memory_layer(search_layer)

        for key in candidate_keys:
            if key not in layer_data:
                continue

            memory = layer_data[key]

            # Text search if query provided
            if query:
                content_str = str(memory.get("content", ""))
                query_str = str(query) if query is not None else ""
                if query_str and query_str.lower() not in content_str.lower():
                    continue

            # Calculate relevance score
            relevance = calculate_relevance_score(memory, query, tags, mem_type)

            # Prepare result
            result = {
                "key": key,
                "layer": search_layer,
                "content": memory.get("content"),
                "timestamp": memory.get("timestamp"),
                "relevance_score": relevance,
            }

            if include_metadata:
                result["metadata"] = memory.get("metadata", {})

            results.append(result)

    # Sort by relevance and timestamp
    results.sort(key=lambda x: (x["relevance_score"], x["timestamp"]), reverse=True)

    # Apply limit
    return results[:limit]


def calculate_relevance_score(
    memory: dict[str, Any],
    query: Optional[str],
    tags: Optional[list[str]],
    mem_type: Optional[str],
) -> float:
    """Calculate relevance score for a memory based on search criteria."""
    score = 0.0
    metadata = memory.get("metadata", {})

    # Base quality score
    score += metadata.get("quality_score", 0.5) * 0.3

    # Query match scoring
    if query:
        content_str = str(memory.get("content", "")).lower()
        query_str = str(query) if query is not None else ""
        query_lower = query_str.lower()

        # Exact match bonus
        if query_lower in content_str:
            score += 0.3

            # Position bonus (earlier matches score higher)
            position = content_str.find(query_lower)
            if position >= 0:
                position_factor = 1.0 - (position / max(len(content_str), 1))
                score += position_factor * 0.1

    # Tag match scoring
    if tags:
        memory_tags = {tag.lower() for tag in metadata.get("tags", [])}
        query_tags = {tag.lower() for tag in tags}

        if memory_tags and query_tags:
            # Jaccard similarity
            intersection = len(memory_tags & query_tags)
            union = len(memory_tags | query_tags)
            if union > 0:
                score += (intersection / union) * 0.2

    # Type match scoring
    if mem_type and metadata.get("type") == mem_type:
        score += 0.2

    # Recency bonus
    try:
        timestamp = memory.get("timestamp", "")
        dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        age_hours = (datetime.now(timezone.utc) - dt).total_seconds() / 3600

        # Logarithmic decay - recent memories score higher
        if age_hours < 24:
            score += 0.1
        elif age_hours < 168:  # 1 week
            score += 0.05
    except Exception:
        pass

    return min(1.0, max(0.0, score))


def update_memory_access(key: str, layer: str):
    """Update access count for a memory."""
    try:
        layer_data = _load_memory_layer(layer)
        if key in layer_data:
            memory = layer_data[key]
            metadata = memory.get("metadata", {})
            metadata["access_count"] = metadata.get("access_count", 0) + 1
            metadata["last_accessed"] = datetime.now(timezone.utc).isoformat()
            memory["metadata"] = metadata

            # Recalculate quality score
            new_quality = calculate_memory_quality(memory)
            metadata["quality_score"] = new_quality

            # Update index
            index = get_memory_index()
            index.remove_memory(key)
            index.add_memory(key, layer, metadata, memory.get("timestamp"))

            # Save updates
            _save_memory_layer(layer, layer_data)
            save_memory_index(index)
    except Exception as e:
        logger.debug(f"Failed to update memory access: {e}")


def cleanup_old_memories(days: int = MEMORY_DECAY_DAYS * 2):
    """Remove memories older than specified days with low quality scores."""
    if not ENABLE_ENHANCED_MEMORY or not MEMORY_DECAY_ENABLED:
        return

    logger.info(f"Cleaning up memories older than {days} days with low quality...")

    cutoff_date = datetime.now(timezone.utc) - timedelta(days=days)
    index = get_memory_index()
    removed_count = 0

    for layer in ["global", "project"]:
        layer_data = _load_memory_layer(layer)
        keys_to_remove = []

        for key, memory in layer_data.items():
            try:
                timestamp = memory.get("timestamp", "")
                dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))

                if dt < cutoff_date:
                    quality = calculate_memory_quality(memory)
                    if quality < MEMORY_QUALITY_THRESHOLD:
                        keys_to_remove.append(key)
            except Exception:
                continue

        # Remove old memories
        for key in keys_to_remove:
            del layer_data[key]
            index.remove_memory(key)
            removed_count += 1

        if keys_to_remove:
            _save_memory_layer(layer, layer_data)

    if removed_count > 0:
        save_memory_index(index)
        logger.info(f"Removed {removed_count} old memories with low quality scores")


def token_aware_memory_recall(
    query: Optional[str] = None,
    tags: Optional[list[str]] = None,
    mem_type: Optional[str] = None,
    layer: Optional[str] = None,
    time_range: Optional[tuple[datetime, datetime]] = None,
    min_quality: float = MEMORY_QUALITY_THRESHOLD,
    match_mode: str = "any",
    token_limit: int = MEMORY_TOKEN_LIMIT,
    include_metadata: bool = True,
    specified_files: Optional[list[str]] = None,
) -> dict[str, Any]:
    """
    Token-aware memory recall with structured order and 20000 token limit.

    回忆顺序：类型 → 索引 → 指定文件

    Args:
        query: 文本搜索查询
        tags: 标签过滤
        mem_type: 记忆类型过滤
        layer: 特定层搜索
        time_range: 时间范围元组
        min_quality: 最小质量分数
        match_mode: 标签匹配模式 ("any" 或 "all")
        token_limit: Token 限制 (默认 20000)
        include_metadata: 是否包含完整元数据
        specified_files: 指定要查看的文件列表

    Returns:
        包含结构化回忆结果的字典：
        {
            "total_tokens": int,
            "recall_summary": {
                "by_type": {...},
                "by_index": {...},
                "specified_files": {...}
            },
            "memories": [...],
            "truncated": bool
        }
    """
    if not ENABLE_ENHANCED_MEMORY:
        return {
            "total_tokens": 0,
            "recall_summary": {"by_type": {}, "by_index": {}, "specified_files": {}},
            "memories": [],
            "truncated": False,
        }

    result = {
        "total_tokens": 0,
        "recall_summary": {"by_type": {}, "by_index": {}, "specified_files": {}},
        "memories": [],
        "truncated": False,
    }

    current_tokens = 0

    # 第一步：按类型回忆 (type)
    logger.debug("开始按类型回忆记忆...")
    type_memories, type_tokens = _recall_by_type(
        query,
        tags,
        mem_type,
        layer,
        time_range,
        min_quality,
        match_mode,
        token_limit - current_tokens,
        include_metadata,
    )

    result["recall_summary"]["by_type"] = {
        "count": len(type_memories),
        "tokens": type_tokens,
        "types_found": list({m.get("metadata", {}).get("type", "general") for m in type_memories}),
    }

    result["memories"].extend(type_memories)
    current_tokens += type_tokens

    # 第二步：按索引回忆 (index) - 如果还有 token 空间
    if current_tokens < token_limit:
        logger.debug("开始按索引回忆记忆...")
        remaining_tokens = token_limit - current_tokens

        index_memories, index_tokens = _recall_by_index(
            query,
            tags,
            mem_type,
            layer,
            time_range,
            min_quality,
            match_mode,
            remaining_tokens,
            include_metadata,
            exclude_keys={m["key"] for m in type_memories},
        )

        result["recall_summary"]["by_index"] = {
            "count": len(index_memories),
            "tokens": index_tokens,
            "index_categories": _get_index_categories_used(index_memories),
        }

        result["memories"].extend(index_memories)
        current_tokens += index_tokens

    # 第三步：查看指定文件 (specified files) - 如果还有 token 空间且指定了文件
    if current_tokens < token_limit and specified_files:
        logger.debug(f"开始查看指定文件: {specified_files}")
        remaining_tokens = token_limit - current_tokens

        file_memories, file_tokens = _recall_specified_files(
            specified_files, remaining_tokens, include_metadata, exclude_keys={m["key"] for m in result["memories"]}
        )

        result["recall_summary"]["specified_files"] = {
            "count": len(file_memories),
            "tokens": file_tokens,
            "files_processed": specified_files,
            "files_found": [f for f in specified_files if any(f in str(m.get("content", "")) for m in file_memories)],
        }

        result["memories"].extend(file_memories)
        current_tokens += file_tokens

    result["total_tokens"] = current_tokens
    result["truncated"] = current_tokens >= token_limit

    logger.info(
        f"记忆回忆完成 - 总 tokens: {current_tokens}/{token_limit}, "
        f"记忆条数: {len(result['memories'])}, 是否截断: {result['truncated']}"
    )

    return result


def _recall_by_type(
    query: Optional[str],
    tags: Optional[list[str]],
    mem_type: Optional[str],
    layer: Optional[str],
    time_range: Optional[tuple[datetime, datetime]],
    min_quality: float,
    match_mode: str,
    remaining_tokens: int,
    include_metadata: bool,
) -> tuple[list[dict[str, Any]], int]:
    """按类型回忆记忆，优先处理重要类型。"""
    if remaining_tokens <= 0:
        return [], 0

    # 按重要性排序的记忆类型
    important_types = [
        "bug",
        "error",
        "issue",
        "critical",
        "feature",
        "enhancement",
        "improvement",
        "decision",
        "architecture",
        "design",
        "performance",
        "security",
        "test",
        "refactor",
        "documentation",
        "general",
    ]

    memories = []
    total_tokens = 0

    # 如果指定了特定类型，优先处理
    types_to_process = [mem_type] if mem_type else important_types

    for type_name in types_to_process:
        if total_tokens >= remaining_tokens:
            break

        # 获取该类型的记忆
        type_memories = intelligent_recall_memory(
            query=query,
            tags=tags,
            mem_type=type_name,
            layer=layer,
            time_range=time_range,
            min_quality=min_quality,
            match_mode=match_mode,
            limit=50,  # 每种类型最多50条
            include_metadata=include_metadata,
        )

        # 按 token 预算添加记忆
        for memory in type_memories:
            content = str(memory.get("content", ""))
            if include_metadata:
                content += str(memory.get("metadata", {}))

            memory_tokens = estimate_tokens(content)

            if total_tokens + memory_tokens <= remaining_tokens:
                memories.append(memory)
                total_tokens += memory_tokens
            else:
                break

        if mem_type:  # 如果指定了类型，只处理这一种
            break

    return memories, total_tokens


def _recall_by_index(
    query: Optional[str],
    tags: Optional[list[str]],
    mem_type: Optional[str],
    layer: Optional[str],
    time_range: Optional[tuple[datetime, datetime]],
    min_quality: float,
    match_mode: str,
    remaining_tokens: int,
    include_metadata: bool,
    exclude_keys: set[str],
) -> tuple[list[dict[str, Any]], int]:
    """按索引回忆记忆，使用多维度索引检索。"""
    if remaining_tokens <= 0:
        return [], 0

    memories = []
    total_tokens = 0

    # 按标签索引检索
    if tags:
        tag_memories = intelligent_recall_memory(
            query=query,
            tags=tags,
            layer=layer,
            time_range=time_range,
            min_quality=min_quality,
            match_mode=match_mode,
            limit=30,
            include_metadata=include_metadata,
        )

        for memory in tag_memories:
            if memory["key"] in exclude_keys:
                continue

            content = str(memory.get("content", ""))
            if include_metadata:
                content += str(memory.get("metadata", {}))

            memory_tokens = estimate_tokens(content)

            if total_tokens + memory_tokens <= remaining_tokens:
                memories.append(memory)
                total_tokens += memory_tokens
                exclude_keys.add(memory["key"])
            else:
                break

    # 按时间索引检索最近的记忆
    if total_tokens < remaining_tokens:
        recent_memories = intelligent_recall_memory(
            query=query,
            layer=layer,
            time_range=time_range,
            min_quality=min_quality,
            limit=20,
            include_metadata=include_metadata,
        )

        for memory in recent_memories:
            if memory["key"] in exclude_keys:
                continue

            content = str(memory.get("content", ""))
            if include_metadata:
                content += str(memory.get("metadata", {}))

            memory_tokens = estimate_tokens(content)

            if total_tokens + memory_tokens <= remaining_tokens:
                memories.append(memory)
                total_tokens += memory_tokens
                exclude_keys.add(memory["key"])
            else:
                break

    return memories, total_tokens


def _recall_specified_files(
    file_list: list[str], remaining_tokens: int, include_metadata: bool, exclude_keys: set[str]
) -> tuple[list[dict[str, Any]], int]:
    """回忆指定文件相关的记忆。"""
    if remaining_tokens <= 0 or not file_list:
        return [], 0

    memories = []
    total_tokens = 0

    for file_path in file_list:
        if total_tokens >= remaining_tokens:
            break

        # 搜索包含文件路径的记忆
        file_memories = intelligent_recall_memory(
            query=file_path, tags=["file", "code", "implementation"], limit=10, include_metadata=include_metadata
        )

        for memory in file_memories:
            if memory["key"] in exclude_keys:
                continue

            content = str(memory.get("content", ""))
            if include_metadata:
                content += str(memory.get("metadata", {}))

            memory_tokens = estimate_tokens(content)

            if total_tokens + memory_tokens <= remaining_tokens:
                memories.append(memory)
                total_tokens += memory_tokens
                exclude_keys.add(memory["key"])
            else:
                break

    return memories, total_tokens


def _get_index_categories_used(memories: list[dict[str, Any]]) -> list[str]:
    """获取索引回忆中使用的分类。"""
    categories = set()

    for memory in memories:
        metadata = memory.get("metadata", {})

        # 添加标签分类
        tags = metadata.get("tags", [])
        if isinstance(tags, list):
            categories.update(tags)

        # 添加类型分类
        mem_type = metadata.get("type")
        if mem_type:
            categories.add(mem_type)

    return sorted(categories)


def get_memory_stats_with_tokens() -> dict[str, Any]:
    """获取包含 token 统计的记忆系统状态。"""
    if not ENABLE_ENHANCED_MEMORY:
        return {"enabled": False}

    index = get_memory_index()
    stats = {
        "enabled": True,
        "total_memories": len(index.memory_metadata),
        "token_limit": MEMORY_TOKEN_LIMIT,
        "layers": {},
        "types": {},
        "total_tokens": 0,
    }

    # 统计各层记忆和 token 数量
    for layer in ["global", "project"]:
        layer_data = _load_memory_layer(layer)
        layer_tokens = 0
        layer_count = 0

        for _key, memory in layer_data.items():
            content = str(memory.get("content", ""))
            memory_tokens = estimate_tokens(content)
            layer_tokens += memory_tokens
            layer_count += 1

        stats["layers"][layer] = {"count": layer_count, "tokens": layer_tokens}
        stats["total_tokens"] += layer_tokens

    # 统计各类型记忆
    for mem_type, keys in index.type_index.items():
        stats["types"][mem_type] = len(keys)

    stats["token_utilization"] = stats["total_tokens"] / MEMORY_TOKEN_LIMIT if MEMORY_TOKEN_LIMIT > 0 else 0

    return stats


# Initialize index on module load
if ENABLE_ENHANCED_MEMORY:
    _memory_index = load_memory_index()
