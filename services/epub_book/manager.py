import json

class FileManager:
    @staticmethod
    def read_file(file_path):
        """
        读取文件内容
        """
        with open(file_path, 'r', encoding='utf-8') as file:
            return file.read()

    @staticmethod
    def write_file(file_path, content):
        """
        写入文件内容
        """
        with open(file_path, 'w', encoding='utf-8') as file:
            file.write(content)

    @staticmethod
    def load_progress(progress_file_path):
        """
        加载翻译进度
        """
        try:
            with open(progress_file_path, 'r') as progress_file:
                progress_data = json.load(progress_file)
                return progress_data.get('completed_indices', [])
        except (FileNotFoundError, json.JSONDecodeError):
            return []

    @staticmethod
    def save_progress(progress_file_path, completed_indices):
        """
        保存翻译完成的索引列表
        """
        with open(progress_file_path, 'w') as progress_file:
            json.dump({'completed_indices': completed_indices}, progress_file)
