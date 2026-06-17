-- ============================================================================
-- concisepost — Database Blueprint (PostgreSQL 14+ / Supabase)
-- ----------------------------------------------------------------------------
-- Tables : companies · api_keys · plans · usage_logs
-- Tiers   : Free 1,000 · Pro 25,000 · Team 100,000 · Enterprise NULL (uncapped)
-- Speed   : composite (company_id, created_at) indexes for sub-ms dashboards
-- Tenancy : Row-Level Security keyed on the `app.current_company` GUC
--
-- Fully idempotent — safe to run repeatedly (used as the deploy migration).
-- ============================================================================

BEGIN;

-- gen_random_uuid() lives in pgcrypto (preinstalled on Supabase).
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ---------------------------------------------------------------------------
-- Tier enum
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'tier_type') THEN
        CREATE TYPE tier_type AS ENUM ('free', 'pro', 'team', 'enterprise');
    END IF;
END
$$;

-- ---------------------------------------------------------------------------
-- plans — pricing + monthly quota. Single source of truth for tier limits.
-- A NULL monthly_message_limit means "uncapped" (Enterprise: 1,000,000 and above).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS plans (
    tier                  tier_type    PRIMARY KEY,
    display_name          text         NOT NULL,
    price_usd_monthly     numeric(8,2) NOT NULL DEFAULT 0,
    monthly_message_limit bigint       CHECK (monthly_message_limit IS NULL OR monthly_message_limit > 0)
);

-- Idempotent guard for databases created before the column became nullable.
ALTER TABLE plans ALTER COLUMN monthly_message_limit DROP NOT NULL;

INSERT INTO plans (tier, display_name, price_usd_monthly, monthly_message_limit)
VALUES
    ('free',       'Free',         0.00,     1000),
    ('pro',        'Pro',         49.00,    25000),
    ('team',       'Team',       129.00,   100000),
    ('enterprise', 'Enterprise', 499.00,     NULL)   -- 1,000,000 and above (uncapped)
ON CONFLICT (tier) DO UPDATE SET
    display_name          = EXCLUDED.display_name,
    price_usd_monthly     = EXCLUDED.price_usd_monthly,
    monthly_message_limit = EXCLUDED.monthly_message_limit;

-- ---------------------------------------------------------------------------
-- companies — tenants
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS companies (
    id         uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    name       text        NOT NULL,
    tier       tier_type   NOT NULL DEFAULT 'free' REFERENCES plans(tier),
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_companies_tier ON companies (tier);

-- ---------------------------------------------------------------------------
-- api_keys — only a SHA-256 *hash* of the key is stored, never the plaintext.
-- The X-ConcisePost-API-Key header value is hashed by the API and matched here.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS api_keys (
    id           uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id   uuid        NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    key_hash     text        NOT NULL UNIQUE,   -- sha256 hex of the raw key
    key_prefix   text        NOT NULL DEFAULT '',  -- e.g. 'cp_live_demo' (display only)
    active       boolean     NOT NULL DEFAULT true,
    last_used_at timestamptz,
    created_at   timestamptz NOT NULL DEFAULT now()
);
-- Partial index: authentication only ever looks up *active* keys.
CREATE INDEX IF NOT EXISTS idx_api_keys_hash_active
    ON api_keys (key_hash) WHERE active;
CREATE INDEX IF NOT EXISTS idx_api_keys_company
    ON api_keys (company_id);

-- ---------------------------------------------------------------------------
-- usage_logs — append-only log of every optimized agent message.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS usage_logs (
    id               bigint        GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    company_id       uuid          NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    agent_id         text          NOT NULL,
    original_tokens  integer       NOT NULL CHECK (original_tokens  >= 0),
    optimized_tokens integer       NOT NULL CHECK (optimized_tokens >= 0),
    cost_saved_usd   numeric(14,6) NOT NULL DEFAULT 0 CHECK (cost_saved_usd >= 0),
    loop_prevented   boolean       NOT NULL DEFAULT false,
    created_at       timestamptz   NOT NULL DEFAULT now()
);

-- HEAVY INDEXING — sub-millisecond tenant-scoped analytical reads.
-- The composite (company_id, created_at) index satisfies every dashboard query:
-- it filters by tenant and ranges/sorts by time in a single index scan.
CREATE INDEX IF NOT EXISTS idx_usage_logs_company_created
    ON usage_logs (company_id, created_at DESC);
-- Plain company index for pure full-history tenant aggregates.
CREATE INDEX IF NOT EXISTS idx_usage_logs_company
    ON usage_logs (company_id);
-- BRIN on created_at — tiny footprint, accelerates time-window scans at scale.
CREATE INDEX IF NOT EXISTS idx_usage_logs_created_brin
    ON usage_logs USING brin (created_at);
-- Partial index to count prevented loops without scanning the whole table.
CREATE INDEX IF NOT EXISTS idx_usage_logs_company_loops
    ON usage_logs (company_id) WHERE loop_prevented;

-- ===========================================================================
-- ROW-LEVEL SECURITY (tenant isolation)
-- The API sets `app.current_company` per request (SELECT set_config(..., true)).
-- Policies confine every read/write on usage_logs to the authenticated tenant,
-- even if a query forgets a WHERE clause. RLS is intentionally NOT enabled on
-- plans/companies/api_keys: authentication must read those *before* the tenant
-- is known (the company is resolved by hashing the API key). Privileged roles
-- (e.g. the Supabase service role) bypass RLS as usual.
-- ===========================================================================
ALTER TABLE usage_logs ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_usage_logs ON usage_logs;
CREATE POLICY tenant_isolation_usage_logs ON usage_logs
    USING      (company_id = NULLIF(current_setting('app.current_company', true), '')::uuid)
    WITH CHECK (company_id = NULLIF(current_setting('app.current_company', true), '')::uuid);

-- ===========================================================================
-- DEMO SEED (idempotent) — local testing tenant on the Pro tier.
-- Raw API key (for the X-ConcisePost-API-Key header): cp_live_demo_5f3b9a1c7e2d48f6
-- Stored only as its SHA-256 hash; the plaintext is never persisted.
-- ===========================================================================
DO $$
DECLARE
    v_company uuid;
    v_hash    text := '95c6e756b39df95d2d90082855164aa425a65ee052f1f0c53cefc88ac0121839';
BEGIN
    IF NOT EXISTS (SELECT 1 FROM api_keys WHERE key_hash = v_hash) THEN
        INSERT INTO companies (name, tier)
        VALUES ('Acme Robotics (Demo)', 'pro')
        RETURNING id INTO v_company;

        INSERT INTO api_keys (company_id, key_hash, key_prefix, active)
        VALUES (v_company, v_hash, 'cp_live_demo', true);
    END IF;
END
$$;

COMMIT;

-- ============================================================================
-- Operational notes:
--   * VACUUM/ANALYZE usage_logs on a schedule; the BRIN index assumes roughly
--     time-ordered inserts (true for append-only logs).
--   * At very high write volume, convert usage_logs to a monthly partitioned
--     table (PARTITION BY RANGE (created_at)); the indexes above translate
--     directly to per-partition local indexes.
-- ============================================================================
