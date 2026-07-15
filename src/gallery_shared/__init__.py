"""Shared code importable by any gallery notebook.

Notebooks running from the project environment can `import gallery_shared`
directly. Sandboxed notebooks (``sandbox: true`` in meta.yaml) receive
``PYTHONPATH=<repo>/src`` from the gateway so the import works there too;
in production, ship this package as a wheel and add it to the notebook's
PEP 723 dependencies instead.
"""
