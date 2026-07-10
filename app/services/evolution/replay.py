"""AI 自进化引擎 · 被动复盘模块（推理回溯 + 诊断摘要生成）。

当用户覆盖 AI 建议后，本模块：
1. 从用户纠正备注中提取品类词 → 应匹配类目的语义规则
2. 对比标题信号 vs AI 推荐类目 vs 人工选择类目，诊断差异
3. 生成结构化 ReplayResult（findings + optimizations + learned_rule）
4. 自动生成可部署的品类映射补丁（keyword_boost 类型）

核心理念：用户写的纠正备注 = 教材，系统自动从中学习。
"""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from app.services.evolution.types import (
    ReplayFinding,
    ReplayFindingType,
    ReplayFindingSeverity,
    ReplayOptimization,
    ReplayOptimizationType,
    ReplayResult,
)


# ─── 品类词表（对齐 category_mapper.py） ───

# 非商品噪声词（标题里的营销/测试/形象词，不应作为品类信号）
SEMANTIC_NOISE_WORDS = frozenset({
    "测试", "形象", "模特", "代言", "广告", "展示",
    "样品", "样衣", "参考", "图片", "实物", "实拍",
})

# 属性/工艺/装饰词（出现在标题但不代表申报品类本身）
ATTRIBUTE_TERMS = frozenset({
    "刺绣", "绣花", "印花", "镶钻", "钉珠", "烫钻", "水钻", "玻璃钻", "亮片", "珠片",
    "流苏", "蕾丝", "网纱", "雪纺", "拼接", "拼色", "不对称", "波点", "条纹", "格子",
    "碎花", "褶皱", "镂空", "勾花", "钩花", "宽松", "修身", "显瘦", "收腰", "高腰",
    "气质", "优雅", "复古", "时尚", "百搭", "新款", "爆款", "热卖", "潮款",
    "欧美", "跨境", "外贸", "中东", "长袖", "短袖", "中袖", "无袖", "七分",
    "高领", "圆领", "立领", "翻领", "方领",
})

# 人群/场景词（不代表品类）
AUDIENCE_TERMS = frozenset({
    "孕妇", "产妇", "孕妈", "哺乳", "月子", "男士", "女士", "男款", "女款",
    "成人", "儿童", "婴童", "学生", "中老年", "青少年",
})

# 常见商品品类核心词（用来从标题中提取品类信号）
PRODUCT_CATEGORY_TERMS = frozenset({
    # 上衣类
    "T恤", "衬衫", "打底衫", "上衣", "外套", "毛衣", "针织衫", "卫衣", " polo",
    "夹克", "风衣", "大衣", "棉衣", "羽绒服", "马甲", "开衫", "套头衫", "吊带",
    # 下衣类
    "裤子", "裤", "牛仔裤", "短裤", "长裤", "阔腿裤", "运动裤", "休闲裤", "裙裤",
    "裙子", "裙", "连衣裙", "半身裙", "短裙", "长裙", "百褶裙", "A字裙",
    # 内衣/家居
    "内衣", "内裤", "文胸", "胸罩", "睡衣", "家居服", "袜子", "袜", "保暖内衣",
    # 鞋类
    "鞋", "凉鞋", "拖鞋", "靴", "运动鞋", "高跟鞋", "平底鞋", "皮鞋",
    # 配饰
    "帽", "围巾", "手套", "皮带", "领带", "包", "背包", "手提包",
    # 制服/工作服
    "制服", "工作服", "职业装", "演出服", "校服", "护士服", "厨师服",
    "保安服", "军装", "警服", "消防服", "西装", "西服", "正装", "礼服",
})

# 合并所有"非品类词"集合
NON_PRODUCT_WORDS = SEMANTIC_NOISE_WORDS | ATTRIBUTE_TERMS | AUDIENCE_TERMS


# ─── 纠正备注解析 ───


