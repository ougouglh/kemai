import csv
import re
import json
import os
from collections import defaultdict
from difflib import SequenceMatcher
from datetime import datetime
from tqdm import tqdm

try:
    from rapidfuzz import fuzz
    RAPIDFUZZ_AVAILABLE = True
except ImportError:
    RAPIDFUZZ_AVAILABLE = False
    print("RapidFuzz未安装，将使用difflib作为后备")


def load_function_rules(json_path='config/function.json'):
    try:
        with open(json_path, 'r', encoding='utf-8') as f:
            rules = json.load(f)
        print(f"加载功能规则库: {len(rules)} 条")
        return rules
    except FileNotFoundError:
        print(f"规则库文件 {json_path} 不存在，使用空规则库")
        return {}


def load_flavor_explicit_map(json_path='config/flavor_explicit_map.json'):
    """从JSON文件加载口味显式映射表
    
    返回:
        dict: {标准口味: {'markers': [触发词列表], 'is_fruit': bool}} 格式的映射表
    """
    try:
        with open(json_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        # 解析新格式的映射表
        mapping = {}
        for k, v in data.items():
            if k.startswith('_'):
                continue  # 跳过元数据键
            if isinstance(v, dict) and 'markers' in v:
                # V17.27: 保留完整的字典格式，包括 markers 和 is_fruit
                mapping[k] = {
                    'markers': v['markers'],
                    'is_fruit': v.get('is_fruit', True)  # 默认为 True（向后兼容）
                }
            elif isinstance(v, list):
                mapping[k] = {
                    'markers': v,
                    'is_fruit': True  # 旧格式默认为水果口味
                }
        
        print(f"加载口味显式映射表: {len(mapping)} 条")
        return mapping
    except FileNotFoundError:
        print(f"口味映射文件 {json_path} 不存在，使用内置映射")
        return None


def extract_fruit_words_from_json(json_path='config/flavor_explicit_map.json'):
    """从JSON文件自动提取fruit_words列表
    
    提取所有is_fruit=true的口味中的fruit_words字段
    """
    try:
        with open(json_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        fruit_words = set()
        for k, v in data.items():
            if k.startswith('_'):
                continue
            if isinstance(v, dict) and v.get('is_fruit', False):
                # 从fruit_words字段提取
                if 'fruit_words' in v:
                    fruit_words.update(v['fruit_words'])
                # 同时从markers中提取不含"味"的词作为补充
                for marker in v.get('markers', []):
                    if '味' not in marker and len(marker) >= 2:
                        fruit_words.add(marker)
        
        result = sorted(list(fruit_words), key=len, reverse=True)  # 按长度降序，优先匹配长的
        print(f"自动提取fruit_words: {len(result)} 个")
        return result
    except FileNotFoundError:
        print(f"口味映射文件 {json_path} 不存在，使用内置fruit_words")
        return None


def load_flavor_signal_rules(json_path='config/flavor_explicit_map.json'):
    """从JSON文件加载口味信号检测规则"""
    try:
        with open(json_path, 'r', encoding='utf-8') as f:
            data = json.load(f)

        rules = data.get('_flavor_signal_rules', {})
        context_words = rules.get('context_words', [])
        flavor_friendly_categories = rules.get('flavor_friendly_categories', [])

        print(f"加载口味信号规则: {len(context_words)} 个context词, {len(flavor_friendly_categories)} 个适配类目")
        return context_words, flavor_friendly_categories
    except FileNotFoundError:
        print(f"口味映射文件 {json_path} 不存在，使用内置规则")
        return None, None


def load_category_specific_fruit_map(json_path='config/flavor_explicit_map.json'):
    """从JSON文件加载类目特定的水果映射表"""
    try:
        with open(json_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        fruit_map = data.get('_category_specific_fruit_map', {})
        print(f"加载类目特定水果映射表: {len(fruit_map)} 个类目")
        return fruit_map
    except FileNotFoundError:
        print(f"口味映射文件 {json_path} 不存在，使用空映射")
        return {}


AUTO_CATEGORY_SPECIFIC_FRUIT_MAP = load_category_specific_fruit_map()


def extract_non_fruit_flavor_words_from_json(json_path='config/flavor_explicit_map.json'):
    """从JSON文件自动提取non_fruit_flavor_words列表
    提取_non_fruit_flavor_words字段
    """
    try:
        with open(json_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        non_fruit_words = data.get('_non_fruit_flavor_words', [])
        print(f"自动提取non_fruit_flavor_words: {len(non_fruit_words)} 个")
        return set(non_fruit_words)
    except FileNotFoundError:
        print(f"口味映射文件 {json_path} 不存在，使用内置non_fruit_flavor_words")
        return None


FUNCTION_RULES = load_function_rules()
FLAVOR_EXPLICIT_MAP = load_flavor_explicit_map()
AUTO_FRUIT_WORDS = extract_fruit_words_from_json()
AUTO_NON_FRUIT_FLAVOR_WORDS = extract_non_fruit_flavor_words_from_json()
AUTO_CONTEXT_WORDS, AUTO_FLAVOR_FRIENDLY_CATEGORIES = load_flavor_signal_rules()


class AttributeMappingSystemV173:
    """
    V17.3 生产级结构:
    - flavor: 显式规则优先 + fuzzy补充 + 其他口味兜底 + 统一后处理
    - function: 小规则库优先 + 白名单类目 fuzzy补充 + auto alias
    - 仅处理 S/A 类目

    相比 V17.2 的关键升级:
    1. 新增真正的 _rule_match_flavor()，实现 explicit_rule 优先
    2. flavor 主流程升级为 explicit -> fuzzy -> fallback -> post_process
    3. flavor 后处理统一进入 _post_process_flavors()
    4. 新增 _apply_flavor_mutex()
    5. 括号清洗从硬编码大正则改为信号识别
    """

    def __init__(self):
        self.supported_categories = self._extract_supported_categories()
        self.category_attributes = self._extract_category_attributes()
        # 新增：二级类目映射和二级类目标准口味
        self.fourth_to_second_category = self._build_fourth_to_second_category_map()

        # V17.32: 构建四级到三级类目映射（用于三级分类兜底）
        self.fourth_to_third_category = self._build_fourth_to_third_category_map()

        # V17.31: 构建非食品类目集合（用于功能映射）
        self.non_food_categories = self._build_non_food_category_set()

        # V17.5: 加载统一的口味池表（四级类目池为主力，二级类目池备用）
        self._load_unified_flavor_pools()

        # V17.31: 加载兜底规则配置
        self.fallback_rules = self._load_fallback_rules_config()

        # V17.37: 加载口味聚合映射配置
        self._load_flavor_aggregate_map()

        # V17.6: 从CSV自动构建互斥规则
        self.mutex_rules_from_csv = self._build_mutex_rules_from_csv()

        self.brand_names = self._build_brand_names()
        self.noise_words = self._build_noise_words()

        self.auto_alias_dict = self._load_auto_alias_dict()

        self.alias_candidate_pool = defaultdict(lambda: {
            'count': 0,
            'max_score': 0.0,
            'examples': []
        })

        # 性能优化：添加缓存
        self._text_normalize_cache = {}
        self._tokens_extract_cache = {}
        self._score_cache = {}  # 评分结果缓存

        # V17.31: 统计数据
        self.stats = {}

        # 弱token：共享后缀，不能单独作为决定性证据
        self.weak_flavor_tokens = {
            '莓味', '果味', '桃味', '橘味', '奶味', '茶味', '子味', '瓜味', '梨味',
            '柚味', '合莓', '莓果', '百香', '双柚', '油柑',
            '风味', '口味', '味道'  # 通用后缀，不能单独决定口味
        }

        # 系列词/产品线词：不是口味，但会污染ngram
        # 只删强污染词，不删上下文词（如乳酸菌、发酵型等）
        self.series_noise_words = {
            'O泡', 'o泡', 'AD钙', '原香', '经典', '青幽爽', 'young', 'YOUNG',
            '餐后轻体', '机智君', '爽心', '排装', 'A钙',
            '乐养', '利乐枕', '花生王'
        }

        # 非水果口味词（用于信号识别）
        # 优先从JSON文件加载，如果没有则使用默认值
        if AUTO_NON_FRUIT_FLAVOR_WORDS is not None:
            self.non_fruit_flavor_words = AUTO_NON_FRUIT_FLAVOR_WORDS
        else:
            # 内置默认值（作为后备）
            self.non_fruit_flavor_words = {
                '巧克力', '可可', '咖啡', '香草', '抹茶', '奶香', '芝士', '榛子',
                '核桃', '花生', '红枣', '芝麻', '豆奶', '燕麦', '谷物'
            }

        # 具体水果词到标准口味的映射（显式规则）
        # 注意：这里不再做互相混指，尽量保持"一对一明确触发"
        self.flavor_explicit_map = self._build_flavor_explicit_map()

        # 类目特定的水果映射表（用于水果罐头等）
        self.category_specific_fruit_map = AUTO_CATEGORY_SPECIFIC_FRUIT_MAP

        # V17.20: 加载完整口味池（从flavor_mapping.csv）
        self.full_flavor_pool = self._load_full_flavor_pool()

        # V17.40: 从dim_item_tag_info加载口味和功能编码
        self._load_tag_code_mapping()

    # -----------------------------
    # 基础配置
    # -----------------------------

    def _load_tag_code_mapping(self):
        """
        V17.40: 从dim_item_tag_info加载口味和功能编码
        统一从同一个文件加载口味和功能编码
        """
        tag_info_file = os.path.join(os.path.dirname(__file__), 'config', 'dim_item_tag_info_20260331_202604080931.csv')

        self.flavor_codes = {}
        self.function_codes = {}

        try:
            with open(tag_info_file, 'r', encoding='utf-8-sig') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    if row.get('tag_level') == '2':
                        if row.get('parent_tag_name') == '口味':
                            self.flavor_codes[row['tag_name']] = row['tag_code']
                        elif row.get('parent_tag_name') == '功能':
                            self.function_codes[row['tag_name']] = row['tag_code']
            print(f"[V17.40] 加载标签编码: {len(self.flavor_codes)} 个口味, {len(self.function_codes)} 个功能")
        except FileNotFoundError:
            print(f"[V17.40] 警告: 标签文件 {tag_info_file} 不存在")

    def _build_fourth_to_second_category_map(self):
        """
        构建四级类目到二级类目的映射
        """
        fourth_to_second = {}
        with open('data/品类定义表_20260409_品类定义详情.csv', 'r', encoding='utf-8-sig') as f:
            reader = csv.DictReader(f)
            for row in reader:
                fourth_category = row.get('四级类目', '').strip()
                second_category = row.get('二级类目', '').strip()
                if fourth_category and second_category:
                    fourth_to_second[fourth_category] = second_category
        return fourth_to_second

    def _build_fourth_to_third_category_map(self):
        """
        V17.32: 构建四级类目到三级类目的映射
        用于三级分类兜底规则
        """
        fourth_to_third = {}
        with open('data/品类定义表_20260409_品类定义详情.csv', 'r', encoding='utf-8-sig') as f:
            reader = csv.DictReader(f)
            for row in reader:
                fourth_category = row.get('四级类目', '').strip()
                third_category = row.get('三级类目', '').strip()
                if fourth_category and third_category:
                    fourth_to_third[fourth_category] = third_category
        return fourth_to_third

    def _build_non_food_category_set(self):
        """
        V17.31: 构建非食品类目集合
        用于判断是否允许功能fuzzy匹配
        """
        non_food = set()
        with open('data/品类定义表_20260409_品类定义详情.csv', 'r', encoding='utf-8-sig') as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get('一级类目', '').strip() == '非食品':
                    category = row.get('四级类目', '').strip()
                    if category:
                        non_food.add(category)
        print(f"构建非食品类目集合: {len(non_food)} 个类目")
        return non_food

    def _load_fallback_rules_config(self):
        """
        V17.31: 从配置文件加载兜底规则

        替代硬编码的 default_original_categories 和特殊口味规则
        """
        config_path = 'config/fallback_rules.json'

        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                config = json.load(f)

            # 构建原味兜底类目集合（简化格式：直接是字符串列表）
            original_flavor_categories = set(config['original_flavor_fallback']['categories'])

            # 加载三级分类兜底配置
            third_level_categories = set(config.get('third_level_fallback', {}).get('categories', []))

            self.fallback_rules_config = config
            self.original_flavor_fallback_categories = original_flavor_categories
            self.third_level_fallback_categories = third_level_categories

            print(f"[V17.31] 加载兜底规则配置: {len(original_flavor_categories)}个四级类目, {len(third_level_categories)}个三级分类")
            return config

        except FileNotFoundError:
            print(f"[V17.31] 警告: 配置文件 {config_path} 不存在，使用内置规则")
            self.fallback_rules_config = None
            self.original_flavor_fallback_categories = set()
            return {}
        except json.JSONDecodeError as e:
            print(f"[V17.31] 错误: 配置文件格式错误 - {e}")
            self.fallback_rules_config = None
            self.original_flavor_fallback_categories = set()
            return {}

    def _load_flavor_aggregate_map(self):
        """
        V17.37: 加载口味聚合映射配置
        将二级池的详细口味映射到四级池的聚合口味
        """
        config_path = 'config/flavor_aggregate_map.json'
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                config = json.load(f)

            # 构建反向映射：source -> aggregate_to
            source_to_aggregate = {}
            for category, mapping in config.get('mappings', {}).items():
                aggregate = mapping.get('aggregate_to')
                sources = mapping.get('sources', [])
                for source in sources:
                    source_to_aggregate[source] = aggregate

            self.flavor_aggregate_map = source_to_aggregate
            print(f"[V17.37] 加载口味聚合映射: {len(source_to_aggregate)} 个映射规则")
            return source_to_aggregate

        except FileNotFoundError:
            print(f"[V17.37] 警告: 聚合映射文件 {config_path} 不存在")
            self.flavor_aggregate_map = {}
            return {}
        except json.JSONDecodeError as e:
            print(f"[V17.37] 错误: 聚合映射文件格式错误 - {e}")
            self.flavor_aggregate_map = {}
            return {}

    def _aggregate_flavor(self, flavor, standard_flavors):
        """
        V17.37: 将口味聚合到四级池的标准口味
        如果聚合后的口味在标准池中，返回聚合口味；否则返回原口味
        """
        if not hasattr(self, 'flavor_aggregate_map'):
            return flavor

        aggregate = self.flavor_aggregate_map.get(flavor)
        if aggregate and aggregate in standard_flavors:
            return aggregate
        return flavor

    def _map_fruit_to_category_flavor(self, product_name, category_name, standard_flavors):
        """
        V17.50: 处理类目特定的水果口味映射
        例如：水果罐头中的"黄桃"应映射为"核果类"，"橘子"应映射为"柑橘类"
        """
        if category_name not in self.category_specific_fruit_map:
            return None

        cleaned = self._normalize_text(product_name)
        fruit_map = self.category_specific_fruit_map[category_name]

        for fruit_name, flavor_category in fruit_map.items():
            if fruit_name in cleaned and flavor_category in standard_flavors:
                return {
                    'standard_attr': flavor_category,
                    'best_token': fruit_name,
                    'matched_trigger': fruit_name,
                    'score': 100.0,
                    'final_score': 100.0,
                    'source': 'category_fruit_mapping'
                }
        return None

    def _build_flavor_explicit_map(self):
        """
        构建口味显式映射表
        优先从外部JSON文件加载，如果不存在则使用内置映射
        """
        # 如果外部映射表已加载，直接使用
        if FLAVOR_EXPLICIT_MAP is not None:
            return FLAVOR_EXPLICIT_MAP
        
        # 内置默认映射表（作为后备）
        return {
            '原味': ['原味'],
            '草莓味': ['草莓味', '草莓'],
            '蓝莓味': ['蓝莓味', '蓝莓'],
            '蔓越莓味': ['蔓越莓味', '蔓越莓'],
            '树莓味': ['树莓味', '树莓'],
            '葡萄味': ['葡萄味', '葡萄', '葡萄汁'],
            '青提味': ['青提味', '青提'],
            '橙味': ['橙味', '橙子味', '橙子', '橙汁', '鲜橙', '鲜橙汁', '橙混合'],
            '香橙味': ['香橙味', '香橙'],
            '柠檬味': ['柠檬味', '柠檬'],
            '青柠味': ['青柠味', '青柠','清柠'],
            '柚子味': ['柚子味', '柚子'],
            '西柚味': ['西柚味', '西柚', '西柚汁'],
            '椰子味': ['椰子味', '椰子', '椰子水', '椰汁'],
            '桃味': ['桃味', '桃汁', '鲜桃', '桃果肉'],
            '水蜜桃味': ['水蜜桃味', '水蜜桃', '蜜桃味', '蜜桃'],
            '白桃味': ['白桃味', '白桃'],
            '黄桃味': ['黄桃味', '黄桃'],
            '芒果味': ['芒果味', '芒果'],
            '荔枝味': ['荔枝味', '荔枝'],
            '苹果味': ['苹果味', '苹果', '苹果汁'],
            '菠萝味': ['菠萝味', '菠萝'],
            '凤梨味': ['凤梨味', '凤梨'],
            '西瓜味': ['西瓜味', '西瓜'],
            '哈密瓜味': ['哈密瓜味', '哈密瓜'],
            '百香果味': ['百香果味', '百香果'],
            '石榴味': ['石榴味', '石榴'],
            '山楂味': ['山楂味', '山楂'],
            '梨味': ['梨味', '梨汁', '鲜梨', '梨果肉'],
            '雪梨味': ['雪梨味', '雪梨'],
            '枇杷味': ['枇杷味', '枇杷'],
            '桑葚味': ['桑葚味', '桑葚'],
            '青梅味': ['青梅味', '青梅'],
            '冰糖雪梨味': ['冰糖雪梨味', '冰糖雪梨'],
            '番茄味': ['番茄味', '番茄', '番茄汁'],
            '柿子味': ['柿子味', '柿子', '柿子汁'],
            '桔味': ['桔味', '桔子', '桔果汁', '桔果', '柑橘'],
            '三柚味': ['三柚味', '三柚', '三柚汁'],
            # 非水果口味
            '巧克力味': ['巧克力味', '巧克力', '可可味'],
            '咖啡味': ['咖啡味', '咖啡'],
            '香草味': ['香草味', '香草'],
            '抹茶味': ['抹茶味', '抹茶'],
            '芝士味': ['芝士味', '芝士', '奶酪味', '奶酪'],
            '核桃味': ['核桃味', '核桃'],
            '花生味': ['花生味', '花生'],
            '红枣味': ['红枣味', '红枣', '大枣味', '大枣'],
            '芝麻味': ['芝麻味', '芝麻', '黑芝麻味', '黑芝麻'],
            '豆奶味': ['豆奶味', '豆奶', '豆乳味', '豆乳'],
            '燕麦味': ['燕麦味', '燕麦'],
            '谷物味': ['谷物味', '谷物'],
            # V17.14: 冷冻饺子口味
            '猪肉香菇': ['猪肉香菇'],
            '香菇猪肉': ['香菇猪肉'],
            '猪肉白菜': ['猪肉白菜'],
            '白菜猪肉': ['白菜猪肉'],
            '猪肉荠菜': ['猪肉荠菜', '荠菜猪肉'],
            '荠菜猪肉': ['荠菜猪肉'],
            '猪肉韭菜': ['猪肉韭菜', '韭菜猪肉'],
            '韭菜猪肉': ['韭菜猪肉'],
            '芹菜猪肉': ['芹菜猪肉'],
            '三鲜': ['三鲜', '三鲜味'],
            '羊肉大葱': ['羊肉大葱'],
            '玉米': ['玉米'],
            '虾皇': ['虾皇', '虾仁'],
            '虾仁胡萝卜': ['虾仁胡萝卜'],
            '鳕鱼海苔': ['鳕鱼海苔']
        }

    def _load_full_flavor_pool(self):
        """
        加载完整口味池（从flavor_mapping.csv）
        V17.20: 用于检查识别到的口味是否在全局标准池中
        """
        full_flavor_pool = set()
        flavor_mapping_path = 'out/attribute_mappings/flavor_mapping.csv'
        
        try:
            with open(flavor_mapping_path, 'r', encoding='utf-8-sig') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    flavor_name = row.get('口味名称', '').strip()
                    if flavor_name:
                        full_flavor_pool.add(flavor_name)
            print(f"加载完整口味池: {len(full_flavor_pool)} 个口味")
        except FileNotFoundError:
            pass
        
        return full_flavor_pool

    def _build_flavor_mutex_groups(self):
        """
        互斥组只放“标准值”，不放 alias
        """
        return [
            {'草莓味', '蓝莓味', '蔓越莓味', '树莓味'},
            {'白桃味', '黄桃味', '水蜜桃味'},
            {'青柠味', '柠檬味'},
            {'橙味', '香橙味'},
            {'菠萝味', '凤梨味'},
            {'葡萄味', '青提味'},
            {'雪梨味', '冰糖雪梨味'},
            # 中国白酒香型互斥组
            {'芝麻香', '浓香', '凤香', '兼香', '米香', '清香', '酱香', '药香'}
        ]

    def _build_source_priority(self):
        return {
            'explicit_rule': 100,
            'rule': 100,
            'fuzzy': 80,
            'mixed_fallback': 20,
            'other_fallback': 10
        }

    def _is_fruit_flavor(self, flavor_name):
        """
        判断是否是水果口味
        基于显式映射表中的 is_fruit 字段
        """
        if FLAVOR_EXPLICIT_MAP:
            flavor_data = FLAVOR_EXPLICIT_MAP.get(flavor_name)
            if isinstance(flavor_data, dict):
                return flavor_data.get('is_fruit', False)
            elif isinstance(flavor_data, list):
                # 旧格式：列表形式，默认为水果口味（因为大多数旧格式的都是水果）
                return True
        
        # 内置默认判断逻辑（作为后备）
        # V17.27: 修复误判问题，'桃'单独匹配会导致"其他"被误判为水果口味
        fruit_keywords = {
            '草莓', '蓝莓', '蔓越莓', '树莓', '芒果', '荔枝', 
            '苹果', '菠萝', '凤梨', '西瓜', '哈密瓜', '百香果', 
            '石榴', '山楂', '梨', '雪梨', '枇杷', '桑葚', '青梅',
            '橙', '柠檬', '青柠', '柚子', '西柚', '椰子', '白桃', '黄桃',
            '水蜜桃', '葡萄', '青提', '香蕉'
            # 注意：单独的'桃'被移除了，因为会导致"其他"等词被误判
        }
        
        for keyword in fruit_keywords:
            if keyword in flavor_name:
                return True
        
        # 特殊处理：只有当'桃'前面有修饰词时才认为是水果口味
        if '桃' in flavor_name:
            # 检查是否是具体的桃类口味
            peach_flavors = ['白桃', '黄桃', '水蜜桃', '桃味', '桃子']
            if any(pf in flavor_name for pf in peach_flavors):
                return True
        
        return False

    def _extract_supported_categories(self):
        supported_categories = set()
        all_categories = set()
        with open('data/品类定义表_20260409_品类定义详情.csv', 'r', encoding='utf-8-sig') as f:

            reader = csv.DictReader(f)
            for row in reader:
                category = row.get('四级类目', '').strip()
                level = row.get('类目等级', '').strip().upper()
                if category:
                    all_categories.add(category)
                    if level in ['S', 'A', 'B', 'C']:
                        supported_categories.add(category)
        print(f"定义表中共有 {len(all_categories)} 个四级类目")
        print(f"提取到 {len(supported_categories)} 个支持的类目(S/A/B/C)")
        return supported_categories

    def _extract_category_attributes(self):
        category_attributes = defaultdict(
            lambda: {'functions': set(), 'flavors': set(), 'has_function': False, 'has_flavor': False}
        )

        # V17.5: 从品类定义表读取功能和口味标记
        # 注意：has_flavor 基于品类定义表的"口味"列判断，具体口味值从口味池文件加载
        with open('data/品类定义表_20260409_品类定义详情.csv', 'r', encoding='utf-8-sig') as f:
            reader = csv.DictReader(f)
            current_category = None
            for row in reader:
                category = row.get('四级类目', '').strip()
                if category:
                    current_category = category

                if not current_category or current_category not in self.supported_categories:
                    continue

                # 读取功能
                function = row.get('功能', '').strip()
                if function:
                    category_attributes[current_category]['functions'].add(function)
                    category_attributes[current_category]['has_function'] = True

                # 读取口味标记（只要有口味列数据，就标记为需要口味映射）
                flavor = row.get('口味', '').strip()
                if flavor:
                    category_attributes[current_category]['has_flavor'] = True

        return category_attributes

    def _load_unified_flavor_pools(self):
        """
        V17.5: 加载统一的口味池表（四级类目标准口味池为主力，三级类目池中间层，二级类目池备用）

        架构：
        1. 从flavor_pool_unified.csv读取（统一文件）
        2. 四级类目池加载到category_attributes
        3. 三级类目池保持独立，按需使用（中间层）
        4. 二级类目池保持独立，按需使用（兜底）
        """
        unified_pool_file = 'data/flavor_pool_unified.csv'

        # 加载统一口味池文件
        category_count = 0
        third_level_pools = {}
        second_level_pools = {}

        try:
            with open(unified_pool_file, 'r', encoding='utf-8-sig') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    pool_level = row.get('pool_level', '').strip()
                    category_name = row.get('category_name', '').strip()
                    flavors_str = row.get('pool_flavors', '').strip()

                    if not category_name or not flavors_str:
                        continue

                    flavors = {f.strip() for f in flavors_str.split(',') if f.strip()}

                    # 四级类目：直接加载到category_attributes
                    if pool_level == '四级' and category_name in self.category_attributes:
                        self.category_attributes[category_name]['flavors'] = flavors
                        if flavors:
                            self.category_attributes[category_name]['has_flavor'] = True
                        category_count += 1

                    # 三级类目：保持独立，按需使用（中间层）
                    elif pool_level == '三级':
                        third_level_pools[category_name] = flavors

                    # 二级类目：保持独立，按需使用，不自动合并
                    elif pool_level == '二级':
                        second_level_pools[category_name] = flavors

        except Exception as e:
            print(f"  警告: 加载{unified_pool_file}失败 - {e}")
            return

        # 保存三级和二级类目口味池供后续按需查找
        self.third_level_flavors = third_level_pools
        self.second_level_flavors = second_level_pools

        print(f"[V17.5] 加载统一口味池:")
        print(f"  四级类目标准池: {category_count} 个类目")
        print(f"  三级类目中间池: {len(third_level_pools)} 个类目（按需使用）")
        print(f"  二级类目备用池: {len(second_level_pools)} 个类目（按需使用）")

    def _build_brand_names(self):
        return {
            '可口可乐', '百事可乐', '雪碧', '芬达', '美汁源', '酷儿',
            '康师傅', '统一', '农夫山泉', '娃哈哈', '脉动', '红牛',
            '王老吉', '加多宝', '和其正', '养乐多', '优益C', '伊利', '蒙牛',
            '光明', '三元', '君乐宝', '新希望', '味全', '达利园', '盼盼',
            '汇源', '农夫果园', '果粒橙', '美年达', '七喜', '冰峰', '北冰洋',
            '健力宝', '激活', '尖叫', '营养快线', '爽歪歪', 'AD钙奶',
            '佳洁士', '高露洁', '黑人', '云南白药', '舒适达', '狮王',
            '中华', '冷酸灵', '两面针', '田七', '六必治', '竹盐', '草珊瑚',
            '舒客', '云南三七', '纳爱斯',
            '三只松鼠', '良品铺子', '百草味', '来伊份', '卫龙', '双汇',
            '雨润', '金锣', '乐事', '上好佳', '可比克', '奥利奥', '趣多多',
            '好丽友', '达能', '旺旺', '徐福记', '德芙', '费列罗',
            '元气森林', '怡泉', '巴黎水', '圣培露',
            '星巴克', '雀巢', 'Costa', '瑞幸', 'Manner',
            '均瑶', '味动力', 'WERDERY', '天友', '蝶泉',
            '怡宝', '恒大冰泉', '百岁山', '昆仑山',
            '今麦郎', '哇哈哈', '银鹭', '椰树', '养元', '露露', 'OATLY',
            '王致和', '海天', '李锦记', '厨邦', '加加', '欣和',
            '德和', '冠生园', '梅林',
            '乌江', '吉香居', '川南', '饭扫光', '下饭菜',
            '老干妈', '老干爹',
            '争强', '争强咖啡王', '张新发', '宾之', '皇爷', '胖哥',  # 槟榔品牌
            '五粮液', '茅台', '泸州老窖', '洋河', '汾酒', '郎酒',
            '青岛', '雪花', '燕京', '哈尔滨', '珠江', '金威',
            '红星', '牛栏山', '古井贡', '洋河大曲',
            '金龙鱼', '福临门', '鲁花', '多力', '刀唛', '鹰唛',
            '金健', '北大荒', '十月稻田', '柴火大院',
            '绿箭', '益达', '炫迈', '曼妥思', '荷氏', '不凡',
            '金丝猴', '阿尔卑斯', '太平', '嘉士利',
            '植贝美', '青蛙', '好来', '牙博士', '皓齿健',
            '芝麻官',  # 水果罐头品牌
        }

    def _build_noise_words(self):
        return {
            '官方', '旗舰店', '家庭装', '组合装', '促销装', '实惠装',
            '超值装', '优惠装', '套装', '礼盒', '礼袋', '礼品',
            '装', '版', '型', '款', '支', '盒', '瓶', '袋', '罐', '杯',
            'ml', 'mL', 'ML', 'g', '克', 'kg', '千克', 'L', '升',
            '全新', '升级', '新一代', '新版',
            '经典', '传统', '正宗', '地道',
            '活力', '能量', '动力',
            '美味', '好吃'
        }

    # -----------------------------
    # alias 读写
    # -----------------------------
    def _load_auto_alias_dict(self):
        alias_dict = defaultdict(set)
        path = 'data/auto_alias_dict.csv'
        try:
            with open(path, 'r', encoding='utf-8-sig') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    category = row.get('四级类别名称', '').strip()
                    attr_type = row.get('属性类型', '').strip()
                    std_value = row.get('标准属性值', '').strip()
                    alias = row.get('别名', '').strip()
                    status = row.get('状态', 'active').strip()

                    if status == 'active' and all([category, attr_type, std_value, alias]):
                        alias_dict[(category, attr_type, std_value)].add(alias)
            total = sum(len(v) for v in alias_dict.values())
            print(f"加载 auto alias: {total} 条")
        except FileNotFoundError:
            print("auto_alias_dict.csv 不存在，将自动创建")
        return alias_dict

    def _save_auto_alias_dict(self):
        path = 'data/auto_alias_dict.csv'
        existing_rows = []
        existing_keys = set()
        alias_to_standards = {}

        try:
            with open(path, 'r', encoding='utf-8-sig') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    key = (
                        row.get('四级类别名称', '').strip(),
                        row.get('属性类型', '').strip(),
                        row.get('标准属性值', '').strip(),
                        row.get('别名', '').strip()
                    )
                    existing_keys.add(key)
                    existing_rows.append(row)

                    cat = row.get('四级类别名称', '').strip()
                    atype = row.get('属性类型', '').strip()
                    alias = row.get('别名', '').strip()
                    std = row.get('标准属性值', '').strip()
                    alias_key = (cat, atype, alias)
                    if alias_key not in alias_to_standards:
                        alias_to_standards[alias_key] = set()
                    alias_to_standards[alias_key].add(std)
        except FileNotFoundError:
            pass

        for (category, attr_type, std_value, alias), info in self.alias_candidate_pool.items():
            if self._should_promote_alias(category, attr_type, std_value, alias, info):
                key = (category, attr_type, std_value, alias)
                if key not in existing_keys:
                    alias_key = (category, attr_type, alias)
                    if alias_key in alias_to_standards:
                        if std_value not in alias_to_standards[alias_key]:
                            print(
                                f"跳过歧义alias: {category}/{attr_type}/{alias} -> "
                                f"已存在 {alias_to_standards[alias_key]}, 新 {std_value}"
                            )
                            continue
                    else:
                        alias_to_standards[alias_key] = set()

                    existing_rows.append({
                        '四级类别名称': category,
                        '属性类型': attr_type,
                        '标准属性值': std_value,
                        '别名': alias,
                        'count': info['count'],
                        'score': round(info['max_score'], 2),
                        '状态': 'active'
                    })
                    existing_keys.add(key)
                    alias_to_standards[alias_key].add(std_value)
                    self.auto_alias_dict[(category, attr_type, std_value)].add(alias)
                    print(
                        f"自动沉淀 alias: {category}/{attr_type}/{std_value} <- {alias} "
                        f"(count={info['count']}, score={info['max_score']:.1f})"
                    )

        with open(path, 'w', newline='', encoding='utf-8-sig') as f:
            fieldnames = ['四级类别名称', '属性类型', '标准属性值', '别名', 'count', 'score', '状态']
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(existing_rows)

        total = sum(len(v) for v in self.auto_alias_dict.values())
        print(f"已保存 auto alias: {total} 条")

    def _should_promote_alias(self, category, attr_type, std_value, alias, info):
        if not self._is_valid_alias(alias, category, attr_type):
            return False

        if attr_type == 'function':
            min_count = 4
            min_score = 90
        else:
            min_count = 3
            min_score = 82

        return info['count'] >= min_count and info['max_score'] >= min_score

    def _save_results_as_json(self, results, json_output_file):
        """将处理结果保存为JSON格式"""
        import os

        json_results = {}
        for r in results:
            product_name = r['商品名称']
            barcode = r.get('商品条码', '')
            entry = {'barcode': barcode}

            flavor_pairs = []
            for i in range(1, 4):
                flavor_key = f'flavor_{i}'
                id_key = f'flavor_id_{i}'
                flavor_value = r.get(flavor_key)
                id_value = r.get(id_key)
                if flavor_value:
                    pair = {f'flavor_{i}': flavor_value}
                    if id_value:
                        pair['flavor_id'] = id_value
                    flavor_pairs.append(pair)

            function_pairs = []
            for i in range(1, 4):
                function_key = f'function_{i}'
                id_key = f'function_id_{i}'
                function_value = r.get(function_key)
                id_value = r.get(id_key)
                if function_value:
                    pair = {f'function_{i}': function_value}
                    if id_value:
                        pair['function_id'] = id_value
                    function_pairs.append(pair)

            if flavor_pairs:
                entry['flavor'] = flavor_pairs
            else:
                entry['flavor'] = []

            if function_pairs:
                entry['function'] = function_pairs
            else:
                entry['function'] = []

            if entry:
                json_results[product_name] = entry

        with open(json_output_file, 'w', encoding='utf-8') as f:
            json.dump(json_results, f, ensure_ascii=False, indent=2)

        print(f"JSON结果已保存到: {json_output_file}")

    # -----------------------------
    # 文本清洗
    # -----------------------------
    def _remove_brand_names(self, text):
        if not text:
            return text
        for brand in sorted(self.brand_names, key=len, reverse=True):
            text = text.replace(brand, '')
        return text

    def _remove_series_words(self, text):
        """删除系列词/产品线词，避免污染口味识别"""
        if not text:
            return text
        for word in sorted(self.series_noise_words, key=len, reverse=True):
            text = text.replace(word, '')
        return text

    def _looks_like_flavor_fragment(self, text):
        """
        判断括号内片段是否像口味信息
        """
        if not text:
            return False

        text = re.sub(r'\s+', '', str(text))
        if not text:
            return False

        if any(x in text for x in ['原味', '风味', '口味', '味道']):
            return True

        # 对大多数商品标题，出现“味”基本就是口味线索
        if '味' in text:
            return True

        fruit_words = [
            '荔枝', '青提', '青柚', '柚子', '西柚', '柠檬', '青柠', '芒果', '草莓', '蓝莓',
            '蜜桃', '白桃', '黄桃', '水蜜桃', '葡萄', '苹果', '橙子', '香橙', '橘子',
            '西瓜', '哈密瓜', '椰子', '椰子水', '菠萝', '凤梨', '百香果', '石榴',
            '青梅', '山楂', '雪梨', '枇杷', '桑葚', '蔓越莓', '树莓', '莓果', '石榴'
        ]
        if any(w in text for w in fruit_words):
            return True

        # 非水果口味词
        if any(w in text for w in self.non_fruit_flavor_words):
            return True

        return False

    def _normalize_text(self, text):
        if not text:
            return ''

        # 检查缓存
        if text in self._text_normalize_cache:
            return self._text_normalize_cache[text]

        text = str(text)
        text = self._remove_brand_names(text)
        text = self._remove_series_words(text)  # 删除系列词

        # 删除规格
        text = re.sub(
            r'\d+(\.\d+)?\s?(ml|mL|ML|g|kg|克|千克|L|升|支|盒|瓶|袋|罐|片)',
            ' ',
            text,
            flags=re.IGNORECASE
        )

        # 括号：若内容像口味则保留内容；否则删除整个括号
        def replace_bracket(m):
            inner = re.sub(r'\s+', '', m.group(1))
            if self._looks_like_flavor_fragment(inner):
                return inner
            return ' '

        text = re.sub(r'[（\(]([^）\)]*?)[）\)]', replace_bracket, text)

        text = re.sub(r'\s+', '', text).strip()
        
        # 缓存结果
        self._text_normalize_cache[text] = text
        return text

    def _filter_category_keywords(self, text, category_name):
        """
        根据类目过滤掉品类特征词
        例如：对于"低温酸奶"类目，过滤掉"酸奶"、"牛奶"
        对于"椰子乳"类目，过滤掉"椰子"、"椰汁"、"椰奶"
        """
        if not text or not category_name:
            return text

        category_keywords = {
            '酸奶': ['酸奶', '酸牛奶'],
            '常温酸奶': ['酸奶', '酸牛奶'],
            '低温酸奶': ['酸奶', '酸牛奶'],
            '低温纯牛奶': ['牛奶', '纯牛奶', '牛乳', '纯牛乳'],
            '常温纯牛奶': ['牛奶', '纯牛奶', '牛乳', '纯牛乳'],
            '风味牛奶': ['牛奶'],
            # V17.5: 植物蛋白饮料 - 过滤原料词，避免被误识别为口味
            '椰子乳': ['椰子', '椰汁', '椰奶', '椰乳'],
            '核桃乳': ['核桃', '核桃露', '核桃乳'],
            '花生乳': ['花生', '花生露'],
            '杏仁乳': ['杏仁', '杏仁露'],
            '燕麦乳': ['燕麦', '燕麦露'],
            '豆奶': ['豆奶', '豆乳', '大豆'],
            # 食用油类目：过滤掉品类特征词，避免被误识别为口味
            '玉米油': ['玉米'],
            '花生油': ['花生'],
            '菜籽油': ['菜籽'],
            '大豆油': ['大豆'],
            '葵花籽油': ['葵花籽', '葵花'],
            # 葡萄酒类目：过滤品类特征词，避免误识别
            '葡萄酒/红酒': ['葡萄酒', '红葡萄酒', '白葡萄酒', '红酒'],
        }

        if category_name in category_keywords:
            for keyword in category_keywords[category_name]:
                text = text.replace(keyword, '')

        return text

    # -----------------------------
    # 基础匹配能力
    # -----------------------------
    def _extract_meaningful_tokens(self, text):
        """
        提取有意义的tokens（改进版ngram）

        相比原版ngram的改进：
        1. 按分隔符切分（处理混合口味）
        2. 提取连续语义块（不是固定长度）
        3. 过滤噪音字符
        4. 按长度排序，长token优先
        """
        # 检查缓存
        if text in self._tokens_extract_cache:
            return self._tokens_extract_cache[text]
        
        tokens = set()

        # 策略1: 按常见分隔符切分
        # 例如: "草莓+蓝莓" -> ['草莓', '蓝莓']
        separators = ['+', '&', '、', '，', ',', '和', '加']
        for sep in separators:
            if sep in text:
                parts = text.split(sep)
                for part in parts:
                    if len(part) >= 2:
                        tokens.add(part.strip())

        # 策略2: 提取连续的"语义块"
        # 例如: "草莓果肉酸奶" -> ['草莓', '果肉', '酸奶', '草莓果肉', '果肉酸奶']
        n = len(text)
        for i in range(n):
            for length in [2, 3, 4, 5]:  # 支持更长的token
                if i + length <= n:
                    token = text[i:i+length]
                    # 过滤明显的噪音
                    if self._is_valid_alias(token, None, None):
                        tokens.add(token)

        # 策略3: 提取标准口味相关的token
        # 如果包含"味"、"风味"等词，尝试提取前面的核心词
        flavor_suffixes = ['味', '风味', '口味', '味道']
        for suffix in flavor_suffixes:
            if suffix in text:
                # 找到所有以这些词结尾的token
                idx = 0
                while True:
                    pos = text.find(suffix, idx)
                    if pos == -1:
                        break
                    # 提取前面的2-4个字作为核心
                    start = max(0, pos - 4)
                    core = text[start:pos]
                    if len(core) >= 2:
                        tokens.add(core + suffix)
                        tokens.add(core)  # 同时添加不带后缀的版本
                    idx = pos + 1

        # 按长度降序排序，优先匹配长token
        result = sorted(tokens, key=len, reverse=True)
        
        # 缓存结果
        self._tokens_extract_cache[text] = result
        return result

    def _enhanced_token_match(self, std_value, std_aliases, cleaned_text, attr_type):
        """
        增强的token匹配（多策略）

        V17.5 修复：提高阈值，减少过度匹配
        - direct_keyword: 必须完全匹配才给100分
        - token_match: 阈值从82提高到90，且要求高相似度
        - 对于"牛肉味"→"红烧牛肉味"这种情况，要求至少95分才能匹配

        返回: (best_score, best_token, match_method)
        """
        # 策略1: 直接关键词匹配（最高优先级，但要求精确）
        # 修复：不是简单的 in 检查，而是要求完全匹配或前后有分隔符
        for alias in std_aliases:
            # 完全匹配
            if cleaned_text == alias:
                return 100.0, alias, 'direct_keyword'

            # 检查alias是否作为独立词汇出现（前后是文本边界或分隔符）
            # 避免把"牛肉味"匹配到"红烧牛肉味"
            try:
                alias_pattern = re.escape(alias)
                if re.search(r'(^|[+&、，,])' + alias_pattern + r'($|[+&、，,])', cleaned_text):
                    return 100.0, alias, 'direct_keyword'
            except:
                # 如果正则表达式编译失败，跳过此检查
                pass

            # 对于短词（<=3字），检查是否完整包含
            # 避免"牛肉"匹配到"红烧牛肉"
            if len(alias) <= 3 and alias in cleaned_text:
                # 检查前后是否有其他字符（避免部分匹配）
                idx = cleaned_text.find(alias)
                if idx == 0 or not cleaned_text[idx - 1].isalpha():
                    end_idx = idx + len(alias)
                    if end_idx >= len(cleaned_text) or not cleaned_text[end_idx].isalpha():
                        return 95.0, alias, 'direct_keyword'

        # 策略2: Partial matching（处理子串匹配）
        if RAPIDFUZZ_AVAILABLE:
            partial_score = fuzz.partial_ratio(std_value, cleaned_text)
            if partial_score >= 95:  # 提高阈值：88 → 95
                return float(partial_score), None, 'partial_match'

        # 策略3: 智能token匹配（严格阈值）
        tokens = self._extract_meaningful_tokens(cleaned_text)
        best_token_score = 0.0
        best_token = None

        for token in tokens:
            # 过滤过短的token
            if len(token) < 2:
                continue

            for alias in std_aliases:
                # token长度不能比alias短太多
                if len(token) < len(alias) * 0.6:  # 提高比例要求：0.5 → 0.6
                    continue

                score = self._score_text(token, alias)

                # 弱token降权
                if attr_type == 'flavor' and token in self.weak_flavor_tokens:
                    score *= 0.6

                # 关键修复：如果std_value比token长很多（比如"红烧牛肉味" vs "牛肉"）
                # 则要求非常高的分数，否则不匹配
                if len(std_value) > len(token) + 2:
                    if score < 96:  # 非常严格的阈值
                        continue

                # 对于短token（<=3字），要求更高分数
                if len(token) <= 3 and score < 94:
                    continue

                if score > best_token_score:
                    best_token_score = score
                    best_token = token

        # 提高最终阈值
        if best_token_score >= 92:  # 提高阈值：90 → 92
            return best_token_score, best_token, 'token_match'

        # 策略4: Full string fallback（最低优先级，几乎不使用）
        full_score = self._score_text(cleaned_text, std_value)
        if full_score >= 90:  # 至少90分才接受
            return full_score, None, 'full_string'
        else:
            return 0.0, None, 'no_match'  # 明确返回不匹配

    def _is_valid_alias(self, word, category_name=None, attr_type=None):
        if not word or len(word) < 2:
            return False
        if re.fullmatch(r'[a-zA-Z]+', word):
            return False
        if re.search(r'\d', word):
            return False
        if word in self.noise_words:
            return False
        if word in self.brand_names:
            return False

        dangerous_words = {'混合', '健康', '活力', '经典'}
        if word in dangerous_words:
            return False

        if attr_type == 'function' and word in {'清爽', '清新'}:
            return False

        return True

    def _score_text(self, a, b):
        if not a or not b:
            return 0.0
        if RAPIDFUZZ_AVAILABLE:
            return float(fuzz.WRatio(a, b))
        return SequenceMatcher(None, a, b).ratio() * 100

    def _build_std_aliases(self, category_name, attr_type, std_value):
        aliases = {std_value}

        if std_value == '原味':
            aliases.add('原味')
            auto_aliases = self.auto_alias_dict.get((category_name, attr_type, std_value), set())
            aliases.update(auto_aliases)
            return {a for a in aliases if a and len(a) >= 2}

        suffixes = ['味', '风味', '口味']
        core = std_value
        for suffix in suffixes:
            if std_value.endswith(suffix):
                core = std_value[:-len(suffix)]
                aliases.add(core)

        if core:
            aliases.add(core)
            if attr_type == 'flavor':
                aliases.add(core + '味')
                aliases.add(core + '风味')
                aliases.add(core + '口味')

        auto_aliases = self.auto_alias_dict.get((category_name, attr_type, std_value), set())
        aliases.update(auto_aliases)

        return {a for a in aliases if a and len(a) >= 2}

    def _score_one_standard_attr(self, product_name, category_name, attr_type, std_value):
        """
        增强的属性匹配评分

        V17.4升级：使用多策略匹配（直接关键词 -> partial -> token -> full）
        相比旧版本的改进：
        1. 直接关键词匹配优先（最快最准）
        2. 智能token提取（处理混合口味）
        3. 更准确的分数计算
        """
        # 缓存检查：避免重复计算相同的（商品名+类目+标准属性）组合
        # V17.51: 修复缓存键缺少category_name导致的跨类目污染问题
        cache_key = (product_name, category_name, std_value, attr_type)
        if cache_key in self._score_cache:
            return self._score_cache[cache_key]

        cleaned = self._normalize_text(product_name)
        if not cleaned:
            return None

        std_aliases = self._build_std_aliases(category_name, attr_type, std_value)

        # 使用增强的多策略匹配
        best_token_score, best_token, match_method = self._enhanced_token_match(
            std_value, std_aliases, cleaned, attr_type
        )

        # 特殊排除逻辑：防止某些词被错误匹配
        # 例如："葡萄糖酸"不应该匹配"葡萄味"
        if attr_type == 'flavor' and std_value == '葡萄味':
            if '葡萄糖酸' in cleaned or '葡锌' in cleaned:
                best_token_score *= 0.1
            # V17.5: 葡萄酒类目不匹配"葡萄味"
            if category_name == '葡萄酒/红酒':
                best_token_score *= 0.1

        # V17.5: 瓜子类目不匹配"西瓜味"（西瓜子是品类，不是西瓜味）
        if attr_type == 'flavor' and std_value == '西瓜味' and category_name == '瓜子':
            if '西瓜子' in cleaned or '西瓜' in cleaned:
                best_token_score *= 0.1

        # 计算最终分数
        if match_method == 'direct_keyword':
            # 直接关键词匹配，给满分
            final_score = 100.0
        elif match_method == 'partial_match':
            # Partial匹配，给较高权重
            final_score = best_token_score
        elif match_method == 'token_match':
            # Token匹配，给高权重
            if attr_type == 'flavor':
                final_score = 0.95 * best_token_score
            else:
                final_score = 0.9 * best_token_score
        else:  # full_string
            # Full string fallback，给较低权重
            if attr_type == 'flavor':
                final_score = 0.7 * best_token_score
            else:
                final_score = 0.6 * best_token_score

        result = {
            'standard_attr': std_value,
            'best_token': best_token,
            'best_alias': std_value,  # 简化：不再单独记录alias
            'best_token_score': best_token_score,
            'full_score': best_token_score,  # 简化：与token_score相同
            'final_score': final_score,
            'source': match_method  # 记录匹配方法，便于调试
        }

        # 存入缓存
        self._score_cache[cache_key] = result
        return result

    def _fuzzy_match_attr_type(self, product_name, category_name, attr_type, threshold=88, top_k=3, exclude=None):
        if exclude is None:
            exclude = set()

        attrs = self.category_attributes.get(category_name, {})
        candidates = list(attrs.get('functions', set()) if attr_type == 'function' else attrs.get('flavors', set()))
        filtered_candidates = candidates  # 默认使用candidates，在flavor时会过滤

        # 优化：只对显式规则未匹配到的情况使用完整口味池
        # 并且对完整口味池进行预过滤，只保留可能相关的口味
        if attr_type == 'flavor':
            # V17.5: 应用类目排除规则到四级类目口味池
            category_excluded_words_local = {
                '椰子乳': ['椰子', '椰汁', '椰奶', '植物', '天然'],
                '核桃乳': ['核桃', '核桃露', '植物', '天然'],
                '水果罐头': ['黄桃', '杨梅', '橘子', '菠萝', '荔枝', '龙眼', '苹果', '梨', '山楂',
                            '海棠', '桃', '白桃', '杏', '李子', '樱桃', '草莓', '蓝莓', '桑葚', '树莓',
                            '橙子', '柚子', '柠檬', '芒果', '木瓜', '香蕉', '西瓜', '哈密瓜',
                            '甜瓜', '葡萄', '猕猴桃', '什锦水果', '什锦', '水果'],
                '番茄酱': ['番茄', '番茄沙司', '茄汁'],
                '酸奶': ['酸奶', '酸奶味', '牛奶', '奶'],
                '常温酸奶': ['酸奶', '酸奶味', '牛奶', '奶'],
                '低温酸奶': ['酸奶', '酸奶味', '牛奶', '奶'],
                '低温纯牛奶': ['牛奶', '奶'],
                '常温纯牛奶': ['牛奶', '奶'],
                '发酵型含乳饮料': ['乳酸菌', '发酵', '含乳'],
                '配制型含乳饮料': ['乳酸菌', '含乳'],
                '植物饮料': ['草本', '植物', '植物风味'],
                '运动饮料': ['含气', '电解质', '电解质味', '低糖', '无糖', '等渗', '低渗', '高渗'],
                '碳酸饮料': ['含气', '气泡', '碳酸'],
                '花生乳': ['花生', '花生味'],
                '杏仁乳': ['杏仁', '杏仁味'],
                '燕麦乳': ['燕麦', '燕麦味', '燕麦谷物'],
                '豆奶': ['大豆', '大豆味', '豆奶', '豆乳'],
                '复合型植物蛋白饮料': ['植物', '蛋白'],
                '麦片': ['坚果', '燕麦', '谷物', '麦片味'],
            }

            # 过滤四级类目口味池中被排除的口味
            if category_name in category_excluded_words_local:
                exclude_words = category_excluded_words_local[category_name]
                filtered_candidates = [
                    f for f in candidates
                    if not any(ew in f for ew in exclude_words)
                ]
            else:
                filtered_candidates = candidates

            # 先尝试类目标准池匹配
            std_pool_results = self._score_candidates(product_name, category_name, attr_type, filtered_candidates, exclude)
            
            # 过滤满足阈值的结果
            std_pool_filtered = [r for r in std_pool_results if r['final_score'] >= threshold]
            
            if len(std_pool_filtered) >= top_k:
                # 如果标准池已经有足够的高分匹配，直接返回
                return std_pool_filtered[:top_k]
            
            # V17.38: 逐级映射 - 四级池不足时，先尝试三级池，再尝试二级池
            # Step 1: 尝试三级类目池（中间层）
            third_category = self.fourth_to_third_category.get(category_name)
            if third_category and hasattr(self, 'third_level_flavors'):
                third_level_flavor_pool = self.third_level_flavors.get(third_category, set())
                
                # 应用类目排除规则到三级类目口味池
                category_excluded_words = {
                    '椰子乳': ['椰子', '椰汁', '椰奶', '植物', '天然'],
                    '核桃乳': ['核桃', '核桃露', '植物', '天然'],
                    '水果罐头': ['黄桃', '杨梅', '橘子', '菠萝', '荔枝', '龙眼', '苹果', '梨', '山楂',
                                '海棠', '桃', '白桃', '杏', '李子', '樱桃', '草莓', '蓝莓', '桑葚', '树莓',
                                '橙子', '柚子', '柠檬', '芒果', '木瓜', '香蕉', '西瓜', '哈密瓜',
                                '甜瓜', '葡萄', '猕猴桃', '什锦水果', '什锦', '水果'],
                    '番茄酱': ['番茄', '番茄沙司', '茄汁'],
                    '酸奶': ['酸奶', '酸奶味', '牛奶', '奶'],
                    '常温酸奶': ['酸奶', '酸奶味', '牛奶', '奶'],
                    '低温酸奶': ['酸奶', '酸奶味', '牛奶', '奶'],
                    '低温纯牛奶': ['牛奶', '奶'],
                    '常温纯牛奶': ['牛奶', '奶'],
                    '发酵型含乳饮料': ['乳酸菌', '发酵', '含乳'],
                    '配制型含乳饮料': ['乳酸菌', '含乳'],
                    '植物饮料': ['草本', '植物', '植物风味'],
                    '运动饮料': ['含气', '电解质', '电解质味', '低糖', '无糖', '等渗', '低渗', '高渗'],
                    '碳酸饮料': ['含气', '气泡', '碳酸'],
                    '花生乳': ['花生', '花生味'],
                    '杏仁乳': ['杏仁', '杏仁味'],
                    '燕麦乳': ['燕麦', '燕麦味', '燕麦谷物'],
                    '豆奶': ['大豆', '大豆味', '豆奶', '豆乳'],
                    '复合型植物蛋白饮料': ['植物', '蛋白'],
                    '麦片': ['坚果', '燕麦', '谷物', '麦片味'],
                }
                
                # 过滤掉包含排除词的三级类目口味
                if category_name in category_excluded_words:
                    exclude_words = category_excluded_words[category_name]
                    third_level_flavor_pool = {
                        f for f in third_level_flavor_pool 
                        if not any(ew in f for ew in exclude_words)
                    }
                
                # 单字口味不允许从三级池映射
                third_level_candidates = self._filter_relevant_flavors(
                    product_name, category_name,
                    third_level_flavor_pool - set(candidates) - exclude
                )
                # 过滤掉单字口味
                third_level_candidates = [
                    c for c in third_level_candidates
                    if len(c.replace('味', '').replace('型', '')) > 1
                ]
                # V17.5: 使用filtered_candidates代替candidates，避免被排除的口味被重新引入
                filtered_candidates.extend(third_level_candidates)
                
                # 重新评分，检查三级池是否满足要求
                if filtered_candidates:
                    third_pool_results = self._score_candidates(product_name, category_name, attr_type, filtered_candidates, exclude)
                    third_pool_filtered = [r for r in third_pool_results if r['final_score'] >= threshold]
                    if len(third_pool_filtered) >= top_k:
                        return third_pool_filtered[:top_k]
            
            # Step 2: 三级池仍不足时，尝试二级类目池（兜底）
            # V17.41: 休闲零食二级类目不跨二级池映射，避免跨类目误匹配
            second_category = self.fourth_to_second_category.get(category_name)
            if second_category and second_category != "休闲零食":
                second_level_flavor_pool = self.second_level_flavors.get(second_category, set())
                
                # V17.39: 从二级池排除通用描述词（如"水果味"），避免与具体水果口味冲突
                # 这些词在二级池中是通用描述，不应该作为具体口味映射
                generic_flavors = {'水果味', '果味', '混合味', '综合味'}
                second_level_flavor_pool = second_level_flavor_pool - generic_flavors
                
                # 应用相同的类目排除规则
                if category_name in category_excluded_words:
                    exclude_words = category_excluded_words[category_name]
                    second_level_flavor_pool = {
                        f for f in second_level_flavor_pool 
                        if not any(ew in f for ew in exclude_words)
                    }
                
                # 单字口味不允许从二级池映射
                second_level_candidates = self._filter_relevant_flavors(
                    product_name, category_name,
                    second_level_flavor_pool - set(candidates) - exclude
                )
                # 过滤掉单字口味
                second_level_candidates = [
                    c for c in second_level_candidates
                    if len(c.replace('味', '').replace('型', '')) > 1
                ]
                filtered_candidates.extend(second_level_candidates)

        fallback_options = {'其他', '其他口味', '其他功能'}
        filtered_candidates = [c for c in filtered_candidates if c not in exclude and c not in fallback_options]

        if not filtered_candidates:
            return []

        # 优化：使用缓存避免重复计算
        scored = self._score_candidates(product_name, category_name, attr_type, filtered_candidates, exclude)

        # 添加排除规则检查
        if attr_type == 'flavor':
            cleaned = self._normalize_text(product_name)
            # 类目特定的排除：某些词在特定类目中是品类特征，不是口味
            category_excluded_words = {
                '椰子乳': ['椰子', '椰汁', '椰奶', '植物', '天然'],
                '核桃乳': ['核桃', '核桃露', '植物', '天然'],
                '水果罐头': ['黄桃', '杨梅', '橘子', '菠萝', '荔枝', '龙眼', '苹果', '梨', '山楂',
                            '海棠', '桃', '白桃', '杏', '李子', '樱桃', '草莓', '蓝莓', '桑葚', '树莓',
                            '橙子', '柚子', '柠檬', '芒果', '木瓜', '香蕉', '西瓜', '哈密瓜',
                            '甜瓜', '葡萄', '猕猴桃', '什锦水果', '什锦', '水果'],
                '番茄酱': ['番茄', '番茄沙司', '茄汁'],
                '酸奶': ['酸奶', '酸奶味', '牛奶', '奶'],
                '常温酸奶': ['酸奶', '酸奶味', '牛奶', '奶'],
                '低温酸奶': ['酸奶', '酸奶味', '牛奶', '奶'],
                '低温纯牛奶': ['牛奶', '奶'],
                '常温纯牛奶': ['牛奶', '奶'],
                '发酵型含乳饮料': ['乳酸菌', '发酵', '含乳'],
                '配制型含乳饮料': ['乳酸菌', '含乳'],
                '植物饮料': ['草本', '植物', '植物风味'],
                '运动饮料': ['含气', '电解质', '电解质味', '低糖', '无糖', '等渗', '低渗', '高渗'],
                '碳酸饮料': ['含气', '气泡', '碳酸'],
                # V17.27: 植物蛋白饮料排除原料名称作为口味
                '花生乳': ['花生', '花生味'],
                '杏仁乳': ['杏仁', '杏仁味'],
                '燕麦乳': ['燕麦', '燕麦味', '燕麦谷物'],
                '豆奶': ['大豆', '大豆味', '豆奶', '豆乳'],
                '复合型植物蛋白饮料': ['植物', '蛋白'],
                # V17.5: 冲泡奶茶排除奶茶作为口味（奶茶是产品类型，不是口味）
                '冲泡奶茶': ['奶茶', '牛乳奶茶'],
            }
            
            # V17.5: 添加瓜子类目排除
            category_excluded_words['瓜子'] = ['西瓜子', '西瓜']

            # 检查是否有排除的口味需要移除
            if category_name and category_name in category_excluded_words:
                exclude_words = category_excluded_words[category_name]
                # 检查是否有任何排除词在清洗后的商品名中
                has_excluded_word = any(exclude_word in cleaned for exclude_word in exclude_words)
                if has_excluded_word:
                    # 如果有排除词，需要过滤掉包含这些词的口味
                    filtered_scored = []
                    for row in scored:
                        flavor_name = row['standard_attr']
                        # 检查口味名是否包含排除词
                        should_exclude = False
                        for exclude_word in exclude_words:
                            if exclude_word in flavor_name:
                                should_exclude = True
                                break
                        # V17.5: 对于瓜子类目，如果marker是排除词，也跳过
                        if category_name == '瓜子' and row.get('best_token') in exclude_words:
                            should_exclude = True
                        if not should_exclude:
                            filtered_scored.append(row)
                    scored = filtered_scored

        outputs = []
        used = set()
        for row in scored:
            if row['final_score'] >= threshold and row['standard_attr'] not in used:
                outputs.append(row)
                used.add(row['standard_attr'])
            if len(outputs) >= top_k:
                break

        return outputs

    def _score_candidates(self, product_name, category_name, attr_type, candidates, exclude):
        """批量评分候选属性，支持缓存"""
        scored = []
        for std_attr in candidates:
            if std_attr in exclude:
                continue
            result = self._score_one_standard_attr(product_name, category_name, attr_type, std_attr)
            if result:
                scored.append(result)
        scored.sort(key=lambda x: x['final_score'], reverse=True)
        return scored

    def _filter_relevant_flavors(self, product_name, category_name, flavor_pool):
        """过滤完整口味池中可能相关的口味，减少计算量"""
        cleaned = self._normalize_text(product_name)
        # 过滤掉品类特征词
        cleaned = self._filter_category_keywords(cleaned, category_name)
        if not cleaned:
            return []
        
        # 提取商品名称中的关键词
        tokens = self._extract_meaningful_tokens(cleaned)
        token_set = set(tokens)
        
        # 只保留与商品名称有共同关键词的口味
        relevant_flavors = []
        
        # 对于特殊类目，使用更严格的过滤
        strict_categories = {'中国白酒', '方便面', '即饮奶茶'}
        is_strict = category_name in strict_categories
        
        # 优化：先检查简单包含关系（更快）
        for flavor in flavor_pool:
            # 优化：先检查直接包含（最简单快速）
            if flavor in cleaned:
                relevant_flavors.append(flavor)
                continue
            
            # 优化：检查商品名称中的关键词是否在口味名称中
            # 这比完整的token提取更高效
            has_keyword_match = False
            for token in tokens:
                # 对于严格类目，要求更长的匹配
                min_token_length = 3 if is_strict else 2
                if token in flavor and len(token) >= min_token_length:
                    # 额外检查：确保token不是单字或过于宽泛
                    if len(token) >= min_token_length:
                        # 对于严格类目，避免"香"、"酒"等宽泛词匹配
                        if is_strict and token in {'香', '酒', '白', '干'}:
                            continue
                        has_keyword_match = True
                        break
            
            if has_keyword_match:
                relevant_flavors.append(flavor)
        
        # 限制数量，严格类目更少，一般类目更多
        max_candidates = 20 if is_strict else 50
        return relevant_flavors[:max_candidates]

    # -----------------------------
    # 显式规则
    # -----------------------------
    def _rule_match_function(self, product_name, category_name, standard_functions):
        if not standard_functions:
            return []

        cleaned = self._normalize_text(product_name)
        if not cleaned:
            return []

        matched = []
        for std_func in sorted(standard_functions):
            triggers = FUNCTION_RULES.get(std_func, [])
            for trigger in sorted(triggers, key=len, reverse=True):
                if trigger in cleaned:
                    matched.append({
                        'standard_attr': std_func,
                        'matched_trigger': trigger,
                        'score': 100.0,
                        'final_score': 100.0,
                        'source': 'rule'
                    })
                    break

        return matched

    def _rule_match_flavor(self, product_name, category_name, standard_flavors):
        """
        真正的 flavor explicit_rule 层
        只对类目标准池内的标准口味做显式匹配
        """
        cleaned = self._normalize_text(product_name)
        if not cleaned or not standard_flavors:
            return []

        # V17.27: 类目特定的排除词（用于"其他"口味的排除）
        category_excluded_words = {
            '椰子乳': ['椰'],
            '核桃乳': ['核桃'],
            '花生乳': ['花生'],
            '杏仁乳': ['杏仁'],
            '燕麦乳': ['燕麦'],
            '豆奶': ['豆'],
            # V17.5: 玉米油类目排除"玉米"作为口味（玉米是原料，不是口味）
            '玉米油': ['玉米'],
            # V17.5: 瓜子类目排除"西瓜"作为口味（西瓜子是品类，不是西瓜味）
            '瓜子': ['西瓜子', '西瓜','南瓜','青瓜'],
            # V17.5: 水果罐头类目排除"白桃"等作为"其他"口味（这些应该映射到核果类）
            '水果罐头': ['黄桃', '杨梅', '橘子', '菠萝', '荔枝', '龙眼', '苹果', '梨', '山楂',
                        '海棠', '桃', '白桃', '杏', '李子', '樱桃', '草莓', '蓝莓', '桑葚', '树莓',
                        '橙子', '柚子', '柠檬', '芒果', '木瓜', '香蕉', '西瓜', '哈密瓜',
                        '甜瓜', '葡萄', '猕猴桃', '什锦水果', '什锦', '水果'],
            # V17.5: 麦片类目排除"坚果"、"燕麦"作为口味（这些是成分，不是口味）
            '麦片': ['坚果', '燕麦'],
        }

        # V17.5: 葡萄酒类目 - 排除非标准口味
        # 葡萄酒的标准口味是：干型、甜型、桃红、白葡萄酒、红葡萄酒
        # 需要排除：葡萄味、水果味等通用口味
        wine_excluded_flavors = {'葡萄味', '水果味', '果味'}

        matched = []
        for std_flavor in sorted(standard_flavors):
            # V17.27: 适配新的字典格式
            flavor_data = self.flavor_explicit_map.get(std_flavor, {})
            if isinstance(flavor_data, dict):
                markers = flavor_data.get('markers', [])
            else:
                markers = flavor_data if isinstance(flavor_data, list) else []
            if not markers:
                continue

            for marker in sorted(set(markers), key=len, reverse=True):
                if marker and marker in cleaned:
                    # V17.27: 对于"其他"口味，检查 marker 是否是排除词
                    if std_flavor == '其他' and category_name in category_excluded_words:
                        if marker in category_excluded_words[category_name]:
                            continue  # 跳过这个匹配

                    # V17.27: 对于植物蛋白饮料，排除原料名称作为口味
                    # 例如：核桃乳中的"核桃"、花生乳中的"花生"
                    if category_name in category_excluded_words:
                        excluded_words = category_excluded_words[category_name]
                        # 检查口味名是否是原料口味（如"花生味"、"核桃味"）
                        if any(ew in std_flavor for ew in excluded_words):
                            # 检查 marker 是否是原料名称
                            if marker in excluded_words:
                                continue  # 跳过这个匹配

                    # V17.5: 对于特定类目，排除某些词作为任何口味的marker
                    # 例如：瓜子中的"西瓜"（西瓜子是品类，不是西瓜味）
                    if category_name in category_excluded_words:
                        excluded_words = category_excluded_words[category_name]
                        if marker in excluded_words:
                            continue  # 跳过这个匹配

                    # V17.5: 对于葡萄酒类目，排除非标准口味
                    if category_name == '葡萄酒/红酒' and std_flavor in wine_excluded_flavors:
                        continue  # 跳过这个匹配

                    matched.append({
                        'standard_attr': std_flavor,
                        'best_token': marker,
                        'matched_trigger': marker,
                        'score': 100.0,
                        'final_score': 100.0,
                        'source': 'explicit_rule'
                    })
                    break

        return matched

    # -----------------------------
    # alias 学习
    # -----------------------------
    def _collect_alias_candidate(self, category_name, attr_type, standard_attr, best_token, final_score, product_name):
        if not best_token:
            return
        if not self._is_valid_alias(best_token, category_name, attr_type):
            return
        if final_score < (90 if attr_type == 'function' else 86):
            return
        if best_token in self.weak_flavor_tokens:
            return

        base_score = self._score_text(best_token, standard_attr)
        if base_score < 65:
            return

        key = (category_name, attr_type, standard_attr, best_token)
        info = self.alias_candidate_pool[key]
        info['count'] += 1
        info['max_score'] = max(info['max_score'], final_score)
        if len(info['examples']) < 3:
            info['examples'].append(product_name)

    # -----------------------------
    # flavor 辅助逻辑
    # -----------------------------
    def _get_other_flavor_label(self, category_name):
        attrs = self.category_attributes.get(category_name, {})
        flavors = attrs.get('flavors', set())

        if '其他口味' in flavors:
            return '其他口味'
        if '其他' in flavors:
            return '其他'
        return None

    def _has_non_standard_flavor(self, cleaned, category_name=None, standard_flavors=None):
        """
        检测是否存在非标准口味信号
        从 flavor_explicit_map 中动态提取所有口味，与类目的标准口味池对比
        如果口味词在商品名中，但不在类目的标准口味池中，则为非标准口味
        返回: (是否有非标准口味, 最佳匹配词)
        """
        # 特殊排除：某些词虽然匹配口味marker，但实际不是口味
        # 例如："葡萄糖酸"是营养强化剂，不是"葡萄"口味
        # 例如："乳酸菌"是菌种，不是"酸"口味
        excluded_contexts = {
            '葡萄': ['葡萄糖酸', '葡锌', '葡萄糖'],
            '酸': ['乳酸菌', '益生菌', '乳酸菌饮料', '乳酸菌饮品'],
        }

        # 类目特定的排除：某些词在特定类目中是品类特征，不是口味
        # 例如："椰子"在椰子乳类目中是原料/品类特征，不是"水果味"
        # 例如："黄桃"在水果罐头中是原料，不是口味（口味是糖水原味、蜂蜜口味等）
        category_excluded_words = {
            '椰子乳': ['椰子', '椰汁', '椰奶', '植物', '天然'],
            '核桃乳': ['核桃', '核桃露', '植物', '天然'],
            '水果罐头': ['黄桃', '杨梅', '橘子', '菠萝', '荔枝', '龙眼', '苹果', '梨', '山楂',
                        '海棠', '桃', '白桃', '杏', '李子', '樱桃', '草莓', '蓝莓', '桑葚', '树莓',
                        '橙子', '柚子', '柠檬', '芒果', '木瓜', '香蕉', '西瓜', '哈密瓜',
                        '甜瓜', '葡萄', '猕猴桃', '什锦水果', '什锦', '水果'],
            '番茄酱': ['番茄', '番茄沙司', '茄汁'],  # V17.12: 番茄酱中的这些词是品类特征，不是口味，默认经典原味；但草莓风味应映射为其他
            # V17.20: 酸奶相关类目排除"酸奶"作为口味
            '酸奶': ['酸奶', '酸奶味'],
            '常温酸奶': ['酸奶', '酸奶味'],
            '低温酸奶': ['酸奶', '酸奶味'],
            # V17.25: 植物饮料排除"植物"、"草本"作为口味
            '植物饮料': ['植物', '草本', '植物风味'],
            # V17.25: 运动饮料排除"含气"、"电解质"作为口味
            '运动饮料': ['含气', '电解质', '电解质味', '低糖', '无糖'],
            # V17.25: 碳酸饮料排除"含气"、"气泡"作为口味
            '碳酸饮料': ['含气', '气泡', '碳酸'],
            # V17.27: 植物蛋白饮料排除原料名称作为口味
            '花生乳': ['花生', '花生味'],
            '椰子乳': ['椰子', '椰子味', '椰奶', '椰汁'],
            '核桃乳': ['核桃', '核桃味', '核桃露'],
            '杏仁乳': ['杏仁', '杏仁味'],
            '燕麦乳': ['燕麦', '燕麦味', '燕麦谷物'],
            '豆奶': ['大豆', '大豆味', '豆奶', '豆乳'],
            '复合型植物蛋白饮料': ['植物', '蛋白'],
            # V17.45: 水类商品排除品类特征词作为口味
            '天然水': ['天然', '矿泉水', '矿泉', '泉水', '山泉水'],
            '矿泉水': ['矿泉', '矿泉水', '天然', '泉水', '山泉水'],
            '矿物质水': ['矿物质', '矿泉', '矿泉水', '天然'],
            '其他包装水': ['天然', '矿泉', '矿泉水', '泉水', '山泉水'],
            # V17.5: 葡萄酒类目排除"葡萄"作为口味（葡萄是原料，不是口味）
            '葡萄酒/红酒': ['葡萄'],
            # V17.5: 玉米油类目排除"玉米"作为口味（玉米是原料，不是口味）
            '玉米油': ['玉米'],
            # V17.5: 瓜子类目排除"西瓜"作为口味（西瓜子是品类，不是西瓜味）
            '瓜子': ['西瓜子', '西瓜', '南瓜'],
            # V17.5: 麦片类目排除"燕麦"、"坚果"作为口味（这些是成分，不是口味）
            '麦片': ['燕麦', '坚果', '谷物', '麦片', '牛奶'],
        }

        # 从 flavor_explicit_map 中遍历所有口味定义
        # flavor_explicit_map 格式: {标准口味: [触发词列表]}
        # 先收集所有匹配，然后选择最合适的（非"其他"优先）
        all_matches = []
        for flavor_name, markers in self.flavor_explicit_map.items():
            # 支持旧格式（list）和新格式（dict with 'markers' key）
            if isinstance(markers, dict):
                marker_list = markers.get('markers', [])
            elif isinstance(markers, list):
                marker_list = markers
            else:
                continue

            # 检查是否有 marker 匹配
            for marker in marker_list:
                if marker and marker in cleaned:
                    # 特殊处理："其他"口味的marker很宽泛，需要排除规则检查
                    if flavor_name == '其他' and category_name and category_name in category_excluded_words:
                        # 如果"其他"的marker是排除词，跳过这个匹配
                        # 注意：要检查marker是否是排除词的一部分，或者排除词是否是marker的一部分
                        exclude_words = category_excluded_words[category_name]
                        should_skip = False
                        for exclude_word in exclude_words:
                            if marker in exclude_word or exclude_word in marker:
                                should_skip = True
                                break
                        if should_skip:
                            continue
                    # 检查是否需要排除（上下文检查）
                    should_exclude = False
                    for core_word, exclude_list in excluded_contexts.items():
                        if marker == core_word or marker in core_word:
                            # 如果匹配了核心词，检查是否在排除上下文中
                            for exclude_word in exclude_list:
                                if exclude_word in cleaned:
                                    should_exclude = True
                                    break
                        if should_exclude:
                            break

                    if should_exclude:
                        continue

                    # 检查类目特定的排除
                    if category_name and category_name in category_excluded_words:
                        if marker in category_excluded_words[category_name]:
                            continue

                    # 收集所有有效的匹配
                    all_matches.append((flavor_name, marker))
        
        # 按优先级排序：非"其他"优先，marker更长的优先
        def match_priority(item):
            flavor_name, marker = item
            # "其他"优先级最低
            if flavor_name == '其他':
                return (0, len(marker))
            else:
                return (1, len(marker))
        
        all_matches.sort(key=match_priority, reverse=True)
        
        # 处理排序后的匹配
        for flavor_name, marker in all_matches:
            # V17.26: 检查是否在类目标准口味池或二级类目口味池中
            # 不再使用完整口味池，只使用类目相关的口味池
            is_valid_flavor = False
            
            # 1. 检查是否在类目标准口味池中
            if standard_flavors and flavor_name in standard_flavors:
                is_valid_flavor = True
            else:
                # 2. 检查是否在二级类目口味池中
                second_category = self.fourth_to_second_category.get(category_name)
                if second_category:
                    second_level_flavors = self.second_level_flavors.get(second_category, set())
                    if flavor_name in second_level_flavors:
                        is_valid_flavor = True
            
            if is_valid_flavor:
                # 在类目相关口味池中 → 是有效口味，不是"非标准口味"
                # 但是需要额外检查：是否是品类名称的变体
                # 例如："核桃味"对于"核桃乳"类目，"椰子味"对于"椰子乳"类目
                # 这些应该被当作品类特征，而不是额外口味，应该走原味兜底
                if category_name and category_name in category_excluded_words:
                    # 检查口味名是否是排除词的变体
                    for exclude_word in category_excluded_words[category_name]:
                        if exclude_word in flavor_name:
                            # 如果口味名包含品类特征词，这不是真正的额外口味
                            # 返回None，让调用方走默认原味路线
                            return True, None
                
                return False, flavor_name
            else:
                # 不在类目相关口味池中 → 是非标准口味
                return True, marker

        # V17.14: 特殊处理冷冻饺子的馅料
        # 冷冻饺子中，"猪肉"、"牛肉"等馅料名称应被识别为口味信号，映射为"其他"
        if category_name == '冷冻饺子':
            # 先检查是否包含完整的口味组合
            complete_combinations = ['猪肉香菇', '香菇猪肉', '猪肉白菜', '白菜猪肉',
                                   '猪肉荠菜', '荠菜猪肉', '猪肉韭菜', '韭菜猪肉',
                                   '芹菜猪肉', '三鲜', '三鲜味', '羊肉大葱',
                                   '虾皇', '虾仁', '虾仁胡萝卜', '鳕鱼海苔']
            has_complete = any(combo in cleaned for combo in complete_combinations)

            if not has_complete:
                # 如果没有完整口味组合，检查是否有馅料名称
                filling_keywords = ['猪肉', '牛肉', '羊肉', '鸡肉', '鸭肉', '虾肉', '鱼肉',
                                  '白菜', '韭菜', '芹菜', '荠菜', '香菇', '玉米', '豆角',
                                  '鲜肉', '虾仁', '虾皇']
                for filling in filling_keywords:
                    if filling in cleaned:
                        return True, filling

        return False, None

    def _prefer_specific_flavor_by_marker(self, cleaned, flavor_results):
        """
        具体水果词优先于共享后缀
        如果标题中有“草莓”，优先保留“草莓味”，压掉其他莓类
        如果标题中有具体水果词但没有“原味”，不保留“原味”
        如果标题中有“综合/混合口味”，只保留显式命中的口味
        """
        # V17.45: 水类商品即使flavor_results为空，也需要执行兜底映射逻辑
        # 所以不能在这里直接返回空列表
        if not flavor_results:
            return []

        explicit_hits = set()
        for std_attr, markers in self.flavor_explicit_map.items():
            marker_list = markers.get('markers', []) if isinstance(markers, dict) else (markers if isinstance(markers, list) else [])
            if any(marker in cleaned for marker in marker_list):
                explicit_hits.add(std_attr)

        has_specific_fruit = bool(explicit_hits - {'原味'})
        has_original = '原味' in explicit_hits

        has_mixed_indicator = '综合口味' in cleaned or '混合口味' in cleaned
        mutex_groups = self._build_flavor_mutex_groups()

        outputs = []
        for item in flavor_results:
            std_attr = item['standard_attr']

            if has_mixed_indicator and std_attr not in explicit_hits:
                continue

            if std_attr == '原味' and has_specific_fruit and not has_original:
                continue

            skip = False
            for group in mutex_groups:
                if std_attr in group:
                    if std_attr not in explicit_hits and len(explicit_hits & group) > 0:
                        skip = True
                        break
            if not skip:
                outputs.append(item)

        return outputs

    def _limit_weak_token_outputs(self, cleaned, flavor_results):
        """
        如果只有弱token没有具体水果词，
        同组最多保留1个，避免输出多个相近具体莓/桃/果口味
        """
        if not flavor_results:
            return []

        has_specific = False
        for _, markers in self.flavor_explicit_map.items():
            marker_list = markers.get('markers', []) if isinstance(markers, dict) else (markers if isinstance(markers, list) else [])
            if any(marker in cleaned for marker in marker_list):
                has_specific = True
                break

        if has_specific:
            return flavor_results

        weak_groups = {
            '莓': {'草莓味', '蓝莓味', '蔓越莓味', '树莓味'},
            '桃': {'白桃味', '黄桃味', '水蜜桃味'},
            '果': {'芒果味', '苹果味', '百香果味'}
        }

        outputs = list(flavor_results)
        for _, group in weak_groups.items():
            group_hits = [item for item in outputs if item['standard_attr'] in group]
            if len(group_hits) > 1:
                best = max(group_hits, key=lambda x: x.get('final_score', 0))
                outputs = [item for item in outputs if item['standard_attr'] not in group or item == best]

        return outputs

    def _apply_flavor_mutex(self, flavor_results):
        """
        互斥组裁决：
        - 优先 explicit_rule / rule
        - 再按 final_score
        """
        if not flavor_results:
            return []

        source_priority = self._build_source_priority()
        outputs = list(flavor_results)

        for group in self._build_flavor_mutex_groups():
            group_hits = [x for x in outputs if x['standard_attr'] in group]
            if len(group_hits) <= 1:
                continue

            best = sorted(
                group_hits,
                key=lambda x: (
                    source_priority.get(x.get('source', ''), 0),
                    x.get('final_score', 0)
                ),
                reverse=True
            )[0]

            outputs = [x for x in outputs if x['standard_attr'] not in group or x is best]

        return outputs

    def _remove_flavor_variants(self, flavor_results):
        """
        V17.31: 动态去除口味变体重复

        识别并去除"词根"vs"词根+味/香/型"的重复
        例如：香辣 vs 香辣味，原味 vs 原味型

        规则：
        1. 提取每个口味的词根（去掉"味"、"香"、"型"、"风味"、"口味"等后缀）
        2. 如果多个口味共享同一个词根，只保留分数最高的
        3. 保留带"味"后缀的优先（作为标准形式）
        """
        if not flavor_results:
            return []

        from collections import defaultdict

        # 构建词根到候选口味的映射
        root_map = defaultdict(list)
        for item in flavor_results:
            flavor = item['standard_attr']
            # 提取词根（去掉常见的后缀）
            # V17.32: 修复bug - 后缀按长度从长到短排列，避免误匹配
            # 例如："海鲜风味" 应该去掉 "风味" 得到 "海鲜"，而不是去掉 "味" 得到 "海鲜风"
            root = flavor
            for suffix in ['风味', '口味', '香型', '辣味', '酸味', '甜味', '咸味', '味', '香', '型']:
                if root.endswith(suffix):
                    root = root[:-len(suffix)]
                    break
            root = root.strip()

            # 只处理词根长度>=2的情况
            if len(root) >= 2:
                root_map[root].append(item)
            else:
                # 词根太短，单独处理
                root_map[flavor].append(item)

        # 每个词根只保留最合适的候选
        filtered = []
        removed_count = 0

        for root, items in root_map.items():
            if len(items) > 1:
                # 有变体，选择最佳的
                # V17.32: 优先选择更具体的（名称更长的），而不是仅仅依赖分数
                # 例如："海鲜风味" > "海鲜味" > "海鲜"
                best = max(items, key=lambda x: (
                    len(x['standard_attr']),  # 长度优先（更具体的口味）
                    x.get('final_score', 0),  # 同长度时选高分
                    x.get('source', '') == 'explicit_rule'  # 同分时优先explicit_rule
                ))
                filtered.append(best)
                removed_count += len(items) - 1
            else:
                # 没有变体，直接保留
                filtered.extend(items)

        # 记录去重统计
        if removed_count > 0:
            self.stats['variant_dedup_count'] = self.stats.get('variant_dedup_count', 0) + removed_count

        return filtered

    def _protect_original_flavor(self, cleaned, flavor_results, category_name):
        """
        乳饮类目原味保护：
        如果标题中显式出现"原味"，且没有其他显式具体口味，则强制保留原味
        """
        protected_categories = {
            '发酵型含乳饮料',
            '配制型含乳饮料',
            '含乳饮料',
            '发酵乳',
            '调味乳',
            '乳酸菌饮料',
            '复合型植物蛋白饮料',
            # V17.5: 添加植物蛋白饮料具体类目
            '椰子乳',
            '核桃乳',
            '花生乳',
            '杏仁乳',
            '燕麦乳',
            '豆奶',
        }

        if category_name not in protected_categories:
            return flavor_results

        if '原味' not in cleaned:
            return flavor_results

        # 检查是否有显式命中的原味
        explicit_original = [
            x for x in flavor_results
            if x['standard_attr'] == '原味' and x.get('source') == 'explicit_rule'
        ]

        # 检查是否有显式命中的具体口味
        explicit_specific = [
            x for x in flavor_results
            if x['standard_attr'] != '原味' and x.get('source') == 'explicit_rule'
        ]

        # 如果没有显式具体口味，但有显式原味，只保留原味
        if explicit_original and not explicit_specific:
            return [x for x in flavor_results if x['standard_attr'] == '原味']

        return flavor_results

    def _deduplicate_attr_results(self, results):
        """
        同一个标准值可能同时来自 explicit_rule 和 fuzzy
        保留优先级更高 / 分数更高的那条
        """
        if not results:
            return []

        source_priority = self._build_source_priority()
        best_map = {}

        for item in results:
            key = item['standard_attr']
            if key not in best_map:
                best_map[key] = item
                continue

            old = best_map[key]
            old_key = (source_priority.get(old.get('source', ''), 0), old.get('final_score', 0))
            new_key = (source_priority.get(item.get('source', ''), 0), item.get('final_score', 0))
            if new_key > old_key:
                best_map[key] = item

        return list(best_map.values())

    def _build_mutex_rules_from_csv(self):
        """
        从 flavor_alias_manual.csv 自动构建互斥规则

        策略：
        1. 按 comment 分组口味
        2. 泛指词（果味、茶味）与具体口味互斥
        3. 复合口味（复合XX味、混合XX味、什锦XX味）单独处理，不参与泛指互斥

        Returns:
            dict: {short_flavor: [long_flavor1, long_flavor2, ...]}
        """
        import csv
        import os

        csv_file = 'data/flavor_alias_manual.csv'
        if not os.path.exists(csv_file):
            return {}

        # 按 comment 分组
        grouped = {}
        with open(csv_file, 'r', encoding='utf-8-sig') as f:
            reader = csv.DictReader(f)
            for row in reader:
                standard = row.get('standard_flavor', '')
                comment = row.get('comment', '')

                # 空值检查
                if not standard:
                    continue
                standard = standard.strip() if standard else ''
                comment = comment.strip() if comment else ''

                if not standard or standard.startswith('#'):
                    continue

                if comment not in grouped:
                    grouped[comment] = []
                grouped[comment].append(standard)

        # 构建互斥规则
        mutex_rules = {}

        # 辅助函数：判断是否为复合口味
        def is_composite_flavor(flavor):
            """检查是否为复合/混合口味"""
            composite_keywords = ['复合', '混合', '什锦', '多味', '缤纷']
            return any(kw in flavor for kw in composite_keywords)

        # 1. "果味" 与所有具体水果口味互斥（排除复合口味）
        fruit_flavors = grouped.get('水果口味', []) + grouped.get('热带水果口味', [])
        specific_fruits = [f for f in fruit_flavors if not is_composite_flavor(f) and f != '果味']
        if specific_fruits:
            mutex_rules['果味'] = specific_fruits

        # 2. "茶味" 与所有具体茶类口味互斥
        tea_flavors = grouped.get('茶类口味', [])
        specific_teas = [f for f in tea_flavors if f != '茶味']
        if specific_teas:
            mutex_rules['茶味'] = specific_teas

        # 3. 其他泛指词的处理
        # "坚果味" 与具体坚果口味互斥
        nut_flavors = grouped.get('坚果口味', [])
        specific_nuts = [f for f in nut_flavors if not is_composite_flavor(f) and f != '坚果味']
        if specific_nuts:
            mutex_rules['坚果味'] = specific_nuts

        # 4. 复合口味与具体口味互斥
        # 如果有"复合水果味"，应该与所有具体水果互斥
        all_fruit_flavors = grouped.get('水果口味', []) + grouped.get('热带水果口味', [])
        composite_fruits = [f for f in all_fruit_flavors if is_composite_flavor(f)]
        for composite in composite_fruits:
            specific = [f for f in all_fruit_flavors if not is_composite_flavor(f) and f != composite]
            if specific:
                mutex_rules[composite] = specific

        return mutex_rules

    def _merge_similar_flavors(self, flavor_results):
        """
        合并相似的口味：如果两个口味名称存在包含关系，保留更具体的（更长的）

        规则来源：
        1. 硬编码的特殊规则（如白酒香型、特殊包含关系）
        2. CSV自动生成的规则（果味、茶味等泛指词与具体口味互斥）
        3. 自动检测：通用包含关系检测（如"蛋黄"和"蛋黄味"）

        例如："凤梨味"和"梨味" → 保留"凤梨味"
        例如："红枣味"和"枣味" → 保留"红枣味"
        例如："蛋黄味"和"蛋黄" → 保留"蛋黄味"
        """
        if len(flavor_results) <= 1:
            return flavor_results

        # 硬编码的特殊规则（处理非标准命名和特殊包含关系）
        hardcoded_rules = {
            '梨味': ['凤梨味', '雪梨味', '冰糖雪梨味', '菠萝味', '凤梨'],
            '枣味': ['红枣味', '黑枣味'],
            '清香': ['清香型', '清香味'],
            '浓香': ['浓香型', '浓香味'],
            '酱香': ['酱香型', '酱香味', '酱香 / 酱香型'],
            '绿茶': ['绿茶味'],
            '抹茶': ['抹茶味'],
            '红茶': ['红茶味'],
            '酸辣': ['酸辣味'],
            '孜然': ['孜然味'],
            '蜂蜜': ['蜂蜜味'],
            '藤椒': ['藤椒味'],
            '酸味': ['酸辣味'],
            '草本': ['草本味'],
            '果香': ['果香型'],
            '橙味': ['橙子味'],
            '巧克力': ['巧克力味'],
            '蜜桃味': ['水蜜桃味'],
            '芝麻味': ['黑芝麻味', '白芝麻味'],
            '咖啡': ['咖啡味'],
            # V17.33: 咖啡类型互斥规则（正确写法）
            '咖啡味': ['美式', '拿铁', '卡布奇诺', '摩卡', '白咖啡', '意式', '速溶'],
            '咖啡': ['美式', '拿铁', '卡布奇诺', '摩卡', '白咖啡', '意式', '速溶'],
            '红豆': ['红豆味'],
            # V17.34: 口味互斥规则（修复泛指+具体重复）
            '酸味': ['酸菜味', '老坛酸菜味', '酸辣味', '酸辣', '香酸味', '微酸', '微酸味'],
            '辣味': ['麻辣', '麻辣味', '香辣', '香辣味', '酸辣', '酸辣味',
                     '微辣', '微辣味', '中辣', '中辣味', '特辣', '特辣味',
                     '香辣味', '麻辣味', '野山椒味'],
            '甜味': [ '微甜', '微甜味', '甜蜜', '甜蜜味', '甘甜', '甘甜味'],
            '麻辣': ['麻辣味'],  # 变体去重
            '香辣': ['香辣味'],  # 变体去重
            # V17.32: 甜度相关互斥规则（已合并到上面）
            # V17.40: 黑椒/黑胡椒互斥规则 - 保留标准名称"黑胡椒味"，排除简称"黑椒味"和"黑椒"
            '黑椒': ['黑胡椒味'],
            '黑椒味': ['黑胡椒味'],
            # V17.42: 复合口味互斥规则 - 具体复合口味优先于基础口味
            '麻辣味': ['麻辣小龙虾味', '麻辣香锅味', '麻辣火锅味', '麻辣牛肉味', '麻辣鸡味'],
            '香辣味': ['香辣小龙虾味', '香辣蟹味', '香辣牛肉味', '香辣鸡味'],
            # V17.43: 混合果味互斥规则 - 混合果味优先于具体水果味
            '苹果味': ['混合果味', '复合水果味', '什锦水果味'],
            '山楂味': ['混合果味', '复合水果味', '什锦水果味'],
            '黄桃味': ['混合果味', '复合水果味', '什锦水果味'],
            '水蜜桃味': ['混合果味', '复合水果味', '什锦水果味'],
            '橙子味': ['混合果味', '复合水果味', '什锦水果味'],
            '葡萄味': ['混合果味', '复合水果味', '什锦水果味'],
            '草莓味': ['混合果味', '复合水果味', '什锦水果味'],
            '芒果味': ['混合果味', '复合水果味', '什锦水果味'],
            '菠萝味': ['混合果味', '复合水果味', '什锦水果味'],
            '荔枝味': ['混合果味', '复合水果味', '什锦水果味'],
            '蓝莓味': ['混合果味', '复合水果味', '什锦水果味'],
        }

        # 合并硬编码规则和CSV自动生成的规则
        # CSV规则优先（更全面），硬编码规则作为补充
        similar_groups = {**hardcoded_rules, **self.mutex_rules_from_csv}

        flavor_names = [x['standard_attr'] for x in flavor_results]
        to_remove = set()

        for short_flavor, long_flavors in similar_groups.items():
            if short_flavor not in flavor_names:
                continue

            # 检查是否有更长的口味存在
            for long_flavor in long_flavors:
                if long_flavor in flavor_names:
                    # 存在更长的口味，删除短的
                    to_remove.add(short_flavor)
                    break

        # V17.35: 自动检测通用包含关系
        # 例如："蛋黄"和"蛋黄味" → 保留"蛋黄味"
        # 例如："芝士"和"芝士味" → 保留"芝士味"
        for i, flavor1 in enumerate(flavor_names):
            if flavor1 in to_remove:
                continue
            for j, flavor2 in enumerate(flavor_names):
                if i == j or flavor2 in to_remove:
                    continue
                # 检查包含关系（排除相同字符串）
                if flavor1 != flavor2:
                    # flavor1 是 flavor2 的子串，且 flavor2 更长 → 删除 flavor1
                    if flavor1 in flavor2 and len(flavor1) < len(flavor2):
                        to_remove.add(flavor1)
                    # flavor2 是 flavor1 的子串，且 flavor1 更长 → 删除 flavor2
                    elif flavor2 in flavor1 and len(flavor2) < len(flavor1):
                        to_remove.add(flavor2)

        if to_remove:
            flavor_results = [x for x in flavor_results if x['standard_attr'] not in to_remove]

        return flavor_results


    def _fallback_flavor(self, cleaned, category_name, standard_flavors):
        """
        统一口味兜底逻辑
        V17.5: 整合所有分散的fallback检查到统一方法
        """
        third_category = self.fourth_to_third_category.get(category_name, '')
        is_fallback_category = (
            self.fallback_rules_config and
            (category_name in self.original_flavor_fallback_categories or
             third_category in self.third_level_fallback_categories)
        )

        # 1. 检查是否有明显口味信号
        has_non_std, flavor_word = self._has_non_standard_flavor(cleaned, category_name, standard_flavors)

        # 2. 如果检测到有效口味（在完整池中），直接映射
        if not has_non_std and flavor_word:
            return [{
                'standard_attr': flavor_word,
                'best_token': flavor_word,
                'final_score': 0,
                'source': 'valid_flavor_from_full_pool'
            }]

        # 3. 有明显口味但不在完整口味池 → 尝试映射到标准口味
        if has_non_std and flavor_word:
            all_pools = set(standard_flavors)
            third_category = self.fourth_to_third_category.get(category_name)
            if third_category:
                all_pools.update(self.third_level_flavors.get(third_category, set()))

            mapped_flavor = None
            for std_flavor, markers_data in self.flavor_explicit_map.items():
                marker_list = markers_data.get('markers', []) if isinstance(markers_data, dict) else (markers_data if isinstance(markers_data, list) else [])
                if flavor_word in marker_list and std_flavor in all_pools:
                    mapped_flavor = std_flavor
                    break

            if mapped_flavor:
                return [{
                    'standard_attr': mapped_flavor,
                    'best_token': flavor_word,
                    'final_score': 0,
                    'source': 'marker_to_standard_flavor'
                }]

            other_label = self._get_other_flavor_label(category_name)
            if other_label:
                return [{
                    'standard_attr': other_label,
                    'best_token': flavor_word,
                    'final_score': 0,
                    'source': 'non_standard_fallback'
                }]
            elif is_fallback_category:
                default_flavor = self._get_default_flavor_from_pool(category_name)
                if default_flavor:
                    return [{
                        'standard_attr': default_flavor,
                        'best_token': flavor_word,
                        'final_score': 90,
                        'source': 'default_original_when_no_other'
                    }]
            return []

        # 4. 检测到品类特征词但不是真正口味 → 原味兜底
        if has_non_std and not flavor_word:
            if is_fallback_category:
                default_flavor = self._get_default_flavor_from_pool(category_name)
                if default_flavor:
                    return [{
                        'standard_attr': default_flavor,
                        'best_token': None,
                        'final_score': 90,
                        'source': 'category_feature_to_default_original'
                    }]
            return []

        # 5. 无明显口味，检查特殊兜底规则
        # 5.1 特殊口味映射为"其他"
        special_other_indicators = ['椰子水', '西柚汁', '西柚', '莓果汁', '混合果汁', '石榴汁']
        if category_name not in ['椰子乳', '核桃乳', '花生乳', '杏仁乳', '燕麦乳', '豆奶']:
            if any(indicator in cleaned for indicator in special_other_indicators):
                other_label = self._get_other_flavor_label(category_name)
                if other_label:
                    return [{
                        'standard_attr': other_label,
                        'best_token': '',
                        'final_score': 0,
                        'source': 'special_other_fallback'
                    }]

        # 5.2 mixed fallback
        mixed_indicators = ['综合口味', '混合口味', '混合果汁', '复合果汁']
        if any(indicator in cleaned for indicator in mixed_indicators):
            other_label = self._get_other_flavor_label(category_name)
            if other_label:
                return [{
                    'standard_attr': other_label,
                    'best_token': '',
                    'final_score': 0,
                    'source': 'mixed_fallback'
                }]

        # 6. 类目特定兜底
        # 6.1 糖果类兜底
        if category_name == '硬糖':
            if any(word in cleaned for word in ['薄荷', '清凉', '润喉', '桉叶', '冰爽']):
                return [{'standard_attr': '薄荷味', 'best_token': '薄荷', 'final_score': 0, 'source': 'candy_default'}]
            elif '陈皮' in cleaned:
                return [{'standard_attr': '陈皮味', 'best_token': '陈皮', 'final_score': 0, 'source': 'candy_default'}]
            elif any(word in cleaned for word in ['奶', '乳', '奶油']):
                return [{'standard_attr': '牛奶味', 'best_token': '奶', 'final_score': 0, 'source': 'candy_default'}]

        elif category_name == '软糖':
            if any(word in cleaned for word in ['话梅', '梅子', '乌梅', '酸梅']):
                return [{'standard_attr': '话梅味', 'best_token': '话梅', 'final_score': 0, 'source': 'candy_default'}]
            elif '可乐' in cleaned:
                return [{'standard_attr': '可乐味', 'best_token': '可乐', 'final_score': 0, 'source': 'candy_default'}]
            elif any(word in cleaned for word in ['果汁', '水果', '果味']):
                return [{'standard_attr': '水果味', 'best_token': '果汁', 'final_score': 0, 'source': 'candy_default'}]
            elif '乳酸菌' in cleaned:
                return [{'standard_attr': '酸奶味', 'best_token': '乳酸菌', 'final_score': 0, 'source': 'candy_default'}]
            return [{'standard_attr': '其他', 'best_token': '软糖', 'final_score': 0, 'source': 'candy_default'}]

        elif category_name == '胶基糖果':
            if any(word in cleaned for word in ['薄荷', '清凉', '清新', '劲爽']):
                return [{'standard_attr': '薄荷味', 'best_token': '薄荷', 'final_score': 0, 'source': 'candy_default'}]
            elif any(word in cleaned for word in ['水果', '果味', '草莓', '葡萄', '西瓜', '菠萝', '芒果']):
                return [{'standard_attr': '水果味', 'best_token': '水果', 'final_score': 0, 'source': 'candy_default'}]
            else:
                return [{'standard_attr': '其他', 'best_token': '口香糖', 'final_score': 0, 'source': 'candy_default'}]

        # 6.2 能量饮料兜底
        if category_name == '能量饮料':
            non_standard_signals = ['人参', '王浆', '枸杞', '玛咖', '瓜拉纳', '肌醇']
            if any(signal in cleaned for signal in non_standard_signals):
                return [{'standard_attr': '其他', 'best_token': '能量饮料', 'final_score': 0, 'source': 'energy_drink_default'}]
            else:
                return [{'standard_attr': '原味', 'best_token': '能量饮料', 'final_score': 0, 'source': 'energy_drink_default'}]

        # 6.3 即饮奶茶兜底 - 已删除

        # 7. 特殊口味规则（从配置文件读取）
        if self.fallback_rules_config and 'special_flavor_rules' in self.fallback_rules_config:
            for rule in self.fallback_rules_config['special_flavor_rules']['rules']:
                if rule['category'] == category_name:
                    for mapping in rule['mappings']:
                        if any(kw in cleaned for kw in mapping['keywords']):
                            return [{
                                'standard_attr': mapping['flavor'],
                                'best_token': mapping['flavor'],
                                'final_score': 0,
                                'source': 'special_flavor'
                            }]
                    if 'default' in rule:
                        return [{
                            'standard_attr': rule['default'],
                            'best_token': rule['default'],
                            'final_score': 0,
                            'source': 'special_flavor'
                        }]
                    break

        # 8. 一般口味信号检测
        if self._has_flavor_signal(cleaned, category_name):
            if not is_fallback_category:
                other_label = self._get_other_flavor_label(category_name)
                if other_label:
                    return [{
                        'standard_attr': other_label,
                        'best_token': '',
                        'final_score': 0,
                        'source': 'other_fallback'
                    }]

        # 9. 兜底类目默认原味
        if is_fallback_category:
            default_flavor = self._get_default_flavor_from_pool(category_name)
            if default_flavor:
                return [{
                    'standard_attr': default_flavor,
                    'best_token': '',
                    'final_score': 90,
                    'source': 'default_original'
                }]

        return []

    def _get_default_flavor_from_pool(self, category_name):
        """从类目标准口味池中获取默认原味"""
        attrs = self.category_attributes.get(category_name, {})
        flavors = attrs.get('flavors', set())
        for flavor in flavors:
            if '原味' in flavor:
                return flavor
        return '原味'


    def _post_process_flavors(self, cleaned, flavor_results, category_name):
        """
        flavor 统一后处理：
        1. 去重
        2. 具体水果词优先
        3. 弱token限制
        4. 原味压制
        5. 删除"其他"
        6. 互斥组裁决
        7. 排序取前3
        """
        # V17.45: 水类商品即使flavor_results为空，也需要执行兜底映射逻辑
        # 所以不能在这里直接返回空列表
        if not flavor_results:
            return []

        source_priority = self._build_source_priority()

        # 1. 去重
        flavor_results = self._deduplicate_attr_results(flavor_results)

        # 1.5 复合水果优先：如果商品名包含"复合"或多个水果名称组合，优先映射到复合水果味
        # V17.44: 扩展检测逻辑，检测如"苹果香蕉"、"草莓蓝莓"等多水果组合
        has_composite_indicator = '复合' in cleaned or '混合' in cleaned
        
        # 检测商品名中是否包含多个水果名称
        fruit_names = ['苹果', '香蕉', '草莓', '蓝莓', '芒果', '菠萝', '橙子', '葡萄', '水蜜桃', '梨', '西瓜', '猕猴桃', '柠檬', '樱桃']
        found_fruits = [fruit for fruit in fruit_names if fruit in cleaned]
        if len(found_fruits) >= 2:
            has_composite_indicator = True
        
        if has_composite_indicator:
            # 检查口味池中是否有复合水果味（包括四级池和三级池）
            cat_attrs = self.category_attributes.get(category_name, {})
            std_flavors = cat_attrs.get('flavors', set())
            
            # 获取三级类目
            third_category = self.fourth_to_third_category.get(category_name, '')
            third_pool = self.third_level_flavors.get(third_category, set())
            
            # 合并四级池和三级池
            all_available_flavors = std_flavors | third_pool
            
            # 优先使用"复合水果味"，其次"混合果味"
            composite_target = None
            if '复合水果味' in all_available_flavors:
                composite_target = '复合水果味'
            elif '混合果味' in all_available_flavors:
                composite_target = '混合果味'
            
            if composite_target:
                # V17.44: 检查是否已经有任何复合口味在结果中（包括复合水果味、混合果味等）
                existing_composite = [x for x in flavor_results if '复合' in x['standard_attr'] or '混合' in x['standard_attr']]
                if existing_composite:
                    # 已有复合口味，只保留复合口味，删除其他具体水果口味
                    fruit_names_in_pool = ['苹果', '香蕉', '草莓', '蓝莓', '芒果', '菠萝', '橙子', '葡萄', '水蜜桃', '梨', '西瓜', '猕猴桃', '柠檬', '樱桃', '桃子', '黄桃', '白桃', '荔枝', '龙眼', '榴莲', '山竹', '火龙果', '百香果', '石榴', '椰子', '木瓜', '哈密瓜', '甜瓜', '柚子', '橘子', '金桔', '杨梅', '青梅', '桑葚', '树莓', '蔓越莓', '黑莓', '枸杞']
                    flavor_results = [x for x in flavor_results if ('复合' in x['standard_attr'] or '混合' in x['standard_attr']) or not any(fruit in x['standard_attr'] for fruit in fruit_names_in_pool)]
                else:
                    # 没有复合口味，添加一个
                    flavor_results.append({
                        'standard_attr': composite_target,
                        'best_token': ''.join(found_fruits[:2]) if found_fruits else '',
                        'final_score': 95,  # V17.44: 给复合水果味高分，确保通过质量过滤
                        'source': 'composite_fruit_detected'
                    })
                    # 删除其他具体水果口味（通过检查是否包含已知水果名称）
                    fruit_names_in_pool = ['苹果', '香蕉', '草莓', '蓝莓', '芒果', '菠萝', '橙子', '葡萄', '水蜜桃', '梨', '西瓜', '猕猴桃', '柠檬', '樱桃', '桃子', '黄桃', '白桃', '荔枝', '龙眼', '榴莲', '山竹', '火龙果', '百香果', '石榴', '椰子', '木瓜', '哈密瓜', '甜瓜', '柚子', '橘子', '金桔', '杨梅', '青梅', '桑葚', '树莓', '蔓越莓', '黑莓', '枸杞']
                    flavor_results = [x for x in flavor_results if x['standard_attr'] == composite_target or not any(fruit in x['standard_attr'] for fruit in fruit_names_in_pool)]

        # 1.6 多水果合并为复合水果味：如果检测到2个或以上水果口味，合并为复合水果味
        # V17.27: 排除"水果味"，因为"水果"通常是描述词而不是具体口味
        # V17.45: 改进检测逻辑，不依赖is_fruit，直接检查是否包含水果名称
        fruit_names_in_pool = ['苹果', '香蕉', '草莓', '蓝莓', '芒果', '菠萝', '橙子', '葡萄', '水蜜桃', '梨', '西瓜', '猕猴桃', '柠檬', '樱桃', '桃子', '黄桃', '白桃', '荔枝', '龙眼', '榴莲', '山竹', '火龙果', '百香果', '石榴', '椰子', '木瓜', '哈密瓜', '甜瓜', '柚子', '橘子', '金桔', '杨梅', '青梅', '桑葚', '树莓', '蔓越莓', '黑莓', '枸杞']
        specific_fruit_flavors = [x for x in flavor_results if any(fruit in x['standard_attr'] for fruit in fruit_names_in_pool)]
        has_specific_fruit = len(specific_fruit_flavors) >= 1
        has_fruit_flavor = any(x['standard_attr'] == '水果味' for x in flavor_results)
        
        # 如果有具体水果口味，删除"水果味"
        if has_specific_fruit and has_fruit_flavor:
            flavor_results = [x for x in flavor_results if x['standard_attr'] != '水果味']
        
        # 重新计算水果口味数量
        fruit_flavors = [x for x in flavor_results if self._is_fruit_flavor(x['standard_attr'])]
        if len(fruit_flavors) >= 2:
            # V17.44: 检查是否已经有复合口味（避免重复添加）
            has_composite = any('复合' in x['standard_attr'] or '混合' in x['standard_attr'] for x in flavor_results)
            
            if not has_composite:
                # 检查标准口味池中是否有"复合水果味"（包括四级池和三级池）
                cat_attrs = self.category_attributes.get(category_name, {})
                std_flavors = cat_attrs.get('flavors', set())
                
                # 获取三级类目
                third_category = self.fourth_to_third_category.get(category_name, '')
                third_pool = self.third_level_flavors.get(third_category, set())
                
                # 合并四级池和三级池
                all_available_flavors = std_flavors | third_pool
                
                if '复合水果味' in all_available_flavors:
                    # 添加复合水果味到结果中
                    flavor_results.append({
                        'standard_attr': '复合水果味',
                        'best_token': '',
                        'final_score': 95,  # V17.44: 给复合水果味高分，确保通过质量过滤
                        'source': 'composite_fruit_merge'
                    })
                    # 删除其他具体水果口味
                    fruit_names_in_pool = ['苹果', '香蕉', '草莓', '蓝莓', '芒果', '菠萝', '橙子', '葡萄', '水蜜桃', '梨', '西瓜', '猕猴桃', '柠檬', '樱桃', '桃子', '黄桃', '白桃', '荔枝', '龙眼', '榴莲', '山竹', '火龙果', '百香果', '石榴', '椰子', '木瓜', '哈密瓜', '甜瓜', '柚子', '橘子', '金桔', '杨梅', '青梅', '桑葚', '树莓', '蔓越莓', '黑莓', '枸杞']
                    flavor_results = [x for x in flavor_results if x['standard_attr'] == '复合水果味' or not any(fruit in x['standard_attr'] for fruit in fruit_names_in_pool)]

        # 2. 具体水果词优先
        # V17.45: 只有当flavor_results不为空时才调用_prefer_specific_flavor_by_marker
        if flavor_results:
            flavor_results = self._prefer_specific_flavor_by_marker(cleaned, flavor_results)

        # 3. 限制弱token输出数量
        flavor_results = self._limit_weak_token_outputs(cleaned, flavor_results)

        # 4. 原味压制：如果有具体口味（非原味、非其他），则删除原味
        # V17.32: 扩展压制条件，不仅限于explicit_rule
        # 规则：如果有水果口味或explicit_rule的具体口味，就压制原味
        has_specific_flavor = False

        # 4.1 检查是否有水果口味
        fruit_flavors = [x for x in flavor_results if self._is_fruit_flavor(x['standard_attr'])]
        if fruit_flavors:
            has_specific_flavor = True

        # 4.2 检查是否有explicit_rule的具体口味
        explicit_specific = [
            x for x in flavor_results
            if x['standard_attr'] not in {'原味', '其他', '其他口味'} and x.get('source') == 'explicit_rule'
        ]
        if explicit_specific:
            has_specific_flavor = True

        # 如果有具体口味，删除原味
        if has_specific_flavor:
            flavor_results = [x for x in flavor_results if x['standard_attr'] != '原味']

        # 4.5 乳饮类目原味保护：如果标题有"原味"且没有其他显式具体口味，强制保留原味
        flavor_results = self._protect_original_flavor(cleaned, flavor_results, category_name)

        # 5. 删除"其他"：若已有明确口味，则删掉其他/其他口味
        if any(x['standard_attr'] not in {'其他', '其他口味'} for x in flavor_results):
            flavor_results = [x for x in flavor_results if x['standard_attr'] not in {'其他', '其他口味'}]

        # 5.5 相似口味合并：如果两个口味名称存在包含关系，保留更具体的（更长的）
        # 例如："凤梨味"和"梨味" → 保留"凤梨味"
        # 例如："红枣味"和"枣味" → 保留"红枣味"
        flavor_results = self._merge_similar_flavors(flavor_results)

        # 6. 互斥组裁决
        flavor_results = self._apply_flavor_mutex(flavor_results)

        # V17.31: 6.5 动态去重：去除"香辣"vs"香辣味"等变体重复
        flavor_results = self._remove_flavor_variants(flavor_results)

        # 7. 最终排序
        flavor_results.sort(
            key=lambda x: (
                source_priority.get(x.get('source', ''), 0),
                x.get('final_score', 0)
            ),
            reverse=True
        )

        # V17.31: 8. 质量优先：只返回高质量结果，宁缺毋滥
        MIN_SCORE = 82  # 最低质量阈值
        high_quality_results = [r for r in flavor_results if r['final_score'] >= MIN_SCORE]

        # 最多返回3个，但可能少于3个（避免为了凑数而降低质量）
        return high_quality_results[:3]

    # -----------------------------
    # function 辅助逻辑
    # -----------------------------
    def _allow_fuzzy_function(self, category_name):
        """
        V17.31: 只对非食品类目允许功能fuzzy匹配

        食品类目虽然定义表中有功能列，但实际是产品特点（如低糖、零卡、补水），
        不是用户选择产品时的功能属性，不需要映射。

        非食品类目（个人护理、清洁用品等）的功能（如防蛀、去屑、去污）
        才是真正的功能属性，需要映射。
        """
        # 检查该类目是否有功能数据
        attrs = self.category_attributes.get(category_name, {})
        if not attrs.get('has_function'):
            return False

        # 只对非食品类目允许fuzzy匹配
        return category_name in self.non_food_categories

    # -----------------------------
    # fallback 信号判断
    # -----------------------------
    def _has_flavor_signal(self, cleaned, category_name):
        if not cleaned:
            return False

        strong_markers = ['风味', '口味', '果味', '味道', '果蔬汁', '复合果蔬']
        if any(x in cleaned for x in strong_markers):
            return True

        if '味' in cleaned:
            return True

        if '原味' in cleaned:
            return True

        # 使用从JSON自动提取的fruit_words，如果没有则使用内置列表
        if AUTO_FRUIT_WORDS is not None:
            fruit_words = AUTO_FRUIT_WORDS
        else:
            return False  # 如果JSON加载失败，不触发口味信号

        hit_fruits = [w for w in fruit_words if w in cleaned]

        # 使用从JSON加载的context_words
        if AUTO_CONTEXT_WORDS is not None:
            context_words = AUTO_CONTEXT_WORDS
        else:
            context_words = ['茶', '茶饮', '茶饮料', '饮料', '果汁', '汁',
                           '乳饮', '乳酸菌', '酸奶', '发酵乳', '含乳',
                           '苏打', '汽水', '果奶', '牛奶', '奶']
        has_context = any(w in cleaned for w in context_words)

        if len(hit_fruits) >= 2:
            return True

        if len(hit_fruits) >= 1 and has_context:
            return True

        # 果汁/水果/果味饮料类目：只要有1个水果就触发（不需要context）
        if category_name == '果汁/水果/果味饮料' and len(hit_fruits) >= 1:
            return True

        # 使用从JSON加载的flavor_friendly_categories
        if AUTO_FLAVOR_FRIENDLY_CATEGORIES is not None:
            flavor_friendly_categories = set(AUTO_FLAVOR_FRIENDLY_CATEGORIES)
        else:
            flavor_friendly_categories = {'即饮茶', '果汁饮料', '果味饮料', '乳酸菌饮料',
                                         '发酵型含乳饮料', '配制型含乳饮料', '碳酸饮料', '调味乳', '含乳饮料'}
        if category_name in flavor_friendly_categories and len(hit_fruits) >= 1:
            return True

        # 非水果口味词（排除类目特定的原料词）
        # V17.5: 对于植物蛋白饮料，"花生"、"杏仁"、"燕麦"等是原料，不是口味
        category_excluded_words = {
            '花生乳': ['花生'],
            '杏仁乳': ['杏仁'],
            '燕麦乳': ['燕麦'],
            '核桃乳': ['核桃'],
            '椰子乳': ['椰子'],
            '豆奶': ['大豆', '豆奶'],
            '麦片': ['燕麦', '坚果', '谷物', '麦片', '牛奶'],
        }
        excluded = category_excluded_words.get(category_name, [])
        non_fruit_words = [w for w in self.non_fruit_flavor_words if w not in excluded]
        if any(w in cleaned for w in non_fruit_words):
            return True

        return False

    # -----------------------------
    # 主抽取逻辑
    # -----------------------------
    def extract_attributes(self, product_name, category_name):
        if category_name not in self.supported_categories:
            return {'flavors': [], 'functions': [], 'decision_path': 'out_of_scope'}

        attrs = self.category_attributes.get(category_name, {})
        flavor_results = []
        function_results = []

        # function: 规则优先，只有白名单类目允许 fuzzy 补充
        if attrs.get('has_function'):
            standard_functions = attrs.get('functions', set())

            rule_results = self._rule_match_function(product_name, category_name, standard_functions)
            function_results.extend(rule_results)

            rule_matched = {r['standard_attr'] for r in rule_results}

            if len(function_results) < 3 and self._allow_fuzzy_function(category_name):
                fuzzy_results = self._fuzzy_match_attr_type(
                    product_name,
                    category_name,
                    'function',
                    threshold=88,
                    top_k=3 - len(function_results),
                    exclude=rule_matched
                )
                function_results.extend(fuzzy_results)

        if attrs.get('has_flavor'):
            cleaned = self._normalize_text(product_name)
            # 过滤掉品类特征词，避免被误识别为口味
            cleaned = self._filter_category_keywords(cleaned, category_name)
            standard_flavors = attrs.get('flavors', set())

            # 1) explicit_rule
            rule_results = self._rule_match_flavor(product_name, category_name, standard_flavors)
            matched_flavors = {r['standard_attr'] for r in rule_results}
            flavor_results.extend(rule_results)

            # V17.50: 如果explicit_rule没有匹配结果，尝试类目特定的水果口味映射
            # 例如：水果罐头中的"黄桃"应映射为"核果类"
            if not rule_results and category_name in self.category_specific_fruit_map:
                category_flavor_result = self._map_fruit_to_category_flavor(
                    product_name, category_name, standard_flavors
                )
                if category_flavor_result:
                    flavor_results.append(category_flavor_result)
                    matched_flavors.add(category_flavor_result['standard_attr'])

            # 2) fuzzy 运行（用于发现alias和学习），结果按需补充
            # 即使显式规则已匹配，也运行fuzzy来发现新的alias候选
            # V17.5: 提高默认阈值，减少过度匹配
            if category_name in ['方便面', '即饮奶茶', '中国白酒']:
                fuzzy_threshold = 95  # 这些类目容易误匹配，用更高阈值
            else:
                fuzzy_threshold = 90  # 默认阈值：82 → 90

            fuzzy_results = self._fuzzy_match_attr_type(
                product_name,
                category_name,
                'flavor',
                threshold=fuzzy_threshold,
                top_k=3,
                exclude=set()  # 不排除任何，用于完整学习
            )

            # V17.5: 去重检查 - 避免相似口味同时出现
            # 例如：不应该同时有"红烧牛肉味"和"香辣牛肉味"
            # V17.6: 完整匹配优先 - 如果商品名包含完整口味描述，不要拆分
            # V17.6: 添加组合词规则 - 避免将"酸辣牛肉"拆分成"酸辣"+"牛肉"
            filtered_fuzzy = []

            # 特殊规则1: 如果商品名包含"酸辣"，排除所有"牛肉"类口味
            # 因为"酸辣牛肉"应该被理解为"酸辣"口味，而不是"牛肉"口味
            has_suan_la = '酸辣' in cleaned
            if has_suan_la:
                # 标记所有包含"牛肉"的口味为需要排除
                beef_flavors_to_exclude = set()
                for fr in fuzzy_results:
                    if '牛肉' in fr['standard_attr']:
                        beef_flavors_to_exclude.add(fr['standard_attr'])

            # 先检查是否有"完整匹配"（如"酸辣牛肉"完整出现在商品名中）
            complete_match_found = False
            for fr in fuzzy_results:
                flavor_name = fr['standard_attr']
                # 应用特殊规则1的排除
                if has_suan_la and '牛肉' in flavor_name:
                    continue

                # 提取口味核心词
                flavor_core = flavor_name.replace('味', '').replace('风味', '').replace('口味', '')

                # 检查完整核心词是否在清洗后的商品名中
                # 例如："酸辣牛肉" 是否在 "酸辣牛肉面" 中
                if flavor_core in cleaned and len(flavor_core) >= 3:
                    # 这是一个完整匹配
                    filtered_fuzzy.append(fr)
                    complete_match_found = True
                    # 只保留第一个完整匹配，避免多个完整匹配
                    break

            # 如果没有完整匹配，才考虑部分匹配
            if not complete_match_found:
                for fr in fuzzy_results:
                    flavor_name = fr['standard_attr']

                    # V17.21: 对于葡萄酒类目，只保留标准口味池中的口味
                    # 避免从完整口味池中匹配到"葡萄味"等非标准口味
                    if category_name == '葡萄酒/红酒' and flavor_name not in standard_flavors:
                        continue

                    # 应用特殊规则1的排除
                    if has_suan_la and '牛肉' in flavor_name:
                        continue

                    # 检查是否与已匹配的口味冲突
                    is_duplicate = False

                    # 检查是否包含相同的核心词
                    for existing in matched_flavors:
                        # 提取核心词（去掉"味"、"风味"等后缀）
                        existing_core = existing.replace('味', '').replace('风味', '').replace('口味', '')
                        flavor_core = flavor_name.replace('味', '').replace('风味', '').replace('口味', '')

                        # 如果核心词相同或高度重叠，视为重复
                        if existing_core == flavor_core:
                            is_duplicate = True
                            break

                        # 检查包含关系（例如"牛肉"和"红烧牛肉"）
                        if len(existing_core) > 2 and existing_core in flavor_core:
                            is_duplicate = True
                            break
                        if len(flavor_core) > 2 and flavor_core in existing_core:
                            is_duplicate = True
                            break

                    if not is_duplicate and flavor_name not in matched_flavors:
                        filtered_fuzzy.append(fr)

            # 只把fuzzy结果中不在rule_results的补充到最终结果
            # V17.37: 应用口味聚合映射，将二级池口味映射到四级池聚合口味
            if len(flavor_results) < 3:
                for fr in filtered_fuzzy:
                    original_flavor = fr['standard_attr']
                    # 尝试聚合到四级池口味
                    aggregated_flavor = self._aggregate_flavor(original_flavor, standard_flavors)

                    # 如果聚合后口味不同，更新结果
                    if aggregated_flavor != original_flavor:
                        fr['standard_attr'] = aggregated_flavor
                        fr['best_token'] = aggregated_flavor
                        fr['source'] = fr.get('source', '') + '_aggregated'

                    if aggregated_flavor not in matched_flavors:
                        flavor_results.append(fr)
                        matched_flavors.add(aggregated_flavor)
                    if len(flavor_results) >= 3:
                        break

            # 3) 后处理
            flavor_results = self._post_process_flavors(cleaned, flavor_results, category_name)

            # 4) 统一口味兜底
            if len(flavor_results) == 0:
                flavor_results = self._fallback_flavor(cleaned, category_name, standard_flavors)

        # alias 学习：flavor 从 fuzzy 和 explicit_rule 结果学习
        # explicit_rule 提高阈值到95分，fuzzy保持86分
        for item in flavor_results:
            source = item.get('source')
            final_score = item.get('final_score', 0)
            # fuzzy: 阈值86, explicit_rule: 阈值95
            if source == 'fuzzy' and final_score >= 86:
                self._collect_alias_candidate(
                    category_name,
                    'flavor',
                    item['standard_attr'],
                    item.get('best_token'),
                    final_score,
                    product_name
                )
            elif source == 'explicit_rule' and final_score >= 95:
                self._collect_alias_candidate(
                    category_name,
                    'flavor',
                    item['standard_attr'],
                    item.get('best_token'),
                    final_score,
                    product_name
                )

        # function 从非规则结果学习，阈值保持90分
        for item in function_results:
            if item.get('source') != 'rule' and item.get('final_score', 0) >= 90:
                self._collect_alias_candidate(
                    category_name,
                    'function',
                    item['standard_attr'],
                    item.get('best_token'),
                    item.get('final_score', 0),
                    product_name
                )

        return {
            'flavors': [x['standard_attr'] for x in flavor_results[:3]],
            'functions': [x['standard_attr'] for x in function_results[:3]],
            'decision_path': 'explicit_rule_plus_fuzzy'
        }

    # -----------------------------
    # 对外处理接口
    # -----------------------------
    def process_product(self, product_data):
        product_name = product_data.get('商品名称', '')
        category_name = product_data.get('新类别', '')
        barcode = product_data.get('商品条码', '')

        attributes = self.extract_attributes(product_name, category_name)

        flavors = attributes['flavors']
        functions = attributes['functions']

        flavor_id_1 = self.flavor_codes.get(flavors[0], '') if len(flavors) > 0 else None
        flavor_id_2 = self.flavor_codes.get(flavors[1], '') if len(flavors) > 1 else None
        flavor_id_3 = self.flavor_codes.get(flavors[2], '') if len(flavors) > 2 else None

        function_id_1 = self.function_codes.get(functions[0], '') if len(functions) > 0 else None
        function_id_2 = self.function_codes.get(functions[1], '') if len(functions) > 1 else None
        function_id_3 = self.function_codes.get(functions[2], '') if len(functions) > 2 else None

        return {
            '商品条码': barcode,
            '商品名称': product_name,
            '新类别': category_name,
            'flavor_1': flavors[0] if len(flavors) > 0 else None,
            'flavor_id_1': flavor_id_1,
            'flavor_2': flavors[1] if len(flavors) > 1 else None,
            'flavor_id_2': flavor_id_2,
            'flavor_3': flavors[2] if len(flavors) > 2 else None,
            'flavor_id_3': flavor_id_3,
            'function_1': functions[0] if len(functions) > 0 else None,
            'function_id_1': function_id_1,
            'function_2': functions[1] if len(functions) > 1 else None,
            'function_id_2': function_id_2,
            'function_3': functions[2] if len(functions) > 2 else None,
            'function_id_3': function_id_3,
            'decision_path': attributes['decision_path']
        }

    def process_csv_file(self, input_file, output_file):
        results = []

        with open(input_file, 'r', encoding='utf-8-sig') as f:
            rows = list(csv.DictReader(f))

        # 统计商品数据中的类目分布
        product_categories = set()
        sa_product_categories = set()
        for row in rows:
            category = row.get('新类别', '').strip()
            if category:
                product_categories.add(category)
                if category in self.supported_categories:
                    sa_product_categories.add(category)

        print(f"商品数据中涉及 {len(product_categories)} 个四级类目")
        print(f"其中属于S/A/B/C等级的有 {len(sa_product_categories)} 个类目")

        # 使用 tqdm 显示进度条
        for row in tqdm(rows, desc="处理商品", unit="个"):
            result = self.process_product(row)
            results.append(result)

        with open(output_file, 'w', newline='', encoding='utf-8-sig') as f:
            fieldnames = [
                '商品条码', '商品名称', '新类别',
                'flavor_1', 'flavor_id_1', 'flavor_2', 'flavor_id_2', 'flavor_3', 'flavor_id_3',
                'function_1', 'function_id_1', 'function_2', 'function_id_2', 'function_3', 'function_id_3',
                'decision_path'
            ]
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(results)

        # 生成JSON格式的结果文件
        json_output_file = output_file.replace('.csv', '.json')
        self._save_results_as_json(results, json_output_file)

        self._save_auto_alias_dict()

        total = len(results)
        supported_count = sum(1 for r in results if r['新类别'] in self.supported_categories)
        sa_count = supported_count  # 兼容旧变量名

        need_flavor_count = 0
        need_function_count = 0
        with_flavor = 0
        with_function = 0

        for r in results:
            category = r['新类别']
            if category not in self.supported_categories:
                continue

            attrs = self.category_attributes.get(category, {})

            if attrs.get('has_flavor'):
                need_flavor_count += 1
                if r['flavor_1']:
                    with_flavor += 1

            if attrs.get('has_function'):
                need_function_count += 1
                if r['function_1']:
                    with_function += 1

        print(f"处理完成，结果已保存到: {output_file}")
        print(f"总商品数: {total}")
        print(f"S/A类目商品数: {sa_count}")
        if need_flavor_count > 0:
            print(
                f"有口味映射: {with_flavor} / 需口味映射: {need_flavor_count} "
                f"({with_flavor / need_flavor_count * 100:.1f}%)"
            )
        else:
            print(f"有口味映射: {with_flavor} / 需口味映射: 0")

        if need_function_count > 0:
            print(
                f"有功能映射: {with_function} / 需功能映射: {need_function_count} "
                f"({with_function / need_function_count * 100:.1f}%)"
            )
        else:
            print(f"有功能映射: {with_function} / 需功能映射: 0")

        print(f"本轮 alias 候选数: {len(self.alias_candidate_pool)}")
        print(f"auto alias 总量: {sum(len(v) for v in self.auto_alias_dict.values())}")

        return {
            'total': total,
            'sa_count': sa_count,
            'with_flavor': with_flavor,
            'with_function': with_function,
            'need_flavor_count': need_flavor_count,
            'need_function_count': need_function_count,
            'flavor_rate': with_flavor / need_flavor_count * 100 if need_flavor_count > 0 else 0,
            'function_rate': with_function / need_function_count * 100 if need_function_count > 0 else 0,
            'alias_count': sum(len(v) for v in self.auto_alias_dict.values())
        }


if __name__ == '__main__':
    import sys

    num_rounds = 1
    if len(sys.argv) > 1:
        try:
            num_rounds = int(sys.argv[1])
        except ValueError:
            pass

    print(f"将运行 {num_rounds} 轮")

    rounds = []
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    for r in range(1, num_rounds + 1):
        print(f"\n{'=' * 60}")
        print(f"第 {r} 轮")
        print(f"{'=' * 60}")

        system = AttributeMappingSystemV173()
        result = system.process_csv_file(
            'data/分类商品0414.csv',
            f'out/result_v173_round{r}_{timestamp}.csv'
        )
        rounds.append(result)

    if num_rounds > 1:
        print("\n" + "=" * 60)
        print(f"{num_rounds}轮对比结果")
        print("=" * 60)
        print(f"{'轮次':<6} {'口味映射率':<12} {'功能映射率':<12} {'alias数量':<10}")
        print("-" * 50)
        for i, r in enumerate(rounds, 1):
            print(f"第{i}轮   {r['flavor_rate']:.1f}%        {r['function_rate']:.1f}%        {r['alias_count']}")
