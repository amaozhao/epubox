# EPUBox 项目结构

```
epubox/
├── docs/                    # 项目文档
│   ├── architecture/       # 架构设计文档
│   └── development/       # 开发相关文档
├── src/                    # 源代码
│   ├── api/               # API接口
│   │   ├── __init__.py
│   │   ├── auth.py       # 认证相关接口
│   │   ├── files.py      # 文件管理接口
│   │   └── translation.py # 翻译相关接口
│   ├── core/              # 核心业务逻辑
│   │   ├── __init__.py
│   │   ├── epub.py       # EPUB处理器
│   │   ├── html.py       # HTML处理器
│   │   └── translation.py # 翻译服务
│   ├── infrastructure/    # 基础设施
│   │   ├── __init__.py
│   │   ├── auth.py       # 认证服务
│   │   ├── config.py     # 配置管理
│   │   ├── database.py   # 数据库连接
│   │   ├── logging.py    # 日志管理
│   │   └── storage.py    # 存储服务
│   └── utils/            # 工具函数
│       ├── __init__.py
│       ├── errors.py     # 错误定义
│       └── validators.py # 数据验证
├── tests/                 # 测试代码
│   ├── __init__.py
│   ├── conftest.py       # 测试配置
│   ├── test_api/        # API测试
│   ├── test_core/       # 核心组件测试
│   └── test_infrastructure/ # 基础设施测试
├── .env.example          # 环境变量示例
├── .gitignore           # Git忽略文件
├── README.md            # 项目说明
└── requirements.txt     # 依赖包列表
```

## 目录说明

### src/
主要源代码目录，包含所有业务逻辑代码。

#### api/
FastAPI 接口定义，包含所有HTTP接口的实现。

#### core/
核心业务逻辑实现，包含主要的业务处理组件。

#### infrastructure/
基础设施代码，提供底层服务支持。

#### utils/
通用工具函数和辅助代码。

### tests/
测试代码目录，与src目录结构对应。

### docs/
项目文档目录，包含架构设计、开发计划等文档。
