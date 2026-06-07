#!/usr/bin/env python3
"""
delivery_report.py
Connects to Supabase, runs the same calculations and charts as part_1_EDA.ipynb,
and writes a self-contained HTML report → delivery_report.html

Usage:
    python delivery_report.py
"""

import sys
import numpy as np
from datetime import datetime

from dotenv import load_dotenv
import os

load_dotenv()
url = os.getenv("SUPABASE_URL")
key = os.getenv("SUPABASE_KEY")

# ── dependency checks ─────────────────────────────────────────────────────────
try:
    import pandas as pd
except ImportError:
    sys.exit("Missing dependency — run: pip install pandas")

try:
    import plotly.express as px
    import plotly.graph_objects as go
except ImportError:
    sys.exit("Missing dependency — run: pip install plotly")

try:
    from supabase import create_client
except ImportError:
    sys.exit("Missing dependency — run: pip install supabase")

try:
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_KEY")
except ImportError:
    sys.exit(
        "credentials.py not found.\n"
        "Create it with:\n"
        "  url = 'https://your-project-ref.supabase.co'\n"
        "  key = 'your-anon-key'"
    )

# =============================================================================
# 1. FETCH
# =============================================================================

def load_table(supabase, table_name, page_size=1000):
    """Fetch all rows using pagination (Supabase max 1 000/request)."""
    all_rows, offset = [], 0
    while True:
        try:
            resp = (
                supabase.table(table_name)
                .select('*')
                .range(offset, offset + page_size - 1)
                .execute()
            )
        except Exception as exc:
            sys.exit(f"Error fetching '{table_name}': {exc}")
        batch = resp.data or []
        all_rows.extend(batch)
        if len(batch) < page_size:
            break
        offset += page_size
    return pd.DataFrame(all_rows)


def fetch_all_tables(supabase):
    print('Fetching tables from Supabase...')
    dfs = {}
    for name in ['customer', 'deliveries', 'delivery_lines', 'products']:
        dfs[name] = load_table(supabase, name)
        print(f'  {name}: {len(dfs[name]):,} rows')
    return dfs


# =============================================================================
# 2. CLEAN  (logic copied verbatim from notebook)
# =============================================================================

def strip_and_nullify(df):
    """Strip whitespace from all string cols; replace empty strings with NaN."""
    for col in df.select_dtypes('object').columns:
        df[col] = df[col].str.strip().replace('', np.nan)
    return df


def parse_dt(series, fmt):
    return pd.to_datetime(series, format=fmt, errors='coerce')


def clean_customer(df):
    df = df.copy()
    df = strip_and_nullify(df)
    df['County'] = df['County'].str.title()
    df['Customer_Status'] = df['Customer_Status'].str.title()
    df['Country_Code'] = df['Country_Code'].replace('Ireland', 'IE')
    df['Customer_Since'] = pd.to_datetime(df['Customer_Since'], errors='coerce')
    for col in ['Postcode', 'Postal Code']:
        if col in df.columns:
            df[col] = df[col].str.replace(' ', '', regex=False)
    return df


def clean_deliveries(df):
    df = df.copy()
    df = strip_and_nullify(df)
    empty_cols = [c for c in df.columns if df[c].isna().all()]
    df.drop(columns=empty_cols, inplace=True)
    for col in ['Assembly Time', 'Act On Site']:
        if col in df.columns:
            df[col] = df[col].replace(' ', np.nan)
    df['Status'] = df['Status'].str.title()
    for col in ['Run Date', 'Order Date']:
        df[col] = parse_dt(df[col], '%d/%m/%Y')
    for col in ['Confirmed Date', 'Slot From', 'Slot To', 'ETA', 'ETD', 'ATA', 'ATD']:
        if col in df.columns:
            df[col] = parse_dt(df[col], '%d/%m/%Y %H:%M')
    df['Customer_ID'] = pd.to_numeric(df['Customer_ID'], errors='coerce').astype('Int64')
    if 'Days Ord to Del' in df.columns:
        df['Days Ord to Del'] = pd.to_numeric(df['Days Ord to Del'], errors='coerce').astype('Int64')
    if 'Tracking Link' in df.columns:
        df['Tracking Link'] = df['Tracking Link'].str.strip()
    for col in ['Postcode', 'Postal Code']:
        if col in df.columns:
            df[col] = df[col].str.replace(' ', '', regex=False)
    return df


