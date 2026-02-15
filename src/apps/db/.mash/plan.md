# Campaign Ad Performance Metrics Layer Plan

## Goal
Build a semantic metrics layer config for campaign ad performance analysis using the `campaign_ads` table as the central source, enriched with campaign, channel, creative, and conversion data.

## Scope
- **Primary Source:** `campaign_ads` (campaign ad performance facts)
- **Supporting Sources:** `campaigns`, `campaign_channels`, `ad_creatives`, `conversion_events`
- **Key Metrics:** Impressions, clicks, CTR, conversion rate, revenue, ROAS

## Detected Changes

### New Source: campaign_ads_fact
- **Table:** `marketing_db.campaign_ads`
- **Grain:** `[campaign_ad_id]` (fact-level, one row per campaign ad run)
- **Dimensions:** campaign_id, creative_id, channel_id, start_date, end_date, status
- **Measures:** impressions, clicks, conversions, spent_amount, revenue
- **Joins:**
  - To `campaigns` on `campaign_id`
  - To `campaign_channels` on `channel_id`
  - To `ad_creatives` on `creative_id`

### New Source: conversion_events_fact
- **Table:** `marketing_db.conversion_events`
- **Grain:** `[conversion_id]` (one row per conversion event)
- **Dimensions:** campaign_id, customer_id, conversion_type, product_purchased
- **Measures:** conversion_value, revenue, quantity

### New Metrics (on campaign_ads_fact)
1. **ad_impressions** (simple) – SUM(impressions)
2. **ad_clicks** (simple) – SUM(clicks)
3. **ad_ctr** (ratio) – ad_clicks / ad_impressions
4. **ad_conversions** (simple) – SUM(conversions)
5. **ad_conversion_rate** (ratio) – ad_conversions / ad_clicks
6. **ad_spend** (simple) – SUM(spent_amount)
7. **ad_revenue** (simple) – SUM(revenue)
8. **ad_roas** (ratio) – ad_revenue / ad_spend

## Files to Create/Update

### 1. Source: campaign_ads_fact
- **Path:** `src/apps/db/metrics-layer/marketing_db/sources/campaign_ads_fact.yml`
- **Status:** NEW

### 2. Source: conversion_events_fact
- **Path:** `src/apps/db/metrics-layer/marketing_db/sources/conversion_events_fact.yml`
- **Status:** NEW

### 3. Metrics (campaign ad performance)
- **Path:** `src/apps/db/metrics-layer/marketing_db/metrics/ad_performance.yml`
- **Status:** NEW

### 4. Index Update
- **Path:** `src/apps/db/metrics-layer/marketing_db/index.yml`
- **Status:** UPDATE (add sources and metrics references)

## Validation Steps
1. Validate each source YAML against `schema/source.schema.yml`
2. Validate each metric YAML against `schema/metric.schema.yml`
3. Verify index.yml references are syntactically correct
4. Check for join consistency and foreign key alignment

## Rollback Notes
- All files are new or simple appends to the index
- Rollback is safe: delete new `.yml` files and revert `index.yml`

## Next Steps
1. User reviews and approves plan
2. Execute creates all source and metric files
3. Validate outputs
4. Provide execution summary with created files
