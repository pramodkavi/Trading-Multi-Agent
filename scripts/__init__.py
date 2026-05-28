"""Operational scripts (migrate, run_scan, etc.).

Made a package so test modules can `from scripts.migrate import ...`
without sys.path manipulation. Not part of the deployed wheel.
"""
