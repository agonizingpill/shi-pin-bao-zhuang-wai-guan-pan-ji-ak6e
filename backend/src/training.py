"""训练数据发包的 PostgreSQL 事务实现。

与 ``training_domain`` 的纯函数规则一一对应；并发控制依赖行锁与唯一索引：
- ``one_active_cohort`` 唯一索引兜底重试幂等；
- 膜卷边归属在登记/发包事务内检查并写回；
- 更正与撤销在同一事务内停用相关活跃包，旧下载令牌因状态变化失效。
"""
from fastapi import HTTPException
from psycopg import errors

from .inspection import audit, lot_row
from .training_domain import (ACTIVE, DEACT_ROLL, DEACT_WITHDRAW, HOLDOUT, SIDES, TRAIN,
                              cohort_hash, package_code, sign_download)


def get_measure(db, measure_id, lock=True):
    row = db.execute('SELECT * FROM visual_measure WHERE id=%s' + (' FOR UPDATE' if lock else ''),
                     (measure_id,)).fetchone()
    if not row:
        raise HTTPException(404, '找不到测量样本')
    return row


def current_roll(db, measure_id, lock=False):
    return db.execute('SELECT * FROM film_roll_usage WHERE measure_id=%s AND is_current=true'
                      + (' FOR UPDATE' if lock else ''), (measure_id,)).fetchone()


def roll_side(db, roll_code):
    return db.execute("""SELECT side FROM film_roll_usage WHERE roll_code=%s AND is_current=true
                         AND side IS NOT NULL LIMIT 1""", (roll_code,)).fetchone()


def lot_side(db, lot_id):
    return db.execute("""SELECT DISTINCT side FROM film_roll_usage
                         WHERE lot_id=%s AND is_current=true AND side IS NOT NULL""",
                      (lot_id,)).fetchall()


def register_roll(db, data, actor):
    measure = get_measure(db, data.measure_id)
    if measure['revoked_at']:
        raise HTTPException(409, '样本已撤销，不能再登记膜卷')
    if current_roll(db, measure['id']):
        raise HTTPException(409, '该样本已登记膜卷，卷号有误请走更正流程')
    row = db.execute("""INSERT INTO film_roll_usage(measure_id,lot_id,roll_code,created_by)
                        VALUES (%s,%s,%s,%s) RETURNING *""",
                     (measure['id'], measure['lot_id'], data.roll_code, actor)).fetchone()
    audit(db, measure['lot_id'], actor, 'roll_registered', measure_id=measure['id'],
          sample=measure['sample'], roll_code=data.roll_code)
    return row


def _deactivate_packages(db, measure_id, reason, detail, actor):
    rows = db.execute("""SELECT p.* FROM training_package p
                         JOIN training_package_item i ON i.package_id=p.id
                         WHERE i.measure_id=%s AND p.state='active' FOR UPDATE OF p""",
                      (measure_id,)).fetchall()
    ids = []
    for package in rows:
        db.execute("""UPDATE training_package SET state='inactive',deactivation_reason=%s,
                      deactivation_detail=%s,deactivated_by=%s,deactivated_at=now() WHERE id=%s""",
                   (reason, detail, actor, package['id']))
        ids.append(package['id'])
    return ids


