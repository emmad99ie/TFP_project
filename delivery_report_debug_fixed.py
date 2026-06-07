"""
delivery_report.py
==================
The Fruit People Ltd — Operations & Supply Chain Reporting
Internal tooling | Data & Systems Team

--------------------------------------------------------------------------------
PURPOSE
--------------------------------------------------------------------------------
This script is the primary automated reporting tool for daily delivery
operations at The Fruit People. It connects to our Supabase data warehouse,
pulls the four core operational tables (customers, deliveries, delivery_lines,
products), computes a standard set of KPIs used by the operations team, and
renders a self-contained HTML report that can be emailed or opened directly
in a browser.

The report is designed to be run each morning after the overnight data sync
completes. It covers the most recent available run data and is consumed by:
  - The operations manager (on-time rates, driver performance)
  - The warehouse team (stock alerts, product volume)
  - Account management (revenue by customer)

--------------------------------------------------------------------------------
DATA SOURCES
--------------------------------------------------------------------------------
All data is fetched via the Supabase REST API (PostgREST interface). We use
the REST API rather than a direct psycopg2 connection for two reasons:
  1. The anon key grants row-level-security-scoped read access without
     exposing the database connection string.
  2. The REST API is available from any network without VPN, making it
     suitable for running locally or from a cloud function.

Tables used:
  customers       — master customer list with county, status, credit limit
  deliveries      — one row per delivery stop; includes driver, timing, status
  delivery_lines  — one row per product per order; includes SKU, qty, delivered
  products        — product master; SKU, category, price, stock level

--------------------------------------------------------------------------------
OUTPUT
--------------------------------------------------------------------------------
A single self-contained HTML file: delivery_report.html
  - Summary KPI cards
  - On-time delivery rate (overall and per driver)
  - Orders by status breakdown
  - Top 10 products by volume delivered
  - Top 10 customers by revenue (Quantity x Unit_Price)
  - Stock alerts: products below reorder level

--------------------------------------------------------------------------------
DEPENDENCIES
--------------------------------------------------------------------------------
  pip install requests pandas

No other dependencies. The HTML report uses inline CSS and no external assets,
so it renders correctly when emailed or opened offline.

--------------------------------------------------------------------------------
CONFIGURATION
--------------------------------------------------------------------------------
Set SUPABASE_URL and SUPABASE_KEY below, or move them to a .env file and
load with python-dotenv. Do not commit credentials to version control.

--------------------------------------------------------------------------------
KNOWN DATA CHARACTERISTICS
--------------------------------------------------------------------------------
The following quirks in the source data are handled by the cleaning functions:

  - deliveries.Status:      mixed casing ('Complete' / 'complete') -- normalised
                            to title case in clean_deliveries()
  - products.Category:      mixed casing ('Frozen' / 'frozen') -- normalised
                            to title case in clean_products()
  - delivery_lines.Item:    some rows have null SKU (items without a product
                            code) -- these are excluded from revenue and volume
                            calculations by the left merge; NaN revenue rows
                            are naturally excluded from the groupby sum
  - Weight (Kg):            some rows carry a value of -1; this appears to be
                            a placeholder used by the upstream system when
                            weight data is unavailable. The field is not used
                            in any metric in this report so no action is taken.

--------------------------------------------------------------------------------
VERSION HISTORY
--------------------------------------------------------------------------------
  v1.0  -- Initial implementation. Single-table fetch, basic HTML output.
  v1.1  -- Added per-driver on-time breakdown and stock alert section.
  v1.2  -- Switched from direct DB connection to Supabase REST API.
  v1.3  -- Added category and status normalisation after data quality review.
  v1.4  -- Revenue metric added; top 10 customers by Quantity x Unit_Price.
  v1.5  -- Current version. Refactored into discrete cleaning and metric
           functions for testability. HTML template expanded.

--------------------------------------------------------------------------------
USAGE
--------------------------------------------------------------------------------
  python delivery_report.py

Output: delivery_report.html in the current working directory.

--------------------------------------------------------------------------------
CONTACT
--------------------------------------------------------------------------------
  Data & Systems Team -- The Fruit People Ltd
  Internal tooling -- not for external distribution
"""

import os
import requests
import pandas as pd
from datetime import datetime
from credentials import url, key


# =============================================================================
# CONFIGURATION
# =============================================================================
# Set these directly for local runs, or load from environment / .env file.
# The anon key is safe to use here -- it only grants read access scoped by RLS.


