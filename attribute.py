"""
批量商品口味映射工具
读取商品CSV，使用LLM发现口味，输出带口味列的新CSV和总结报告
特点：实时写入、支持断点续传、tqdm进度条
"""
import csv
import json
import os
import time
from datetime import datetime
from collections import defaultdict, Counter
from typing import List, Dict, Tuple
import requests


class GLM4Client:
    """GLM-4-Flash-250414 API客户端 - 支持批量和并发"""

    def __init__(self, api_key: str = None, max_workers: int = 5):
        self.api_key = api_key or os.getenv("GLM_API_KEY")
        self.api_url = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
        self.request_count = 0
        self.cache_hits = 0
        self.max_workers = max_workers

    def chat(self, messages: List[Dict], temperature: float = 0.3) -> str:
        """调用GLM-4-Flash-250414模型"""
        if not self.api_key:
            return None

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }

        payload = {
            "model": "GLM-4-Flash-250414",
            "messages": messages,
            "temperature": temperature
        }

        try:
            self.request_count += 1
            response = requests.post(
                self.api_url,
                headers=headers,
                json=payload,
                timeout=30
            )
            response.raise_for_status()
            return response.json()["choices"][0]["message"]["content"]
        except Exception as e:
            return None

    def batch_extract(self, products: List[Dict]) -> List[Dict]:
        """批量提取口味 - 一次处理多个商品"""
        if not products:
            return []

        # 构建批量提示
        products_text = "\n".join([
            f"{i+1}. {p['name']} (类别: {p.get('category', '未知')})"
            for i, p in enumerate(products)
        ])

        prompt = f"""你是一位专业的食品口味分析师。请从以下商品列表中提取口味信息。

商品列表:
{products_text}

提取规则:
1. 识别描述味道、风味的词汇（单一口味或复合口味）
2. 复合口味优先整体提取，如"海盐椰子"、"韩式辣牛肉"
3. 包含修饰词，如"磁磁烤肉味"→"磁磁烤肉"
4. 排除产品类型（薯片、火腿肠等）和规格（150g、15个等）
5. 只有明确标注"原味"时才返回"原味"
6. 无口味时返回空字符串""

请输出JSON数组格式:
[
    {{"index": 1, "flavor": "口味", "is_original": false}},
    {{"index": 2, "flavor": "", "is_original": false}},
    ...
]

只输出JSON数组。"""

        messages = [
            {"role": "system", "content": "你是食品口味识别专家，擅长批量处理。"},
            {"role": "user", "content": prompt}
        ]

        response = self.chat(messages, temperature=0.3)

        if not response:
            return [{'flavor': '', 'is_original': False} for _ in products]

        try:
            import re
            json_match = re.search(r'\[[\s\S]*\]', response)
            if json_match:
                results = json.loads(json_match.group())
                # 确保返回数量匹配
                if len(results) == len(products):
                    return results
        except:
            pass

        return [{'flavor': '', 'is_original': False} for _ in products]


