"""训练发包业务规则的全场景验证（仅用标准库，可直接 python -m unittest）。

覆盖需求：
1. 按批登记膜卷，缺卷号不入包；
2. 训练/留出不共用膜卷，同批膜卷只归同一边；
3. 卷号更正先受理先落库，并停用关联已发包、旧链接失效；
4. 样本撤销整包停用、旧链接失效；
5. 发包中断恢复重试原样本集合，不新增训练包；
6. 工作台查询包内样本、膜卷、停用原因；
7. 下载令牌签名、过期与停用失效。
"""
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.training_domain import (ACTIVE, DEACT_ROLL, DEACT_WITHDRAW, HOLDOUT, INACTIVE, TRAIN,
                                 MemoryStore, RuleError, cohort_hash, correct_roll, create_package,
                                 eligible_measures, normalize_roll_code, register_roll,
                                 sign_download, verify_download, withdraw_sample)

SECRET = 'unit-test-secret'


def make_store(lots=2, measures_per_lot=2):
    store = MemoryStore()
    for lot_id in range(1, lots + 1):
        store.lots[lot_id] = {'id': lot_id, 'code': f'PK-{lot_id:03d}'}
        for j in range(1, measures_per_lot + 1):
            measure_id = (lot_id - 1) * measures_per_lot + j
            store.measures[measure_id] = {
                'id': measure_id, 'lot_id': lot_id, 'sample': f'PK-{lot_id:03d}-S{j:02d}',
                'revision': 1, 'revoked_at': None, 'revoke_reason': None,
            }
            store.decisions[measure_id] = {'final_grade': 'A' if j == 1 else 'B',
                                           'model_grade': 'A'}
    return store


def seed_rolls(store, roll_of_measure):
    for measure_id, code in roll_of_measure.items():
        register_roll(store, measure_id, code, 'operator')


class RollRegistrationTests(unittest.TestCase):
    def setUp(self):
        self.store = make_store()

    def test_register_and_duplicate_rejected(self):
        row = register_roll(self.store, 1, 'RL-01', 'operator')
        self.assertEqual(row['roll_code'], 'RL-01')
        self.assertTrue(row['is_current'])
        with self.assertRaises(RuleError) as ctx:
            register_roll(self.store, 1, 'RL-02', 'operator')
        self.assertEqual(ctx.exception.status, 409)

    def test_missing_roll_kept_out_of_package(self):
        seed_rolls(self.store, {1: 'RL-01'})  # 样本 2/3/4 缺卷号
        chosen, skipped = eligible_measures(self.store, [1, 2, 3, 4])
        self.assertEqual([m['id'] for m in chosen], [1])
        self.assertEqual(skipped['missing_roll'], ['PK-001-S02', 'PK-002-S01', 'PK-002-S02'])

    def test_unsigned_and_revoked_excluded(self):
        seed_rolls(self.store, {1: 'RL-01', 2: 'RL-02', 3: 'RL-03'})
        del self.store.decisions[2]  # 未人工终判
        withdraw_sample(self.store, 3, '误采', 'supervisor')  # 先登记再撤销
        chosen, skipped = eligible_measures(self.store, [1, 2, 3])
        self.assertEqual([m['id'] for m in chosen], [1])
        self.assertEqual(skipped['unsigned'], ['PK-001-S02'])
        self.assertEqual(skipped['withdrawn'], ['PK-002-S01'])

    def test_roll_code_normalized(self):
        self.assertEqual(normalize_roll_code('  RL-07 '), 'RL-07')
        with self.assertRaises(RuleError):
            normalize_roll_code('坏卷号!')

    def test_register_on_unknown_measure_404(self):
        with self.assertRaises(RuleError) as ctx:
            register_roll(self.store, 99, 'RL-X', 'operator')
        self.assertEqual(ctx.exception.status, 404)