def clean_delivery_lines(df):
    df = df.copy()
    df = strip_and_nullify(df)
    empty_cols = [c for c in df.columns if df[c].isna().all()]
    df.drop(columns=empty_cols, inplace=True)
    df['Run Date'] = parse_dt(df['Run Date'], '%d/%m/%Y')
    if 'Delivered Date' in df.columns:
        df['Delivered Date'] = df['Delivered Date'].replace(' ', np.nan)
        df['Delivered Date'] = parse_dt(df['Delivered Date'], '%d/%m/%Y')
    if 'Delivered Time' in df.columns:
        df['Delivered Time'] = df['Delivered Time'].replace(' ', np.nan)
    delivered_map = {
        '1': True, 'true': True, 'y': True, 'yes': True,
        '0': False, 'false': False, 'n': False, 'no': False,
    }
    df['Delivered'] = df['Delivered'].str.lower().map(delivered_map)
    if 'Completed' in df.columns:
        df['Completed'] = df['Completed'].map({'Y': True, 'N': False})
    if 'Weight (Kg)' in df.columns:
        df['Weight (Kg)'] = pd.to_numeric(df['Weight (Kg)'], errors='coerce').clip(lower=0)
    # ensure Quantity is numeric (Supabase may return as string)
    df['Quantity'] = pd.to_numeric(df['Quantity'], errors='coerce').fillna(0)
    for col in ['Postcode', 'Postal Code']:
        if col in df.columns:
            df[col] = df[col].str.replace(' ', '', regex=False)
    return df


def clean_products(df):
    df = df.copy()
    df = strip_and_nullify(df)
    df['Category'] = df['Category'].str.title()
    for col in ['Unit_Price', 'In_Stock', 'Reorder_Level']:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors='coerce')
    return df


# =============================================================================
# 3. ON-TIME CALCULATION  (copied verbatim from notebook cell 12266266)
# =============================================================================

def calculate_ontime(deliveries_new):
    deliveries_new = deliveries_new.copy()

    def parse_onsite(val):
        try:
            v = str(val).strip()
            if v in ('', 'nan', 'NaT', 'None'):
                return pd.NaT
            parts = v.split(':')
            return pd.Timedelta(hours=int(parts[0]), minutes=int(parts[1]))
        except:
            return pd.NaT

    act      = deliveries_new['Act On Site'].apply(parse_onsite)
    est      = deliveries_new['Est On Site'].apply(parse_onsite)
    variance = pd.to_numeric(deliveries_new['On Site Variance'], errors='coerce')

    deliveries_new['Effective On Site Calculated'] = act.where(
        act.notna(),
        est.where((act.isna()) & (variance.fillna(0) == 0), pd.NaT)
    )

    deliveries_new['ATA_Calculated'] = deliveries_new['ATA'].copy()
    deliveries_new['ATD_Calculated'] = deliveries_new['ATD'].copy()

    mask_onsite = (
        deliveries_new['ATA_Calculated'].isna() &
        deliveries_new['Effective On Site Calculated'].notna() &
        deliveries_new['ATD'].notna()
    )
    deliveries_new.loc[mask_onsite, 'ATA_Calculated'] = (
        deliveries_new.loc[mask_onsite, 'ATD'] -
        deliveries_new.loc[mask_onsite, 'Effective On Site Calculated']
    )

    mask_fallback = (
        (variance == 0) &
        deliveries_new['ATA_Calculated'].isna() &
        deliveries_new['ATD_Calculated'].isna()
    )
    deliveries_new.loc[mask_fallback, 'ATA_Calculated'] = deliveries_new.loc[mask_fallback, 'ETA']
    deliveries_new.loc[mask_fallback, 'ATD_Calculated'] = deliveries_new.loc[mask_fallback, 'ETD']

    has_data = deliveries_new['ATA_Calculated'].notna() & deliveries_new['ATD_Calculated'].notna()

    deliveries_new['Early Calculated'] = (
        has_data & (deliveries_new['ATA_Calculated'] < deliveries_new['Slot From'])
    )
    deliveries_new['Late Calculated'] = (
        has_data & (deliveries_new['ATA_Calculated'] > deliveries_new['Slot To'])
        | (has_data & (deliveries_new['ATD_Calculated'] > deliveries_new['Slot To']))
    )

    deliveries_new['On Time Calculated'] = pd.NA
    deliveries_new.loc[has_data & (deliveries_new['Early Calculated'] == True),
                       'On Time Calculated'] = 0
    deliveries_new.loc[
        has_data & deliveries_new['On Time Calculated'].isna() &
        (deliveries_new['ATA_Calculated'] >= deliveries_new['Slot From']) &
        (deliveries_new['ATD_Calculated'] <= deliveries_new['Slot To']),
        'On Time Calculated'
    ] = 1
    deliveries_new.loc[has_data & (deliveries_new['Late Calculated'] == True),
                       'On Time Calculated'] = 2

    deliveries_new['On Time Calculated Bool'] = deliveries_new['On Time Calculated'] == 1

    # Cast to float so groupby .mean() works correctly in pandas 2+ / 3+
    for col in ['On Time Calculated Bool', 'Early Calculated', 'Late Calculated']:
        deliveries_new[col] = pd.to_numeric(deliveries_new[col], errors='coerce')

    print(f"  Early   (0): {(deliveries_new['On Time Calculated'] == 0).sum():,}")
    print(f"  On Time (1): {(deliveries_new['On Time Calculated'] == 1).sum():,}")
    print(f"  Late    (2): {(deliveries_new['On Time Calculated'] == 2).sum():,}")


    return deliveries_new