def correct_roll(db, data, actor):
    measure = get_measure(db, data.measure_id)
    current = current_roll(db, measure['id'], lock=True)
    if not current:
        raise HTTPException(409, '该样本尚未登记膜卷，不能更正，请先登记卷号')
    if data.roll_code == current['roll_code']:
        raise HTTPException(422, '新卷号与当前卷号相同，无需更正')
    other = roll_side(db, data.roll_code)
    if other:
        raise HTTPException(409, f'膜卷 {data.roll_code} 已用于'
                                 f'{"训练" if other["side"] == TRAIN else "留出"}包，不能再归另一侧')
    lot_sides = [row['side'] for row in lot_side(db, measure['lot_id'])]
    if len(set(lot_sides)) > 1:
        raise HTTPException(409, '该批次膜卷已分属两侧，数据异常，请先停用相关包')
    detail = f'样本 {measure["sample"]} 卷号由 {current["roll_code"]} 更正为 {data.roll_code}'
    deactivated = _deactivate_packages(db, measure['id'], DEACT_ROLL, detail, actor)
    db.execute('UPDATE film_roll_usage SET is_current=false WHERE id=%s', (current['id'],))
    row = db.execute("""INSERT INTO film_roll_usage(measure_id,lot_id,roll_code,is_current,side,
                            corrected_from_id,created_by)
                        VALUES (%s,%s,%s,true,%s,%s,%s) RETURNING *""",
                     (measure['id'], measure['lot_id'], data.roll_code, current['side'],
                      current['id'], actor)).fetchone()
    audit(db, measure['lot_id'], actor, 'roll_corrected', measure_id=measure['id'],
          sample=measure['sample'], old_roll=current['roll_code'], new_roll=data.roll_code,
          deactivated_packages=deactivated)
    return {'roll': row, 'deactivated_packages': deactivated}


def withdraw_sample(db, data, actor):
    measure = get_measure(db, data.measure_id)
    if measure['revoked_at']:
        raise HTTPException(409, '样本已撤销，请勿重复操作')
    detail = f'样本 {measure["sample"]} 撤销：{data.reason}'
    deactivated = _deactivate_packages(db, measure['id'], DEACT_WITHDRAW, detail, actor)
    db.execute('UPDATE visual_measure SET revoked_at=now(),revoke_reason=%s WHERE id=%s',
               (data.reason, measure['id']))
    audit(db, measure['lot_id'], actor, 'sample_withdrawn', measure_id=measure['id'],
          sample=measure['sample'], reason=data.reason, deactivated_packages=deactivated)
    return {'measure_id': measure['id'], 'deactivated_packages': deactivated}


def _candidate_measures(db, measure_ids):
    """返回可入包样本与跳过原因。缺卷号的先不入包。"""
    chosen, skipped = [], {'missing_roll': [], 'unsigned': [], 'withdrawn': []}
    for measure_id in dict.fromkeys(measure_ids):
        measure = db.execute('SELECT * FROM visual_measure WHERE id=%s', (measure_id,)).fetchone()
        if not measure:
            raise HTTPException(404, f'样本 {measure_id} 不存在')
        if measure['revoked_at']:
            skipped['withdrawn'].append(measure['sample'])
            continue
        signed = db.execute("""SELECT 1 FROM grade_decision WHERE measure_id=%s AND final_grade IS NOT NULL
                               LIMIT 1""", (measure_id,)).fetchone()
        if not signed:
            skipped['unsigned'].append(measure['sample'])
            continue
        if not current_roll(db, measure_id):
            skipped['missing_roll'].append(measure['sample'])
            continue
        chosen.append(measure)
    return chosen, skipped


