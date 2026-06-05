# Debug Notes — delivery_report.py

## Why these fixes aren't in the main README

The main README documents how to *use* the finished tool — installation, configuration, and expected output. Debug steps are temporary diagnostic work: once the bug is fixed, the print statements and intermediate checks have no place in production code or user-facing docs. They describe the journey, not the destination.

---

## Bug 1 — Deprecated `infer_datetime_format` parameter

**Where:** `clean_deliveries()` and `clean_delivery_lines()`

**What it was:**
```python
df["Run Date"] = pd.to_datetime(df["Run Date"], infer_datetime_format=True)
```

**What it produced:** A `TypeError` on newer versions of pandas (2.0+), where `infer_datetime_format` was removed. The script crashed before producing any output.

**Fix:** Removed the parameter entirely. Pandas infers the format automatically by default.
```python
df["Run Date"] = pd.to_datetime(df["Run Date"])
```

---

## Bug 2 — `On Time` column stored as string, mean() returned NaN

**Where:** `on_time_rate()`

**What it was:** The `On Time` column was returned from Supabase as a string (`"0"` / `"1"`). Calling `.mean()` on a string column returns `NaN`, so the overall on-time rate and all per-driver rates came back as `NaN`.

**How it was found:** Added a `print(deliveries_df[["On Time"]].dtypes)` to inspect the column type, which confirmed it was `object` rather than `int64`.

**Fix:** Explicitly cast to numeric before the groupby:
```python
deliveries_df['On Time'] = pd.to_numeric(deliveries_df['On Time'], errors='coerce').astype('Int64')
```

---

## Bug 3 — Wrong table name for customers

**Where:** `main()` — the `fetch_table()` call

**What it was:**
```python
raw_customers = fetch_table("customers")
```

**What it produced:** A 404 response from the Supabase REST API. The actual table in the database is named `customer` (no 's'), so the API returned an error object rather than a list of rows. This caused a downstream crash when `pd.DataFrame()` was called on the error dict.

**Fix:** Corrected the table name to match what exists in Supabase:
```python
raw_customers = fetch_table("customer")
```

---

## Bug 4 — `Customer_ID` type mismatch blocking the county merge

**Where:** `delivery_volume_by_county()`

**What it was:** `Customer_ID` in the `customers` table came from Supabase as a string (`object` dtype), while `Customer_ID` in `deliveries` was numeric. The merge on a string-vs-integer key produced zero matches, so every row returned `NaN` for County and the county breakdown was empty.

**Fix:** Cast `Customer_ID` in the customers dataframe to match before merging:
```python
customers_df["Customer_ID"] = pd.to_numeric(customers_df["Customer_ID"], errors='coerce').astype('Int64')
```

---

## Bug 5 — `fetch_table()` capped at 1,000 rows with no pagination

**Where:** `fetch_table()`

**What it was:** A single API request with `MAX_ROWS = 1000`. Supabase's REST API returns at most 1,000 rows per request, so any table larger than that was silently truncated — no error, just missing data affecting metrics 2, 3, and 4.

**Fix:** Added an offset loop that pages through the full table until a batch smaller than `MAX_ROWS` signals the last page:
```python
while True:
    params = {"select": "*", "limit": MAX_ROWS, "offset": offset}
    batch = requests.get(..., params=params).json()
    all_rows.extend(batch)
    if len(batch) < MAX_ROWS:
        break
    offset += MAX_ROWS
```

---

## Bug 6 — `Delivered` flag only matched uppercase `"Y"`, undercounting delivered lines

**Where:** `clean_delivery_lines()` — affects Metric 3 (top products by volume)

**What it was:**
```python
df["is_delivered"] = df["Delivered"] == "Y"
```

**What it produced:** Any row where `Delivered` was stored as `"y"`, `"yes"`, `"true"`, or `"1"` was treated as not delivered, understating product volumes in Metric 3.

**Fix:** Normalise to lowercase and match against the full set of truthy values:
```python
_truthy = {'y', 'yes', '1', 'true'}
df["is_delivered"] = df["Delivered"].str.strip().str.lower().isin(_truthy)
```