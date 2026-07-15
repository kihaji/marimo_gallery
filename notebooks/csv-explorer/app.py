import marimo

__generated_with = "0.23.14"
app = marimo.App(width="medium", app_title="CSV Explorer")


@app.cell
def _():
    import altair as alt
    import marimo as mo
    import pandas as pd

    from gallery_shared import storage, theming

    theming.altair_theme()
    return alt, mo, pd, storage


@app.cell
def _(mo):
    uploader = mo.ui.file(kind="area", filetypes=[".csv"], label="Drop a CSV file here")
    uploader
    return (uploader,)


@app.cell
def _(mo, pd, storage, uploader):
    mo.stop(
        not uploader.value,
        mo.md("*Upload a CSV above to explore it.*").center(),
    )
    upload = uploader.value[0]
    saved_path = storage.save_upload(upload.name, upload.contents)
    df = pd.read_csv(saved_path)
    mo.md(f"Loaded **{upload.name}** ({len(df):,} rows × {len(df.columns)} columns)")
    return (df,)


@app.cell
def _(df, mo, pd):
    profile = pd.DataFrame(
        {
            "dtype": df.dtypes.astype(str),
            "nulls": df.isna().sum(),
            "null %": (df.isna().mean() * 100).round(1),
            "unique": df.nunique(),
        }
    )
    mo.vstack([mo.md("### Column profile"), profile])
    return


@app.cell
def _(df, mo):
    mo.vstack([mo.md("### Explore"), mo.ui.dataframe(df)])
    return


@app.cell
def _(df, mo):
    numeric_cols = df.select_dtypes("number").columns.tolist()
    column_picker = mo.ui.dropdown(
        options=numeric_cols,
        value=numeric_cols[0] if numeric_cols else None,
        label="Histogram column",
    )
    column_picker
    return (column_picker,)


@app.cell
def _(alt, column_picker, df, mo):
    mo.stop(column_picker.value is None, mo.md("*No numeric columns to plot.*"))
    hist = (
        alt.Chart(df.head(50_000))
        .mark_bar()
        .encode(
            x=alt.X(f"{column_picker.value}:Q", bin=alt.Bin(maxbins=40)),
            y="count()",
        )
        .properties(height=280, width="container", title=f"Distribution of {column_picker.value}")
    )
    mo.ui.altair_chart(hist)
    return


@app.cell
def _(df, mo, storage):
    # Write a derived summary into per-session scratch space, then offer it
    # for download. Scratch is cleaned up when the notebook is reaped.
    export_path = storage.scratch_dir() / "summary.csv"
    df.describe(include="all").to_csv(export_path)
    mo.download(
        data=export_path.read_bytes(),
        filename="summary.csv",
        label="Download summary statistics",
    )
    return


if __name__ == "__main__":
    app.run()