def _parse_reviewer_note(note: str, title: str) -> dict[str, Any]:
    """从用户纠正备注中提取语义规则。

    支持的模式：
    - "标题有/含 X Y Z 等品类词" → 品类关键词
    - "AI推荐的是 A B 这种非品类词" → AI错误信号
    - "应/应该/优先匹配/优先推荐 C D" → 正确目标品类
    - "品类识别错误/品类词未命中" → 通用品类信号缺失标识
    """
    result: dict[str, Any] = {
        "user_category_keywords": [],   # 用户明确提到的品类词
        "user_wrong_signals": [],       # 用户明确指出AI推荐的错误词
        "user_target_keywords": [],     # 用户期望匹配的品类方向
        "is_category_error": False,     # 用户认为品类识别出错
        "note_signals": [],             # 从备注中提取的所有有意义的词
    }

    if not note:
        return result

    note_lower = note.lower()

    # 1. 检测品类识别错误类表述
    category_error_patterns = ["品类识别错误", "品类词未命中", "非品类词", "品类词", "没有命中"]
    for p in category_error_patterns:
        if p in note_lower:
            result["is_category_error"] = True
            break

    # 2. 提取"标题有/含 X Y Z"中的品类词
    has_pattern = re.findall(
        r"标题[有含]\s*([^\u3001，,但而的]+?)(?:\s*等\s*品类词|\s*品类词|\s*等)",
        note
    )
    if has_pattern:
        for phrase in has_pattern:
            words = re.findall(r"[\u4e00-\u9fff]{2,4}|T恤|polo", phrase)
            result["user_category_keywords"].extend(
                w for w in words if w not in NON_PRODUCT_WORDS
            )

    # 3. 提取"AI推荐的是 X Y 这种非品类词"中的错误信号
    ai_pattern = re.findall(
        r"AI[推荐建议的是]+\s*([^\u3001，,但而这种]+?)(?:\s*这种\s*非品类词|\s*非品类|\s*等)",
        note
    )
    if ai_pattern:
        for phrase in ai_pattern:
            words = re.findall(r"[\u4e00-\u9fff]{2,4}", phrase)
            result["user_wrong_signals"].extend(words)

    # 4. 提取"应/应该/优先匹配/优先推荐 X Y"中的目标品类
    target_pattern = re.findall(
        r"(?:应|应该|优先匹配|优先推荐|优先|优先推荐到)\s*([^\u3001，,但而的]+?)(?:\s*品类|\s*类目|\s*等|$)",
        note
    )
    if target_pattern:
        for phrase in target_pattern:
            words = re.findall(r"[\u4e00-\u9fff]{2,4}", phrase)
            result["user_target_keywords"].extend(words)

    # 5. 从备注中直接提取所有品类词（不依赖语法模式）
    # 扫描备注中出现的商品品类词
    for term in PRODUCT_CATEGORY_TERMS:
        if term in note and term not in result["user_category_keywords"]:
            result["note_signals"].append(term)

    # 6. 去重
    result["user_category_keywords"] = list(dict.fromkeys(result["user_category_keywords"]))
    result["user_wrong_signals"] = list(dict.fromkeys(result["user_wrong_signals"]))
    result["user_target_keywords"] = list(dict.fromkeys(result["user_target_keywords"]))
    result["note_signals"] = list(dict.fromkeys(result["note_signals"]))

    return result


def _extract_product_signals_from_title(title: str) -> list[str]:
    """从标题中提取品类核心信号词（排除属性词、噪声词、人群词）。

    这是复盘的关键：找到标题里真正代表商品品类的词。
    只从品类词表中匹配，不做贪婪正则拆分（避免产生「螺纹半高领打」这种垃圾）。
    """
    if not title:
        return []

    # 只匹配品类词表中的已知品类词
    found = []
    for term in PRODUCT_CATEGORY_TERMS:
        if term in title and term not in found:
            found.append(term)

    return found[:12]


def _extract_category_keywords(category_name: str) -> list[str]:
    """从类目名中提取关键词（如 'lolita衬衫/内搭' → ['lolita衬衫', '衬衫', '内搭']）。"""
    if not category_name:
        return []
    parts = re.split(r"[/／>｜|]", category_name)
    keywords = []
    for p in parts:
        p = p.strip()
        if len(p) >= 2:
            keywords.append(p)
            # 进一步拆分长词
            sub_tokens = re.findall(r"[\u4e00-\u9fff]{2,4}", p)
            for s in sub_tokens:
                if s not in keywords and s not in NON_PRODUCT_WORDS:
                    keywords.append(s)
    return keywords[:10]


