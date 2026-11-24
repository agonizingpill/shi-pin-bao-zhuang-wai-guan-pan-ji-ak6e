-- 训练数据发包：膜卷登记/更正、样本撤销、训练包与留出包。
ALTER TABLE visual_measure ADD COLUMN IF NOT EXISTS revoked_at timestamptz;
ALTER TABLE visual_measure ADD COLUMN IF NOT EXISTS revoke_reason text;

-- 一条测量样本的膜卷登记链：is_current 行唯一，历史行保留更正轨迹。
CREATE TABLE IF NOT EXISTS film_roll_usage (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    measure_id bigint NOT NULL REFERENCES visual_measure(id),
    lot_id bigint NOT NULL REFERENCES pack_lot(id),
    roll_code text NOT NULL CHECK (length(btrim(roll_code)) BETWEEN 1 AND 64),
    is_current boolean NOT NULL DEFAULT true,
    side text CHECK (side IN ('train', 'holdout')),
    corrected_from_id bigint REFERENCES film_roll_usage(id),
    created_by text NOT NULL REFERENCES staff(username),
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS one_current_roll ON film_roll_usage(measure_id) WHERE is_current;
CREATE INDEX IF NOT EXISTS roll_usage_lot ON film_roll_usage(lot_id);
CREATE INDEX IF NOT EXISTS roll_usage_code ON film_roll_usage(roll_code);

CREATE TABLE IF NOT EXISTS training_package (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    code text NOT NULL UNIQUE,
    purpose text NOT NULL CHECK (purpose IN ('train', 'holdout')),
    state text NOT NULL DEFAULT 'active' CHECK (state IN ('active', 'inactive')),
    cohort_hash char(64) NOT NULL,
    item_count integer NOT NULL CHECK (item_count > 0),
    roll_count integer NOT NULL CHECK (roll_count > 0),
    deactivation_reason text CHECK (deactivation_reason IN ('roll_corrected', 'sample_withdrawn')),
    deactivation_detail text,
    deactivated_by text REFERENCES staff(username),
    deactivated_at timestamptz,
    created_by text NOT NULL REFERENCES staff(username),
    created_at timestamptz NOT NULL DEFAULT now()
);
-- 同一队列重试复用同一活跃包，避免重复发包。
CREATE UNIQUE INDEX IF NOT EXISTS one_active_cohort ON training_package(cohort_hash, purpose) WHERE state = 'active';

CREATE TABLE IF NOT EXISTS training_package_item (
    package_id bigint NOT NULL REFERENCES training_package(id),
    measure_id bigint NOT NULL REFERENCES visual_measure(id),
    roll_code text NOT NULL,
    PRIMARY KEY (package_id, measure_id)
);
CREATE INDEX IF NOT EXISTS package_item_measure ON training_package_item(measure_id);