## UPDATE creds
from dotenv import load_dotenv
import os

load_dotenv()
url = os.getenv("SUPABASE_URL")
key = os.getenv("SUPABASE_KEY")

# Supabase REST API base path. PostgREST serves all tables under /rest/v1/.
API_BASE = f"{SUPABASE_URL}/rest/v1"

# Request timeout in seconds. Supabase cold-starts can occasionally take 3-5s
# on the free tier; 10s gives comfortable headroom without hanging indefinitely.
REQUEST_TIMEOUT = 10

# Maximum rows to fetch per table. Supabase defaults to a 1000-row page limit.
# For tables with more rows, pagination would be needed (see fetch_table notes).
# Our current dataset is well within this limit so a single request suffices.
MAX_ROWS = 1000

# Report output filename. Written to the current working directory.
REPORT_FILENAME = "delivery_report.html"


# =============================================================================
# API LAYER
# =============================================================================

def build_headers():
    """
    Construct the HTTP headers required for Supabase REST API authentication.

    Supabase's PostgREST interface accepts the API key via the 'apikey' header.
    This is the standard authentication pattern documented at:
    https://supabase.com/docs/guides/api

    Returns:
        dict: Headers dict ready to pass to requests.get()
    """
    # The apikey header is used by the Supabase API gateway to identify the
    # project and apply the correct Row Level Security policies. This is
    # sufficient for read access using the anon (public) key.
    headers = {
        "apikey": SUPABASE_KEY,
    }
    return headers

def fetch_table(table_name):
    headers = build_headers()
    all_rows = []
    offset = 0

    while True:
        params = {
            "select": "*",
            "limit": MAX_ROWS,
            "offset": offset,
        }
        response = requests.get(
            f"{API_BASE}/{table_name}",
            headers=headers,
            params=params,
            timeout=REQUEST_TIMEOUT,
        )
        batch = response.json()
        if not isinstance(batch, list):
            raise ValueError(f"Unexpected response fetching '{table_name}': {batch}")
        all_rows.extend(batch)
        if len(batch) < MAX_ROWS:
            break
        offset += MAX_ROWS

    return all_rows


# =============================================================================
# DATA CLEANING
# =============================================================================

def clean_customers(df):
    """
    Clean and normalise the customers dataframe.

    Handles:
      - County casing inconsistency (e.g. 'CORK' vs 'Cork')
      - Customer_Status casing inconsistency ('ACTIVE' vs 'Active')
      - Strips whitespace from string columns

    Args:
        df (pd.DataFrame): Raw customers dataframe from Supabase.

    Returns:
        pd.DataFrame: Cleaned dataframe.
    """
    # Normalise County to title case -- the source data has a mix of
    # 'Cork', 'CORK', and 'cork' depending on how the record was entered.
    df["County"] = df["County"].str.strip().str.title()

    # Same treatment for Customer_Status -- 'Active', 'ACTIVE', 'active'
    # all appear in the data. Title case gives us 'Active' / 'Inactive'
    # as the two canonical values.
    df["Customer_Status"] = df["Customer_Status"].str.strip().str.title()

    return df


def clean_deliveries(df):
    """
    Clean and normalise the deliveries dataframe.

    Handles:
      - Run Date parsing to datetime
      - Status casing normalisation
      - Whitespace stripping

    The deliveries table is the primary fact table for timing and driver
    performance metrics. Run Date is parsed here so downstream functions
    can do date arithmetic without conversion overhead.

    Args:
        df (pd.DataFrame): Raw deliveries dataframe from Supabase.

    Returns:
        pd.DataFrame: Cleaned dataframe with Run Date as datetime.
    """
    # Parse Run Date to datetime. The deliveries table uses ISO 8601 format
    # (YYYY-MM-DD) consistently, so pandas can infer the format correctly.
    # Using infer_datetime_format=True for performance on larger datasets.

    ## UPDATE type error
    ##df["Run Date"] = pd.to_datetime(df["Run Date"], infer_datetime_format=True)
    df["Run Date"] = pd.to_datetime(df["Run Date"])

    # Normalise Status column -- values include 'Complete', 'complete',
    # 'Outstanding', 'outstanding', 'Part Complete'. Title case gives a
    # clean set of canonical values for groupby operations.
    df["Status"] = df["Status"].str.strip().str.title()

    # Strip whitespace from Driver Name to avoid groupby anomalies where
    # 'Driver_001 ' and 'Driver_001' would appear as separate groups.
    df["Driver Name"] = df["Driver Name"].str.strip()

    return df


