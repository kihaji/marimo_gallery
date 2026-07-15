import marimo

__generated_with = "0.23.14"
app = marimo.App(width="medium", app_title="Sales Dashboard")


@app.cell
def _():
    import altair as alt
    import marimo as mo
    import pandas as pd

    from gallery_shared import data, theming

    theming.altair_theme()
    return alt, data, mo, pd


@app.cell
def _(data):
    sales = data.load_sales()
    return (sales,)


@app.cell
def _(mo, sales):
    date_range = mo.ui.date_range(
        start=sales["date"].min().date(),
        stop=sales["date"].max().date(),
        value=(sales["date"].min().date(), sales["date"].max().date()),
        label="Date range",
    )
    regions = mo.ui.multiselect(
        options=sorted(sales["region"].unique()),
        value=sorted(sales["region"].unique()),
        label="Regions",
    )
    mo.hstack([date_range, regions], justify="start", gap=2)
    return date_range, regions


@app.cell
def _(date_range, pd, regions, sales):
    start, stop = date_range.value
    filtered = sales[
        sales["date"].between(pd.Timestamp(start), pd.Timestamp(stop))
        & sales["region"].isin(regions.value)
    ]
    return (filtered,)


@app.cell
def _(filtered, mo):
    total_revenue = filtered["revenue"].sum()
    total_units = filtered["units"].sum()
    avg_daily = filtered.groupby("date")["revenue"].sum().mean() if len(filtered) else 0
    mo.hstack(
        [
            mo.stat(f"${total_revenue:,.0f}", label="Total revenue"),
            mo.stat(f"{total_units:,}", label="Units sold"),
            mo.stat(f"${avg_daily:,.0f}", label="Avg daily revenue"),
        ],
        widths="equal",
    )
    return


@app.cell
def _(alt, filtered, mo):
    weekly = (
        filtered.set_index("date")
        .groupby("category")
        .resample("W")["revenue"]
        .sum()
        .reset_index()
    )
    trend = (
        alt.Chart(weekly)
        .mark_line(point=False)
        .encode(
            x=alt.X("date:T", title="Week"),
            y=alt.Y("revenue:Q", title="Revenue"),
            color=alt.Color("category:N", title="Category"),
            tooltip=["date:T", "category:N", alt.Tooltip("revenue:Q", format=",.0f")],
        )
        .properties(height=300, width="container", title="Weekly revenue by category")
    )
    mo.ui.altair_chart(trend)
    return


@app.cell
def _(alt, filtered, mo):
    by_region = filtered.groupby(["region", "category"], as_index=False)["revenue"].sum()
    breakdown = (
        alt.Chart(by_region)
        .mark_bar()
        .encode(
            x=alt.X("region:N", title="Region"),
            y=alt.Y("revenue:Q", title="Revenue"),
            color=alt.Color("category:N", title="Category"),
            tooltip=["region:N", "category:N", alt.Tooltip("revenue:Q", format=",.0f")],
        )
        .properties(height=300, width="container", title="Revenue by region")
    )
    mo.ui.altair_chart(breakdown)
    return


if __name__ == "__main__":
    app.run()