# ─── 品类映射复盘 ───


def replay_category_mapping(
    title: str,
    ai_suggestion: str,
    human_correction: str,
    correction_value: Optional[str] = None,
    reviewer_note: Optional[str] = None,
) -> ReplayResult:
    """品类映射复盘：从标题信号、AI推荐、人工选择、纠正备注中诊断差异并生成规则。

    核心逻辑：
    1. 提取标题中的品类核心词
    2. 解析用户纠正备注（如果有），提取用户明确指出的品类词和问题
    3. 对比：标题品类词 vs AI推荐类目词 vs 人工选择类目词
    4. 诊断：哪些品类词标题有但AI没匹配到？AI推荐了哪些标题不存在的词？
    5. 生成规则：标题含[X词] → 优先匹配[Y类目]
    """
    findings: list[ReplayFinding] = []
    optimizations: list[ReplayOptimization] = []
    patch_generated = False
    patch_id: Optional[str] = None
    learned_rule: Optional[str] = None

    # ─── 1. 纠正摘要 ───
    correction_summary = f"{ai_suggestion} → {human_correction}"

    # ─── 2. 提取标题品类信号 ───
    title_product_words = _extract_product_signals_from_title(title)

    # ─── 3. 解析纠正备注 ───
    note_analysis = _parse_reviewer_note(reviewer_note or "", title)

    # ─── 4. 提取 AI 推荐 / 人工选择 的类目关键词 ───
    ai_keywords = _extract_category_keywords(ai_suggestion)
    human_keywords = _extract_category_keywords(human_correction)

    # ─── 5. 诊断：标题品类词 vs AI 类目词 ───

    # 5a. 标题品类词在人工类目中命中但在AI类目中缺失 → 核心诊断
    title_words_in_human = [w for w in title_product_words if any(w in hk for hk in human_keywords)]
    title_words_not_in_ai = [w for w in title_product_words if not any(w in ak or ak in w for ak in ai_keywords)]
    # 标题品类词既不在AI也不在人工类目名 → 可能是映射粒度问题
    title_words_in_both_miss = [w for w in title_product_words if not any(w in hk for hk in human_keywords) and not any(w in ak or ak in w for ak in ai_keywords)]

    # 5b. AI 推荐了标题中不存在的品类词 → 错误推荐信号
    ai_words_not_in_title = [ak for ak in ai_keywords if ak not in title and ak not in NON_PRODUCT_WORDS and len(ak) >= 2]

    # 5c. 用户备注中明确提到的品类词
    user_mentioned = note_analysis["user_category_keywords"] or note_analysis["note_signals"]
    # 用户备注中明确提到的AI错误词
    user_wrong = note_analysis["user_wrong_signals"]

    # ─── 6. 生成诊断发现 ───

    # 发现1: 标题品类词未被AI匹配到
    missed_words = [w for w in title_product_words if w in title_words_not_in_ai and w not in NON_PRODUCT_WORDS]
    if missed_words:
        # 优先使用用户明确指出的品类词
        emphasis_words = [w for w in user_mentioned if w in title] if user_mentioned else missed_words
        if not emphasis_words:
            emphasis_words = missed_words[:4]
        findings.append(ReplayFinding(
            type=ReplayFindingType.MISSING_SIGNAL,
            severity=ReplayFindingSeverity.CRITICAL,
            description=f"标题品类词「{', '.join(emphasis_words)}」未被 AI 推荐匹配到",
        ))

    # 发现2: AI 推荐了标题不存在/非品类的词
    wrong_signals = ai_words_not_in_title
    if user_wrong:
        # 优先使用用户明确指出的错误词
        wrong_signals = [w for w in user_wrong if w in ai_keywords] + [w for w in ai_words_not_in_title if w not in user_wrong]
    if wrong_signals:
        findings.append(ReplayFinding(
            type=ReplayFindingType.NOISE_INTERFERENCE,
            severity=ReplayFindingSeverity.MAJOR,
            description=f"AI 推荐了标题中不存在的词「{', '.join(wrong_signals[:4])}」，可能为非品类信号",
        ))

    # 发现3: 标题品类词在人工选择类目中命中但AI没选中 → 排序失误
    title_words_hit_human = [w for w in title_product_words if any(w in hk for hk in human_keywords)]
    title_words_miss_ai = [w for w in title_words_hit_human if not any(w in ak or ak in w for ak in ai_keywords)]
    if title_words_miss_ai:
        findings.append(ReplayFinding(
            type=ReplayFindingType.RANKING_FAILURE,
            severity=ReplayFindingSeverity.MAJOR,
            description=f"标题品类词「{', '.join(title_words_miss_ai)}」在人工选择类目中命中，但 AI 推荐类目未命中这些词",
        ))

    # 发现4: AI推荐了无HS码的类目
    if correction_value and ai_keywords:
        # 检查AI推荐的最后一部分是否是有效HS码
        ai_last = ai_keywords[-1] if ai_keywords else ""
        if not _looks_like_hs_code(ai_last) and not _looks_like_hs_code(correction_value):
            # 两者都没有有效HS码 → 需要更细粒度映射
            pass
        elif not _looks_like_hs_code(ai_last) and _looks_like_hs_code(correction_value):
            findings.append(ReplayFinding(
                type=ReplayFindingType.RANKING_FAILURE,
                severity=ReplayFindingSeverity.MAJOR,
                description=f"AI 推荐「{ai_keywords[0]}」无有效 HS 码，人工选择有 HS {correction_value}",
            ))

    # 发现5: 用户备注明确表示品类识别错误
    if note_analysis["is_category_error"]:
        findings.append(ReplayFinding(
            type=ReplayFindingType.MISSING_SIGNAL,
            severity=ReplayFindingSeverity.CRITICAL,
            description="用户明确指出品类识别错误，品类词未被正确映射",
        ))

    # ─── 7. 生成优化方向 + 学习规则 ───

    # 合并品类词：用户指出的 + 标题中提取的
    rule_keywords = list(dict.fromkeys(
        (user_mentioned if user_mentioned else missed_words[:6])
        + [w for w in title_product_words if w in title_words_hit_human and w not in (user_mentioned or missed_words[:6])]
    ))[:8]

    # 目标类目判断：
    # - 如果人工选择了不同的类目 → 用人工类目作为目标
    # - 如果人工选择和AI一样（驳回场景）→ 从备注中提取用户期望的品类方向
    # - 如果两者都没有有效目标 → 标注为"需进一步确认"
    is_rejection = (human_correction == ai_suggestion) or (not correction_value) or (not _looks_like_hs_code(correction_value or "") and not _looks_like_hs_code(ai_keywords[-1] if ai_keywords else ""))
    if not is_rejection:
        target_category = human_correction
    else:
        # 驳回场景：用户没选替代类目，但备注可能指出了正确方向
        target_from_note = note_analysis["user_target_keywords"]
        if target_from_note:
            target_category = f"{target_from_note[0]}类目（需确认具体HS）"
        elif user_mentioned:
            # 用户指出了品类词但没说应该映射到什么 → 用品类词本身作为方向指引
            target_category = f"含{user_mentioned[0]}等品类词的相关类目（需确认具体HS）"
        else:
            target_category = "需人工确认正确映射类目"

    if rule_keywords:
        learned_rule = f"标题含「{', '.join(rule_keywords)}」→ 优先匹配「{target_category}」"
        optimizations.append(ReplayOptimization(
            type=ReplayOptimizationType.RULE_BOOST,
            description=f"标题含品类词「{', '.join(rule_keywords)}」时，优先匹配「{target_category.split('/')[0]}」类目",
        ))

    # 如果有噪声词渗入
    if wrong_signals or user_wrong:
        wrong_list = list(dict.fromkeys((user_wrong or []) + wrong_signals))[:4]
        optimizations.append(ReplayOptimization(
            type=ReplayOptimizationType.NOISE_FILTER,
            description=f"过滤非品类词「{', '.join(wrong_list)}」，不再作为主品类候选",
        ))

    # 如果标题品类词在AI类目中缺失但在人工类目中命中
    if title_words_miss_ai:
        optimizations.append(ReplayOptimization(
            type=ReplayOptimizationType.DOMAIN_SIGNAL,
            description=f"增强品类词「{', '.join(title_words_miss_ai)}」的类目信号权重，确保这些词在候选池中优先排序",
        ))

    # ─── 8. 自动生成补丁 ───
    if optimizations:
        patch_generated = True
        patch_id = f"patch-replay-{uuid.uuid4().hex[:8]}"
        _save_replay_patch(
            patch_id, title, findings, optimizations,
            learned_rule=learned_rule,
            rule_keywords=rule_keywords,
            target_category=target_category,
        )

    return ReplayResult(
        correction_summary=correction_summary,
        findings=findings,
        optimizations=optimizations,
        patch_generated=patch_generated,
        patch_id=patch_id,
        learned_rule=learned_rule,
    )