def clean_delivery_lines(df):
    """
    Clean and normalise the delivery_lines dataframe.

    Handles:
      - Run Date parsing to datetime
      - Delivered flag normalisation to boolean is_delivered column
      - Item (SKU) whitespace stripping

    The Delivered column in the source data uses a consistent 'Y'/'N'
    encoding. We map this to a boolean is_delivered column for clarity
    and to make filtering more readable downstream.

    Args:
        df (pd.DataFrame): Raw delivery_lines dataframe from Supabase.

    Returns:
        pd.DataFrame: Cleaned dataframe with is_delivered boolean column.
    """
    # Parse Run Date. The delivery_lines table uses the same date format
    # as the deliveries table, so infer_datetime_format handles it cleanly.
    ## UPDATE type error
    ##df["Run Date"] = pd.to_datetime(df["Run Date"], infer_datetime_format=True)
    df["Run Date"] = pd.to_datetime(df["Run Date"])

    # Map the Delivered flag to a boolean. The source system encodes this
    # as 'Y' for delivered and 'N' for not delivered. Converting to bool
    # makes the downstream filter (df[df["is_delivered"]]) more readable
    # than repeatedly comparing against the string 'Y'.
    ##df["is_delivered"] = df["Delivered"] == "Y"

    ## update
    _truthy = {'y', 'yes', '1', 'true'}
    df["is_delivered"] = df["Delivered"].str.strip().str.lower().isin(_truthy)
    # Strip whitespace from Item (SKU) to prevent merge mismatches.
    df["Item"] = df["Item"].str.strip()

    return df


def clean_products(df):
    """
    Clean and normalise the products dataframe.

    Handles:
      - Category casing normalisation ('Frozen' / 'frozen' -> 'Frozen')
      - Storage_Type whitespace stripping
      - Product_SKU whitespace stripping

    Note on SKU formatting: Product_SKU values in this table follow the
    format 'SKU####' (e.g. SKU1001, SKU1042). The delivery_lines.Item
    column uses the same format. Whitespace stripping is applied to both
    sides of the merge to ensure clean joins.

    Args:
        df (pd.DataFrame): Raw products dataframe from Supabase.

    Returns:
        pd.DataFrame: Cleaned dataframe.
    """
    # Normalise Category to title case -- 'frozen', 'Frozen', 'FROZEN'
    # all appear due to historical data entry inconsistency.
    df["Category"] = df["Category"].str.strip().str.title()

    # Strip whitespace from SKU and Storage_Type columns.
    df["Product_SKU"] = df["Product_SKU"].str.strip()
    df["Storage_Type"] = df["Storage_Type"].str.strip()

    ## UPDATE clean up the product name
    df["Product_Name"] = df["Product_Name"].str.strip()


    return df


# =============================================================================
# METRICS
# =============================================================================

def on_time_rate(deliveries_df):
    """
    Calculate on-time delivery rate per driver and overall.

    The 'On Time' column is a binary integer flag: 1 = on time, 0 = not on time.
    Taking the mean of this column gives the proportion of deliveries that were
    on time, which we then express as a percentage.

    Per-driver rates are calculated using groupby on 'Driver Name'. The overall
    rate is calculated across all rows.

    Args:
        deliveries_df (pd.DataFrame): Cleaned deliveries dataframe.

    Returns:
        dict: {
            'by_driver': {driver_name: rate_pct, ...},
            'overall':   float (percentage, 1 decimal place)
        }
    """
    # Group by driver and take the mean of the On Time flag.
    # Multiplying by 100 converts the proportion to a percentage.
    # round(1) gives one decimal place -- sufficient precision for this KPI.
    ##pd.option_context("display.max_rows", None, "display.max_columns", None)
    print("deliveries_df.dtypes:") 

    ## UPDATE convert to numeric to get average
    deliveries_df['On Time'] = pd.to_numeric(deliveries_df['On Time'], errors='coerce').astype('Int64')
    print(deliveries_df[["On Time"]].dtypes)
    print(deliveries_df[["On Time"]].head())
    per_driver = (
        deliveries_df.groupby("Driver Name")["On Time"]
        .mean()
        .mul(100)
        .round(1)
        .to_dict()
    )

    # Overall rate -- same calculation across all rows regardless of driver.
    overall = round(deliveries_df["On Time"].mean() * 100, 1)

    return {"by_driver": per_driver, "overall": overall}