class FlavorMapperStreaming:
    """流式口味映射器 - 实时输出"""

    def __init__(self, api_key: str = None):
        self.glm_client = GLM4Client(api_key)
        self.flavor_cache = self._load_cache()
        self.new_flavors_by_category = defaultdict(lambda: defaultdict(int))
        self.flavor_stats = Counter()
        self.processed_count = 0
        self.new_flavor_count = 0

    def _load_cache(self) -> Dict:
        """加载缓存"""
        cache_file = 'flavor_cache.json'
        if os.path.exists(cache_file):
            try:
                with open(cache_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except:
                pass
        return {}

    def _save_cache(self):
        """保存缓存"""
        with open('flavor_cache.json', 'w', encoding='utf-8') as f:
            json.dump(self.flavor_cache, f, ensure_ascii=False, indent=2)

    def _rule_based_extract(self, product_name: str) -> str:
        """基于规则的快速提取 - 智能版"""
        import re

        # 1. 先匹配"XX味"模式（最准确）
        flavor_patterns = [
            r'([^\s]{2,6})味',  # 如：烤肉味、蓝莓味、韩式辣牛肉味
            r'([^\s]{2,6})口感',  # 如：酥脆口感
            r'([^\s]{2,6})风味',  # 如：烧烤风味
        ]

        for pattern in flavor_patterns:
            matches = list(re.finditer(pattern, product_name))
            if matches:
                # 取第一个匹配，过滤掉产品类型词
                for match in matches:
                    candidate = match.group(1)
                    # 排除产品类型和规格
                    excluded = {'薯片', '薯条', '火腿肠', '方便面', '酸奶', '牛奶',
                               '饮料', '果汁', '奶茶', '咖啡', '麦片', '蛋', '肠',
                               '面', '粉', '包', '袋', '盒', '瓶', '杯', '碗'}
                    if candidate not in excluded and not any(c.isdigit() for c in candidate):
                        return candidate

        # 2. 复合口味模式（扩展列表）
        compound_patterns = [
            '海盐椰子', '岩烧乳酪', '青柑普洱', '白桃乌龙', '麻辣小龙虾',
            '草莓牛奶', '巧克力牛奶', '红豆薏米', '红枣枸杞', '红烧牛肉',
            '香辣牛肉', '奥尔良烤翅', '芝士奶酪', '蜂蜜柚子', '柠檬红茶',
            '丝绒可可', '红柚豆沙', '麻辣卤藕', '韩式辣牛肉', '磁磁烤肉',
            '玉米热狗', '牛奶燕麦', '蓝莓酸奶', '原味酸奶'
        ]

        for pattern in compound_patterns:
            if pattern in product_name:
                return pattern

        # 3. 常见单一口味（按优先级排序）
        common_flavors = [
            # 水果类（优先）
            '蓝莓', '草莓', '葡萄', '芒果', '菠萝', '柠檬', '橙子', '西瓜', '椰子',
            '苹果', '香蕉', '桃子', '橘子', '百香果', '榴莲', '柚子', '哈密瓜',
            # 乳品类
            '巧克力', '牛奶', '酸奶', '芝士', '奶酪', '奶油',
            # 茶咖类
            '咖啡', '拿铁', '美式', '摩卡', '红茶', '绿茶', '乌龙', '普洱',
            '茉莉', '菊花', '玫瑰', '桂花',
            # 辣味
            '麻辣', '香辣', '酸辣', '甜辣', '微辣', '中辣', '特辣', '韩式辣',
            # 肉类/咸鲜
            '牛肉', '鸡肉', '猪肉', '羊肉', '海鲜', '虾', '蟹', '烤肉', '烧烤',
            # 谷物类
            '燕麦', '麦片', '红豆', '绿豆', '黑豆', '薏米', '黑米', '紫米', '玉米',
            # 其他
            '蜂蜜', '红枣', '枸杞', '花生', '核桃', '芝麻', '抹茶',
            '香草', '薄荷', '焦糖', '海盐', '岩盐', '番茄', '土豆',
            # 原味最后检查
            '原味'
        ]

        found = []
        for flavor in common_flavors:
            if flavor in product_name:
                idx = product_name.index(flavor)
                # 检查是否是其他词的一部分（简单检查）
                is_part_of_other = False
                for other in common_flavors:
                    if other != flavor and flavor in other and other in product_name:
                        # 如果存在更长的包含该词的口味，优先使用长的
                        other_idx = product_name.index(other)
                        if other_idx <= idx <= other_idx + len(other):
                            is_part_of_other = True
                            break
                if not is_part_of_other:
                    found.append((flavor, idx))

        if found:
            # 按位置排序，优先前面的
            found.sort(key=lambda x: x[1])
            return found[0][0]

        return ''

    def _is_common_flavor(self, flavor: str) -> bool:
        """检查是否是常见口味"""
        common = {'草莓', '蓝莓', '葡萄', '苹果', '香蕉', '芒果', '菠萝',
                 '柠檬', '橙子', '西瓜', '椰子', '巧克力', '牛奶', '酸奶',
                 '咖啡', '红茶', '绿茶', '乌龙', '茉莉', '燕麦', '红豆',
                 '麻辣', '香辣', '番茄', '牛肉', '鸡肉', '原味'}
        return flavor in common

    def _llm_extract(self, product_name: str, category: str = None) -> Tuple[str, bool]:
        """使用LLM提取口味 - 专业版提示词"""
        category_hint = f"\n商品类别: {category}" if category else ""

        prompt = f"""你是一位专业的食品口味分析师，擅长从商品名称中准确识别口味信息。

【任务】
从以下商品名称中提取口味描述词。

商品名称: {product_name}{category_hint}

【口味定义】
口味是指描述食品味道、风味、口感的词汇，包括：
- 水果味：草莓、蓝莓、葡萄、苹果、香蕉、芒果、菠萝、柠檬、橙子、西瓜、椰子等
- 乳品味：牛奶、酸奶、芝士、奶酪、奶油、巧克力等
- 茶咖味：红茶、绿茶、乌龙、普洱、茉莉、咖啡、拿铁等
- 甜味：蜂蜜、焦糖、香草、抹茶等
- 咸味/鲜味：海盐、岩烧、烧烤、烤肉、牛肉、鸡肉、海鲜等
- 辣味：麻辣、香辣、酸辣、甜辣、韩式辣等
- 谷物味：燕麦、红豆、绿豆、芝麻、花生、核桃、红枣、枸杞等
- 复合口味：海盐椰子、岩烧乳酪、青柑普洱、白桃乌龙、麻辣小龙虾、韩式辣牛肉等

【提取规则】
1. **精准定位**：找到商品名称中明确描述味道的词汇或短语
2. **复合优先**：如"韩式辣牛肉面"应提取"韩式辣牛肉"而非单独的"牛肉"
3. **包含修饰词**：如"磁磁烤肉味"应提取"磁磁烤肉"而非单独的"烤肉"
4. **排除产品类型**：不要提取"薯片"、"火腿肠"、"方便面"等产品类别词
5. **排除规格信息**：不要提取"15个"、"180g"、"82g"等规格
6. **原味判定**：只有明确标注"原味"时才返回"原味"，否则返回空字符串
7. **无口味时**：如果确实没有口味描述，返回空字符串""

【示例】
- "乐事无限薯片磁磁烤肉味104g" → "磁磁烤肉"
- "金锣玉米热狗肠150g" → "玉米"
- "人人欢喜牛奶加钙燕麦片720g" → "牛奶燕麦"
- "统一汤达人韩式辣牛肉面清真版82g/杯" → "韩式辣牛肉"
- "盖瑞酸牛奶蓝莓味180g" → "蓝莓"
- "邹记虫草蛋15个" → "" (无口味)
- "原味酸奶" → "原味"

【输出格式】
请严格输出JSON格式：
{{"flavor": "提取的口味词", "is_original": false, "confidence": 0.95, "reasoning": "简要说明提取理由"}}

只输出JSON，不要有其他内容。"""

        messages = [
            {"role": "system", "content": "你是一位资深的食品口味分析专家，精通各类食品的口味描述和命名规则。你的任务是准确提取商品名称中的口味信息，要求精准、全面、不遗漏复合口味。"},
            {"role": "user", "content": prompt}
        ]

        response = self.glm_client.chat(messages, temperature=0.3)

        if not response:
            return '', False

        try:
            import re
            json_match = re.search(r'\{{[\s\S]*?\}}', response)
            if json_match:
                result = json.loads(json_match.group())
                flavor = result.get('flavor', '')
                is_original = result.get('is_original', False)

                if not flavor and is_original:
                    flavor = '原味'

                is_new = len(flavor) >= 4 and not self._is_common_flavor(flavor)
                return flavor, is_new
        except:
            pass

        return '', False

    def extract_flavor(self, product_name: str, category: str = None) -> Tuple[str, bool]:
        """提取口味（带缓存）"""
        cache_key = f"{product_name}_{category}"

        if cache_key in self.flavor_cache:
            self.glm_client.cache_hits += 1
            return self.flavor_cache[cache_key]

        # 先尝试规则匹配
        flavor = self._rule_based_extract(product_name)

        if flavor:
            is_new = len(flavor) >= 4 and not self._is_common_flavor(flavor)
        else:
            # 规则未匹配，使用LLM
            flavor, is_new = self._llm_extract(product_name, category)

        result = (flavor, is_new)
        self.flavor_cache[cache_key] = result

        return result

    def _load_progress(self, output_path: str) -> set:
        """加载已处理的商品条码，用于断点续传"""
        processed = set()
        if os.path.exists(output_path):
            try:
                with open(output_path, 'r', encoding='utf-8-sig') as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        barcode = row.get('商品条码', '')
                        if barcode:
                            processed.add(barcode)
                print(f"发现已有输出文件，已处理 {len(processed)} 条，将跳过这些记录")
            except:
                pass
        return processed

    def process_csv_streaming(self, input_path: str, output_path: str, resume: bool = True):
        """流式处理CSV文件 - 实时写入，支持断点续传"""
        try:
            from tqdm import tqdm
            use_tqdm = True
        except ImportError:
            use_tqdm = False
            print("注意: pip install tqdm 可显示进度条")

        # 读取输入文件
        print(f"读取文件: {input_path}")
        with open(input_path, 'r', encoding='utf-8-sig') as f:
            reader = csv.DictReader(f)
            rows = list(reader)
            fieldnames = reader.fieldnames + ['口味', '是否新口味']

        total = len(rows)
        print(f"共读取 {total} 条商品数据")

        # 检查已处理的记录
        processed_barcodes = set()
        if resume:
            processed_barcodes = self._load_progress(output_path)

        # 过滤已处理的记录
        rows_to_process = [r for r in rows if r.get('商品条码', '') not in processed_barcodes]
        skip_count = len(rows) - len(rows_to_process)

        if skip_count > 0:
            print(f"跳过已处理: {skip_count} 条，待处理: {len(rows_to_process)} 条")

        if not rows_to_process:
            print("所有记录已处理完成！")
            return {
                'total': total,
                'skipped': skip_count,
                'processed': 0,
                'with_flavor': 0,
                'new_flavors': 0,
                'flavor_stats': [],
                'api_calls': 0,
                'cache_hits': 0,
                'elapsed_time': 0
            }

        # 创建或追加输出文件
        file_exists = os.path.exists(output_path) and os.path.getsize(output_path) > 0
        if not file_exists:
            print(f"\n创建输出文件: {output_path}")
            with open(output_path, 'w', encoding='utf-8-sig', newline='') as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
        else:
            print(f"\n追加到已有文件: {output_path}")

        # 处理数据 - 全部使用大模型批量处理
        print("\n开始处理...")
        print("全部调用大模型GLM-4-Flash-250414，并发限制5")
        start_time = time.time()

        # 批量处理参数
        batch_size = 5  # 每批5个（符合并发限制）
        total_batches = (len(rows_to_process) + batch_size - 1) // batch_size

        print(f"总批次: {total_batches}，每批{batch_size}个商品")

        # 分批处理
        for batch_idx in range(total_batches):
            start_idx = batch_idx * batch_size
            end_idx = min(start_idx + batch_size, len(rows_to_process))
            batch = rows_to_process[start_idx:end_idx]

            # 准备批量请求数据
            products = [{'name': row.get('商品名称', ''), 'category': row.get('新类别', '')} for row in batch]

            # 调用批量API
            results = self.glm_client.batch_extract(products)

            # 处理结果
            for row, result in zip(batch, results):
                product_name = row.get('商品名称', '')
                category = row.get('新类别', '')

                flavor = result.get('flavor', '')
                is_original = result.get('is_original', False)

                if not flavor and is_original:
                    flavor = '原味'

                is_new = len(flavor) >= 4 and not self._is_common_flavor(flavor)

                row['口味'] = flavor
                row['是否新口味'] = '是' if is_new else '否'

                # 实时写入
                with open(output_path, 'a', encoding='utf-8-sig', newline='') as f:
                    writer = csv.DictWriter(f, fieldnames=fieldnames)
                    writer.writerow(row)

                # 更新统计
                self.processed_count += 1
                if flavor:
                    self.flavor_stats[flavor] += 1
                if is_new:
                    self.new_flavor_count += 1
                    if category:
                        self.new_flavors_by_category[category][flavor] += 1

                # 缓存
                cache_key = f"{product_name}_{category}"
                self.flavor_cache[cache_key] = (flavor, is_new)

            # 每10批保存一次缓存
            if (batch_idx + 1) % 10 == 0:
                self._save_cache()

            # 每20批输出进度
            if (batch_idx + 1) % 20 == 0 or batch_idx == total_batches - 1:
                processed = min((batch_idx + 1) * batch_size, len(rows_to_process))
                elapsed = time.time() - start_time
                speed = self.processed_count / elapsed if elapsed > 0 else 0
                progress_pct = processed / len(rows_to_process) * 100
                print(f"\n  进度: {processed}/{len(rows_to_process)} ({progress_pct:.1f}%) | "
                      f"速度: {speed:.1f}条/秒 | "
                      f"已识别: {len(self.flavor_stats)} 种口味 | "
                      f"新口味: {self.new_flavor_count} 个")

        # 保存最终缓存
        self._save_cache()

        elapsed = time.time() - start_time
        print(f"\n处理完成！用时: {elapsed:.1f}秒")

        return {
            'total': total,
            'skipped': skip_count,
            'processed': len(rows_to_process),
            'with_flavor': sum(1 for _ in range(len(rows_to_process)) if self.flavor_stats),
            'new_flavors': self.new_flavor_count,
            'flavor_stats': self.flavor_stats.most_common(30),
            'api_calls': self.glm_client.request_count,
            'cache_hits': self.glm_client.cache_hits,
            'elapsed_time': elapsed
        }

    def generate_report(self, output_file: str = "data/flavor_mapping_report.txt"):
        """生成总结报告"""
        lines = []
        lines.append("=" * 80)
        lines.append("口味映射总结报告")
        lines.append(f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append("=" * 80)

        # 按四级分类统计新口味
        lines.append("\n【各四级分类下的新口味统计】")
        lines.append("-" * 80)

        # 按分类名称排序
        sorted_categories = sorted(self.new_flavors_by_category.keys())

        for category in sorted_categories:
            flavors = self.new_flavors_by_category[category]
            if not flavors:
                continue

            lines.append(f"\n分类: {category}")
            lines.append(f"  新口味数量: {len(flavors)}")

            # 按出现次数排序
            sorted_flavors = sorted(flavors.items(), key=lambda x: x[1], reverse=True)
            for flavor, count in sorted_flavors[:20]:  # 每类只显示前20
                lines.append(f"    - {flavor}: {count} 次")

        # 总体统计
        lines.append("\n" + "=" * 80)
        lines.append("【总体统计】")
        lines.append("=" * 80)

        all_new_flavors = Counter()
        for flavors in self.new_flavors_by_category.values():
            all_new_flavors.update(flavors)

        lines.append(f"\n处理商品总数: {self.processed_count}")
        lines.append(f"发现新口味总数: {len(all_new_flavors)}")
        lines.append(f"API调用次数: {self.glm_client.request_count}")
        lines.append(f"缓存命中次数: {self.glm_client.cache_hits}")

        lines.append("\n新口味TOP 30:")
        for flavor, count in all_new_flavors.most_common(30):
            lines.append(f"  {flavor}: {count} 次")

        lines.append("\n口味分布TOP 30:")
        for flavor, count in self.flavor_stats.most_common(30):
            lines.append(f"  {flavor}: {count} 次")

        report_text = "\n".join(lines)

        # 写入文件
        with open(output_file, 'w', encoding='utf-8') as f:
            f.write(report_text)

        print(f"\n报告已保存: {output_file}")
        return report_text


def main():
    """主程序"""
    api_key = os.getenv("GLM_API_KEY")

    if not api_key:
        print("错误: 未设置 GLM_API_KEY 环境变量")
        print("请设置: $env:GLM_API_KEY='your-api-key'")
        return

    # 初始化映射器
    mapper = FlavorMapperStreaming(api_key=api_key)

    # 输入输出路径
    input_path = "data/分类商品0401.csv"
    output_path = "data/分类商品0401_带口味.csv"

    print("=" * 80)
    print("批量商品口味映射 - 实时流式版")
    print("=" * 80)
    print("特点: 每处理一条实时写入CSV，可随时查看进度")
    print("=" * 80)

    # 处理CSV（支持断点续传，自动跳过已处理的记录）
    stats = mapper.process_csv_streaming(input_path, output_path, resume=True)

    print("\n" + "=" * 80)
    print("处理统计")
    print("=" * 80)
    print(f"总商品数: {stats['total']}")
    print(f"跳过已处理: {stats.get('skipped', 0)} 条")
    print(f"本次处理: {stats.get('processed', 0)} 条")
    print(f"识别出口味: {stats['with_flavor']} 条")
    print(f"新口味数量: {stats['new_flavors']} 个")
    print(f"API调用次数: {stats['api_calls']}")
    print(f"缓存命中: {stats['cache_hits']}")
    if stats['elapsed_time'] > 0:
        print(f"处理速度: {stats.get('processed', stats['total'])/stats['elapsed_time']:.1f} 条/秒")

    print("\n口味分布TOP 30:")
    for flavor, count in stats['flavor_stats']:
        print(f"  {flavor}: {count} 次")

    # 生成报告
    print("\n" + "=" * 80)
    report = mapper.generate_report("data/flavor_mapping_report.txt")
    print(report)

    print("\n" + "=" * 80)
    print("全部完成！")
    print(f"输出文件: {output_path}")
    print("=" * 80)


if __name__ == '__main__':
    main()