# =============================================================================
# 4. CHARTS  (copied verbatim from notebook — fig.show() replaced with HTML)
# =============================================================================

def _to_html(fig):
    """Convert Plotly figure to an HTML div (no inline JS — loaded once in <head>)."""
    return fig.to_html(full_html=False, include_plotlyjs=False)


def chart_metric1(deliveries_new):
    """Two charts side-by-side: coloured bar (cell 12266266) + stacked bar (cell 452b49da)."""

    # Use explicit sum/count to avoid pandas 3.0 boolean mean() issues
    grp = (
        deliveries_new
        .groupby('Driver Name')
        .agg(
            on_time_sum=('On Time Calculated Bool', 'sum'),
            early_sum  =('Early Calculated',        'sum'),
            late_sum   =('Late Calculated',          'sum'),
            total      =('On Time Calculated Bool',  'count'),
        )
        .reset_index()
    )
    grp['on_time_pct'] = grp['on_time_sum'] / grp['total'] * 100
    grp['early_pct']   = grp['early_sum']   / grp['total'] * 100
    grp['late_pct']    = grp['late_sum']    / grp['total'] * 100

    overall = grp['on_time_sum'].sum() / grp['total'].sum() * 100

    # ── Chart A: coloured bar with 90 % target line ───────────────────────────
    per_driver = grp.sort_values('on_time_pct').copy()
    per_driver['color'] = per_driver['on_time_pct'].apply(
        lambda x: '#2ecc71' if x >= 90 else '#e67e22' if x >= 75 else '#e74c3c'
    )

    figA = go.Figure()
    figA.add_trace(go.Bar(
        x=per_driver['on_time_pct'].to_list(), y=per_driver['Driver Name'],
        orientation='h',
        marker_color=per_driver['color'],
        text=per_driver['on_time_pct'].apply(lambda x: f'{x:.1f}%'),
        textposition='outside',
        customdata=per_driver['total'],
        hovertemplate='%{y}<br>On-Time: %{x:.1f}%<br>Total deliveries: %{customdata}<extra></extra>'
    ))
    figA.add_vline(x=90, line_dash='dash', line_color='grey',
                   annotation_text='90% target', annotation_position='bottom right')
    figA.update_layout(
        title=f'On-Time Delivery Rate by Driver  (Overall: {overall:.1f}%)',
        xaxis_title='On-Time %', xaxis_range=[0, 112],
        height=500, plot_bgcolor='white', showlegend=False
    )

    # ── Chart B: stacked Early / On Time / Late ───────────────────────────────
    driver_stats = grp.sort_values('on_time_pct')

    figB = go.Figure()
    figB.add_trace(go.Bar(
        name='On Time',
        x=driver_stats['on_time_pct'].to_list(), y=driver_stats['Driver Name'],
        orientation='h', marker_color='#2ecc71',
        text=driver_stats['on_time_pct'].apply(lambda x: f'{x:.1f}%'),
        textposition='inside',
        hovertemplate='%{y}<br>On Time: %{x:.1f}%<extra></extra>'
    ))
    figB.add_trace(go.Bar(
        name='Early',
        x=driver_stats['early_pct'].to_list(), y=driver_stats['Driver Name'],
        orientation='h', marker_color='#3498db',
        text=driver_stats['early_pct'].apply(lambda x: f'{x:.1f}%'),
        textposition='inside',
        hovertemplate='%{y}<br>Early: %{x:.1f}%<extra></extra>'
    ))
    figB.add_trace(go.Bar(
        name='Late',
        x=driver_stats['late_pct'].to_list(), y=driver_stats['Driver Name'],
        orientation='h', marker_color='#e74c3c',
        text=driver_stats['late_pct'].apply(lambda x: f'{x:.1f}%'),
        textposition='inside',
        hovertemplate='%{y}<br>Late: %{x:.1f}%<extra></extra>'
    ))
    figB.update_layout(
    barmode='stack',
    title=f'Delivery Timing by Driver  (Overall On-Time: {overall:.1f}%)',
    xaxis_title='% of Deliveries', xaxis_range=[0, 100],
    height=500, plot_bgcolor='white',
    legend=dict(orientation='v', x=1.02, xanchor='left', y=1)
)

    html = f"""
    <div style="display:flex; gap:16px; flex-wrap:wrap;">
      <div style="flex:1; min-width:420px;">{_to_html(figA)}</div>
      <div style="flex:1; min-width:420px;">{_to_html(figB)}</div>
    </div>"""
    return html, overall