def calculate_revenue(delivery_lines_df, products_df):
    """
    Calculate total revenue per customer.

    Revenue is defined as: sum(Quantity x Unit_Price) per customer,
    joined on the SKU (delivery_lines.Item = products.Product_SKU).

    A left merge is used so that delivery line items without a matching
    product SKU (e.g. null Item values) produce NaN revenue rather than
    being silently excluded. The groupby sum naturally ignores NaN values,
    so unmatched rows contribute zero revenue without distorting results.

    Args:
        delivery_lines_df (pd.DataFrame): Cleaned delivery lines dataframe.
        products_df       (pd.DataFrame): Cleaned products dataframe.

    Returns:
        pd.Series: Top 10 customers by total revenue, index = Customer Name,
                   values = revenue in euros.
    """
    # Merge delivery lines with product master to get Unit_Price per line item.
    # We only need Product_SKU and Unit_Price from the products table.
    # left merge preserves all delivery line rows; unmatched SKUs get NaN price.
    merged = delivery_lines_df.merge(
        products_df[["Product_SKU", "Unit_Price"]],
        left_on="Item",
        right_on="Product_SKU",
        how="left",
    )

    # Revenue per line item = quantity ordered x unit price.
    # Rows with NaN Unit_Price (unmatched SKUs) produce NaN revenue,
    # which the groupby sum handles correctly by treating them as zero.
    merged["revenue"] = merged["Quantity"] * merged["Unit_Price"]

    # Aggregate to customer level, sort descending, return top 10.
    # 'Customer Name' is used here as it appears in delivery_lines --
    # this avoids needing an additional join back to the customers table.
    revenue_by_customer = (
        merged.groupby("Customer Name")["revenue"]
        .sum()
        .sort_values(ascending=False)
        .head(10)
    )

    return revenue_by_customer


def stock_alerts(products_df):
    """
    Identify products where current stock is below the reorder level.

    This is a simple threshold filter. The result is used by the warehouse
    team to prioritise purchase orders. Products at exactly the reorder level
    are not included -- only those strictly below it.

    Args:
        products_df (pd.DataFrame): Cleaned products dataframe.

    Returns:
        pd.DataFrame: Subset of products_df with columns:
                      Product_Name, Category, In_Stock, Reorder_Level
    """
    # Filter to products where In_Stock < Reorder_Level.
    # Both columns are integers in the source data so the comparison is exact.
    alerts = products_df[products_df["In_Stock"] < products_df["Reorder_Level"]]

    return alerts[["Product_Name", "Category", "In_Stock", "Reorder_Level"]]


def top_products_by_volume(delivery_lines_df, products_df):
    """
    Return the top 10 products by total quantity delivered.

    Only rows where is_delivered is True are included -- we want volume
    of goods that actually reached the customer, not items that were
    on the manifest but not delivered.

    The result is joined to the products table to get a readable product
    name rather than displaying the raw SKU code.

    Args:
        delivery_lines_df (pd.DataFrame): Cleaned delivery lines dataframe.
        products_df       (pd.DataFrame): Cleaned products dataframe.

    Returns:
        pd.Series: Top 10 products by total quantity,
                   index = Product_Name, values = total quantity.
    """
    # Filter to delivered line items only.
    delivered = delivery_lines_df[delivery_lines_df["is_delivered"]]

    # Join to products to get Product_Name for display.
    merged = delivered.merge(
        products_df[["Product_SKU", "Product_Name"]],
        left_on="Item",
        right_on="Product_SKU",
        how="left",
    )

    # Sum quantity by product name, descending, top 10.
    top = (
        merged.groupby("Product_Name")["Quantity"]
        .sum()
        .sort_values(ascending=False)
        .head(10)
    )

    return top


def orders_by_status(deliveries_df):
    """
    Count deliveries grouped by normalised Status value.

    Status has been normalised to title case in clean_deliveries(), so this
    groupby will produce clean keys: 'Complete', 'Outstanding', 'Part Complete'.

    Args:
        deliveries_df (pd.DataFrame): Cleaned deliveries dataframe.

    Returns:
        dict: {status_string: count, ...}
    """
    return deliveries_df["Status"].value_counts().to_dict()


