# delivery_report.py

Connects to a Supabase database, processes delivery data, and generates a self-contained interactive HTML report (`delivery_report.html`).

## Requirements

- Python 3.8+
- Dependencies:

```bash
pip install pandas plotly supabase
```

## Setup

Create a `credentials.py` file in the same directory as the script:

```python
url = "https://your-project-ref.supabase.co"
key = "your-anon-key"
```

Your Supabase project must have the following tables:

| Table | Description |
|---|---|
| `customer` | Customer records including county and status |
| `deliveries` | Delivery headers with slot times, ATA/ATD, driver info |
| `delivery_lines` | Line-level delivery items with quantities and delivered status |
| `products` | Product catalogue with SKU, price, and stock levels |

## Usage

```bash
python delivery_report.py
```

The script prints progress to the terminal and writes `delivery_report.html` to the current directory. Open it in any browser — no server required.

## What the Report Contains

| Metric | Description |
|---|---|
| **1 — On-Time Rate by Driver** | Per-driver on-time %, colour-coded against a 90% target, plus a stacked Early / On Time / Late breakdown |
| **2 — Orders by Status** | Count of deliveries by status (Complete, Part Complete, Outstanding, Failed) |
| **3 — Top 10 Products by Volume** | Products with the highest total delivered quantity |
| **4 — Top 10 Customers by Revenue** | Customers ranked by Quantity × Unit Price |
| **5 — Delivery Volume by County** | Donut chart showing geographic distribution of deliveries |
| **6 — Stock Alerts** | Products below reorder level, split into low-stock and backordered |

KPI cards at the top summarise overall on-time rate, total deliveries, and stock alert count.

## On-Time Logic

Arrival time (ATA) is derived in order of priority:

1. Use recorded `ATA` if present.
2. Reconstruct as `ATD − Act On Site` (or `Est On Site` when On Site Variance = 0).
3. Fall back to `ETA`/`ETD` when both ATA/ATD are absent and variance = 0.

A delivery is classified as:
- **Early** — ATA before Slot From
- **On Time** — ATA ≥ Slot From and ATD ≤ Slot To
- **Late** — ATA after Slot To

## Notes

- Large tables are fetched in pages of 1,000 rows to stay within Supabase's per-request limit.
- The output HTML is fully self-contained (Plotly loaded from CDN); no Python is needed to view it.