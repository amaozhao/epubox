"""属性处理器模块，用于压缩和解压缩HTML标签属性。"""

from typing import Dict, List, Union


class AttributeProcessor:
    """处理HTML标签属性的压缩和解压缩。"""

    def __init__(self):
        self._attr_map = {}
        self._counter = 0
        self._prefix = f"__attr_{id(self)}_"

    def compress_attrs(
        self, attrs: Dict[str, Union[str, List[str]]]
    ) -> Dict[str, Union[str, List[str]]]:
        """压缩属性字典，保持原始结构。"""
        if not attrs:
            return attrs

        compressed = {}
        for key, value in attrs.items():
            if isinstance(value, (list, tuple)):
                compressed[key] = [self.compress_value(v) for v in value]
            else:
                compressed[key] = self.compress_value(value)
        return compressed

    def decompress_attrs(
        self, attrs: Dict[str, Union[str, List[str]]]
    ) -> Dict[str, Union[str, List[str]]]:
        """解压缩属性字典，保持原始结构。"""
        if not attrs:
            return attrs

        decompressed = {}
        for key, value in attrs.items():
            if isinstance(value, (list, tuple)):
                decompressed[key] = [self.decompress_value(v) for v in value]
            else:
                decompressed[key] = self.decompress_value(value)
        return decompressed

    def compress_value(self, value: str) -> str:
        """压缩单个属性值。"""
        if not isinstance(value, str) or len(value) <= 30:
            return value

        if not value.startswith(self._prefix):
            self._counter += 1
            compressed = f"{self._prefix}{self._counter}"
            self._attr_map[compressed] = value
            return compressed

        return value

    def decompress_value(self, value: str) -> str:
        """解压缩单个属性值。"""
        if not isinstance(value, str) or not value.startswith(self._prefix):
            return value
        return self._attr_map.get(value, value)

    def reset(self):
        """重置处理器状态。"""
        self._attr_map.clear()
        self._counter = 0
