"""The application layer — everything needed to run superforecaster as a service.

Storage, the gated-run state machine, the scheduler, the CLI, and the eval harness.
None of it belongs in the core library: a consumer who imports `superforecaster` to get
the methodology brings their own database, their own process, and their own interface.

The import direction is one way, and `tests/test_layering.py` enforces it:

    api -> app -> superforecaster
"""
