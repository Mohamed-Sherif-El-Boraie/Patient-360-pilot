-- Model costs: the price of each model (USD per 1M tokens) and the cost of every call, computed when it is saved.
-- INTERNAL ONLY. Prices are changed with tools/set_price.py (it closes the current row and opens a new one).
-- model_id is the normalized id (no region or provider prefix, no date or -v1:0), e.g. claude-haiku-5-5, nova-2-lite.
-- Safe to run on a database that already has it.
BEGIN;

CREATE TABLE IF NOT EXISTS model_prices (
  model_id               text NOT NULL,
  mode                   text NOT NULL CHECK (mode IN ('on_demand','batch_inference')),
  effective_from         timestamptz NOT NULL DEFAULT now(),
  effective_to           timestamptz,                      -- NULL = the current price
  input_per_1m           numeric(12,6) NOT NULL CHECK (input_per_1m >= 0),
  output_per_1m          numeric(12,6) NOT NULL CHECK (output_per_1m >= 0),
  cache_write_1h_per_1m  numeric(12,6) CHECK (cache_write_1h_per_1m >= 0),   -- NULL = no cache (batch)
  cache_read_per_1m      numeric(12,6) CHECK (cache_read_per_1m >= 0),
  PRIMARY KEY (model_id, mode, effective_from)
);

INSERT INTO model_prices (model_id, mode, effective_from, input_per_1m, output_per_1m, cache_write_1h_per_1m,
                          cache_read_per_1m) VALUES
  ('claude-haiku-5-5',  'on_demand',       '2026-01-01+00', 0.10,   0.50,   0.20, 0.01),
  ('claude-sonnet-5-5', 'on_demand',       '2026-01-01+00', 2.00,   10.00,  4.00, 0.10),
  ('nova-2-lite',       'on_demand',       '2026-01-01+00', 0.429,  3.597,  NULL, NULL),
  ('claude-haiku-4-5',  'batch_inference', '2026-01-01+00', 1.00,   5.00,   NULL, NULL),
  ('claude-sonnet-4-6', 'batch_inference', '2026-01-01+00', 3.00,   15.00,  NULL, NULL),
  ('nova-2-lite',       'batch_inference', '2026-01-01+00', 0.2145, 1.9785, NULL, NULL)
ON CONFLICT DO NOTHING;

ALTER TABLE ai_model_calls
  ADD COLUMN IF NOT EXISTS input_cost_usd     numeric(12,8),
  ADD COLUMN IF NOT EXISTS output_cost_usd    numeric(12,8),
  ADD COLUMN IF NOT EXISTS cache_cost_usd     numeric(12,8),
  ADD COLUMN IF NOT EXISTS total_cost_usd     numeric(12,8),          -- NULL = a token count or the price is unknown
  ADD COLUMN IF NOT EXISTS price_input_per_1m  numeric(12,6),         -- the prices used: old rows keep what they cost
  ADD COLUMN IF NOT EXISTS price_output_per_1m numeric(12,6);

COMMIT;
