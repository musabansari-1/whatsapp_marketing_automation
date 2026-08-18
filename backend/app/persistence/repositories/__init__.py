"""Query modules, one per aggregate.

Route handlers call these rather than building SQL inline, so the HTTP layer stays
about HTTP and the queries can be exercised directly in tests.
"""
