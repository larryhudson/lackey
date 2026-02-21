"""Hello world test to verify pytest is working."""


def test_hello_world():
    """Test that a simple assertion passes."""
    assert True


def test_hello_world_with_message():
    """Test that we can run multiple tests."""
    message = "Hello, World!"
    assert message == "Hello, World!"
    assert len(message) > 0