def publish_package(db, data, actor):
    purpose = str(data.purpose)
    if purpose not in SIDES:
        raise HTTPException(422, '发包用途只能是 train 或 holdout')
    ids = data.unique_ids()
    if not ids:
        raise HTTPException(422, '发包样本集合不能为空')
    digest = cohort_hash(ids)
    # 中断恢复：同 cohort + purpose 的活跃包直接复用，不新增训练包。
    existing = db.execute("""SELECT * FROM training_package WHERE cohort_hash=%s AND purpose=%s
                             AND state='active'""", (digest, purpose)).fetchone()
    if existing:
        return existing, True, {'retried': True}
    chosen, skipped = _candidate_measures(db, ids)
    if not chosen:
        raise HTTPException(422, {'message': '没有可入包的样本：缺卷号的先不入包，且必须已完成人工终判', **skipped})
    opposite = HOLDOUT if purpose == TRAIN else TRAIN
    chosen_rolls = {m['id']: current_roll(db, m['id'])['roll_code'] for m in chosen}
    # 膜卷级互斥。
    conflict_rolls = sorted({
        code for code in set(chosen_rolls.values())
        if db.execute("""SELECT 1 FROM film_roll_usage WHERE roll_code=%s AND is_current=true
                         AND side=%s LIMIT 1""", (code, opposite)).fetchone()
    })
    if conflict_rolls:
        raise HTTPException(409, {'message': f'膜卷已用于{"留出" if purpose == TRAIN else "训练"}包：'
                                             f'{"、".join(conflict_rolls)}', 'rolls': conflict_rolls})
    # 批次级互斥：同批用过的卷都归同一边。
    lot_ids = sorted({m['lot_id'] for m in chosen})
    conflict_lots = []
    for lot_id in lot_ids:
        if db.execute("""SELECT 1 FROM film_roll_usage WHERE lot_id=%s AND is_current=true
                         AND side=%s LIMIT 1""", (lot_id, opposite)).fetchone():
            conflict_lots.append(lot_row(db, lot_id)['code'])
    if conflict_lots:
        raise HTTPException(409, {'message': f'批次膜卷已归另一侧，不能跨边发包：{"、".join(conflict_lots)}',
                                 'lots': conflict_lots})
    seq = db.execute("SELECT nextval(pg_get_serial_sequence('training_package','id')) AS seq").fetchone()['seq']
    code = package_code(seq, purpose)
    roll_codes = sorted(set(chosen_rolls.values()))
    try:
        # savepoint：并发撞唯一索引时回滚到此处，事务仍可查询并复用对方建好的活跃包。
        db.execute('SAVEPOINT publish_package')
        package = db.execute("""INSERT INTO training_package(code,purpose,state,cohort_hash,item_count,roll_count,
                                    created_by)
                                VALUES (%s,%s,'active',%s,%s,%s,%s) RETURNING *""",
                             (code, purpose, digest, len(chosen), len(roll_codes), actor)).fetchone()
    except errors.UniqueViolation:
        db.execute('ROLLBACK TO SAVEPOINT publish_package')
        row = db.execute("""SELECT * FROM training_package WHERE cohort_hash=%s AND purpose=%s
                            AND state='active'""", (digest, purpose)).fetchone()
        if row:
            return row, True, {'retried': True}
        raise
    db.executemany("""INSERT INTO training_package_item(package_id,measure_id,roll_code)
                      VALUES (%(package_id)s,%(measure_id)s,%(roll_code)s)""",
                   [{'package_id': package['id'], 'measure_id': m['id'], 'roll_code': chosen_rolls[m['id']]}
                    for m in chosen])
    # 固定本批涉及膜卷的边归属（同批所有当前卷一起归边）。
    db.execute("""UPDATE film_roll_usage SET side=%s WHERE is_current=true AND side IS NULL
                  AND lot_id = ANY(%s)""", (purpose, lot_ids))
    audit(db, chosen[0]['lot_id'], actor, 'training_package_published',
          package_id=package['id'], code=code, purpose=purpose,
          item_count=package['item_count'], rolls=roll_codes, skipped=skipped)
    return package, False, skipped


# --------------------------------------------------------------------------- #
# 查询
# --------------------------------------------------------------------------- #
def package_list(db):
    return db.execute("""SELECT p.*,
                         (SELECT count(*) FROM training_package_item i WHERE i.package_id=p.id) AS items
                         FROM training_package p ORDER BY p.id DESC""").fetchall()