def chart_metric2(deliveries_new):
    """Orders by status — vertical bar (notebook cell 3b18c696)."""
    status_counts = (
        deliveries_new['Status'].str.strip().str.title()
        .value_counts().reset_index()
    )

    status_counts.columns = ['Status', 'Count']

    color_map = {
        'Complete':      '#2ecc71',
        'Part Complete': '#e67e22',
        'Outstanding':   '#e74c3c',
        'Failed':        '#c0392b',
    }
    # Use go.Bar with explicit marker_color — px.bar + color_discrete_map broken in Plotly 6.x
    fig = go.Figure(go.Bar(
    x=status_counts['Status'].tolist(),
    y=status_counts['Count'].tolist(),
    marker_color=[color_map.get(s, '#95a5a6') for s in status_counts['Status']],
    text=status_counts['Count'].tolist(),
    textposition='auto',
))
    fig.update_layout(
        title='Orders by Status',
        showlegend=False, plot_bgcolor='white',
        yaxis_title='Number of Orders', xaxis_title='',
        yaxis=dict(showgrid=True, gridcolor='#f0f0f0')
    )
    return _to_html(fig)


def chart_metric3(lines, products):
    """Top 10 products by delivered volume — Blues (go.Bar version)."""

    # 1) Aggregate delivered quantities
    top_products = (
        lines[lines['Delivered'] == True]
        .groupby('Item', as_index=False)['Quantity'].sum()
        .rename(columns={'Item': 'SKU', 'Quantity': 'Total Qty'})
        .sort_values('Total Qty', ascending=False)
        .head(10)
    )

    # 2) Join product names
    top_products = top_products.merge(
        products[['Product_SKU', 'Product_Name']],
        left_on='SKU', right_on='Product_SKU', how='left'
    )

    # 3) Order small->large for horizontal bars
    top_products = top_products.sort_values('Total Qty', ascending=True).copy()
    top_products['Total Qty'] = pd.to_numeric(top_products['Total Qty'], errors='coerce').fillna(0).astype(float)

    # 4) Labels and tick formatting helpers
    def _fmt(x):
        return f'{int(round(x/1000.0))}K' if x >= 1000 else f'{int(x)}'
    labels = top_products['Total Qty'].map(_fmt).tolist()

    y_names = top_products['Product_Name'].tolist()
    x_vals  = top_products['Total Qty'].tolist()

    # 5) Build bar trace
    bar = go.Bar(
        x=x_vals,
        y=y_names,
        orientation='h',
        text=labels,
        textposition='outside',
        marker=dict(
            color=x_vals,
            colorscale='Blues',
            showscale=False
        ),
        hovertemplate='%{y}<br>Total: %{x:,}<extra></extra>'
    )

    # 6) X-axis ticks: 0, 1K, 2K, ...
    max_x = int(max(x_vals)) if x_vals else 0
    if max_x >= 1000:
        step = 1000
        max_tick = (max_x // step + 1) * step
        tickvals = list(range(0, max_tick + 1, step))
        ticktext = ['0'] + [f'{t//1000}K' for t in tickvals[1:]]
    else:
        tickvals = None
        ticktext = None

    fig = go.Figure(data=[bar])

    fig.update_layout(
        title='Top 10 Products by Delivered Volume',
        plot_bgcolor='white',
        paper_bgcolor='white',
        margin=dict(l=140, r=80, t=60, b=40),
        xaxis=dict(
            title='Total Quantity Delivered',
            type='linear',
            rangemode='tozero',
            showgrid=True, gridcolor='#f0f0f0',
            tickmode='array' if tickvals else 'auto',
            tickvals=tickvals, ticktext=ticktext
        ),
        yaxis=dict(
            title='',
            showgrid=False,
            categoryorder='array',
            categoryarray=y_names  # lock order to our list
        )
    )

    # Prevent clipping of outside labels
    fig.update_traces(cliponaxis=False)

    return _to_html(fig)



def chart_metric4(lines, products):
    """Top 10 customers by revenue — Purples (go.Bar, x-axis locked numeric)."""

    # Join prices and compute revenue
    df = lines.merge(
        products[['Product_SKU', 'Unit_Price']],
        left_on='Item', right_on='Product_SKU', how='left'
    ).copy()

    df['Quantity'] = pd.to_numeric(df['Quantity'], errors='coerce').fillna(0)
    df['Unit_Price'] = pd.to_numeric(df['Unit_Price'], errors='coerce').fillna(0.0)
    df['Revenue'] = (df['Quantity'] * df['Unit_Price']).astype(float)

    # Aggregate and pick top 10
    top_customers = (
        df.groupby('Customer Name', as_index=False)['Revenue']
          .sum()
          .sort_values('Revenue', ascending=False)
          .head(10)
          .sort_values('Revenue', ascending=True)   # small->large for h-bars
          .copy()
    )

    y_names = top_customers['Customer Name'].tolist()
    x_vals  = top_customers['Revenue'].astype(float).tolist()

    # Text labels (outside bars) — but DO NOT set x ticktext
    def _fmt_cur(x):
        return f'£{int(round(x/1000.0))}K' if x >= 1000 else f'£{int(round(x))}'
    labels = [_fmt_cur(x) for x in x_vals]

    bar = go.Bar(
        x=x_vals,
        y=y_names,
        orientation='h',
        text=labels,
        textposition='outside',
        marker=dict(color=x_vals, colorscale='Purples', showscale=False),
        hovertemplate='%{y}<br>Revenue: £%{x:,.0f}<extra></extra>',
        cliponaxis=False
    )

    # Numeric x-axis: use tickformat with currency + thousands (K) shorthand
    # ~s gives SI units (k), combine with £ prefix via separatethousands formatting on hover only.
    # For axis: prefix via ticksuffix/prefix with tickformat, not custom ticktext.
    fig = go.Figure(data=[bar])

    fig.update_layout(
        title='Top 10 Customers by Revenue',
        plot_bgcolor='white', paper_bgcolor='white',
        margin=dict(l=170, r=90, t=60, b=50),
        xaxis=dict(
            type='linear',              # force numeric
            rangemode='tozero',
            showgrid=True, gridcolor='#f0f0f0',
            tickformat='£~s',          # 1000 -> £1k, 250000 -> £250k
            ticksuffix='', tickprefix='',  # avoid double symbols
            tickmode='auto'            # let Plotly place numeric ticks
        ),
        yaxis=dict(
            title='',
            showgrid=False,
            categoryorder='array',
            categoryarray=y_names
        )
    )

    return _to_html(fig)




def chart_metric5(deliveries_new, customer):
    """Delivery Volume by County — donut (go.Pie, count + %)."""

    # Prepare data
    merged = deliveries_new.merge(
        customer[['Customer_ID', 'County']], on='Customer_ID', how='left'
    )
    county_vol = (
        merged['County']
        .value_counts(dropna=False)          # include NaN if present
        .rename_axis('County')
        .reset_index(name='Deliveries')
    )

    # Replace NaN county with a label if needed
    county_vol['County'] = county_vol['County'].fillna('Unknown')

    labels = county_vol['County'].tolist()
    values = county_vol['Deliveries'].astype(int).tolist()

    # Donut pie with explicit values
    fig = go.Figure(
        data=[
            go.Pie(
                labels=labels,
                values=values,
                hole=0.35,
                sort=False,                 # keep current order
                direction='clockwise',
                textposition='inside',
                texttemplate="%{value}<br>(%{percent})",   # count + percent
                hovertemplate="%{label}<br>Deliveries: %{value:,}<br>Share: %{percent}<extra></extra>",
                marker=dict(line=dict(color='white', width=1))
            )
        ]
    )

    fig.update_layout(
        title='Delivery Volume by County',
        height=500,
        showlegend=True,                    # legend shows county names
        uniformtext_minsize=10,
        uniformtext_mode='hide'             # hide text if too small, keep layout clean
    )

    return _to_html(fig)

def chart_metric6(products):
    """Low stock + backordered overlay bar charts (notebook cell fb8fc0dd)."""
    alerts = (
        products[products['In_Stock'] < products['Reorder_Level']]
        [['Product_Name', 'Category', 'In_Stock', 'Reorder_Level']]
        .assign(Deficit=lambda x: x['Reorder_Level'] - x['In_Stock'])
        .sort_values('Reorder_Level', ascending=False)
        .reset_index(drop=True)
    )
    low_stock   = alerts[alerts['In_Stock'] >= 0]
    backordered = alerts[alerts['In_Stock'] < 0]

    # Graph 1: Low Stock
    fig1 = go.Figure()
    fig1.add_trace(go.Bar(
        name='Reorder Level',
        x=low_stock['Product_Name'], y=low_stock['Reorder_Level'].to_list(),
        marker_color='#e74c3c', opacity=0.5
    ))
    fig1.add_trace(go.Bar(
        name='In Stock',
        x=low_stock['Product_Name'], y=low_stock['In_Stock'].to_list(),
        marker_color='#3498db',
        text=low_stock['Deficit'].apply(lambda x: f'-{int(x)}'),
        textposition='outside',
        textfont=dict(color='#e74c3c', size=11)
    ))
    fig1.update_layout(
        barmode='overlay',
        title=f'Low Stock — {len(low_stock)} Products Below Reorder Level',
        xaxis_tickangle=-45, plot_bgcolor='white',
        yaxis_title='Units', xaxis_title='',
        yaxis=dict(showgrid=True, gridcolor='#f0f0f0'),
        legend=dict(orientation='h', y=1.1)
    )

    # Graph 2: Backordered
    fig2 = go.Figure()
    fig2.add_trace(go.Bar(
        name='Reorder Level',
        x=backordered['Product_Name'], y=backordered['Reorder_Level'].to_list(),
        marker_color='#e74c3c', opacity=0.5
    ))
    fig2.add_trace(go.Bar(
        name='In Stock (Backordered)',
        x=backordered['Product_Name'], y=backordered['In_Stock'].to_list(),
        marker_color='#c0392b',
        text=backordered['Deficit'].apply(lambda x: f'-{int(x)}'),
        textposition='outside',
        textfont=dict(color='#c0392b', size=11)
    ))
    fig2.update_layout(
        barmode='overlay',
        title=f'Backordered — {len(backordered)} Products With Negative Stock',
        xaxis_tickangle=-45, plot_bgcolor='white',
        yaxis_title='Units', xaxis_title='',
        yaxis=dict(showgrid=True, gridcolor='#f0f0f0'),
        legend=dict(orientation='h', y=1.1)
    )

    return _to_html(fig1), _to_html(fig2), alerts, low_stock, backordered


# =============================================================================
# 5. HTML ASSEMBLY
# =============================================================================

_CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
       background: #f4f6f9; color: #2d3436; }
header { background: linear-gradient(135deg, #2c3e50, #3d5a80);
         color: white; padding: 28px 40px; }
header h1 { font-size: 1.9rem; font-weight: 700; }
header p  { margin-top: 6px; opacity: .72; font-size: .88rem; }
main { max-width: 1400px; margin: 32px auto; padding: 0 24px 56px; }
.kpi-row { display: flex; gap: 14px; flex-wrap: wrap; margin-bottom: 28px; }
.kpi { flex: 1; min-width: 160px; background: white; border-radius: 12px;
       padding: 20px 24px; box-shadow: 0 2px 8px rgba(0,0,0,.07); }
.kpi .val { font-size: 2rem; font-weight: 700; color: #2c3e50; }
.kpi .lbl { font-size: .72rem; color: #636e72; margin-top: 5px;
            text-transform: uppercase; letter-spacing: .08em; }
section { background: white; border-radius: 12px; padding: 28px 30px;
          margin-bottom: 24px; box-shadow: 0 2px 8px rgba(0,0,0,.07); }
section h2 { font-size: 1.08rem; font-weight: 700; margin-bottom: 18px;
             color: #2c3e50; border-bottom: 2px solid #f0f0f0; padding-bottom: 12px; }
.note { font-size: .82rem; color: #555; margin-top: 14px; line-height: 1.7;
        background: #f8f9fa; padding: 12px 16px; border-radius: 6px;
        border-left: 3px solid #3d5a80; }
footer { text-align: center; color: #b2bec3; font-size: .78rem;
         padding: 20px; border-top: 1px solid #eee; margin-top: 20px; }
"""


def build_html(overall, ch1, ch2, ch3, ch4, ch5,
               ch6a, ch6b, alerts, low_stock, backordered,
               total_deliveries):
    kpi_html = ''.join(
        f'<div class="kpi"><div class="val">{v}</div><div class="lbl">{l}</div></div>'
        for v, l in [
            (f'{overall:.1f}%',       'On-Time Rate'),
            (f'{total_deliveries:,}', 'Total Deliveries'),
            (f'{len(alerts)}',        f'Stock Alerts ({len(backordered)} backordered)'),
        ]
    )
    now = datetime.now().strftime('%d %B %Y, %H:%M')

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Delivery Operations Report</title>
<script src="https://cdn.plot.ly/plotly-2.27.0.min.js"></script>
<style>{_CSS}</style>
</head>
<body>
<header>
  <h1>Delivery Operations Report</h1>
  <p>Generated {now}</p>
</header>
<main>
  <div class="kpi-row">{kpi_html}</div>

  <section>
    <h2>Metric 1 — On-Time Delivery Rate by Driver</h2>
    {ch1}
    <p class="note">
      Bars are coloured green/orange/red against a 90% target so underperformers are visible instantly.
      The stacked breakdown was added because early arrivals — drivers arriving <em>before</em> the slot opened —
      were a significant hidden reason the headline on-time rate was lower than expected, masking a very different
      problem from late arrivals.
    </p>
    <p class="note">
      ATA is derived as ATD − Act On Site (Est On Site used when On Site Variance = 0).
      Where both ATA and ATD are absent and variance = 0, ETA/ETD are used as proxies.
      <strong>Early</strong> = ATA &lt; Slot From &nbsp;|&nbsp;
      <strong>On Time</strong> = ATA ≥ Slot From and ATD ≤ Slot To &nbsp;|&nbsp;
      <strong>Late</strong> = ATA &gt; Slot From.
    </p>
  </section>

  <section>
    <h2>Metric 2 — Orders by Status</h2>
    {ch2}
    <p class="note">
      A simple status count acts as an operational health check — how many orders completed, how many are
      still outstanding or failed. Colour follows severity (green → red) so problem categories draw
      the eye without needing to read every bar. Simply displays the majority of devliveries are delivered adn incomplete deliveries are rare.
    </p>
  </section>

  <section>
    <h2>Metric 3 — Top 10 Products by Delivered Volume</h2>
    {ch3}
    <p class="note">
      Ranking by delivered volume (confirmed lines only) reveals where fulfilment effort is actually concentrated.
      The top product was delivered twice as much as the second, highlighting a dependency worth monitoring
      closely for stock and capacity planning.
    </p>
  </section>

  <section>
    <h2>Metric 4 — Top 10 Customers by Revenue</h2>
    {ch4}
    <p class="note">
      Revenue (quantity × unit price) shows commercial value rather than just delivery count.
      The top 3 customers alone account for over £700K of revenue, underscoring how critical
      a small number of accounts are to overall business performance.
    </p>
  </section>

  <section>
    <h2>Metric 5 — Delivery Volume by County</h2>
    {ch5}
    <p class="note">
      The donut chart shows market share is near-equal across counties, with some separated by just one delivery —
      Waterford and Dublin differ by only one, suggesting real room to grow in counties with more businesses.
      Each segment displays both count and percentage so volume and share are readable at a glance.
    </p>
  </section>

  <section>
    <h2>Metric 6 — Stock Alerts ({len(alerts)} products below reorder level)</h2>
    <div style="margin-bottom:24px">{ch6a}</div>
    {ch6b}
    <p class="note">
      The overlay bars make it easy to see exactly how many units each product needs to reach its reorder level,
      without any mental arithmetic. Backordered products are separated into their own chart to reflect their
      greater severity — negative stock means demand is already going unfulfilled and requires immediate action,
      distinct from products that are simply running low.
    </p>
    <p class="note">
      Low Stock (In Stock ≥ 0): {len(low_stock)} products &nbsp;|&nbsp;
      Backordered (In Stock &lt; 0): {len(backordered)} products.
      Red labels show units needed to reach reorder level.
    </p>
  </section>
</main>
<footer>delivery_report.py &middot; Data: Supabase &middot; {now}</footer>
</body>
</html>"""


# =============================================================================
# 6. MAIN
# =============================================================================

def main():
    print('Connecting to Supabase...')
    supabase = create_client(url, key)

    dfs = fetch_all_tables(supabase)

    print('Cleaning data...')
    customer       = clean_customer(dfs['customer'])
    deliveries     = clean_deliveries(dfs['deliveries'])
    delivery_lines = clean_delivery_lines(dfs['delivery_lines'])
    products       = clean_products(dfs['products'])

    print('Calculating on-time metrics...')
    deliveries_new = calculate_ontime(deliveries)

    print('Building charts...')
    ch1, overall  = chart_metric1(deliveries_new)
    ch2           = chart_metric2(deliveries_new)
    ch3           = chart_metric3(delivery_lines, products)
    ch4           = chart_metric4(delivery_lines, products)
    ch5           = chart_metric5(deliveries_new, customer)
    ch6a, ch6b, alerts, low_stock, backordered = chart_metric6(products)

    print('Assembling HTML...')
    html = build_html(
        overall, ch1, ch2, ch3, ch4, ch5,
        ch6a, ch6b, alerts, low_stock, backordered,
        len(deliveries_new)
    )

    output = 'delivery_report_Emma.html'
    with open(output, 'w', encoding='utf-8') as f:
        f.write(html)

    print(f'\nDone — report saved to {output}')
    print('Open it in any browser.')


if __name__ == '__main__':
    main()