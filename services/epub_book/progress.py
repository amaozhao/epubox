import json
import os


class ProgressManager:
    def __init__(self, progress_file="progress.json"):
        self.progress_file = progress_file
        # 确保进度文件存在
        if not os.path.exists(self.progress_file):
            with open(self.progress_file, "w", encoding="utf-8") as f:
                json.dump({}, f)

    def load_progress(self, file_path):
        """
        加载指定文件的翻译进度，返回已完成的索引列表
        """
        if not os.path.exists(self.progress_file):
            return []

        with open(self.progress_file, "r", encoding="utf-8") as f:
            progress_data = json.load(f)

        # 返回文件的进度，如果不存在则返回空列表
        return progress_data.get(file_path, [])

    def save_progress(self, file_path, completed_indices):
        """
        保存文件的翻译进度
        """
        with open(self.progress_file, "r", encoding="utf-8") as f:
            progress_data = json.load(f)

        # 更新文件的进度
        progress_data[file_path] = completed_indices

        # 写回到进度文件
        with open(self.progress_file, "w", encoding="utf-8") as f:
            json.dump(progress_data, f, indent=4, ensure_ascii=False)

