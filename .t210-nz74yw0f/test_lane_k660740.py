
import pytest
from proxyshop_support.neo4j_lock import neo4j_flock

@pytest.fixture
def graph_guard():
    with neo4j_flock(timeout=0.434, poll=0.051, report=lambda message: None):
        yield True

def test_k660740(graph_guard):
    assert graph_guard