class SplitExclusivityTests(unittest.TestCase):
    def setUp(self):
        # 两个批次，每批两个样本，同批共用一卷，跨批不同卷。
        self.store = make_store()
        seed_rolls(self.store, {1: 'RL-A', 2: 'RL-A', 3: 'RL-B', 4: 'RL-B'})

    def test_train_package_pins_rolls_and_lot(self):
        package, retried, _ = create_package(self.store, [1, 2], TRAIN, 'supervisor')
        self.assertFalse(retried)
        self.assertEqual(package['state'], ACTIVE)
        self.assertEqual(package['roll_count'], 1)
        self.assertEqual(self.store.side_of_roll('RL-A'), TRAIN)
        self.assertEqual({r['side'] for r in self.store.rolls_for_lot(1)}, {TRAIN})

    def test_holdout_cannot_share_roll(self):
        create_package(self.store, [1, 2], TRAIN, 'supervisor')
        # 样本 1/2 的卷 RL-A 已归训练，再进留出必须拒绝。
        with self.assertRaises(RuleError) as ctx:
            create_package(self.store, [1, 3], HOLDOUT, 'supervisor')
        self.assertIn('RL-A', ctx.exception.message)

    def test_lot_wholesale_side_assignment(self):
        # 同批两个样本使用不同卷，先让批次1 的一卷归训练，另一卷再进留出必须被拦截
        # （整批归边后可能由膜卷级或批次级规则拦截，二者都满足互斥要求）。
        store = make_store(lots=1, measures_per_lot=2)
        seed_rolls(store, {1: 'RL-A', 2: 'RL-A2'})
        create_package(store, [1], TRAIN, 'supervisor')
        with self.assertRaises(RuleError):
            create_package(store, [2], HOLDOUT, 'supervisor')
        self.assertTrue(all(p['purpose'] != HOLDOUT for p in store.packages))
        self.assertEqual({r['side'] for r in store.rolls}, {TRAIN})

    def test_clean_holdout_on_disjoint_rolls(self):
        create_package(self.store, [1, 2], TRAIN, 'supervisor')
        package, _, skipped = create_package(self.store, [3, 4], HOLDOUT, 'supervisor')
        self.assertEqual(package['purpose'], HOLDOUT)
        self.assertEqual(skipped['missing_roll'], [])
        self.assertEqual(self.store.side_of_roll('RL-B'), HOLDOUT)

    def test_duplicate_measure_ids_deduped(self):
        digest_a = cohort_hash([1, 2])
        digest_b = cohort_hash([2, 1, 1])
        self.assertEqual(digest_a, digest_b)
        package, _, _ = create_package(self.store, [1, 1, 2], TRAIN, 'supervisor')
        self.assertEqual(package['item_count'], 2)


class CorrectionTests(unittest.TestCase):
    def setUp(self):
        self.store = make_store(lots=3, measures_per_lot=1)
        seed_rolls(self.store, {1: 'RL-A', 2: 'RL-B', 3: 'RL-C'})

    def test_correction_keeps_history_and_current(self):
        row, stopped = correct_roll(self.store, 1, 'RL-A2', 'supervisor')
        self.assertEqual(stopped, [])
        self.assertEqual(row['roll_code'], 'RL-A2')
        self.assertEqual(row['corrected_from_id'], self.store.rolls[0]['id'])
        current = self.store.current_roll(1)
        self.assertTrue(current['is_current'])
        self.assertEqual(len([r for r in self.store.rolls if r['measure_id'] == 1]), 2)
        self.assertFalse(self.store.rolls[0]['is_current'])

    def test_correction_deactivates_published_package_and_link(self):
        package, _, _ = create_package(self.store, [1, 2], TRAIN, 'supervisor')
        link = sign_download(package, SECRET)
        # 更正先受理：含该样本的活跃包整包停用。
        _, stopped = correct_roll(self.store, 1, 'RL-A2', 'supervisor')
        self.assertEqual(stopped, [package['id']])
        refreshed = next(p for p in self.store.packages if p['id'] == package['id'])
        self.assertEqual(refreshed['state'], INACTIVE)
        self.assertEqual(refreshed['deactivation_reason'], DEACT_ROLL)
        with self.assertRaises(RuleError) as ctx:
            verify_download(self.store, link['token'], SECRET)
        self.assertEqual(ctx.exception.status, 410)
        self.assertIn('停用', ctx.exception.message)

    def test_correction_then_publish_uses_new_roll(self):
        package, _, _ = create_package(self.store, [1], TRAIN, 'supervisor')
        correct_roll(self.store, 1, 'RL-A2', 'supervisor')
        # 旧包已停用；用更正后的卷重新组训练包应当成功，新链接可下载。
        new_package, retried, _ = create_package(self.store, [1], TRAIN, 'supervisor')
        self.assertFalse(retried)
        self.assertNotEqual(new_package['id'], package['id'])
        self.assertEqual(self.store.current_roll(1)['roll_code'], 'RL-A2')
        link = sign_download(new_package, SECRET)
        verified = verify_download(self.store, link['token'], SECRET)
        self.assertEqual(verified['id'], new_package['id'])

    def test_correction_into_opposite_side_roll_rejected(self):
        create_package(self.store, [2], HOLDOUT, 'supervisor')  # RL-B 归留出
        create_package(self.store, [1], TRAIN, 'supervisor')
        with self.assertRaises(RuleError):
            correct_roll(self.store, 1, 'RL-B', 'supervisor')

    def test_correction_requires_existing_registration(self):
        fresh = make_store(lots=1, measures_per_lot=1)
        with self.assertRaises(RuleError):
            correct_roll(fresh, 1, 'RL-X', 'supervisor')

    def test_same_code_correction_rejected(self):
        with self.assertRaises(RuleError) as ctx:
            correct_roll(self.store, 1, 'RL-A', 'supervisor')
        self.assertEqual(ctx.exception.status, 422)


