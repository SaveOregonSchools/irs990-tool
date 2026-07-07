WITH remaining AS (
  SELECT
    s.signature_hash,
    COALESCE(s.grant_count, 0) AS grant_count,
    COALESCE(s.total_amount, 0) AS total_amount
  FROM grant_recipient_signature s
  WHERE EXISTS (
    SELECT 1
    FROM grant_recipient_ai_candidate c
    WHERE c.signature_hash = s.signature_hash
  )
  AND NOT EXISTS (
    SELECT 1
    FROM grant_recipient_ai_decision d
    WHERE d.signature_hash = s.signature_hash
  )
),
bucketed AS (
  SELECT
    CASE
      WHEN total_amount <= 0 THEN '$0'
      WHEN total_amount > 0 AND total_amount <= 100 THEN '$1-$100'
      WHEN total_amount > 100 AND total_amount <= 500 THEN '$100-$500'
      WHEN total_amount > 500 AND total_amount <= 1000 THEN '$500-$1000'
      WHEN total_amount > 1000 AND total_amount <= 2500 THEN '$1001-$2500'
      WHEN total_amount > 2500 AND total_amount <= 5000 THEN '$2501-$5000'
      WHEN total_amount > 5000 AND total_amount <= 10000 THEN '$5001-$10000'
      WHEN total_amount > 10000 AND total_amount <= 50000 THEN '$10001-$50000'
      WHEN total_amount > 50000 AND total_amount <= 100000 THEN '$50001-$100000'
      WHEN total_amount > 100000 AND total_amount <= 200000 THEN '$100001-$200000'
      WHEN total_amount > 200000 AND total_amount <= 300000 THEN '$200001-$300000'
      WHEN total_amount > 300000 AND total_amount <= 500000 THEN '$300001-$500000'
      WHEN total_amount > 500000 AND total_amount <= 1000000 THEN '$500001-$1000000'
      ELSE '$1000001 and up'
    END AS amount_bucket,
    CASE
      WHEN total_amount <= 0 THEN 0
      WHEN total_amount > 0 AND total_amount <= 100 THEN 1
      WHEN total_amount > 100 AND total_amount <= 500 THEN 2
      WHEN total_amount > 500 AND total_amount <= 1000 THEN 3
      WHEN total_amount > 1000 AND total_amount <= 2500 THEN 4
      WHEN total_amount > 2500 AND total_amount <= 5000 THEN 5
      WHEN total_amount > 5000 AND total_amount <= 10000 THEN 6
      WHEN total_amount > 10000 AND total_amount <= 50000 THEN 7
      WHEN total_amount > 50000 AND total_amount <= 100000 THEN 8
      WHEN total_amount > 100000 AND total_amount <= 200000 THEN 9
      WHEN total_amount > 200000 AND total_amount <= 300000 THEN 10
      WHEN total_amount > 300000 AND total_amount <= 500000 THEN 11
      WHEN total_amount > 500000 AND total_amount <= 1000000 THEN 12
      ELSE 13
    END AS sort_order,
    signature_hash,
    grant_count,
    total_amount
  FROM remaining
),
agg AS (
  SELECT
    sort_order,
    amount_bucket,
    COUNT(*) AS signatures,
    SUM(grant_count) AS grants_represented,
    ROUND(SUM(total_amount), 2) AS total_amount
  FROM bucketed
  GROUP BY sort_order, amount_bucket
)
SELECT
  amount_bucket,
  signatures,
  grants_represented,
  total_amount,
  ROUND(100.0 * signatures / SUM(signatures) OVER (), 2) AS pct_of_signatures,
  ROUND(100.0 * grants_represented / SUM(grants_represented) OVER (), 2) AS pct_of_grants_represented,
  ROUND(100.0 * total_amount / SUM(total_amount) OVER (), 2) AS pct_of_total_amount
FROM agg
ORDER BY sort_order;