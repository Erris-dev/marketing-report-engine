# Data notes: GA4 obfuscated e-commerce sample

Findings from inspecting `bigquery-public-data.ga4_obfuscated_sample_ecommerce.events_*`
on 2026-09-29, and the decisions built on them. The extract is `sql/ga4_daily.sql`.

## Dataset facts (verified)

| Fact | Value |
|---|---|
| Tables | 92 daily tables, `events_20201101` to `events_20210131` (3.58 GB total) |
| Events / sessions / users | 4,295,584 / 360,129 / 270,154 |
| `event_date` basis | the **UTC** date (100% of events match their UTC timestamp's date) |
| Session key | `user_pseudo_id` + `event_params.ga_session_id` (present on every event) |
| Revenue | `ecommerce.purchase_revenue`, USD (identical to `purchase_revenue_in_usd`) |
| Funnel events | `view_item` 386k, `add_to_cart` 58.5k, `begin_checkout` 38.8k, `purchase` 5,692 |

## Traffic source and channel mapping

**Decision: session-level attribution.** A session's source/medium is the first non-null
`source`/`medium` event param among the session's events, ordered by `event_timestamp`.

Why: a weekly report asks which channel brought traffic *that week*. The alternative,
`traffic_source.*`, is the user's first touch and credits every later session to it.

Caveat found during inspection: **`session_start` events carry no `source`/`medium`
params**, so they cannot be used for session attribution. About 26% of sessions have no
source param on any event; they are reported as `unknown`, not guessed.

Mapping (`sql/channel_case.sql`, applied in order):

| Channel | Rule |
|---|---|
| unknown | medium is NULL or a placeholder: `<Other>`, `(data deleted)`, `(not set)`, `NULL`, `''` |
| paid_search | medium = `cpc` (only `google / cpc` occurs) |
| organic_search | medium = `organic` |
| referral | medium = `referral` (includes the store's self-referrals, ~52k sessions) |
| direct | source = `(direct)` and medium = `(none)` |
| other | any other real medium (`affiliate`, `email`) |

Only `paid_search` is a paid channel (`config.yaml: channels.paid`).

Campaign names are not used: over 99% are placeholders (`<Other>`, `(organic)`, ...); the few
real ones (`BlackFriday_V1`, `NewYear_V2`, ...) have under 150 events each.

## Column definitions (`data/raw/ga4_daily.parquet`)

One row per `date` × `channel`. `date` is the UTC `event_date` of the session's first event;
every session metric (including purchases and revenue) is assigned to that date and channel.

| Column | Definition |
|---|---|
| `sessions` | distinct sessions |
| `users` | distinct `user_pseudo_id` (not additive across channels or days) |
| `view_item_sessions`, `add_to_cart_sessions`, `begin_checkout_sessions`, `purchase_sessions` | **sessions** with at least one such event |
| `purchases` | purchase events with revenue, deduplicated by `ecommerce.transaction_id` |
| `revenue` | sum of `purchase_revenue` over those deduplicated purchases |
| `purchases_missing_revenue` | purchase events with NULL revenue (data quality; excluded above) |
| `duplicate_purchase_events` | repeat events for an already-counted transaction id (data quality) |

Purchase cleaning in numbers: 5,692 purchase events − 450 without revenue − 335 duplicate
firings = **4,907 purchases**, revenue **$339,457**. Events whose transaction id is missing or
`(not set)` cannot be deduplicated and are counted individually.

## Extract profile (2026-09-29)

552 rows (92 days × 6 channels), no duplicate keys, no nulls, 1.64 GB scanned (free sandbox).

| Channel | Sessions | Share | Revenue share |
|---|---:|---:|---:|
| unknown | 126,325 | 35.1% | 6.3% |
| organic_search | 103,039 | 28.6% | 32.1% |
| referral | 83,781 | 23.3% | 53.5% |
| direct | 37,786 | 10.5% | 6.7% |
| paid_search | 7,672 | 2.1% | 0.9% |
| other | 1,526 | 0.4% | 0.5% |

Referral's large revenue share is likely driven by store self-referrals (for example,
returning from a payment page); worth keeping in mind when reading the report.

## Known artifacts at the end of the dataset

Found while computing weekly metrics (Phase 4). They are properties of the public sample,
not pipeline bugs, and the report must surface them rather than present them as business
changes.

| From | What happens | Effect |
|---|---|---|
| 2021-01-22 | `google / cpc` tagging nearly stops: paid_search falls from ~90 to 1-5 sessions/day | 2021-W04 paid_search has 18 sessions and 0 purchases; spend metrics look extreme |
| 2021-01-26 | Most purchase events stop carrying revenue (purchase sessions continue at ~50/day) | 2021-W04 has 240 purchases without revenue vs 98 with; revenue appears to drop 81% WoW |

`missing_revenue_share` (purchases without revenue / all purchases) is computed per week so
the report and anomaly rules can call this a tracking gap.

## Metric conventions

- Weeks are ISO weeks. Only complete weeks (7 days present) are reported: 2020-W45 to
  2021-W04 (13 weeks). 2020-W44 contains only 2020-11-01 and is excluded.
- Week-over-week compares a complete week with the complete week 7 days earlier (so
  2021-W01 compares with 2020-W53); otherwise it is null.
- Ratios with a zero or missing denominator are null, never infinity.
- `conversion_rate` = purchase_sessions / sessions; `aov` = revenue / purchases.
- Spend metrics exist only for paid channels, are simulated, and are null for `total`.
- Weekly `users` is not reported: distinct users are not additive across days.
