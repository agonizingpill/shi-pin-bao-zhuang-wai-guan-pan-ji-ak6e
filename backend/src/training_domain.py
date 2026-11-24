"""训练数据发包的纯业务规则。

本模块不依赖数据库或 Web 框架，全部规则以普通函数实现，便于用标准库
unittest 直接验证。``training.py`` 负责把这些规则落到 PostgreSQL 事务里。

规则来源（质检 → 训练组交接）：
- 人工终判（grade_decision.final_grade）是训练标签，只有已签核样本可入包；
- 样本必须先登记膜卷，缺卷号不进入候选；
- 训练包与留出包不得共用膜卷，同一批次用过的膜卷只能归同一侧；
- 卷号更正与发包并发时，先受理的更正必须计入，且更正会停用相关已发包；
- 样本撤销同样整包停用，停用后旧下载链接立即失效；
- 发包中断后恢复只重试原样本集合，重试不新增训练包（同 cohort 幂等）。
"""
import hashlib
import hmac
import json
import re
import time
from dataclasses import dataclass, field

TRAIN = 'train'
HOLDOUT = 'holdout'
SIDES = (TRAIN, HOLDOUT)
ACTIVE = 'active'
INACTIVE = 'inactive'
DEACT_ROLL = 'roll_corrected'
DEACT_WITHDRAW = 'sample_withdrawn'
DOWNLOAD_TTL_SECONDS = 24 * 3600
ROLL_CODE_PATTERN = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$')