def replay_skill(
    skill_id: str,
    title: str,
    ai_suggestion: str,
    human_correction: str,
    correction_value: Optional[str] = None,
    context_ref: Optional[str] = None,
    reviewer_note: Optional[str] = None,
) -> ReplayResult:
    """通用复盘入口：按 skill_id 分发到对应的复盘逻辑。

    当前仅实现 category-mapping 的复盘，后续可扩展其他技能。
    """
    if skill_id == "category-mapping":
        return replay_category_mapping(
            title, ai_suggestion, human_correction, correction_value, reviewer_note,
        )
    # 其他技能暂未实现复盘逻辑
    return ReplayResult(
        correction_summary=f"{ai_suggestion} → {human_correction}",
        findings=[ReplayFinding(
            type=ReplayFindingType.MISSING_SIGNAL,
            severity=ReplayFindingSeverity.MINOR,
            description=f"技能 {skill_id} 的复盘逻辑尚未实现",
        )],
    )


# ─── 内部工具函数 ───


def _looks_like_hs_code(s: str) -> bool:
    """检查字符串是否看起来像 HS 编码（6-10 位数字）。"""
    return bool(re.match(r"^\d{6,10}$", s.strip()))


def _save_replay_patch(
    patch_id: str,
    title: str,
    findings: list[ReplayFinding],
    optimizations: list[ReplayOptimization],
    learned_rule: Optional[str] = None,
    rule_keywords: Optional[list[str]] = None,
    target_category: Optional[str] = None,
) -> None:
    """将复盘生成的品类映射补丁保存到 JSONL。

    补丁格式对齐 category_heuristics.py 的学习机制：
    - keyword_boost 类型：标题含特定品类词 → boost 特定类目
    - 后续 category_mapper.py 可加载此类补丁作为 run-time boost
    """
    data_dir = os.environ.get("EVOLUTION_DATA_DIR", "data/evolution")
    os.makedirs(data_dir, exist_ok=True)
    patch_path = os.path.join(data_dir, "patches.jsonl")

    content = "; ".join(o.description for o in optimizations)

    # 结构化 payload：keyword_boost 补丁的机器可读数据
    payload = {
        "title_trigger": title,
        "finding_types": [f.type.value for f in findings],
        "optimization_types": [o.type.value for o in optimizations],
        "learned_rule": learned_rule,
        "keyword_boost": {
            "trigger_keywords": rule_keywords or [],
            "target_category_name": (target_category or "").split("/")[0],
            "target_hs_code": "",   # 从 correction_value 可后续填充
            "boost_factor": 0.28,   # 对齐 domain_alignment_bonus 的加成量
        },
        "noise_filter": {
            "filtered_keywords": list(dict.fromkeys(
                [f.type.value for f in findings if f.type == ReplayFindingType.NOISE_INTERFERENCE]
            )),
        },
    }

    entry = {
        "id": patch_id,
        "type": "keyword_boost",        # 新补丁类型：品类词 → 类目加成
        "target_skill_id": "category-mapping",
        "domain": "product_processing",
        "content": content,
        "payload": payload,
        "learned_rule": learned_rule,
        "status": "draft",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "created_by": "auto_replay",
        "active": False,
    }

    with open(patch_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