def delivery_volume_by_county(deliveries_df, customers_df):
    """
    Count delivery stops per county.

    deliveries does not carry county directly -- it carries Customer_ID,
    which we join to the customers table to get County. County has been
    normalised to title case in clean_customers().

    Args:
        deliveries_df (pd.DataFrame): Cleaned deliveries dataframe.
        customers_df  (pd.DataFrame): Cleaned customers dataframe.

    Returns:
        pd.Series: Delivery count per county, descending.
    """
    # Merge deliveries with customers on Customer_ID to bring in County.

    print(deliveries_df[['Customer_ID']].dtypes)
    print(customers_df[['Customer_ID']].dtypes)

    ## UPDATE
    deliveries_df["Customer_ID"] = pd.to_numeric(deliveries_df["Customer_ID"], errors='coerce').astype('Int64')

    merged = deliveries_df.merge(
        customers_df[["Customer_ID", "County"]],
        on="Customer_ID",
        how="left",
    )

    return merged["County"].value_counts()


# =============================================================================
# REPORT GENERATION
# =============================================================================

def generate_report(metrics):
    """
    Render all computed metrics into a self-contained HTML report and write
    it to disk.

    The report uses inline CSS only -- no external stylesheets or JS -- so it
    renders correctly when opened as a local file or embedded in an email.

    The output file is written to the current working directory. The filename
    is controlled by the REPORT_FILENAME constant at the top of this module.

    Args:
        metrics (dict): Output of the metric functions, keyed as:
            'on_time'      -- dict from on_time_rate()
            'revenue'      -- Series from calculate_revenue()
            'status'       -- dict from orders_by_status()
            'stock'        -- DataFrame from stock_alerts()
            'top_products' -- Series from top_products_by_volume()
            'by_county'    -- Series from delivery_volume_by_county()

    Returns:
        None. Writes delivery_report.html to disk and prints confirmation.
    """
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")

    on_time      = metrics["on_time"]
    revenue      = metrics["revenue"]
    status       = metrics["status"]
    stock        = metrics["stock"]
    top_products = metrics["top_products"]
    by_county    = metrics["by_county"]

    # -- Build HTML table rows for each section --------------------------------

    # Driver on-time table -- sorted alphabetically by driver name for
    # consistent ordering across report runs.
    driver_rows = "".join(
        f"<tr><td>{driver}</td><td>{rate}%</td></tr>"
        for driver, rate in sorted(on_time["by_driver"].items())
    )

    # Revenue table -- already sorted descending from calculate_revenue().
    revenue_rows = "".join(
        f"<tr><td>{cust}</td><td>E{rev:,.2f}</td></tr>"
        for cust, rev in revenue.items()
    )

    # Status breakdown table.
    status_rows = "".join(
        f"<tr><td>{s}</td><td>{c}</td></tr>"
        for s, c in status.items()
    )

    # Stock alert table -- itertuples() for efficient row access on DataFrame.
    stock_rows = "".join(
        f"<tr><td>{row.Product_Name}</td><td>{row.Category}</td>"
        f"<td>{row.In_Stock}</td><td>{row.Reorder_Level}</td></tr>"
        for row in stock.itertuples()
    )

    # Top products table.
    top_product_rows = "".join(
        f"<tr><td>{prod}</td><td>{qty:,}</td></tr>"
        for prod, qty in top_products.items()
    )

    # County volume table.
    county_rows = "".join(
        f"<tr><td>{county}</td><td>{count:,}</td></tr>"
        for county, count in by_county.items()
    )

    # -- Assemble and write HTML -----------------------------------------------

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Delivery Report - The Fruit People</title>
    <style>
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{ font-family: Arial, sans-serif; background: #f0f4f8; color: #222; padding: 32px; }}
        .wrapper {{ max-width: 960px; margin: 0 auto; }}
        header {{ background: #1F4E79; color: white; padding: 28px 32px; border-radius: 8px; margin-bottom: 28px; }}
        header h1 {{ font-size: 1.6em; margin-bottom: 4px; }}
        header p {{ font-size: 0.9em; opacity: 0.7; }}
        .section {{ background: white; border-radius: 8px; padding: 24px 28px;
                    margin-bottom: 24px; box-shadow: 0 1px 4px rgba(0,0,0,0.08); }}
        .section h2 {{ color: #1F4E79; font-size: 1.1em; margin-bottom: 16px;
                       padding-bottom: 10px; border-bottom: 2px solid #e8f0f8; }}
        table {{ border-collapse: collapse; width: 100%; }}
        th {{ background: #1F4E79; color: white; padding: 9px 14px; text-align: left; font-size: 0.9em; }}
        td {{ padding: 8px 14px; border-bottom: 1px solid #eee; font-size: 0.9em; }}
        tr:last-child td {{ border-bottom: none; }}
        tr:nth-child(even) td {{ background: #f7fafd; }}
        .overall {{ font-size: 2.2em; font-weight: bold; color: #1E8449; margin-bottom: 16px; }}
        .meta {{ color: #888; font-size: 0.85em; }}
        .alert td:nth-child(3) {{ color: #C0392B; font-weight: bold; }}
    </style>
</head>
<body>
<div class="wrapper">
    <header>
        <h1>The Fruit People - Delivery Report</h1>
        <p class="meta">Generated: {timestamp}</p>
    </header>
    <div class="section">
        <h2>On-Time Delivery Rate</h2>
        <div class="overall">{on_time['overall']}%</div>
        <table>
            <tr><th>Driver</th><th>On-Time Rate</th></tr>
            {driver_rows}
        </table>
    </div>
    <div class="section">
        <h2>Orders by Status</h2>
        <table>
            <tr><th>Status</th><th>Count</th></tr>
            {status_rows}
        </table>
    </div>
    <div class="section">
        <h2>Top 10 Products by Volume Delivered</h2>
        <table>
            <tr><th>Product</th><th>Total Qty Delivered</th></tr>
            {top_product_rows}
        </table>
    </div>
    <div class="section">
        <h2>Top 10 Customers by Revenue</h2>
        <table>
            <tr><th>Customer</th><th>Revenue (E)</th></tr>
            {revenue_rows}
        </table>
    </div>
    <div class="section">
        <h2>Delivery Volume by County</h2>
        <table>
            <tr><th>County</th><th>Deliveries</th></tr>
            {county_rows}
        </table>
    </div>
    <div class="section">
        <h2>Stock Alerts - Below Reorder Level</h2>
        <table class="alert">
            <tr><th>Product</th><th>Category</th><th>In Stock</th><th>Reorder Level</th></tr>
            {stock_rows}
        </table>
    </div>
</div>
</body>
</html>"""

    # Write the report to disk. os.getcwd() makes the output path explicit
    # in the confirmation message so the user knows exactly where to find it.
    output_path = os.path.join(os.getcwd(), REPORT_FILENAME)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"Report saved to: {output_path}")


# =============================================================================
# MAIN
# =============================================================================

def main():
    """
    Entry point. Orchestrates fetch -> clean -> compute -> report.

    Fetches all four tables from Supabase, cleans each one, computes the
    full metric set, and writes the HTML report. Progress is printed to
    stdout at each stage.
    """
    print("=" * 60)
    print("The Fruit People - Delivery Report")
    print(f"Run started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    # -- Fetch -----------------------------------------------------------------
    print("\n[1/4] Fetching data from Supabase...")

    ## update customers to customer

    raw_customers      = fetch_table("customer") 
    raw_deliveries     = fetch_table("deliveries")
    raw_delivery_lines = fetch_table("delivery_lines")
    raw_products       = fetch_table("products")

    print(f"      customers:      {len(raw_customers):>5} rows")
    print(f"      deliveries:     {len(raw_deliveries):>5} rows")
    print(f"      delivery_lines: {len(raw_delivery_lines):>5} rows")
    print(f"      products:       {len(raw_products):>5} rows")

    # -- Clean -----------------------------------------------------------------
    print("\n[2/4] Cleaning and normalising data...")

    customers_df  = clean_customers(pd.DataFrame(raw_customers))
    deliveries_df = clean_deliveries(pd.DataFrame(raw_deliveries))
    lines_df      = clean_delivery_lines(pd.DataFrame(raw_delivery_lines))
    products_df   = clean_products(pd.DataFrame(raw_products))

    # -- Compute ---------------------------------------------------------------
    print("\n[3/4] Computing metrics...")

    metrics = {
        "on_time":      on_time_rate(deliveries_df),
        "revenue":      calculate_revenue(lines_df, products_df),
        "status":       orders_by_status(deliveries_df),
        "stock":        stock_alerts(products_df),
        "top_products": top_products_by_volume(lines_df, products_df),
        "by_county":    delivery_volume_by_county(deliveries_df, customers_df),
    }

    print(f"      Overall on-time rate: {metrics['on_time']['overall']}%")
    print(f"      Products below reorder level: {len(metrics['stock'])}")

    # -- Report ----------------------------------------------------------------
    print("\n[4/4] Generating HTML report...")
    generate_report(metrics)

    print("\nDone.")


if __name__ == "__main__":
    main()
