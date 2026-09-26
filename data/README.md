# Data

`raw/sales_raw.csv` is a **synthetic** retail dataset produced by `scripts/generate_sample_data.py`
(seed 42, 24 products x 3 years, daily grain). It is *not* real retail data, and results computed on it
(revenue, forecast accuracy) describe the generator, not any real business.

Why synthetic? It is reproducible, needs no licence or download, and -- most importantly -- the generator
also writes `sales_raw.manifest.json`: an exact ground truth of every data-quality problem it injected
(~4% of rows rejected + ~3,900 repairable values). That lets the tests assert that the ETL found exactly
what was injected.

## Columns

| column      | meaning                       | typical problems injected                                        |
|-------------|-------------------------------|------------------------------------------------------------------|
| sale_date   | day of sale                   | blank, impossible dates, `DD/MM/YYYY`, future dates              |
| item_name   | product                       | blank, UPPER/lower case, stray spaces                            |
| category    | product category              | blank, case/whitespace variants                                  |
| units_sold  | units sold that day           | blank, `twelve`, `3.5`, `-5`, `12.0`, `99999`                    |
| revenue     | revenue that day (any currency)| blank, `abc`, `$1,234.50`, negative, decimal-point shifted x50   |

## Using a real dataset (e.g. from Kaggle) instead

1. Download a retail/e-commerce sales file that has a date, a product, a category, a quantity and an amount.
2. Aggregate/rename it to the five columns above (one row per product per day), e.g. with a 10-line pandas script.
3. `python -m etl.pipeline --input your_file.csv` -- the same validation rules apply.

Things to re-check on real data: the date-format assumption (`DD/MM/YYYY`, day first), the outlier
thresholds in `.env`/`app/config.py`, and whether a *missing* day means "zero sales" or "no data".
