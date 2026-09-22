-- One row per (date, channel) from the GA4 obfuscated e-commerce sample.
-- Session-level attribution; decisions are documented in docs/DATA_NOTES.md.
-- Python fills in the table name and the channel mapping (sql/channel_case.sql).
-- Query parameters: @start_suffix, @end_suffix (YYYYMMDD table suffixes).
WITH events AS (
  SELECT
    PARSE_DATE('%Y%m%d', event_date) AS event_date,  -- event_date is the UTC date
    event_timestamp,
    event_name,
    user_pseudo_id,
    (SELECT value.int_value FROM UNNEST(event_params) WHERE key = 'ga_session_id') AS ga_session_id,
    (SELECT value.string_value FROM UNNEST(event_params) WHERE key = 'source') AS source,
    (SELECT value.string_value FROM UNNEST(event_params) WHERE key = 'medium') AS medium,
    ecommerce.transaction_id AS transaction_id,
    ecommerce.purchase_revenue AS purchase_revenue
  FROM `$table`
  WHERE _TABLE_SUFFIX BETWEEN @start_suffix AND @end_suffix
),

-- session_start carries no source, so a session's source is the first one on any of its events.
sessions AS (
  SELECT
    user_pseudo_id,
    ga_session_id,
    ARRAY_AGG(event_date ORDER BY event_timestamp LIMIT 1)[OFFSET(0)] AS session_date,
    ARRAY_AGG(
      IF(source IS NULL AND medium IS NULL, NULL, STRUCT(source, medium))
      IGNORE NULLS ORDER BY event_timestamp LIMIT 1
    )[SAFE_OFFSET(0)] AS first_touch,
    LOGICAL_OR(event_name = 'view_item') AS has_view_item,
    LOGICAL_OR(event_name = 'add_to_cart') AS has_add_to_cart,
    LOGICAL_OR(event_name = 'begin_checkout') AS has_begin_checkout,
    LOGICAL_OR(event_name = 'purchase') AS has_purchase
  FROM events
  GROUP BY user_pseudo_id, ga_session_id
),

sessions_with_channel AS (
  SELECT
    user_pseudo_id,
    ga_session_id,
    session_date,
    has_view_item,
    has_add_to_cart,
    has_begin_checkout,
    has_purchase,
    $channel_case AS channel
  FROM (
    SELECT s.*, s.first_touch.source AS source, s.first_touch.medium AS medium FROM sessions AS s
  )
),

-- Purchases: a usable transaction_id counts once (duplicate firings are counted separately);
-- purchases without revenue are counted for data quality but excluded from purchases/revenue.
purchase_events AS (
  SELECT
    user_pseudo_id,
    ga_session_id,
    purchase_revenue,
    transaction_id IS NOT NULL AND transaction_id != '(not set)' AS has_txn_id,
    ROW_NUMBER() OVER (
      PARTITION BY IF(transaction_id IS NULL OR transaction_id = '(not set)', NULL, transaction_id)
      ORDER BY event_timestamp
    ) AS txn_rank
  FROM events
  WHERE event_name = 'purchase'
),

purchases_by_session AS (
  SELECT
    user_pseudo_id,
    ga_session_id,
    COUNTIF(purchase_revenue IS NOT NULL AND (NOT has_txn_id OR txn_rank = 1)) AS purchases,
    SUM(IF(NOT has_txn_id OR txn_rank = 1, purchase_revenue, NULL)) AS revenue,
    COUNTIF(purchase_revenue IS NULL) AS purchases_missing_revenue,
    COUNTIF(has_txn_id AND txn_rank > 1) AS duplicate_purchase_events
  FROM purchase_events
  GROUP BY user_pseudo_id, ga_session_id
)

SELECT
  s.session_date AS date,
  s.channel,
  COUNT(*) AS sessions,
  COUNT(DISTINCT s.user_pseudo_id) AS users,
  COUNTIF(s.has_view_item) AS view_item_sessions,
  COUNTIF(s.has_add_to_cart) AS add_to_cart_sessions,
  COUNTIF(s.has_begin_checkout) AS begin_checkout_sessions,
  COUNTIF(s.has_purchase) AS purchase_sessions,
  COALESCE(SUM(p.purchases), 0) AS purchases,
  COALESCE(SUM(p.revenue), 0) AS revenue,
  COALESCE(SUM(p.purchases_missing_revenue), 0) AS purchases_missing_revenue,
  COALESCE(SUM(p.duplicate_purchase_events), 0) AS duplicate_purchase_events
FROM sessions_with_channel AS s
LEFT JOIN purchases_by_session AS p USING (user_pseudo_id, ga_session_id)
GROUP BY date, channel
ORDER BY date, channel
