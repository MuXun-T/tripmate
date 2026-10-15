import os

import pytest


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    if os.getenv("TRIPMATE_INTEGRATION") == "1":
        return
    skip = pytest.mark.skip(reason="set TRIPMATE_INTEGRATION=1 to run real-service tests")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)
