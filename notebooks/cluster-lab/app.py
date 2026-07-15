# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "marimo>=0.13",
#     "scikit-learn>=1.5",
#     "numpy>=1.26",
#     "matplotlib>=3.9",
# ]
# ///

import marimo

__generated_with = "0.23.14"
app = marimo.App(width="medium", app_title="Cluster Lab")


@app.cell
def _():
    import matplotlib

    matplotlib.use("Agg")
    import marimo as mo
    import matplotlib.pyplot as plt
    import numpy as np

    from gallery_shared import storage

    return mo, np, plt, storage


@app.cell
def _(mo):
    mo.md(
        """
        # Cluster Lab
        K-means clustering on synthetic blobs. This notebook runs in an
        **isolated sandbox** with its own dependencies (scikit-learn,
        matplotlib), and caches expensive fits in the shared gallery cache.
        """
    )
    return


@app.cell
def _(mo):
    n_samples = mo.ui.slider(1_000, 50_000, step=1_000, value=10_000, label="Samples")
    n_clusters = mo.ui.slider(2, 12, value=4, label="Clusters (k)")
    seed = mo.ui.number(0, 999, value=7, label="Random seed")
    mo.hstack([n_samples, n_clusters, seed], justify="start", gap=2)
    return n_clusters, n_samples, seed


@app.cell
def _(mo, n_clusters, n_samples, seed, storage):
    cache = storage.get_cache("cluster-lab")

    def fit():
        from sklearn.cluster import KMeans
        from sklearn.datasets import make_blobs

        X, _ = make_blobs(
            n_samples=n_samples.value,
            centers=n_clusters.value,
            cluster_std=1.4,
            random_state=seed.value,
        )
        model = KMeans(n_clusters=n_clusters.value, n_init=10, random_state=seed.value)
        labels = model.fit_predict(X)
        return {"X": X, "labels": labels, "centers": model.cluster_centers_,
                "inertia": model.inertia_}

    key = f"kmeans:{n_samples.value}:{n_clusters.value}:{seed.value}"
    result = cache.get_or_compute(key, fit, ttl=3600)
    mo.md(
        f"Result served via the **{cache.backend}** cache backend "
        f"(key `{key}`, inertia {result['inertia']:,.0f})."
    )
    return (result,)


@app.cell
def _(np, plt, result):
    fig, ax = plt.subplots(figsize=(8, 5))
    X, labels, centers = result["X"], result["labels"], result["centers"]
    # Subsample for plotting so large runs stay snappy.
    idx = np.random.default_rng(0).choice(len(X), size=min(len(X), 5_000), replace=False)
    ax.scatter(X[idx, 0], X[idx, 1], c=labels[idx], s=6, cmap="tab10", alpha=0.6)
    ax.scatter(centers[:, 0], centers[:, 1], c="black", s=120, marker="x", linewidths=2)
    ax.set_title("K-means clusters (subsampled)")
    ax.set_axis_off()
    fig.patch.set_alpha(0)
    fig
    return


if __name__ == "__main__":
    app.run()