def package_detail(db, package_id):
    package = db.execute('SELECT * FROM training_package WHERE id=%s', (package_id,)).fetchone()
    if not package:
        raise HTTPException(404, '找不到训练包')
    items = db.execute("""SELECT i.roll_code, m.id AS measure_id, m.sample, m.revision, m.lot_id,
                                 m.revoked_at, m.revoke_reason, l.code AS lot_code, l.product,
                                 d.final_grade, d.model_grade
                          FROM training_package_item i
                          JOIN visual_measure m ON m.id=i.measure_id
                          JOIN pack_lot l ON l.id=m.lot_id
                          LEFT JOIN grade_decision d ON d.measure_id=m.id
                          WHERE i.package_id=%s ORDER BY l.code, m.revision""", (package_id,)).fetchall()
    rolls = db.execute("""SELECT roll_code, count(*) AS samples, array_agg(DISTINCT lot_id) AS lot_ids
                          FROM training_package_item WHERE package_id=%s
                          GROUP BY roll_code ORDER BY roll_code""", (package_id,)).fetchall()
    return {'package': package, 'items': items, 'rolls': rolls}


def roll_registry(db):
    """工作台膜卷登记总览：每个样本的当前卷、更正轨迹、撤销状态与人工终判。"""
    return db.execute("""SELECT m.id AS measure_id, m.sample, m.revision, m.lot_id, l.code AS lot_code,
                                m.revoked_at, m.revoke_reason, d.final_grade,
                                r.id AS usage_id, r.roll_code, r.side, r.corrected_from_id,
                                r.created_at AS registered_at, r.created_by AS registered_by
                         FROM visual_measure m
                         JOIN pack_lot l ON l.id=m.lot_id
                         LEFT JOIN grade_decision d ON d.measure_id=m.id AND d.final_grade IS NOT NULL
                         LEFT JOIN film_roll_usage r ON r.measure_id=m.id AND r.is_current=true
                         ORDER BY l.code, m.revision DESC""").fetchall()


def download_link(db, package_id, actor, secret):
    package = db.execute('SELECT * FROM training_package WHERE id=%s FOR UPDATE', (package_id,)).fetchone()
    if not package:
        raise HTTPException(404, '找不到训练包')
    link = sign_download(package, secret)
    first_lot = db.execute("""SELECT l.id FROM training_package_item i
                              JOIN visual_measure m ON m.id=i.measure_id
                              JOIN pack_lot l ON l.id=m.lot_id
                              WHERE i.package_id=%s ORDER BY l.id LIMIT 1""", (package_id,)).fetchone()
    if first_lot:
        audit(db, first_lot['id'], actor, 'package_link_issued', package_id=package['id'],
              code=package['code'], state=package['state'], expires_at=link['expires_at'])
    return {**package, 'download': link}


def verify_download_token(db, token, secret):
    """下载令牌校验：签名校验 + 显式停用拦截，停用后旧令牌立即失效。"""
    import hashlib
    import hmac
    import time

    try:
        raw_id, raw_expiry, signature = token.split('.')
        package_id, expiry = int(raw_id), int(raw_expiry)
    except (ValueError, AttributeError) as exc:
        raise HTTPException(400, '下载链接格式不正确') from exc
    if expiry < int(time.time()):
        raise HTTPException(410, '下载链接已过期，请重新获取')
    package = db.execute('SELECT * FROM training_package WHERE id=%s', (package_id,)).fetchone()
    if not package:
        raise HTTPException(404, '训练包不存在')
    payload = f'{package_id}:{expiry}'
    expected = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(403, '下载链接签名无效')
    if package['state'] != ACTIVE:
        raise HTTPException(410, '训练包已停用，旧链接不能下载')
    return package


def build_snapshot(db, package):
    """导出包内容快照（读数 + 人工终判标签 + 膜卷），供训练组下载。"""
    detail = package_detail(db, package['id'])
    return {
        'package': {key: package[key] for key in
                    ('id', 'code', 'purpose', 'state', 'item_count', 'roll_count', 'created_at')},
        'label_source': '人工终判 grade_decision.final_grade',
        'items': [{
            'sample': item['sample'], 'lot': item['lot_code'], 'product': item['product'],
            'revision': item['revision'], 'roll_code': item['roll_code'],
            'label': item['final_grade'], 'model_grade': item['model_grade'],
        } for item in detail['items']],
        'rolls': [{key: roll[key] for key in ('roll_code', 'samples', 'lot_ids')} for roll in detail['rolls']],
    }
