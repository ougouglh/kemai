# 商品属性映射系统 (Attribute Mapping System)

## 项目简介

本系统用于从商品名称中自动提取**口味（flavor）**和**功能（function）**属性，适用于电商平台的商品标准化处理。

### 核心功能
- **口味提取**: 显式规则优先 + 模糊匹配补充 + 兜底机制
- **功能提取**: 规则库匹配 + 白名单类目模糊补充
- **Auto Alias**: 自动学习新的口味别名，持续优化匹配效果

---

## 目录结构

```
attribute_data/
├── attribute_mapping_system.py   # ⭐ 主脚本（生产环境使用）
├── config/                       # 配置文件目录
│   ├── flavor_explicit_map.json  # 口味显式映射表
│   ├── function.json             # 功能规则库
│   ├── fallback_rules.json       # 原味兜底规则
│   └── flavor_aggregate_map.json # 口味聚合映射
├── data/                         # 数据文件目录
│   ├── 分类商品0414.csv          # ⭐ 输入数据（待处理商品）
│   ├── 品类定义表_20260409_品类定义详情.csv
│   ├── flavor_pool_unified.csv   # 统一口味标准池
│   └── auto_alias_dict.csv       # 自动学习别名词典
├── out/                          # 输出目录
│   ├── attribute_mappings/        # 属性编码映射
│   │   ├── flavor_mapping.csv    # 口味名称→编码
│   │   └── function_mapping.csv  # 功能名称→编码
│   └── result_v173_round*_*.csv  # 处理结果
└── templates/                    # Web界面模板
    └── *.html
```

---

## 快速开始

### 1. 安装依赖

```bash
pip install rapidfuzz tqdm
```

> 注: rapidfuzz 用于模糊匹配，未安装时会使用 difflib 作为后备

### 2. 准备数据

将待处理的商品数据放入 `data/分类商品0414.csv`，文件格式：

| 列名 | 说明 | 示例 |
|------|------|------|
| 商品名称 | 商品全称 | 伊利安慕希希腊酸奶黄桃味 |
| 新类别 | 四级类目名称 | 酸奶/调制酸奶 |

### 3. 运行脚本

```bash
# 单轮运行
python attribute_mapping_system.py

# 多轮运行（用于观察 auto alias 学习效果）
python attribute_mapping_system.py 3
```

### 4. 查看结果

输出文件位于 `out/result_v173_round1_*.csv`，包含以下字段：

| 字段 | 说明 |
|------|------|
| 商品名称 | 原始商品名称 |
| 新类别 | 四级类目 |
| flavor_1/2/3 | 提取的口味（第1/2/3优先级） |
| flavor_id_1/2/3 | 口味编码 |
| function_1/2/3 | 提取的功能 |
| function_id_1/2/3 | 功能编码 |
| decision_path | 决策路径（用于调试） |

---

## 配置说明

### 口味显式映射表 (config/flavor_explicit_map.json)

定义口味触发词到标准口味的映射：

```json
{
  "草莓味": {
    "markers": ["草莓味", "草莓"],
    "is_fruit": true
  },
  "巧克力味": {
    "markers": ["巧克力味", "巧克力", "可可味"],
    "is_fruit": false
  }
}
```

### 功能规则库 (config/function.json)

```json
{
  "助消化": ["助消化", "调理肠胃"],
  "零脂肪": ["零脂肪", "无脂肪"]
}
```

### 原味兜底规则 (config/fallback_rules.json)

指定哪些类目在无口味匹配时默认使用"原味"：

```json
{
  "original_flavor_fallback": {
    "categories": ["纯牛奶", "原味酸奶"]
  }
}
```

---

## 数据来源

### 1. 品类定义表 (`data/品类定义表_*.csv`)
- **作用**: 定义类目层级结构（四级→三级→二级→一级）
- **关键字段**:
  - `口味` - 标记该类目是否需要口味映射（有值=需要）
  - `功能` - 标记该类目是否需要功能映射（有值=需要）
- **注意**: 这里只存储"是否需要"，不存储具体口味值

### 2. 口味池文件 (`data/flavor_pool_unified.csv`)
- **作用**: 提供各四级类目的标准口味池
- **关键字段**:
  - `pool_level` - 类目层级（四级/三级/二级）
  - `category_name` - 类目名称
  - `pool_flavors` - 该类目支持的标准口味（逗号分隔）
- **示例**: `四级,低温酸奶,原味,草莓味,黄桃味,...`
- **⚠️ 三级/二级口味池过滤规则**: 从三级/二级口味池匹配时，需排除以下无效口味：
  - `发酵`、`含气`、`夹心`、`低糖`、`无糖`、`乳酸菌味`

---

## 工作流程

```
品类定义表 (*.csv)
    │ - 类目层级结构
    │ - has_flavor / has_function 标记
    │
    ▼
┌─────────────────────────────────────────┐
│  初始化阶段                              │
│  ├── 支持的S/A类目列表                   │
│  ├── 各四级类目是否需要口味/功能          │
│  ├── 四级→三级→二级类目映射              │
│  └── 从flavor_pool_unified.csv加载      │
│      各层级标准口味池                    │
└─────────────────────────────────────────┘
    │
    ▼
┌─────────────────────────────────────────┐
│  逐商品处理                              │
│                                         │
│  口味提取流程:                           │
│  1. explicit_rule (显式规则匹配)          │
│  2. fuzzy (模糊匹配补充)                  │
│  3. fallback (兜底原味)                  │
│  4. post_process (互斥去重/聚合映射)      │
│                                         │
│  功能提取流程:                           │
│  1. rule (规则匹配)                      │
│  2. fuzzy (仅白名单类目)                 │
└─────────────────────────────────────────┘
    │
    ▼
输出CSV + JSON + 自动学习alias
```

---

## 版本说明

| 版本 | 说明 |
|------|------|
| V14 | 基础版，支持 S/A/B/C 类目 |
| V17 | 架构重构，explicit_rule 优先 |
| V18 | 数据驱动增强，口味聚合映射 |
| V19 | 动态阈值，水类原味兜底 |
| 主脚本 | 生产环境版本，使用 0414 数据 |

---

## 辅助脚本

| 脚本 | 用途 |
|------|------|
| `flavor_pool_web_app.py` | 口味池 Web 管理界面 |
| `analyze_accuracy.py` | 映射准确率分析 |
| `discover_missing_flavors.py` | 发现缺失口味 |
| `sync_flavor_pool.py` | 同步口味池数据 |

### 启动 Web 管理界面

```bash
python flavor_pool_web_app.py
# 访问 http://localhost:5000
```

---

## 常见问题

### Q: 运行时提示 "RapidFuzz未安装"
A: 不影响运行，系统会使用 difflib 作为后备。如需更好体验，运行 `pip install rapidfuzz`

### Q: 如何添加新的口味映射？
A: 编辑 `config/flavor_explicit_map.json`，添加新的映射规则

### Q: 如何查看决策过程？
A: 输出 CSV 中的 `decision_path` 字段记录了完整的匹配决策路径

### Q: 多轮运行有什么好处？
A: auto alias 会在每轮学习中积累新发现的别名，多轮运行可以看到学习效果提升

---

## 联系方式

如有问题，请联系项目维护者。