class RuleError(Exception):
    """业务规则冲突，由 API 层映射为 4xx。"""

    def __init__(self, message, status=409, extra=None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.extra = extra or {}


def normalize_roll_code(value):
    code = str(value).strip()
    if not code or not ROLL_CODE_PATTERN.fullmatch(code) or len(code) > 64:
        raise RuleError('卷号格式不正确（1–64 位字母数字或 . _ -）', 422)
    return code


def cohort_hash(measure_ids):
    """同一次发包请求的样本集合指纹，用作重试幂等键（顺序/重复不影响）。"""
    payload = json.dumps(sorted({int(i) for i in measure_ids}), separators=(',', ':'))
    return hashlib.sha256(payload.encode()).hexdigest()


def package_code(seq, purpose):
    return f'TP-{purpose[0].upper()}-{int(seq):05d}'


# --------------------------------------------------------------------------- #
# 内存态规则引擎（与 SQL 实现共用同一套判断），供单测直接驱动
# --------------------------------------------------------------------------- #
@dataclass
class MemoryStore:
    lots: dict = field(default_factory=dict)              # id -> {id, code}
    measures: dict = field(default_factory=dict)          # id -> measure row
    decisions: dict = field(default_factory=dict)         # measure_id -> decision row
    rolls: list = field(default_factory=list)             # film_roll_usage rows
    packages: list = field(default_factory=list)          # training_package rows
    items: list = field(default_factory=list)             # training_package_item rows
    package_seq: int = 0
    roll_seq: int = 0
    events: list = field(default_factory=list)

    def current_roll(self, measure_id):
        return next((r for r in self.rolls if r['measure_id'] == measure_id and r['is_current']), None)

    def active_packages_for_measure(self, measure_id):
        ids = {i['package_id'] for i in self.items if i['measure_id'] == measure_id}
        return [p for p in self.packages if p['id'] in ids and p['state'] == ACTIVE]

    def rolls_for_lot(self, lot_id):
        return [r for r in self.rolls if r['lot_id'] == lot_id and r['is_current']]

    def side_of_roll(self, roll_code):
        for row in self.rolls:
            if row['is_current'] and row['roll_code'] == roll_code and row['side']:
                return row['side']
        return None

    def find_active_cohort(self, digest, purpose):
        return next((p for p in self.packages
                     if p['cohort_hash'] == digest and p['purpose'] == purpose and p['state'] == ACTIVE), None)


def _audit(store, lot_id, actor, action, **details):
    store.events.append({'lot_id': lot_id, 'actor': actor, 'action': action, 'details': details})


def register_roll(store, measure_id, roll_code, actor):
    """登记样本用过的膜卷。缺卷号在 API schema 层拦截，这里再兜一次。"""
    measure = store.measures.get(int(measure_id))
    if not measure:
        raise RuleError('找不到测量样本', 404)
    roll_code = normalize_roll_code(roll_code)
    if store.current_roll(measure['id']):
        raise RuleError('该样本已登记膜卷，卷号有误请走更正流程')
    if measure.get('revoked_at'):
        raise RuleError('样本已撤销，不能再登记膜卷')
    store.roll_seq += 1
    row = {'id': store.roll_seq, 'measure_id': measure['id'], 'lot_id': measure['lot_id'],
           'roll_code': roll_code, 'is_current': True, 'side': None,
           'corrected_from_id': None, 'created_by': actor}
    store.rolls.append(row)
    _audit(store, measure['lot_id'], actor, 'roll_registered', measure_id=measure['id'],
           sample=measure['sample'], roll_code=roll_code)
    return row


def correct_roll(store, measure_id, new_roll_code, actor):
    """卷号更正：先受理的更正先落库；更正使相关已发包整包停用。

    若新卷号已在对侧使用，或本批次膜卷已归对侧，则拒绝更正——训练/留出
    不能共用膜卷、同批卷只能归同一边。
    """
    measure = store.measures.get(int(measure_id))
    if not measure:
        raise RuleError('找不到测量样本', 404)
    current = store.current_roll(measure['id'])
    if not current:
        raise RuleError('该样本尚未登记膜卷，不能更正，请先登记卷号')
    new_code = normalize_roll_code(new_roll_code)
    if new_code == current['roll_code']:
        raise RuleError('新卷号与当前卷号相同，无需更正', 422)
    lot_sides = {r['side'] for r in store.rolls_for_lot(measure['lot_id']) if r['side']}
    roll_side = store.side_of_roll(new_code)
    if roll_side:
        raise RuleError(f'膜卷 {new_code} 已用于{"训练" if roll_side == TRAIN else "留出"}包，不能再归另一侧')
    if len(lot_sides) > 1:
        raise RuleError('该批次膜卷已分属两侧，数据异常，请先停用相关包')
    # 停用所有包含该样本的活跃包（先受理的更正计入，发包在此之后看到的就是新卷）。
    deactivated = [p['id'] for p in store.active_packages_for_measure(measure['id'])]
    for package in store.packages:
        if package['id'] in deactivated:
            package['state'] = INACTIVE
            package['deactivation_reason'] = DEACT_ROLL
            package['deactivation_detail'] = (
                f'样本 {measure["sample"]} 卷号由 {current["roll_code"]} 更正为 {new_code}')
            package['deactivated_by'] = actor
            package['deactivated_at'] = 'now'
    # 旧登记转为历史，插入新的当前登记并沿用旧边归属。
    current['is_current'] = False
    store.roll_seq += 1
    row = {'id': store.roll_seq, 'measure_id': measure['id'], 'lot_id': measure['lot_id'],
           'roll_code': new_code, 'is_current': True, 'side': current['side'],
           'corrected_from_id': current['id'], 'created_by': actor}
    store.rolls.append(row)
    _audit(store, measure['lot_id'], actor, 'roll_corrected', measure_id=measure['id'],
           sample=measure['sample'], old_roll=current['roll_code'], new_roll=new_code,
           deactivated_packages=deactivated)
    return row, deactivated


def withdraw_sample(store, measure_id, reason, actor):
    """样本撤销：含该样本的活跃包整包停用，旧链接随之失效。"""
    measure = store.measures.get(int(measure_id))
    if not measure:
        raise RuleError('找不到测量样本', 404)
    if measure.get('revoked_at'):
        raise RuleError('样本已撤销，请勿重复操作')
    measure['revoked_at'] = 'now'
    measure['revoke_reason'] = reason
    deactivated = [p['id'] for p in store.active_packages_for_measure(measure['id'])]
    for package in store.packages:
        if package['id'] in deactivated:
            package['state'] = INACTIVE
            package['deactivation_reason'] = DEACT_WITHDRAW
            package['deactivation_detail'] = f'样本 {measure["sample"]} 撤销：{reason}'
            package['deactivated_by'] = actor
            package['deactivated_at'] = 'now'
    _audit(store, measure['lot_id'], actor, 'sample_withdrawn', measure_id=measure['id'],
           sample=measure['sample'], reason=reason, deactivated_packages=deactivated)
    return deactivated


def _signed_decision(store, measure):
    decision = store.decisions.get(measure['id'])
    return decision if decision and decision.get('final_grade') else None


def eligible_measures(store, measure_ids):
    """校验候选样本：存在、未撤销、已签核有人工终判、已登记当前膜卷。"""
    chosen, missing_roll, unsigned, withdrawn = [], [], [], []
    seen = set()
    for measure_id in measure_ids:
        measure_id = int(measure_id)
        if measure_id in seen:
            continue
        seen.add(measure_id)
        measure = store.measures.get(measure_id)
        if not measure:
            raise RuleError(f'样本 {measure_id} 不存在', 404)
        if measure.get('revoked_at'):
            withdrawn.append(measure['sample'])
            continue
        if not _signed_decision(store, measure):
            unsigned.append(measure['sample'])
            continue
        if not store.current_roll(measure_id):
            missing_roll.append(measure['sample'])
            continue
        chosen.append(measure)
    return chosen, {'missing_roll': missing_roll, 'unsigned': unsigned, 'withdrawn': withdrawn}


def create_package(store, measure_ids, purpose, actor):
    """按样本集合发训练包/留出包。

    互斥规则：
    - 同 cohort + purpose 已有活跃包时直接返回（中断恢复重试，不新增包）；
    - 任一膜卷已在对侧包使用 → 拒绝；
    - 同批次膜卷只能归同一边，跨边冲突 → 拒绝。
    通过后把本批涉及的膜卷固定到 purpose 一侧。
    """
    if purpose not in SIDES:
        raise RuleError('发包用途只能是 train 或 holdout', 422)
    if not measure_ids:
        raise RuleError('发包样本集合不能为空', 422)
    digest = cohort_hash(measure_ids)
    existing = store.find_active_cohort(digest, purpose)
    if existing:
        return existing, True, {'retried': True}
    chosen, skipped = eligible_measures(store, measure_ids)
    if not chosen:
        raise RuleError('没有可入包的样本：缺卷号的先不入包，且必须已完成人工终判', 422, skipped)
    opposite = HOLDOUT if purpose == TRAIN else TRAIN
    # 膜卷级互斥：训练与留出不能共用膜卷。
    chosen_rolls = {m['id']: store.current_roll(m['id'])['roll_code'] for m in chosen}
    conflicts = sorted({code for code in chosen_rolls.values() if store.side_of_roll(code) == opposite})
    if conflicts:
        raise RuleError(f'膜卷已用于{"留出" if purpose == TRAIN else "训练"}包：{"、".join(conflicts)}', 409)
    # 批次级互斥：同批用过的卷都归同一边。
    lot_conflicts = []
    for measure in chosen:
        for roll in store.rolls_for_lot(measure['lot_id']):
            if roll['side'] == opposite:
                lot_conflicts.append(store.lots[measure['lot_id']]['code'])
                break
    if lot_conflicts:
        raise RuleError(f'批次膜卷已归另一侧，不能跨边发包：{"、".join(sorted(set(lot_conflicts)))}', 409)
    store.package_seq += 1
    roll_codes = sorted(set(chosen_rolls.values()))
    package = {'id': store.package_seq, 'code': package_code(store.package_seq, purpose),
               'purpose': purpose, 'state': ACTIVE, 'cohort_hash': digest,
               'item_count': len(chosen), 'roll_count': len(roll_codes),
               'deactivation_reason': None, 'deactivation_detail': None,
               'deactivated_by': None, 'deactivated_at': None,
               'created_by': actor, 'created_at': 'now'}
    store.packages.append(package)
    for measure in chosen:
        store.items.append({'package_id': package['id'], 'measure_id': measure['id'],
                            'roll_code': chosen_rolls[measure['id']]})
    # 固定本批膜卷（含同批历史登记当前卷）的边归属。
    touched_lots = {m['lot_id'] for m in chosen}
    for lot_id in touched_lots:
        for roll in store.rolls_for_lot(lot_id):
            if roll['side'] is None:
                roll['side'] = purpose
    _audit(store, chosen[0]['lot_id'], actor, 'training_package_published',
           package_id=package['id'], code=package['code'], purpose=purpose,
           item_count=package['item_count'], rolls=roll_codes, skipped=skipped)
    return package, False, skipped


# --------------------------------------------------------------------------- #
# 下载链接：停用即失效的签名令牌
# --------------------------------------------------------------------------- #
def sign_download(package, secret, now=None):
    now = int(time.time()) if now is None else int(now)
    expiry = now + DOWNLOAD_TTL_SECONDS
    # 签名只绑定包 id 与过期时间；停用状态由 verify 显式拦截，返回明确的 410。
    payload = f'{package["id"]}:{expiry}'
    signature = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return {'token': f'{package["id"]}.{expiry}.{signature}', 'expires_at': expiry}


def verify_download(store, token, secret, now=None):
    """校验下载令牌；包已停用则旧链接也不能下载。"""
    try:
        raw_package_id, raw_expiry, signature = token.split('.')
        package_id, expiry = int(raw_package_id), int(raw_expiry)
    except (ValueError, AttributeError) as exc:
        raise RuleError('下载链接格式不正确', 400) from exc
    now = int(time.time()) if now is None else int(now)
    if expiry < now:
        raise RuleError('下载链接已过期，请重新获取', 410)
    package = next((p for p in store.packages if p['id'] == package_id), None)
    if not package:
        raise RuleError('训练包不存在', 404)
    expected_payload = f'{package_id}:{expiry}'
    expected = hmac.new(secret.encode(), expected_payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise RuleError('下载链接签名无效', 403)
    if package['state'] != ACTIVE:
        raise RuleError('训练包已停用，旧链接不能下载', 410)
    return package