class WithdrawalTests(unittest.TestCase):
    def setUp(self):
        self.store = make_store()
        seed_rolls(self.store, {1: 'RL-A', 2: 'RL-A', 3: 'RL-B', 4: 'RL-B'})

    def test_withdraw_deactivates_all_related_active_packages(self):
        train_pkg, _, _ = create_package(self.store, [1, 2], TRAIN, 'supervisor')
        link = sign_download(train_pkg, SECRET)
        stopped = withdraw_sample(self.store, 2, '样本污染，撤销', 'supervisor')
        self.assertEqual(stopped, [train_pkg['id']])
        pkg = next(p for p in self.store.packages if p['id'] == train_pkg['id'])
        self.assertEqual(pkg['state'], INACTIVE)
        self.assertEqual(pkg['deactivation_reason'], DEACT_WITHDRAW)
        self.assertIn('样本污染', pkg['deactivation_detail'])
        with self.assertRaises(RuleError) as ctx:
            verify_download(self.store, link['token'], SECRET)
        self.assertEqual(ctx.exception.status, 410)

    def test_withdrawn_sample_excluded_from_later_package(self):
        withdraw_sample(self.store, 1, '误采', 'supervisor')
        chosen, skipped = eligible_measures(self.store, [1, 2])
        self.assertEqual([m['id'] for m in chosen], [2])
        self.assertEqual(skipped['withdrawn'], ['PK-001-S01'])

    def test_double_withdraw_rejected(self):
        withdraw_sample(self.store, 1, '一次', 'supervisor')
        with self.assertRaises(RuleError):
            withdraw_sample(self.store, 1, '二次', 'supervisor')

    def test_other_package_remains_active(self):
        create_package(self.store, [1, 2], TRAIN, 'supervisor')
        holdout, _, _ = create_package(self.store, [3, 4], HOLDOUT, 'supervisor')
        withdraw_sample(self.store, 1, '撤销', 'supervisor')
        untouched = next(p for p in self.store.packages if p['id'] == holdout['id'])
        self.assertEqual(untouched['state'], ACTIVE)


