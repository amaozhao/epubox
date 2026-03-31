import re
from typing import Dict, List


class Renumberer:
    """全局占位符 → Chunk局部占位符"""

    def renumber(self, text: str, tag_map: Dict[str, str]) -> Dict:
        """
        返回: {
            'text': '[id0]Hello[id1]',  # 局部索引
            'tag_map': {'[id0]': '<p>', '[id1]': '</p>'},
            'indices': [5, 6]  # 对应的全局索引
        }
        """
        # 1. 找所有占位符（按出现顺序）
        occurrences = []
        for m in re.finditer(r'\[id(\d+)\]', text):
            occurrences.append((m.start(), m.end(), f'[id{m.group(1)}]', int(m.group(1))))

        # 2. 逆序替换为临时标记（避免位置偏移）
        temp_markers = []
        for i in range(len(occurrences)):
            temp_markers.append(f'__T{i}__')

        temp_text = text
        for i in range(len(occurrences) - 1, -1, -1):
            start, end = occurrences[i][0], occurrences[i][1]
            temp_text = temp_text[:start] + temp_markers[i] + temp_text[end:]

        # 3. 替换为局部索引 (0, 1, 2...)
        local_map = {}
        global_indices = []
        for i, (_, _, _, global_idx) in enumerate(occurrences):
            local_map[f'[id{i}]'] = tag_map.get(f'[id{global_idx}]', '')
            global_indices.append(global_idx)
            temp_text = temp_text.replace(temp_markers[i], f'[id{i}]', 1)

        return {'text': temp_text, 'tag_map': local_map, 'indices': global_indices}