class RetryIdempotencyTests(unittest.TestCase):
    def setUp(self):
        self.store = make_store()
        seed_rolls(self.store, {1: 'RL-A', 2: 'RL-A', 3: 'RL-B'})

    def test_retry_same_cohort_returns_same_package(self):
        first, retried, _ = create_package(self.store, [1, 2], TRAIN, 'supervisor')
        self.assertFalse(retried)
        # 发包中断后恢复：重试原样本集合，不新增训练包。
        second, retried, _ = create_package(self.store, [2, 1], TRAIN, 'supervisor')
        self.assertTrue(retried)
        self.assertEqual(second['id'], first['id'])
        self.assertEqual(len(self.store.packages), 1)
        self.assertEqual(len([i for i in self.store.items if i['package_id'] == first['id']]), 2)

    def test_different_purpose_creates_separate_package(self):
        # 不同膜卷的样本可分别进训练与留出，各自成包。
        train, _, _ = create_package(self.store, [1, 2], TRAIN, 'supervisor')
        holdout, _, _ = create_package(self.store, [3], HOLDOUT, 'supervisor')
        self.assertNotEqual(train['id'], holdout['id'])
        self.assertEqual(holdout['purpose'], HOLDOUT)
        self.assertEqual(len(self.store.packages), 2)

    def test_retry_after_deactivation_creates_new_package(self):
        first, _, _ = create_package(self.store, [1, 2], TRAIN, 'supervisor')
        withdraw_sample(self.store, 1, '撤销', 'supervisor')
        # 旧包停用后，同 cohort 不再命中活跃包，允许重新组包（只含仍有效的样本2）。
        second, retried, _ = create_package(self.store, [1, 2], TRAIN, 'supervisor')
        self.assertFalse(retried)
        self.assertNotEqual(second['id'], first['id'])
        self.assertEqual(second['item_count'], 1)


class DownloadTokenTests(unittest.TestCase):
    def setUp(self):
        self.store = make_store(lots=1, measures_per_lot=1)
        register_roll(self.store, 1, 'RL-A', 'operator')
        self.package, _, _ = create_package(self.store, [1], TRAIN, 'supervisor')

    def test_valid_token_downloads(self):
        link = sign_download(self.package, SECRET, now=1000)
        verified = verify_download(self.store, link['token'], SECRET, now=1001)
        self.assertEqual(verified['id'], self.package['id'])

    def test_expired_token_rejected(self):
        link = sign_download(self.package, SECRET, now=1000)  # 24h 有效期
        with self.assertRaises(RuleError) as ctx:
            verify_download(self.store, link['token'], SECRET, now=1000 + 24 * 3600 + 1)
        self.assertEqual(ctx.exception.status, 410)

    def test_tampered_token_rejected(self):
        link = sign_download(self.package, SECRET, now=1000)
        tampered = link['token'][:-3] + '000'
        with self.assertRaises(RuleError) as ctx:
            verify_download(self.store, tampered, SECRET, now=1001)
        self.assertEqual(ctx.exception.status, 403)

    def test_wrong_secret_rejected(self):
        link = sign_download(self.package, SECRET, now=1000)
        with self.assertRaises(RuleError):
            verify_download(self.store, link['token'], 'other-secret', now=1001)

    def test_malformed_token(self):
        for bad in ('', 'abc', '1.2', 'a.b.c'):
            with self.assertRaises(RuleError):
                verify_download(self.store, bad, SECRET)

    def test_link_state_bound_signature(self):
        link = sign_download(self.package, SECRET, now=1000)
        # 状态从 active 变 inactive 后，旧令牌签名对不上新状态，直接失效。
        self.package['state'] = INACTIVE
        with self.assertRaises(RuleError):
            verify_download(self.store, link['token'], SECRET, now=1001)


class WorkbenchQueryTests(unittest.TestCase):
    def test_package_items_and_rolls_and_reason(self):
        store = make_store()
        seed_rolls(store, {1: 'RL-A', 2: 'RL-A', 3: 'RL-B'})
        package, _, _ = create_package(store, [1, 2, 3], TRAIN, 'supervisor')
        item_rows = [i for i in store.items if i['package_id'] == package['id']]
        self.assertEqual(len(item_rows), 3)
        rolls = sorted({i['roll_code'] for i in item_rows})
        self.assertEqual(rolls, ['RL-A', 'RL-B'])
        withdraw_sample(store, 2, '撤销原因可追溯', 'supervisor')
        refreshed = next(p for p in store.packages if p['id'] == package['id'])
        self.assertEqual(refreshed['deactivation_reason'], DEACT_WITHDRAW)
        self.assertTrue(refreshed['deactivation_detail'])
        # 审计事件记录了停用包 id，工作台履历可呈现。
        event = next(e for e in store.events if e['action'] == 'sample_withdrawn')
        self.assertEqual(event['details']['deactivated_packages'], [package['id']])


if __name__ == '__main__':
    unittest.main(verbosity=2)
